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
    return f"""You are an OCR + typography + layout analyst for marketplace product-card infographics.
The image is exactly {width}x{height} pixels. You return TWO lists: text "blocks" and visual "objects".

=== PART 1: TEXT BLOCKS ===
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
- List every printed text ONCE. Never output the same text twice for the same place.
  The same word appearing at two different places (e.g. two different cards) is two blocks.
- Text physically printed on the product or its packaging (brand logo, label on the
  item itself) is NOT design text: include it with "product_label": true, or skip it.

For each block return:
- "text": exact text (see rules above).
- "x","y","w","h": tight bounding box around the glyphs of the whole block, NORMALIZED
  to 0..1 (x,y = top-left corner; divide pixels by image width/height). Be precise:
  the box must contain only this block's letters, not neighbouring text.
- "font_family": the closest match from this exact list:
  "Inter" (neutral grotesque, e.g. Helvetica/Arial/Roboto),
  "Montserrat" (geometric/wide sans with round O, typical bold card headline, also tech/extended faces),
  "Oswald" (condensed tall narrow sans),
  "Anton" (ultra-heavy condensed poster face, Bebas/Impact-like),
  "Playfair Display" (serif), "Nunito" (rounded sans), "Caveat" (handwritten/script/brush).
- "font_style": one of "condensed", "sans", "serif", "rounded", "script".
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

=== PART 2: VISUAL OBJECTS ===
List the separate visual elements a designer would want to move independently
(like Canva "Magic Layers"):
- The main product(s): each product item is its own object, "kind": "product".
- Every icon card, badge, plate, pill, ribbon, emblem, standalone icon, arrow,
  sticker or decorative element: "kind": "graphic". A card/plate together with the
  icon drawn on it is ONE object (do not list that icon separately).
- Do NOT list the background (gradients, patterns, scenery, glow, light rays, floor)
  and do NOT list text as an object (text is in PART 1; only its plate/card is an object).
- At most 16 objects; prefer meaningful whole units over tiny fragments.
For each object return:
- "name": a short Uzbek (Latin) name, 1-3 words, e.g. "Mashina", "Tezlik ikonkasi",
  "Ikonka kartasi", "Nishon", "Strelka".
- "kind": "product" or "graphic".
- "x","y","w","h": NORMALIZED bounding box enclosing the whole object (without soft shadow).
- "z": integer stacking order, higher = more in front (e.g. plates 1, product 5,
  a badge overlapping the product 8).

Return ONLY JSON of the form:
{{"blocks": [{{"text": "DAM OLISH UCHUN\\nIDEAL", "x": 0.1, "y": 0.05, "w": 0.8, "h": 0.08,
  "font_family": "Oswald", "font_style": "condensed", "display": false, "geometric": false,
  "font_weight": 800, "color": "#FFFFFF", "align": "center", "uppercase": true,
  "letter_spacing": "normal", "on_plate": false, "plate_color": null, "product_label": false}}],
 "objects": [{{"name": "Mashina", "kind": "product", "x": 0.1, "y": 0.35, "w": 0.8, "h": 0.35, "z": 5}}]}}
If there is nothing, return {{"blocks": [], "objects": []}}."""


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


_FAMILY_ALIASES = {
    "inter": "Inter", "helvetica": "Inter", "arial": "Inter", "roboto": "Inter", "grotesque": "Inter",
    "montserrat": "Montserrat", "futura": "Montserrat", "gotham": "Montserrat", "geometric": "Montserrat",
    "poppins": "Montserrat", "extended": "Montserrat", "wide": "Montserrat",
    "oswald": "Oswald", "condensed": "Oswald", "narrow": "Oswald", "compressed": "Oswald",
    "anton": "Anton", "bebas": "Anton", "impact": "Anton", "poster": "Anton",
    "playfair": "Playfair Display", "serif": "Playfair Display", "didot": "Playfair Display",
    "nunito": "Nunito", "rounded": "Nunito",
    "caveat": "Caveat", "script": "Caveat", "handwritten": "Caveat", "brush": "Caveat", "cursive": "Caveat",
}


