"""O'lchamni moslash: istalgan rasm → Uzum kartasi 1080×1440 (3:4).

Rejimlar:
  * ``ai``    — rasm butunligicha (kesilmasdan) joylanadi, yetishmagan chetlarni
                fal ``fal-ai/bria/expand`` chizadi; asl piksellar natija ustiga
                qayta qo'yiladi — mahsulot bir pikselga ham o'zgarmaydi.
                Rasm allaqachon ~3:4 bo'lsa (±1%) — AI chaqirilmaydi.
  * ``blur``  — fon: shu rasmning o'zi, kattalashtirilgan + xira + biroz to'q;
                ustida asl rasm, yumshoq soya bilan.
  * ``color`` — bir xil rangli fon (``#RRGGBB`` yoki ``auto`` — chetlar rangi).
  * ``crop``  — 3:4 ga kesish; ``focusX``/``focusY`` (0..1) qaysi qism qolishini belgilaydi.

Har qanday rejimda kanvasda joy oladigan qism maqsad o'lchamidan kichik bo'lsa,
avval fal ``fal-ai/esrgan`` bilan kattalashtiriladi (mahsulotni o'zgartirmaydigan,
"ijodiy" bo'lmagan upscaler). fal ishlamasa — Pillow LANCZOS + yengil keskinlik.

1 rasm = 1 token, rejimdan qat'i nazar. Xato yoki bekor qilinsa — qaytariladi
(``tasks.run_job`` orqali, oddiy generatsiyalar bilan bir xil).
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import uuid
from collections import Counter

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, ImageFilter, ImageOps, ImageEnhance

from .providers import GenerationError, _store

logger = logging.getLogger(__name__)

TARGET_W = 1080
TARGET_H = 1440
TARGET_ASPECT = TARGET_W / TARGET_H
ASPECT_TOLERANCE = 0.01

MODES = ("ai", "blur", "color", "crop")
MAX_INPUT_BYTES = 15 * 1024 * 1024
MIN_SHORT_SIDE = 200
JPEG_QUALITY = 92

FAL_EXPAND_ENDPOINT = os.environ.get("FAL_EXPAND_ENDPOINT", "").strip() or "fal-ai/bria/expand"
FAL_UPSCALE_ENDPOINT = os.environ.get("FAL_UPSCALE_ENDPOINT", "").strip() or "fal-ai/esrgan"
EXPAND_TIMEOUT = max(20, int(os.environ.get("RESIZE_EXPAND_TIMEOUT_S", 150)))
UPSCALE_TIMEOUT = max(10, int(os.environ.get("RESIZE_UPSCALE_TIMEOUT_S", 90)))
# Kichik farq (masalan 1050 → 1080) uchun AI upscaler'ga pul to'lash shart emas.
FAL_UPSCALE_MIN_FACTOR = float(os.environ.get("RESIZE_FAL_UPSCALE_MIN_FACTOR", 1.25))
UPSCALE_EPSILON = 1.02

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
DATA_URL_RE = re.compile(r"^data:image/([A-Za-z0-9.+-]+);base64,(.+)$", re.S)

try:  # HEIC/HEIF — faqat pillow-heif o'rnatilgan bo'lsa
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # noqa: BLE001
    HEIF_SUPPORTED = False

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "MPO"} | ({"HEIF", "HEIC"} if HEIF_SUPPORTED else set())


class BadImage(ValueError):
    """Foydalanuvchiga ko'rsatiladigan (o'zbekcha) xabar bilan."""


# --------------------------------------------------------------------------- kirish

def decode_upload(data_url: str) -> tuple[bytes, Image.Image]:
    """data URL → (asl baytlar, tekshirilgan Pillow rasmi). Xato — ``BadImage``."""
    match = DATA_URL_RE.match(data_url or "") if isinstance(data_url, str) else None
    if not match:
        raise BadImage("Rasm yuklanmadi yoki formati noto'g'ri. JPG, PNG yoki WEBP yuklang.")
    payload = match.group(2)
    # base64 hajmi ≈ 4/3 — dekodlashdan oldin ham chegaralaymiz.
    if len(payload) > MAX_INPUT_BYTES * 4 // 3 + 16:
        raise BadImage("Rasm hajmi 15 MB dan oshmasligi kerak.")
    try:
        raw = base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        raise BadImage("Rasmni o'qib bo'lmadi. Boshqa fayl yuklang.")
    if len(raw) > MAX_INPUT_BYTES:
        raise BadImage("Rasm hajmi 15 MB dan oshmasligi kerak.")
    try:
        image = Image.open(io.BytesIO(raw))
        image_format = (image.format or "").upper()
        image.load()
    except Image.DecompressionBombError:
        raise BadImage("Rasm o'lchami juda katta.")
    except Exception:  # noqa: BLE001
        raise BadImage("Rasmni o'qib bo'lmadi. JPG, PNG yoki WEBP yuklang.")
    if image_format not in ALLOWED_FORMATS:
        raise BadImage("Bu format qo'llab-quvvatlanmaydi. JPG, PNG yoki WEBP yuklang.")
    image = ImageOps.exif_transpose(image)
    if min(image.size) < MIN_SHORT_SIDE:
        raise BadImage(f"Rasm juda kichik: qisqa tomoni kamida {MIN_SHORT_SIDE} px bo'lishi kerak.")
    image.format = image_format  # exif_transpose formatni yo'qotadi
    return raw, image


def has_transparency(image: Image.Image) -> bool:
    if image.mode in ("RGBA", "LA", "PA"):
        alpha = image.getchannel("A") if image.mode != "PA" else image.convert("RGBA").getchannel("A")
        return alpha.getextrema()[0] < 255
    if image.mode == "P" and "transparency" in image.info:
        return has_transparency(image.convert("RGBA"))
    return False


def to_srgb(image: Image.Image) -> Image.Image:
    """EXIF burilishi qo'llangan rasm → sRGB, RGB yoki RGBA (shaffoflik bo'lsa)."""
    alpha = has_transparency(image)
    icc = image.info.get("icc_profile")
    if icc and image.mode in ("RGB", "RGBA", "CMYK"):
        try:
            from PIL import ImageCms

            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            dst = ImageCms.createProfile("sRGB")
            out_mode = "RGBA" if image.mode == "RGBA" else "RGB"
            image = ImageCms.profileToProfile(image, src, dst, outputMode=out_mode)
        except Exception:  # noqa: BLE001 — buzuq profil rasmni to'xtatmasin
            logger.info("ICC profilni o'girib bo'lmadi, oddiy konvertatsiya")
    image = image.convert("RGBA" if alpha else "RGB")
    image.info.pop("icc_profile", None)
    return image


def store_source(raw: bytes, image: Image.Image, user_id: int) -> str:
    """Asl faylni ``generated/user_<id>/src_<uuid>.<ext>`` ga saqlaydi, yo'lni qaytaradi."""
    fmt = (image.format or "").upper()
    ext = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "WEBP": "webp"}.get(fmt)
    if ext is None:  # HEIC va h.k. — brauzer ochadigan PNG ga
        buffer = io.BytesIO()
        to_srgb(image).save(buffer, format="PNG")
        raw, ext = buffer.getvalue(), "png"
    name = f"generated/user_{user_id}/src_{uuid.uuid4().hex}.{ext}"
    return default_storage.save(name, ContentFile(raw))


