"""Magic Layers: rasmdagi matnni tahrirlanadigan qatlamlarga aylantirish.

Bu modul ikki ishni bajaradi:

1. OpenAI vision orqali rasmdagi matn bloklarini topadi (aniq matn,
   joylashuv, shrift o'lchami, rang, tekislash, plashka rangi).
2. Pillow bilan inpaint uchun oq-qora maska yasaydi (oq = harflar turgan
   joy). Plashka/badge'lar maskaga KIRMAYDI — ular fonda qoladi va yangi
   matn ularning ustiga qo'yiladi.

Xatolik bo'lsa (kalit yo'q, tarmoq, noto'g'ri JSON) — bo'sh ro'yxat
qaytariladi: matn qismi hech qachon 500 ga olib kelmasligi kerak.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import uuid

import requests
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
TEXT_DETECT_TIMEOUT = max(10, int(os.environ.get("MAGIC_LAYERS_TEXT_TIMEOUT_MS", 45_000)) // 1000)

# Harf atrofidagi soya/antialias qoldiqlari ham o'chishi uchun maska kengaytiriladi.
MASK_DILATE_PX = 10

FONT_WEIGHTS = (400, 600, 700, 800)
ALIGNS = ("left", "center", "right")
DEFAULT_FONT_FAMILY = "Inter"
DEFAULT_LINE_HEIGHT = 1.15

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
lines, numbers, prices, captions, badge labels). Do NOT include text physically printed
on the product or its packaging (brand logos, labels on the item itself).

Rules:
- "text": copy the text EXACTLY as it appears, character by character, in its original
  script and case. Preserve Uzbek Latin letters and apostrophes (oʻ gʻ o‘ g‘ ʼ), Cyrillic,
  digits, symbols (%, +, ×, ₽, so'm). Do not translate or fix spelling.
- Lines that belong to the same paragraph/heading (same style, stacked) form ONE block;
  join its lines with "\\n" exactly where the line breaks are in the image.
- Different styles (headline vs. subtitle, each separate badge) are separate blocks.
- "x","y","w","h": tight bounding box around the glyphs of the whole block, NORMALIZED
  to 0..1 (x,y = top-left corner; divide pixels by image width/height).
- "font_size_px": font size (em size, roughly cap height x 1.4) in pixels of THIS image.
- "font_weight": one of 400, 600, 700, 800.
- "color": text fill color as "#RRGGBB".
- "align": "left", "center" or "right" (alignment of the lines inside the block).
- "on_plate": true if the text sits on a solid colored plate/badge/pill/ribbon;
  "plate_color": that plate's color "#RRGGBB", otherwise null.
- "uppercase": true if the text is rendered in ALL CAPS.

Return ONLY JSON of the form:
{{"blocks": [{{"text": "...", "x": 0.1, "y": 0.05, "w": 0.8, "h": 0.08,
  "font_size_px": 64, "font_weight": 800, "color": "#FFFFFF", "align": "center",
  "on_plate": false, "plate_color": null, "uppercase": true}}]}}
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
    return min(FONT_WEIGHTS, key=lambda w: abs(w - weight))


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def parse_text_blocks(payload, width: int, height: int) -> list[dict]:
    """GPT javobini API kontraktidagi textLayers ro'yxatiga aylantiradi."""
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
        text = str(block.get("text") or "").replace("\r\n", "\n").replace("\\n", "\n").strip()
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

        lines = max(1, text.count("\n") + 1)
        # Qutidan kelib chiqadigan taxminiy o'lcham — GPT bahosini tekshirish uchun.
        from_box = box_h / (lines * DEFAULT_LINE_HEIGHT)
        font_size = _num(block.get("font_size_px", block.get("fontSize")), 0)
        if font_size <= 0 or font_size > from_box * 1.6 or font_size < from_box * 0.4:
            font_size = from_box
        font_size = int(round(max(8, min(font_size, height))))

        align = str(block.get("align") or "left").strip().lower()
        if align not in ALIGNS:
            align = "left"

        on_plate = _as_bool(block.get("on_plate"))
        background = _hex(block.get("plate_color"), None) if on_plate else None

        layers.append({
            "id": f"text_{uuid.uuid4().hex[:10]}",
            "text": text,
            "x": left,
            "y": top,
            "width": box_w,
            "height": box_h,
            "fontSize": font_size,
            "fontWeight": _snap_weight(block.get("font_weight", block.get("fontWeight"))),
            "color": _hex(block.get("color")) or "#000000",
            "align": align,
            "fontFamily": DEFAULT_FONT_FAMILY,
            "lineHeight": DEFAULT_LINE_HEIGHT,
            "uppercase": _as_bool(block.get("uppercase")),
            "background": background,
            "rotation": 0,
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
#  Maska
# ---------------------------------------------------------------------------

def build_text_mask(size: tuple[int, int], text_layers: list[dict], dilate: int = MASK_DILATE_PX) -> Image.Image:
    """Oq = o'chiriladigan harflar, qora = saqlanadi. Faqat harf qutilari, plashka emas."""
    width, height = size
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for layer in text_layers:
        left = max(0, int(layer["x"]) - dilate)
        top = max(0, int(layer["y"]) - dilate)
        right = min(width - 1, int(layer["x"]) + int(layer["width"]) + dilate)
        bottom = min(height - 1, int(layer["y"]) + int(layer["height"]) + dilate)
        if right > left and bottom > top:
            draw.rectangle([left, top, right, bottom], fill=255)
    return mask


def mask_to_png_bytes(mask: Image.Image) -> bytes:
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def merge_inpainted(original_bytes: bytes, inpainted_bytes: bytes, mask: Image.Image) -> bytes:
    """Inpaint natijasini faqat maska ichida qo'llaydi, asl o'lchamda PNG qaytaradi.

    Fill modeli maskadan tashqaridagi piksellarni ham biroz o'zgartirishi mumkin —
    shuning uchun maskadan tashqarisi asl rasmdan olinadi.
    """
    with Image.open(io.BytesIO(original_bytes)) as original_img:
        original = original_img.convert("RGBA")
    with Image.open(io.BytesIO(inpainted_bytes)) as inpainted_img:
        inpainted = inpainted_img.convert("RGBA")
    if inpainted.size != original.size:
        inpainted = inpainted.resize(original.size, Image.LANCZOS)
    alpha = mask.convert("L")
    if alpha.size != original.size:
        alpha = alpha.resize(original.size, Image.NEAREST)
    alpha = alpha.filter(ImageFilter.GaussianBlur(2))
    merged = Image.composite(inpainted, original, alpha)
    buf = io.BytesIO()
    merged.save(buf, format="PNG")
    return buf.getvalue()


def mock_text_layers(width: int, height: int) -> list[dict]:
    """FAL_MOCK rejimi uchun namunaviy matn qatlamlari."""
    return [
        {
            "id": f"text_{uuid.uuid4().hex[:10]}",
            "text": "Sifatli mahsulot\nhar kuni uchun",
            "x": int(width * 0.08), "y": int(height * 0.05),
            "width": int(width * 0.84), "height": int(height * 0.12),
            "fontSize": max(8, int(height * 0.045)), "fontWeight": 800,
            "color": "#FFFFFF", "align": "center",
            "fontFamily": DEFAULT_FONT_FAMILY, "lineHeight": DEFAULT_LINE_HEIGHT,
            "uppercase": True, "background": None, "rotation": 0,
        },
        {
            "id": f"text_{uuid.uuid4().hex[:10]}",
            "text": "Oʻzbekiston boʻylab yetkazib berish",
            "x": int(width * 0.08), "y": int(height * 0.86),
            "width": int(width * 0.6), "height": int(height * 0.04),
            "fontSize": max(8, int(height * 0.025)), "fontWeight": 600,
            "color": "#1A1A1A", "align": "left",
            "fontFamily": DEFAULT_FONT_FAMILY, "lineHeight": DEFAULT_LINE_HEIGHT,
            "uppercase": False, "background": "#FFD400", "rotation": 0,
        },
    ]