def _family_from_name(value) -> str | None:
    """GPT bergan shrift nomi → FONT_FAMILIES'dan biri (aniq yoki taxallus bo'yicha)."""
    name = str(value or "").strip().lower()
    if not name:
        return None
    for family in FONT_FAMILIES:
        if name == family.lower():
            return family
    if "sans" in name and "serif" in name:  # "sans-serif" — serif emas
        name = name.replace("serif", "")
    for alias, family in _FAMILY_ALIASES.items():
        if alias in name:
            return family
    return None


def map_font_family(font_style, weight: int = 700, display=False, geometric=None, font_family=None) -> str:
    """GPT ``font_family``/``font_style`` → frontend yuklaydigan shriftlardan biri.

    v4: avval GPT tanlagan ``font_family`` (ro'yxatdan) olinadi. Aks holda
    ``font_style`` — endi qisman moslik bilan ("bold sans-serif", "geometric sans",
    "condensed display"): v3 da bunday qiymatlar sukutdagi "Inter" ga tushardi.
    """
    picked = _family_from_name(font_family)
    if picked:
        if picked == "Oswald" and (_as_bool(display) or weight >= 900):
            return "Anton"
        return picked
    style = str(font_style or "").strip().lower()
    if any(key in style for key in ("condensed", "narrow", "compressed", "display", "poster")):
        if _as_bool(display) or "display" in style or "poster" in style or weight >= 900:
            return "Anton"
        return "Oswald"
    if "script" in style or "hand" in style or "brush" in style or "cursive" in style:
        return "Caveat"
    if "round" in style:
        return "Nunito"
    if "serif" in style and "sans" not in style:
        return "Playfair Display"
    if "sans" in style or "geometric" in style or "grotesk" in style or "grotesque" in style:
        if "geometric" in style:
            return "Montserrat"
        if geometric is None:
            return "Montserrat" if weight >= 700 else "Inter"
        return "Montserrat" if _as_bool(geometric) else "Inter"
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


def _load_json(payload):
    if isinstance(payload, str):
        cleaned = re.sub(r"^```(?:json)?|```$", "", payload.strip(), flags=re.M).strip()
        payload = json.loads(cleaned)
    return payload