def load_source(path: str) -> Image.Image:
    with default_storage.open(path, "rb") as handle:
        image = Image.open(io.BytesIO(handle.read()))
        image.load()
    return to_srgb(ImageOps.exif_transpose(image))


# --------------------------------------------------------------------------- geometriya

def is_target_aspect(width: int, height: int) -> bool:
    return abs((width / height) / TARGET_ASPECT - 1) <= ASPECT_TOLERANCE


def fit_box(width: int, height: int) -> tuple[int, int, int, int]:
    """Rasm 1080×1440 ichiga to'liq sig'adigan o'lcham va markazlangan joyi: (w, h, x, y)."""
    scale = min(TARGET_W / width, TARGET_H / height)
    fw = max(1, min(TARGET_W, round(width * scale)))
    fh = max(1, min(TARGET_H, round(height * scale)))
    if is_target_aspect(width, height):
        fw, fh = TARGET_W, TARGET_H
    return fw, fh, (TARGET_W - fw) // 2, (TARGET_H - fh) // 2


def crop_box(width: int, height: int, focus_x: float = 0.5, focus_y: float = 0.5) -> tuple[int, int, int, int]:
    """3:4 kesish qutisi (left, top, right, bottom); fokus — qolgan qismning markazi."""
    if width / height > TARGET_ASPECT:
        cw, ch = max(1, round(height * TARGET_ASPECT)), height
    else:
        cw, ch = width, max(1, round(width / TARGET_ASPECT))
    left = round(focus_x * width - cw / 2)
    top = round(focus_y * height - ch / 2)
    left = max(0, min(width - cw, left))
    top = max(0, min(height - ch, top))
    return left, top, left + cw, top + ch


