"""Uzum eksporti uchun ochiq rasm nusxalari.

Uzum rasmni T ustunidagi HTTPS manzildan o'zi yuklab oladi: login, cookie yoki
HTML yo'q. Shuning uchun faqat eksportga tanlangan rasmlarning ALOHIDA
nusxasi tayyorlanadi va ``/api/files/generated/user_<id>/uzum/<uuid>/<NN>.jpg``
orqali anonim beriladi (128-bit tasodifiy papka nomi; ro'yxat olish yo'q).

Nusxa: aniq 1080×1440, sRGB, JPEG, ≤ 5 MB (sifat bosqichma-bosqich
pasaytiriladi). 3:4 bo'lmasa — ``generation.resize`` dagi "fit" mantiqi
(markazda to'liq sig'dirish, chetidagi rang bilan to'ldirish), faqat lokal:
tashqi AI chaqirilmaydi.
"""

from __future__ import annotations

import io
import os
import re
import uuid

from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, ImageOps

from generation import resize as fit

TARGET_SIZE = (fit.TARGET_W, fit.TARGET_H)
MAX_BYTES = int(os.environ.get("UZUM_PHOTO_MAX_BYTES", 5 * 1000 * 1000))  # konservativ: 5 000 000 < 5 MiB
QUALITY_STEPS = (92, 88, 84, 80, 75, 70, 65, 60, 55, 50)
RESULT_PATH_RE = re.compile(r"^generated/user_(\d+)/(?:layers/)?[A-Za-z0-9_-]{8,128}\.(png|jpg|jpeg|webp)$")
EXPORT_PATH_RE = re.compile(r"^generated/user_\d+/uzum/[0-9a-f]{32}/\d{2}\.jpg$")
RASTER_FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}


class PhotoError(ValueError):
    """Foydalanuvchiga ko'rsatiladigan (o'zbekcha) xabar bilan."""


def public_base() -> str:
    base = (
        os.environ.get("UZUM_PUBLIC_BASE")
        or os.environ.get("NASLAI_PUBLIC_MEDIA_BASE")
        or f"https://api.{django_settings.SITE_DOMAIN}"
    ).strip().rstrip("/")
    return base


def public_url(path: str) -> str:
    return f"{public_base()}/api/files/{path}"


def result_storage_path(job, index: int) -> str:
    """Natija URL'idan storage yo'li. Faqat shu job egasining papkasi."""
    results = job.results or []
    if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(results):
        raise PhotoError("Rasm topilmadi")
    url = str((results[index] or {}).get("url") or "")
    media = django_settings.MEDIA_URL
    if url.startswith(("http://", "https://")):
        marker = url.find(media)
        url = url[marker:] if marker >= 0 else ""
    relative = url.replace(media, "", 1).lstrip("/") if url.startswith(media) else ""
    match = RESULT_PATH_RE.match(relative)
    if not match or int(match.group(1)) != job.user_id or ".." in relative:
        raise PhotoError("Bu natija rasm fayli emas")
    return relative


def load_result_image(job, index: int) -> Image.Image:
    path = result_storage_path(job, index)
    try:
        with default_storage.open(path, "rb") as handle:
            raw = handle.read(40 * 1024 * 1024 + 1)
    except (OSError, ValueError):
        raise PhotoError("Rasm fayli serverda topilmadi")
    return _open(raw)


def load_data_url(data_url: str) -> Image.Image:
    try:
        _raw, image = fit.decode_upload(data_url)
    except fit.BadImage as error:
        raise PhotoError(str(error))
    return image


def _open(raw: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(raw))
        fmt = (image.format or "").upper()
        image.load()
    except Image.DecompressionBombError:
        raise PhotoError("Rasm o'lchami juda katta")
    except Exception:  # noqa: BLE001
        raise PhotoError("Rasmni o'qib bo'lmadi")
    if fmt not in RASTER_FORMATS:
        raise PhotoError("Rasm formati qo'llab-quvvatlanmaydi")
    return image


def to_export_image(image: Image.Image) -> Image.Image:
    """Har qanday rasm → aniq 1080×1440 RGB (sRGB)."""
    image = fit.to_srgb(ImageOps.exif_transpose(image))
    if image.mode == "RGBA":
        image = fit._flatten(image, (255, 255, 255))
    else:
        image = image.convert("RGB")
    if image.size == TARGET_SIZE:
        return image
    if fit.is_target_aspect(image.width, image.height):
        return image.resize(TARGET_SIZE, Image.LANCZOS)
    fw, fh, x, y = fit.fit_box(image.width, image.height)
    if fw >= image.width or fh >= image.height:
        fg = fit.lanczos_upscale(image, (fw, fh))
    else:
        fg = image.resize((fw, fh), Image.LANCZOS)
    canvas = Image.new("RGB", TARGET_SIZE, fit.dominant_edge_color(image))
    canvas.paste(fg, (x, y))
    return canvas


def encode_jpeg(image: Image.Image) -> bytes:
    for quality in QUALITY_STEPS:
        buffer = io.BytesIO()
        # optimize=True katta/shovqinli rasmda Pillow bufer xatosini beradi ("broken data stream") —
        # baseline JPEG (eng mos format) va optimize'siz.
        image.save(buffer, format="JPEG", quality=quality, optimize=False, progressive=False, subsampling=0 if quality >= 90 else 2)
        data = buffer.getvalue()
        if len(data) <= MAX_BYTES:
            return data
    raise PhotoError("Rasmni 5 MB ga sig'dirib bo'lmadi")


def store_export_photos(user_id: int, images: list[Image.Image]) -> list[str]:
    """Rasmlarni ``generated/user_<id>/uzum/<uuid>/01.jpg ...`` ga yozadi, yo'llarni qaytaradi."""
    folder = f"generated/user_{user_id}/uzum/{uuid.uuid4().hex}"
    paths = []
    for number, image in enumerate(images, start=1):
        data = encode_jpeg(to_export_image(image))
        name = f"{folder}/{number:02d}.jpg"
        saved = default_storage.save(name, ContentFile(data))
        if saved != name or not EXPORT_PATH_RE.match(saved):
            raise PhotoError("Rasmni saqlab bo'lmadi")
        paths.append(saved)
    return paths
