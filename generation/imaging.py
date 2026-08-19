"""Приведение готовой картинки к формату карточки.

Здесь исправлена ошибка, которая долго портила качество: модель отдавала
JPEG, мы его распаковывали, обрезали и сжимали ЗАНОВО. Двойное сжатие
оставляет грязные ореолы вокруг букв и иконок — на карточках это было
хорошо видно при увеличении.

Теперь: модель отдаёт PNG без потерь → обрезаем → кодируем ОДИН раз.

Формат отдачи зависит от содержимого. Замер на карточке 1080×1440
(плоский фон, белый текст): PNG — 38 КБ и ноль отличий от оригинала,
JPEG 96 — 479 КБ и ошибка до 21 на краях букв. Эта ошибка и видна как
«пятна» вокруг текста при увеличении: JPEG режет цветность вдвое
(4:2:0), а у инфографики белые буквы на цветном фоне. На фотографии
наоборот — PNG в разы тяжелее, а артефактов не видно.
"""

from __future__ import annotations

import io
import math
import os

from PIL import Image

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1440

# Качество финального JPEG. 96 визуально почти неотличимо от исходника,
# а файл втрое легче PNG.
DELIVERED_JPEG_QUALITY = max(70, min(100, int(os.environ.get("DELIVERED_JPEG_QUALITY", 96))))

# Фотографические режимы остаются в JPEG, инфографика уходит в PNG.
PHOTO_LIKE_CONTENT = {"photo", "fashion", "video"}
# Если PNG вышел неожиданно тяжёлым — JPEG 100 (ошибка до 4, а не до 21).
DELIVERED_PNG_MAX_BYTES = max(1, int(os.environ.get("DELIVERED_PNG_MAX_MB", 8))) * 1024 * 1024


def normalize_generated_image(raw: bytes, content_type: str = "card") -> tuple[bytes, str]:
    """PNG или JPEG от модели → картинка 1080×1440 и её расширение.

    Если пропорции почти совпадают (как у 1088×1440), делаем чистую
    обрезку: пиксели переносятся один в один, ничего не пересчитывается.
    Если размеры расходятся — масштабируем LANCZOS, а не «ближайшим
    пикселем»: последний делает текст зубчатым.
    """
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert("RGB")
        width, height = image.size

        source_aspect = width / height
        target_aspect = TARGET_WIDTH / TARGET_HEIGHT

        if math.isclose(source_aspect, target_aspect, rel_tol=1e-6):
            crop_box = (0, 0, width, height)
        elif source_aspect > target_aspect:
            crop_width = round(height * target_aspect)
            left = (width - crop_width) // 2
            crop_box = (left, 0, left + crop_width, height)
        else:
            crop_height = round(width / target_aspect)
            top = (height - crop_height) // 2
            crop_box = (0, top, width, top + crop_height)

        cropped = image.crop(crop_box)
        if cropped.size != (TARGET_WIDTH, TARGET_HEIGHT):
            cropped = cropped.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.LANCZOS)

        if content_type in PHOTO_LIKE_CONTENT:
            buffer = io.BytesIO()
            cropped.save(buffer, format="JPEG", quality=DELIVERED_JPEG_QUALITY, optimize=True)
            return buffer.getvalue(), "jpg"

        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        if len(data) <= DELIVERED_PNG_MAX_BYTES:
            return data, "png"

        fallback = io.BytesIO()
        cropped.save(fallback, format="JPEG", quality=100, optimize=True)
        return fallback.getvalue(), "jpg"