# --------------------------------------------------------------------------- fal

def _fal_enabled() -> bool:
    from .layers import _fal_key, _is_mock

    return bool(_fal_key()) and not _is_mock()


def _data_url(image: Image.Image, fmt: str = "PNG") -> str:
    buffer = io.BytesIO()
    if fmt == "JPEG":
        image.convert("RGB").save(buffer, format="JPEG", quality=95)
        mime = "image/jpeg"
    else:
        image.save(buffer, format="PNG")
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def _fal_image(result: dict) -> Image.Image:
    from .layers import _download

    url = ((result or {}).get("image") or {}).get("url") if isinstance(result, dict) else None
    if not url:
        raise GenerationError("AI rasm qaytarmadi", status=502, code="EMPTY_IMAGE_OUTPUT")
    image = Image.open(io.BytesIO(_download(url, timeout=60)))
    image.load()
    return image.convert("RGB")


def fal_upscale(image: Image.Image, factor: float) -> Image.Image:
    """fal-ai/esrgan (RealESRGAN_x4plus) — faqat RGB. Xato — istisno."""
    from .layers import _fal_run

    scale = max(2.0, min(4.0, round(factor + 0.49)))
    result = _fal_run(
        FAL_UPSCALE_ENDPOINT,
        {
            "image_url": _data_url(image.convert("RGB"), "PNG"),
            "scale": scale,
            "model": "RealESRGAN_x4plus",
            "face": False,
            "output_format": "png",
        },
        timeout=UPSCALE_TIMEOUT,
    )
    return _fal_image(result)


def lanczos_upscale(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    resized = image.resize(size, Image.LANCZOS)
    if resized.mode == "RGBA":
        rgb = resized.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
        rgb.putalpha(resized.getchannel("A"))
        return rgb
    return resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))


def resize_region(image: Image.Image, size: tuple[int, int]) -> tuple[Image.Image, bool, str | None]:
    """Rasmni aniq ``size`` ga keltiradi. Kattalashtirish kerak bo'lsa — avval fal.

    Qaytaradi: (rasm, upscaled, upscaler) — upscaler: "esrgan" | "lanczos" | None.
    """
    tw, th = size
    factor = max(tw / image.width, th / image.height)
    if factor <= UPSCALE_EPSILON:
        out = image if image.size == size else image.resize(size, Image.LANCZOS)
        return out, False, None

    if factor >= FAL_UPSCALE_MIN_FACTOR and _fal_enabled():
        try:
            big = fal_upscale(image, factor)
            out = big.resize(size, Image.LANCZOS)
            if image.mode == "RGBA":
                out = out.convert("RGB")
                out.putalpha(image.getchannel("A").resize(size, Image.LANCZOS))
            return out, True, "esrgan"
        except Exception as error:  # noqa: BLE001 — zaxira yo'l bor
            logger.warning("fal upscale ishlamadi, LANCZOS ga o'tamiz: %s", error)
    return lanczos_upscale(image, size), True, "lanczos"


# --------------------------------------------------------------------------- kompozitsiya

def _flatten(image: Image.Image, background) -> Image.Image:
    if image.mode != "RGBA":
        return image.convert("RGB")
    base = Image.new("RGB", image.size, background)
    base.paste(image, (0, 0), image)
    return base