def _px_box(item: dict, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Normallashtirilgan (yoki piksel) x/y/w/h → rasm ichidagi (left, top, right, bottom)."""
    x, y = _num(item.get("x")), _num(item.get("y"))
    w, h = _num(item.get("w", item.get("width"))), _num(item.get("h", item.get("height")))
    if max(x, y, w, h) > 1.5:
        px, py, pw, ph = x, y, w, h
    else:
        px, py, pw, ph = x * width, y * height, w * width, h * height
    left = int(round(max(0.0, min(px, width - 1))))
    top = int(round(max(0.0, min(py, height - 1))))
    right = int(round(max(left + 1, min(px + pw, width))))
    bottom = int(round(max(top + 1, min(py + ph, height))))
    return left, top, right, bottom


OBJECT_KINDS = ("product", "graphic")
MAX_OBJECTS = 16


def parse_objects(payload, width: int, height: int) -> list[dict]:
    """GPT ``objects`` → [{"name", "kind", "x", "y", "width", "height", "z"}] (px).

    Fon (rasmning ~85% idan ko'p) va juda mayda qutilar tashlanadi; ko'pi bilan 16 ta.
    """
    try:
        payload = _load_json(payload)
    except ValueError:
        return []
    raw = payload.get("objects") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    objects: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        left, top, right, bottom = _px_box(item, width, height)
        box_w, box_h = right - left, bottom - top
        if box_w < 8 or box_h < 8:
            continue
        if box_w * box_h > 0.85 * width * height:
            continue  # bu fon — obyekt emas
        kind = str(item.get("kind") or "graphic").strip().lower()
        if kind not in OBJECT_KINDS:
            kind = "product" if "product" in kind or "mahsulot" in kind else "graphic"
        name = re.sub(r"\s+", " ", str(item.get("name") or "").strip())[:40]
        objects.append({
            "name": name or ("Mahsulot" if kind == "product" else f"Element {idx + 1}"),
            "kind": kind,
            "x": left, "y": top, "width": box_w, "height": box_h,
            "z": _num(item.get("z"), 5.0 if kind == "product" else 3.0),
        })
    # Mahsulotlar har doim kerak — ko'p bo'lsa, eng mayda grafikalar tashlanadi.
    objects.sort(key=lambda o: (o["kind"] != "product", -(o["width"] * o["height"])))
    return objects[:MAX_OBJECTS]


def parse_text_blocks(payload, width: int, height: int) -> list[dict]:
    """GPT javobini API kontraktidagi textLayers ro'yxatiga aylantiradi (GPT qutisi bilan).

    Keyin ``refine_layers`` qutilarni OCR/piksellar bo'yicha aniqlashtiradi.
    """
    payload = _load_json(payload)

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

        # Ba'zan model normalizatsiya o'rniga piksel qaytaradi — _px_box ikkalasini ham biladi.
        left, top, right, bottom = _px_box(block, width, height)
        box_w, box_h = right - left, bottom - top
        if box_w < 2 or box_h < 2:
            continue

        weight = _snap_weight(block.get("font_weight", block.get("fontWeight")))
        family = map_font_family(block.get("font_style"), weight, block.get("display"), block.get("geometric"),
                                 block.get("font_family", block.get("fontFamily")))
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
    return dedupe_layers(layers)


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
                    "max_completion_tokens": 4500,
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


def detect_layout(image_bytes: bytes, timeout: float | None = None) -> tuple[list[dict], list[dict]]:
    """Bitta GPT chaqiruvi: (matn bloklari, vizual obyektlar) px koordinatalarda.

    Xatolikda — ([], []): Magic Layers hech qachon shu sababli 500 bermaydi.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("Matn/obyekt qatlamlari o'tkazib yuborildi: OPENAI_API_KEY yo'q")
        return [], []
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
            return [], []
        payload = _load_json(text)
        blocks = parse_text_blocks(payload, width, height)
        objects = parse_objects(payload, width, height)
        logger.info(
            "Magic layers GPT: bloklar=%d obyektlar=%d shriftlar=%s",
            len(blocks), len(objects), sorted({b["fontFamily"] for b in blocks}),
        )
        return blocks, objects
    except Exception as error:  # matn qismi hech qachon butun so'rovni yiqitmaydi
        logger.warning("Matn bloklarini aniqlab bo'lmadi: %s", error)
        return [], []


def detect_text_blocks(image_bytes: bytes, timeout: float | None = None) -> list[dict]:
    """Faqat matn bloklari (eski API). Xatolikda — bo'sh ro'yxat."""
    return detect_layout(image_bytes, timeout)[0]


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


def _line_count(text: str) -> int:
    return max(1, len([line for line in str(text).split("\n") if line.strip()]))


def match_ocr_lines(layers: list[dict], ocr_lines: list[dict]) -> dict[int, list[dict]]:
    """OCR qatorlarini GPT bloklariga biriktiradi. {blok indeksi: [qatorlar]}.

    Mos kelish: markaz blok ichida / IoU / matn o'xshashligi (difflib). Florence
    o'zbek/kirill matnini buzib o'qishi mumkin — shuning uchun geometriya kuchli
    bo'lsa, past o'xshashlik ham qabul qilinadi; geometriya yo'q bo'lsa — faqat
    deyarli bir xil matn.

    v4 qoidalari (production'dagi "Sport" 209px xatosi):
    - har bir OCR qatori ko'pi bilan BITTA blokka tegishli;
    - qator balandligi blokning bir qatoridan keskin farq qilsa (≥2.5× yoki ≤0.3×)
      va matn deyarli bir xil bo'lmasa — bu boshqa matn (masalan, sarlavha);
    - blok o'z qatorlari sonidan (+1) ko'p qator olmaydi — eng yuqori ballilari qoladi.
    """
    pairs: list[tuple[float, int, int]] = []
    for line_idx, line in enumerate(ocr_lines):
        lbox = (line["x0"], line["y0"], line["x1"], line["y1"])
        cx, cy = (lbox[0] + lbox[2]) / 2, (lbox[1] + lbox[3]) / 2
        line_area = (lbox[2] - lbox[0]) * (lbox[3] - lbox[1])
        line_h = lbox[3] - lbox[1]
        for idx, layer in enumerate(layers):
            bx0, by0 = layer["x"], layer["y"]
            bx1, by1 = bx0 + layer["width"], by0 + layer["height"]
            mx, my = layer["height"] * 0.25 + 4, layer["height"] * 0.25 + 4
            inside = bx0 <= cx <= bx1 and by0 <= cy <= by1
            near = (bx0 - mx) <= cx <= (bx1 + mx) and (by0 - my) <= cy <= (by1 + my)
            iou = _iou(lbox, (bx0, by0, bx1, by1))
            sim = text_similarity(line["text"], layer["text"])
            block_area = max(1.0, layer["width"] * layer["height"])
            single_h = layer["height"] / _line_count(layer["text"])
            if sim < 0.8 and (line_h > 2.5 * single_h + 4 or line_h < 0.3 * single_h):
                continue  # o'lchami mos emas — boshqa matnning qatori
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
            pairs.append((score, line_idx, idx))

    pairs.sort(key=lambda item: -item[0])
    used_lines: set[int] = set()
    matches: dict[int, list[dict]] = {}
    for score, line_idx, idx in pairs:
        if line_idx in used_lines:
            continue
        taken = matches.get(idx, [])
        line = ocr_lines[line_idx]
        if len(taken) >= _line_count(layers[idx]["text"]):
            # Qatorlar soni to'lgan: faqat shu qatorning bo'lagi (OCR qatorni gorizontal
            # bo'lib yuborgan) qo'shiladi. Pastdagi boshqa qator — boshqa matn: aks holda
            # bir qatorli blok balandligi 2-3 baravar oshib, fontSize "209px" bo'lardi.
            def same_row(other):
                overlap = min(line["y1"], other["y1"]) - max(line["y0"], other["y0"])
                return overlap >= 0.5 * min(line["y1"] - line["y0"], other["y1"] - other["y0"])
            if not any(same_row(other) for other in taken):
                continue
        used_lines.add(line_idx)
        matches.setdefault(idx, []).append(ocr_lines[line_idx])
    return matches


def _box_tuple(layer: dict) -> tuple[float, float, float, float]:
    return (layer["x"], layer["y"], layer["x"] + layer["width"], layer["y"] + layer["height"])


def _center_inside(inner: tuple, outer: tuple) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def boxes_collide(a: dict, b: dict, iou_limit: float = 0.3) -> bool:
    """Ikki quti bitta joyni egallaydimi: IoU > 0.3 yoki birining markazi ikkinchisi ichida."""
    ta, tb = _box_tuple(a), _box_tuple(b)
    return _iou(ta, tb) > iou_limit or _center_inside(ta, tb) or _center_inside(tb, ta)


def dedupe_layers(layers: list[dict], confidence: list[float] | None = None) -> list[dict]:
    """Takroriy matn qatlamlarini olib tashlaydi.

    - bir xil (normallashtirilgan) matn + to'qnashuvchi qutilar → bittasi qoladi
      (ishonchliroq: OCR tasdiqlagani, keyin kattaroq ishonch/maydon);
    - har xil matn, lekin qutilar to'qnashadi va bittasi OCR bilan tasdiqlangan,
      ikkinchisi yo'q → tasdiqlanmagan (GPT qutisi boshqa matn ustiga tushgan) tashlanadi.
    """
    if confidence is None:
        confidence = [0.0] * len(layers)
    order = sorted(range(len(layers)), key=lambda i: (-confidence[i], -(layers[i]["width"] * layers[i]["height"]), i))
    kept: list[int] = []
    for idx in order:
        layer = layers[idx]
        norm = _norm_text(layer["text"])
        duplicate = False
        for other_idx in kept:
            other = layers[other_idx]
            if not boxes_collide(layer, other):
                continue
            other_norm = _norm_text(other["text"])
            same = norm == other_norm or (
                len(norm) >= 3 and len(other_norm) >= 3 and (norm in other_norm or other_norm in norm)
                and _iou(_box_tuple(layer), _box_tuple(other)) > 0.3
            )
            if same:
                duplicate = True
            elif confidence[other_idx] > 0 and confidence[idx] <= 0:
                duplicate = True  # tasdiqlanmagan quti boshqa (tasdiqlangan) matn ustida
            if duplicate:
                logger.info("Magic layers: takroriy/to'qnashgan matn tashlandi: %r (≈ %r)",
                            layer["text"][:40], other["text"][:40])
                break
        if not duplicate:
            kept.append(idx)
    kept.sort()
    return [layers[i] for i in kept]


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

MAX_FONT_TO_LINE = 1.45  # fontSize ≤ 1.45 × bir qator quti balandligi


def clamp_font_size(text: str, font_size: int, box_height: float, layer_id: str = "") -> int:
    """Bema'ni shrift o'lchamini (production'dagi 209px) tor quti balandligiga siqadi."""
    single = box_height / _line_count(text)
    limit = max(8, int(round(MAX_FONT_TO_LINE * single)))
    if font_size > limit:
        logger.warning("Magic layers: fontSize %s → %s (%s, quti %.0fpx, %r)",
                       font_size, limit, layer_id, box_height, text[:30])
        return limit
    return font_size


def refine_layers(rgb, layers: list[dict], ocr_lines: list[dict]) -> list[dict]:
    """GPT qutilarini OCR + lokal harf maskasi bilan aniqlashtiradi, takrorlarni olib tashlaydi.

    Natijada: x/y/width/height — harflarning tor qutisi, fontSize/lineHeight —
    shundan (va qutiga siqilgan), ``source`` — tor quti + padding (o'chirish va nusxalash uchun).
    """
    from . import text_eraser as te

    height, width = rgb.shape[:2]
    lab = te._lab(rgb)
    layers = dedupe_layers(layers)
    matches = match_ocr_lines(layers, ocr_lines) if ocr_lines else {}
    refined: list[dict] = []
    confidence: list[float] = []
    for idx, layer in enumerate(layers):
        layer = dict(layer)
        gpt_box = {"x": layer["x"], "y": layer["y"], "width": layer["width"], "height": layer["height"]}
        box = dict(gpt_box)
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
        if tight is not None and not matched and tight["height"] > 2.0 * gpt_box["height"] + 6:
            # Tasdiqlanmagan GPT qutisi qo'shni (kattaroq) matnning harflarini "yutib" yubordi.
            logger.info("Magic layers: %r qutisi %dpx → %dpx o'sib ketdi, rad etildi",
                        layer["text"][:30], gpt_box["height"], tight["height"])
            tight = None
        if tight is not None:
            box = tight
        layer.update(box)

        pitch = None
        lines_count = _line_count(layer["text"])
        if lines_count > 1 and len(matched) == lines_count:
            centers = [(ln["y0"] + ln["y1"]) / 2 for ln in matched]
            gaps = [b - a for a, b in zip(centers, centers[1:])]
            pitch = sorted(gaps)[len(gaps) // 2] if gaps else None
        layer["fontSize"], layer["lineHeight"] = compute_font_size(
            layer["text"], box["height"], layer.get("fontFamily") or DEFAULT_FONT_FAMILY,
            bool(layer.get("uppercase")), line_pitch=pitch,
        )
        layer["fontSize"] = clamp_font_size(layer["text"], layer["fontSize"], box["height"], layer.get("id", ""))
        if layer.get("letterSpacing"):
            layer["letterSpacing"] = round(layer["fontSize"] * 0.06, 1)
        layer["source"] = te.pad_box(box, te.source_pad(box["height"]), width, height)
        refined.append(layer)
        sim = max((text_similarity(ln["text"], layer["text"]) for ln in matched), default=0.0)
        confidence.append((1.0 + sim) if matched else (0.5 if tight is not None else 0.0))
    return dedupe_layers(refined, confidence)


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
