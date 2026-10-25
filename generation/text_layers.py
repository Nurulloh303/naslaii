"""Magic Layers v3: rasmdagi matnni tahrirlanadigan qatlamlarga aylantirish.

1. OpenAI vision matn bloklarini topadi: aniq matn (harfma-harf, o'sha
   alifboda), uslub (font_style, qalinlik, rang, tekislash), taxminiy quti.
2. fal ``florence-2-large/ocr-with-region`` qator qutilari bilan GPT qutilari
   aniqlashtiriladi (markaz/IoU/matn o'xshashligi). OCR bo'lmasa — GPT qutisi.
3. Har ikki holda quti lokal (OpenCV) harf maskasi bo'yicha toraytiriladi;
   shrift o'lchami tor quti balandligidan hisoblanadi; ``source`` — o'chirish
   va frontend nusxalaydigan to'rtburchak (tor quti + ~6px).

Xatolik bo'lsa (kalit yo'q, tarmoq, noto'g'ri JSON) — bo'sh ro'yxat:
matn qismi hech qachon 500 ga olib kelmasligi kerak.
"""

from __future__ import annotations

import base64
import difflib
import io
import json
import logging
import os
import re
import unicodedata
import uuid

import requests
from PIL import Image

logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
TEXT_DETECT_TIMEOUT = max(10, int(os.environ.get("MAGIC_LAYERS_TEXT_TIMEOUT_MS", 45_000)) // 1000)

FONT_WEIGHTS = (400, 500, 600, 700, 800, 900)
ALIGNS = ("left", "center", "right")
FONT_STYLES = ("condensed", "sans", "serif", "rounded", "script")
DEFAULT_FONT_FAMILY = "Inter"
DEFAULT_LINE_HEIGHT = 1.15
# Frontend yuklaydigan shriftlar — boshqasini qaytarmaymiz.
FONT_FAMILIES = ("Inter", "Montserrat", "Oswald", "Anton", "Playfair Display", "Nunito", "Caveat")

# Bosh harf balandligi / fontSize (taxminiy, shrift metrikalaridan).
CAP_HEIGHT = {
    "Inter": 0.73, "Montserrat": 0.70, "Oswald": 0.81, "Anton": 0.86,
    "Playfair Display": 0.71, "Nunito": 0.71, "Caveat": 0.62,
}
# Kichik harfli matn: ko'tarma (b, d, l) ~ bosh harf; tushirma (g, y, p) bilan ~0.95.
DESCENDERS = set("gjpqyçşдруфцщ")

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def _text_model() -> str:
    return (
        os.environ.get("OPENAI_LAYERS_MODEL", "").strip()
        or os.environ.get("OPENAI_BRIEF_MODEL", "").strip()
        or "gpt-4o"
    )


def build_prompt(width: int, height: int) -> str:
    return f"""You are an OCR + typography analyst for marketplace product-card infographics.
The image is exactly {width}x{height} pixels.

Find every piece of DESIGN TEXT overlaid on the card (headlines, subtitles, benefit
lines, numbers, prices, captions, badge labels).

TEXT MUST BE COPIED EXACTLY:
- Copy the text character by character exactly as printed, in the SAME alphabet/script.
- NEVER transliterate: Latin stays Latin, Cyrillic stays Cyrillic. Uzbek Latin words
  such as "uchun", "bilan", "sifatli", "yetkazib" are Latin — never write them in
  Cyrillic. A line can mix scripts only if the image really mixes them.
- NEVER translate, paraphrase, fix spelling or change case.
- Keep every apostrophe/okina exactly as printed (oʻ gʻ o' g' o‘ g‘ ’ ʼ), digits and
  symbols (%, +, -, ×, ₽, so'm, sum).

BLOCKS:
- Lines of the same paragraph/heading (same style, stacked) form ONE block; join its
  lines with "\\n" exactly where the line breaks are in the image.
- Different styles (headline vs. subtitle, each separate badge) are separate blocks.
- Text physically printed on the product or its packaging (brand logo, label on the
  item itself) is NOT design text: include it with "product_label": true, or skip it.

For each block return:
- "text": exact text (see rules above).
- "x","y","w","h": tight bounding box around the glyphs of the whole block, NORMALIZED
  to 0..1 (x,y = top-left corner; divide pixels by image width/height).
- "font_style": one of "condensed" (tall, narrow, heavy, e.g. Oswald/Anton/Bebas),
  "sans" (regular sans-serif), "serif", "rounded", "script" (handwritten/brush).
- "display": true if it is an extra-heavy, poster-like display face (e.g. Anton/Bebas).
- "geometric": true if a sans face looks geometric (Montserrat/Futura-like round O).
- "font_weight": one of 400, 500, 600, 700, 800, 900.
- "color": text fill color as "#RRGGBB".
- "align": "left", "center" or "right" (alignment of the lines inside the block).
- "uppercase": true if the text is rendered in ALL CAPS.
- "letter_spacing": "tight", "normal" or "wide".
- "on_plate": true if the text sits on a solid colored plate/badge/pill/ribbon;
  "plate_color": that plate's color "#RRGGBB", otherwise null.
- "product_label": true only for text printed on the product itself.

Return ONLY JSON of the form:
{{"blocks": [{{"text": "DAM OLISH UCHUN\\nIDEAL", "x": 0.1, "y": 0.05, "w": 0.8, "h": 0.08,
  "font_style": "condensed", "display": false, "geometric": false, "font_weight": 800,
  "color": "#FFFFFF", "align": "center", "uppercase": true, "letter_spacing": "normal",
  "on_plate": false, "plate_color": null, "product_label": false}}]}}
If there is no design text, return {{"blocks": []}}."""


# ---------------------------------------------------------------------------
#  Rasm yordamchilari
# ---------------------------------------------------------------------------

def image_size(image_bytes: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(image_bytes)) as img:
        return img.size


def _image_data_url_for_vision(image_bytes: bytes) -> str:
    """GPT uchun JPEG (kichikroq payload). Asl o'lcham saqlanadi — koordinatalar shunga mos."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
#  Javobni tozalash (normalized → px)
# ---------------------------------------------------------------------------

def _hex(value, default: str | None = "#000000") -> str | None:
    if not isinstance(value, str):
        return default
    match = _HEX_RE.match(value.strip())
    if not match:
        return default
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.upper()


def _num(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _snap_weight(value) -> int:
    weight = _num(value, 700)
    if weight < 100:  # "bold" kabi so'z yoki noto'g'ri qiymat
        weight = 700
    return min(FONT_WEIGHTS, key=lambda w: abs(w - weight))


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def map_font_family(font_style, weight: int = 700, display=False, geometric=None) -> str:
    """GPT ``font_style`` → frontend yuklaydigan shriftlardan biri."""
    style = str(font_style or "").strip().lower()
    if style in ("condensed", "narrow", "compressed", "display"):
        if _as_bool(display) or style == "display" or weight >= 900:
            return "Anton"
        return "Oswald"
    if style == "serif":
        return "Playfair Display"
    if style == "rounded":
        return "Nunito"
    if style in ("script", "handwritten", "brush"):
        return "Caveat"
    if style in ("sans", "sans-serif", "geometric"):
        if geometric is None:
            return "Montserrat" if weight >= 700 else "Inter"
        return "Montserrat" if (_as_bool(geometric) or style == "geometric") else "Inter"
    return DEFAULT_FONT_FAMILY


def _is_caps_text(text: str, uppercase: bool) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return True  # faqat raqam/belgi — bosh harf balandligida
    return uppercase or all(ch.isupper() for ch in letters)


def glyph_height_ratio(text: str, family: str, uppercase: bool) -> float:
    """Bir qator harflarining (siyoh) balandligi / fontSize."""
    cap = CAP_HEIGHT.get(family, CAP_HEIGHT[DEFAULT_FONT_FAMILY])
    if _is_caps_text(text, uppercase):
        # Vergul/qavs/Q/J bosh harf chizig'idan pastga tushadi.
        return cap + (0.12 if any(ch in ",;()[]{}QJЦЩДФ" for ch in text) else 0.0)
    if any(ch in DESCENDERS for ch in text):
        return min(1.05, cap + 0.24)
    return cap + 0.04  # ko'tarmalar bosh harfdan biroz baland


def compute_font_size(text: str, box_height: float, family: str, uppercase: bool,
                      line_height: float = DEFAULT_LINE_HEIGHT, line_pitch: float | None = None) -> tuple[int, float]:
    """Tor quti balandligidan (fontSize, lineHeight).

    n qatorli blok: H = (n-1)·pitch + k·fontSize, pitch = lineHeight·fontSize,
    k — qator siyoh balandligi / fontSize (bosh harf ≈ 0.72, tushirma bilan ≈ 0.95).
    OCR alohida qatorlarni bergan bo'lsa, ``line_pitch`` (px) ma'lum — lineHeight shundan.
    """
    lines = [line for line in text.split("\n") if line.strip()] or [text]
    count = len(lines)
    ratio = glyph_height_ratio(text if count == 1 else lines[-1], family, uppercase)
    # Oxirgi qatordan boshqa qatorlarda tushirma bo'lsa ham H ga ta'sir qilmaydi —
    # faqat oxirgi qator pastki chegarani belgilaydi; birinchi qator — yuqorisini.
    top_extra = 0.0
    if count > 1 and not _is_caps_text(lines[0], uppercase):
        top_extra = 0.04
    base = box_height / ((count - 1) * line_height + ratio + top_extra)
    size = base
    if count > 1 and line_pitch and line_pitch > 0:
        candidate = (box_height - (count - 1) * line_pitch) / (ratio + top_extra)
        if 0.6 * base <= candidate <= 1.4 * base:
            size = candidate
            line_height = max(0.85, min(1.8, line_pitch / candidate))
    return int(round(max(8.0, size))), round(float(line_height), 3)


def parse_text_blocks(payload, width: int, height: int) -> list[dict]:
    """GPT javobini API kontraktidagi textLayers ro'yxatiga aylantiradi (GPT qutisi bilan).

    Keyin ``refine_layers`` qutilarni OCR/piksellar bo'yicha aniqlashtiradi.
    """
    if isinstance(payload, str):
        cleaned = re.sub(r"^```(?:json)?|```$", "", payload.strip(), flags=re.M).strip()
        payload = json.loads(cleaned)

    if isinstance(payload, list):
        raw_blocks = payload
    elif isinstance(payload, dict):
        raw_blocks = payload.get("blocks") or payload.get("textBlocks") or payload.get("text_blocks") or []
    else:
        raw_blocks = []

    layers: list[dict] = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            continue
        if _as_bool(block.get("product_label")):
            continue  # mahsulotning o'zidagi yozuv — rasmda qoladi
        text = str(block.get("text") or "").replace("\r\n", "\n").replace("\\n", "\n").strip()
        text = unicodedata.normalize("NFC", text)
        if not text:
            continue

        x, y = _num(block.get("x")), _num(block.get("y"))
        w, h = _num(block.get("w", block.get("width"))), _num(block.get("h", block.get("height")))
        # Ba'zan model normalizatsiya o'rniga piksel qaytaradi.
        if max(x, y, w, h) > 1.5:
            px, py, pw, ph = x, y, w, h
        else:
            px, py, pw, ph = x * width, y * height, w * width, h * height

        left = int(round(max(0.0, min(px, width - 1))))
        top = int(round(max(0.0, min(py, height - 1))))
        right = int(round(max(left + 1, min(px + pw, width))))
        bottom = int(round(max(top + 1, min(py + ph, height))))
        box_w, box_h = right - left, bottom - top
        if box_w < 2 or box_h < 2:
            continue

        weight = _snap_weight(block.get("font_weight", block.get("fontWeight")))
        family = map_font_family(block.get("font_style"), weight, block.get("display"), block.get("geometric"))
        if family == "Anton":
            weight = 400  # Anton bitta (juda qalin) yozuvda — soxta bold bo'lmasin
        uppercase = _as_bool(block.get("uppercase"))
        font_size, line_height = compute_font_size(text, box_h, family, uppercase)

        align = str(block.get("align") or "left").strip().lower()
        if align not in ALIGNS:
            align = "left"
        spacing = str(block.get("letter_spacing") or "normal").strip().lower()
        letter_spacing = round(font_size * 0.06, 1) if spacing == "wide" else 0.0

        layers.append({
            "id": f"text_{uuid.uuid4().hex[:10]}",
            "text": text,
            "x": left,
            "y": top,
            "width": box_w,
            "height": box_h,
            "fontSize": font_size,
            "fontWeight": weight,
            "color": _hex(block.get("color")) or "#000000",
            "align": align,
            "fontFamily": family,
            "lineHeight": line_height,
            "letterSpacing": letter_spacing,
            "uppercase": uppercase,
            # v3: plashka rasmda qoladi, faqat harflar o'chiriladi.
            "background": None,
            "rotation": 0,
            "source": {"x": left, "y": top, "width": box_w, "height": box_h},
        })

    layers.sort(key=lambda item: (item["y"], item["x"]))
    return layers


# ---------------------------------------------------------------------------
#  OpenAI vision
# ---------------------------------------------------------------------------

def _call_openai(api_key: str, model: str, prompt: str, image_data_url: str, timeout: float) -> str | None:
    for target_model in dict.fromkeys([model, "gpt-4o"]):
        try:
            res = requests.post(
                OPENAI_CHAT_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": target_model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_data_url, "detail": "high"}},
                        ],
                    }],
                    "response_format": {"type": "json_object"},
                    "max_completion_tokens": 3000,
                },
                timeout=timeout,
            )
        except requests.RequestException as error:
            logger.warning("Matn aniqlash: OpenAI (%s) tarmoq xatosi: %s", target_model, error)
            continue
        if not res.ok:
            logger.warning("Matn aniqlash: OpenAI (%s) xatosi: %s %s", target_model, res.status_code, res.text[:200])
            continue
        choices = (res.json() or {}).get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
        if content:
            return content
    return None


def detect_text_blocks(image_bytes: bytes, timeout: float | None = None) -> list[dict]:
    """Rasmdagi matn bloklari (px koordinatalarda). Xatolikda — bo'sh ro'yxat."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("Matn qatlamlari o'tkazib yuborildi: OPENAI_API_KEY yo'q")
        return []
    try:
        width, height = image_size(image_bytes)
        text = _call_openai(
            api_key,
            _text_model(),
            build_prompt(width, height),
            _image_data_url_for_vision(image_bytes),
            timeout or TEXT_DETECT_TIMEOUT,
        )
        if not text:
            return []
        return parse_text_blocks(text, width, height)
    except Exception as error:  # matn qismi hech qachon butun so'rovni yiqitmaydi
        logger.warning("Matn bloklarini aniqlab bo'lmadi: %s", error)
        return []


# ---------------------------------------------------------------------------
#  OCR (fal-ai/florence-2-large/ocr-with-region)
# ---------------------------------------------------------------------------

def parse_ocr_results(data, width: int, height: int) -> list[dict]:
    """Florence-2 OCR javobi → [{"text", "x0", "y0", "x1", "y1"}] (px).

    fal javobi: ``{"results": {"quad_boxes": [...], "labels": [...]}}``. Quti
    elementi turlicha kelishi mumkin — barchasi qo'llab-quvvatlanadi:
    ``{"x","y","w","h","label"}``, ``{"points": [{"x","y"}...]}`` yoki 8 sonli ro'yxat
    ``[x1,y1,...,x4,y4]`` (yorliqlari parallel ``labels`` ro'yxatida).
    """
    if not isinstance(data, dict):
        return []
    results = data.get("results", data)
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except ValueError:
            return []
    if not isinstance(results, dict):
        return []
    boxes = results.get("quad_boxes") or results.get("boxes") or results.get("bboxes") or []
    labels = results.get("labels") or []
    lines: list[dict] = []
    for idx, item in enumerate(boxes if isinstance(boxes, list) else []):
        label = labels[idx] if idx < len(labels) else ""
        xs: list[float] = []
        ys: list[float] = []
        if isinstance(item, dict):
            label = item.get("label", item.get("text", label))
            points = item.get("points") or item.get("quad_box") or item.get("quad")
            if all(k in item for k in ("x", "y", "w", "h")):
                x, y, w, h = (_num(item[k]) for k in ("x", "y", "w", "h"))
                xs, ys = [x, x + w], [y, y + h]
            elif points is not None:
                item = points
        if not xs and isinstance(item, (list, tuple)):
            if item and isinstance(item[0], dict):
                xs = [_num(pt.get("x")) for pt in item]
                ys = [_num(pt.get("y")) for pt in item]
            elif item and isinstance(item[0], (list, tuple)):
                xs = [_num(pt[0]) for pt in item if len(pt) >= 2]
                ys = [_num(pt[1]) for pt in item if len(pt) >= 2]
            else:
                flat = [_num(v) for v in item]
                xs, ys = flat[0::2], flat[1::2]
        if len(xs) < 2 or len(ys) < 2:
            continue
        if max(xs + ys) <= 1.5:  # normallashtirilgan
            xs = [v * width for v in xs]
            ys = [v * height for v in ys]
        x0, x1 = max(0.0, min(xs)), min(float(width), max(xs))
        y0, y1 = max(0.0, min(ys)), min(float(height), max(ys))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        text = str(label or "").replace("</s>", "").replace("<s>", "").strip()
        lines.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return lines


def _norm_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return "".join(ch for ch in value if ch.isalnum())


def text_similarity(ocr_text: str, block_text: str) -> float:
    """OCR qatori ↔ blok matni o'xshashligi 0..1 (blokning eng mos qatori bo'yicha)."""
    needle = _norm_text(ocr_text)
    if not needle:
        return 0.0
    best = 0.0
    candidates = [line for line in block_text.split("\n") if line.strip()] + [block_text]
    for candidate in candidates:
        hay = _norm_text(candidate)
        if not hay:
            continue
        best = max(best, difflib.SequenceMatcher(None, needle, hay).ratio())
        if len(needle) >= 3 and needle in hay:
            best = max(best, 0.9)
    return best


def _iou(a: tuple, b: tuple) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match_ocr_lines(layers: list[dict], ocr_lines: list[dict]) -> dict[int, list[dict]]:
    """Har bir OCR qatorini eng mos GPT blokiga biriktiradi. {blok indeksi: [qatorlar]}.

    Mos kelish: markaz blok ichida / IoU / matn o'xshashligi (difflib). Florence
    o'zbek/kirill matnini buzib o'qishi mumkin — shuning uchun geometriya kuchli
    bo'lsa, past o'xshashlik ham qabul qilinadi; geometriya yo'q bo'lsa — faqat
    deyarli bir xil matn.
    """
    matches: dict[int, list[dict]] = {}
    for line in ocr_lines:
        lbox = (line["x0"], line["y0"], line["x1"], line["y1"])
        cx, cy = (lbox[0] + lbox[2]) / 2, (lbox[1] + lbox[3]) / 2
        line_area = (lbox[2] - lbox[0]) * (lbox[3] - lbox[1])
        best_idx, best_score = None, 0.0
        for idx, layer in enumerate(layers):
            bx0, by0 = layer["x"], layer["y"]
            bx1, by1 = bx0 + layer["width"], by0 + layer["height"]
            mx, my = layer["height"] * 0.25 + 4, layer["height"] * 0.25 + 4
            inside = bx0 <= cx <= bx1 and by0 <= cy <= by1
            near = (bx0 - mx) <= cx <= (bx1 + mx) and (by0 - my) <= cy <= (by1 + my)
            iou = _iou(lbox, (bx0, by0, bx1, by1))
            sim = text_similarity(line["text"], layer["text"])
            block_area = max(1.0, layer["width"] * layer["height"])
            if inside and line_area <= block_area * 2.0:
                required = 0.3 if line["text"] else 0.0
            elif near or iou > 0.1:
                required = 0.55
            else:
                required = 0.85
            if sim < required:
                continue
            geo = 1.0 if inside else (0.6 if near else 0.0)
            score = sim * 0.6 + max(geo, min(1.0, iou * 2)) * 0.4
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is not None:
            matches.setdefault(best_idx, []).append(line)
    return matches


def run_ocr(image_bytes: bytes, fal_run, endpoint: str, timeout: float) -> list[dict]:
    """fal Florence-2 OCR. Har qanday xatoda — bo'sh ro'yxat (GPT qutisi ishlatiladi)."""
    try:
        width, height = image_size(image_bytes)
        data = fal_run(endpoint, {"image_url": _image_data_url_for_vision(image_bytes)}, timeout=timeout)
        lines = parse_ocr_results(data, width, height)
        logger.info("Magic layers OCR: %d qator", len(lines))
        return lines
    except Exception as error:
        logger.warning("Magic layers OCR ishlamadi, GPT qutilari ishlatiladi: %s", error)
        return []


# ---------------------------------------------------------------------------
#  Qutilarni aniqlashtirish
# ---------------------------------------------------------------------------

def refine_layers(rgb, layers: list[dict], ocr_lines: list[dict]) -> list[dict]:
    """GPT qutilarini OCR + lokal harf maskasi bilan aniqlashtiradi.

    Natijada: x/y/width/height — harflarning tor qutisi, fontSize/lineHeight —
    shundan, ``source`` — tor quti + padding (o'chirish va nusxalash uchun).
    """
    from . import text_eraser as te

    height, width = rgb.shape[:2]
    lab = te._lab(rgb)
    matches = match_ocr_lines(layers, ocr_lines) if ocr_lines else {}
    refined: list[dict] = []
    for idx, layer in enumerate(layers):
        layer = dict(layer)
        box = {"x": layer["x"], "y": layer["y"], "width": layer["width"], "height": layer["height"]}
        matched = sorted(matches.get(idx, []), key=lambda line: line["y0"])
        if matched:
            x0 = min(line["x0"] for line in matched)
            y0 = min(line["y0"] for line in matched)
            x1 = max(line["x1"] for line in matched)
            y1 = max(line["y1"] for line in matched)
            box = {"x": int(x0), "y": int(y0), "width": max(2, int(round(x1 - x0))), "height": max(2, int(round(y1 - y0)))}
            # OCR qutisi aniq — kichik qidiruv chegarasi.
            margin = (int(box["height"] * 0.15) + 6, int(box["height"] * 0.15) + 6)
        else:
            margin = None  # GPT qutisi — kengroq qidiramiz
        try:
            tight = te.tighten_box(rgb, box, margin=margin, text_color=layer.get("color"), lab=lab)
        except Exception as error:
            logger.warning("Qutini toraytirib bo'lmadi: %s", error)
            tight = None
        if tight is not None:
            box = tight
        layer.update(box)

        pitch = None
        lines_count = len([ln for ln in layer["text"].split("\n") if ln.strip()])
        if lines_count > 1 and len(matched) == lines_count:
            centers = [(ln["y0"] + ln["y1"]) / 2 for ln in matched]
            gaps = [b - a for a, b in zip(centers, centers[1:])]
            pitch = sorted(gaps)[len(gaps) // 2] if gaps else None
        layer["fontSize"], layer["lineHeight"] = compute_font_size(
            layer["text"], box["height"], layer.get("fontFamily") or DEFAULT_FONT_FAMILY,
            bool(layer.get("uppercase")), line_pitch=pitch,
        )
        if layer.get("letterSpacing"):
            layer["letterSpacing"] = round(layer["fontSize"] * 0.06, 1)
        layer["source"] = te.pad_box(box, te.source_pad(box["height"]), width, height)
        refined.append(layer)
    return refined


def mock_text_layers(width: int, height: int) -> list[dict]:
    """FAL_MOCK rejimi uchun namunaviy matn qatlamlari."""
    def with_source(layer: dict) -> dict:
        pad = 6
        x0, y0 = max(0, layer["x"] - pad), max(0, layer["y"] - pad)
        x1 = min(width, layer["x"] + layer["width"] + pad)
        y1 = min(height, layer["y"] + layer["height"] + pad)
        layer["source"] = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
        return layer

    return [
        with_source({
            "id": f"text_{uuid.uuid4().hex[:10]}",
            "text": "Sifatli mahsulot\nhar kuni uchun",
            "x": int(width * 0.08), "y": int(height * 0.05),
            "width": int(width * 0.84), "height": int(height * 0.12),
            "fontSize": max(8, int(height * 0.045)), "fontWeight": 800,
            "color": "#FFFFFF", "align": "center",
            "fontFamily": "Oswald", "lineHeight": DEFAULT_LINE_HEIGHT, "letterSpacing": 0.0,
            "uppercase": True, "background": None, "rotation": 0,
        }),
        with_source({
            "id": f"text_{uuid.uuid4().hex[:10]}",
            "text": "Oʻzbekiston boʻylab yetkazib berish",
            "x": int(width * 0.08), "y": int(height * 0.86),
            "width": int(width * 0.6), "height": int(height * 0.04),
            "fontSize": max(8, int(height * 0.025)), "fontWeight": 600,
            "color": "#1A1A1A", "align": "left",
            "fontFamily": "Montserrat", "lineHeight": DEFAULT_LINE_HEIGHT, "letterSpacing": 0.0,
            "uppercase": False, "background": None, "rotation": 0,
        }),
    ]