def dominant_edge_color(image: Image.Image) -> tuple[int, int, int]:
    """Rasm chetlaridagi eng ko'p uchraydigan rang (shaffof piksellar hisobga olinmaydi)."""
    small = image.copy()
    small.thumbnail((200, 200))
    rgba = small.convert("RGBA")
    w, h = rgba.size
    band = max(1, round(min(w, h) * 0.04))
    pixels = rgba.load()
    samples = []
    for y in range(h):
        for x in range(w):
            if x < band or x >= w - band or y < band or y >= h - band:
                r, g, b, a = pixels[x, y]
                if a >= 128:
                    samples.append((r, g, b))
    if not samples:
        return (255, 255, 255)
    buckets = Counter((r >> 4, g >> 4, b >> 4) for r, g, b in samples)
    top = buckets.most_common(1)[0][0]
    chosen = [p for p in samples if (p[0] >> 4, p[1] >> 4, p[2] >> 4) == top]
    n = len(chosen)
    return tuple(round(sum(c[i] for c in chosen) / n) for i in range(3))  # type: ignore[return-value]


def parse_color(value, image: Image.Image) -> tuple[int, int, int]:
    if value in (None, "", "auto"):
        return dominant_edge_color(image)
    text = str(value)
    return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _shadow(canvas: Image.Image, fg: Image.Image, x: int, y: int) -> None:
    """Old rasm ostiga yumshoq soya (alfa bo'lsa — shakli bo'yicha)."""
    pad = 60
    mask = Image.new("L", (fg.width + pad * 2, fg.height + pad * 2), 0)
    shape = fg.getchannel("A") if fg.mode == "RGBA" else Image.new("L", fg.size, 255)
    mask.paste(shape, (pad, pad))
    mask = mask.filter(ImageFilter.GaussianBlur(22)).point(lambda v: int(v * 0.45))
    shadow = Image.new("RGB", mask.size, (0, 0, 0))
    canvas.paste(shadow, (x - pad, y - pad + 14), mask)


def blurred_background(image: Image.Image) -> Image.Image:
    """Rasmning o'zi: 1080×1440 ni to'liq qoplaydi, xira (≈r40), biroz to'q va rangsizroq."""
    rgb = _flatten(image, (255, 255, 255)) if image.mode == "RGBA" else image.convert("RGB")
    cover = ImageOps.fit(rgb, (TARGET_W // 4, TARGET_H // 4), Image.LANCZOS)
    # 1/4 o'lchamda r10 ≈ to'liq o'lchamda r40, lekin 16 marta tezroq.
    cover = cover.filter(ImageFilter.GaussianBlur(10))
    cover = cover.resize((TARGET_W, TARGET_H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(4))
    cover = ImageEnhance.Color(cover).enhance(0.75)
    return ImageEnhance.Brightness(cover).enhance(0.82)


def _place(canvas: Image.Image, fg: Image.Image, x: int, y: int, shadow: bool) -> Image.Image:
    if shadow:
        _shadow(canvas, fg, x, y)
    if fg.mode == "RGBA":
        canvas.paste(fg.convert("RGB"), (x, y), fg.getchannel("A"))
    else:
        canvas.paste(fg, (x, y))
    return canvas


def fitted_foreground(image: Image.Image):
    fw, fh, x, y = fit_box(image.width, image.height)
    fg, upscaled, upscaler = resize_region(image, (fw, fh))
    return fg, x, y, upscaled, upscaler


def compose_blur(image: Image.Image):
    fg, x, y, upscaled, upscaler = fitted_foreground(image)
    canvas = blurred_background(image)
    full = (fg.width, fg.height) == (TARGET_W, TARGET_H)
    return _place(canvas, fg, x, y, shadow=not full), upscaled, upscaler


def compose_color(image: Image.Image, color):
    rgb = parse_color(color, image)
    fg, x, y, upscaled, upscaler = fitted_foreground(image)
    canvas = Image.new("RGB", (TARGET_W, TARGET_H), rgb)
    return _place(canvas, fg, x, y, shadow=False), upscaled, upscaler


def compose_crop(image: Image.Image, focus_x: float, focus_y: float):
    box = crop_box(image.width, image.height, focus_x, focus_y)
    region = image.crop(box)
    out, upscaled, upscaler = resize_region(region, (TARGET_W, TARGET_H))
    return out, upscaled, upscaler


def fal_expand(fg: Image.Image, x: int, y: int) -> Image.Image:
    """fal-ai/bria/expand: kanvas 1080×1440, asl rasm (fw×fh) [x, y] da."""
    from .layers import _fal_run

    result = _fal_run(
        FAL_EXPAND_ENDPOINT,
        {
            "image_url": _data_url(fg, "JPEG"),
            "canvas_size": [TARGET_W, TARGET_H],
            "original_image_size": [fg.width, fg.height],
            "original_image_location": [x, y],
            "prompt": "",
            "negative_prompt": "text, watermark, logo, extra objects",
        },
        timeout=EXPAND_TIMEOUT,
    )
    out = _fal_image(result)
    if out.size != (TARGET_W, TARGET_H):
        out = out.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    return out


def compose_ai(image: Image.Image):
    """Qaytaradi: (rasm, upscaled, upscaler, outpainted)."""
    from .layers import _is_mock

    if is_target_aspect(image.width, image.height):
        fg, upscaled, upscaler = resize_region(image, (TARGET_W, TARGET_H))
        if fg.mode == "RGBA":
            fg = _flatten(fg, dominant_edge_color(image))
        return fg, upscaled, upscaler, False
    if _is_mock():
        out, upscaled, upscaler = compose_blur(image)
        return out, upscaled, upscaler, False

    fg, x, y, upscaled, upscaler = fitted_foreground(image)
    if fg.mode == "RGBA":
        fg = _flatten(fg, dominant_edge_color(image))
    canvas = fal_expand(fg, x, y)
    # Asl piksellar — aynan o'zi: AI faqat chetlarni chizadi.
    canvas.paste(fg, (x, y))
    return canvas, upscaled, upscaler, True


def render(image: Image.Image, mode: str, *, color=None, focus_x: float = 0.5, focus_y: float = 0.5) -> dict:
    """Asosiy kompozitsiya. ``image`` — ``to_srgb`` dan o'tgan rasm."""
    outpainted = False
    if mode == "crop":
        out, upscaled, upscaler = compose_crop(image, focus_x, focus_y)
    elif mode == "color":
        out, upscaled, upscaler = compose_color(image, color)
    elif mode == "blur":
        out, upscaled, upscaler = compose_blur(image)
    elif mode == "ai":
        out, upscaled, upscaler, outpainted = compose_ai(image)
    else:
        raise GenerationError("Noma'lum rejim", status=400, code="BAD_MODE")
    if out.size != (TARGET_W, TARGET_H):  # himoya: hech qachon boshqa o'lcham chiqmasin
        out = out.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    return {"image": out, "upscaled": upscaled, "upscaler": upscaler, "outpainted": outpainted}


def encode(image: Image.Image, as_png: bool) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    if as_png:
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), "png"
    image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), "jpg"


# --------------------------------------------------------------------------- provayder

class ResizeProvider:
    """``tasks.run_job`` uchun: ``generate(job, index)`` → natija yozuvi."""

    name = "resize"

    def generate(self, job, index: int, previous=None) -> dict:
        settings = job.settings or {}
        mode = settings.get("mode")
        path = settings.get("sourcePath") or ""
        if not path:
            raise GenerationError("Asl rasm topilmadi", status=400, code="BAD_IMAGE")
        image = load_source(path)
        transparent = image.mode == "RGBA"
        rendered = render(
            image, mode,
            color=settings.get("color"),
            focus_x=float(settings.get("focusX", 0.5)),
            focus_y=float(settings.get("focusY", 0.5)),
        )
        data, ext = encode(rendered["image"], as_png=transparent)
        result = _store(data, job.user_id, ext)
        result.update({
            "width": TARGET_W,
            "height": TARGET_H,
            "title": "1080×1440",
            "mode": mode,
            "upscaled": bool(rendered["upscaled"]),
            "upscaler": rendered["upscaler"],
            "outpainted": bool(rendered["outpainted"]),
            "originalWidth": int(settings.get("sourceWidth") or image.width),
            "originalHeight": int(settings.get("sourceHeight") or image.height),
        })
        return result
