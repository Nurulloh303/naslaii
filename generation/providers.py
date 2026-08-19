"""Поставщики генерации.

Два режима:
  * mock   — рисует заглушку без обращения к API и без затрат. Ставьте его
             при первом запуске: так проблемы развёртывания не смешиваются
             с проблемами модели.
  * openai — реальная генерация через /v1/images/edits.

Порт из server/index.mjs (generateOpenAiImage, requestOpenAiImageEdit).
Цепочка откатов не украшение: OpenAI регулярно отклоняет длинные промпты,
и без неё пользователь просто теряет токены.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import time
import uuid

import requests
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, ImageDraw

from .imaging import TARGET_HEIGHT, TARGET_WIDTH, normalize_generated_image
from .prompts import build_prompt, compact_prompt, safe_minimal_prompt

logger = logging.getLogger(__name__)

OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
OPENAI_IMAGE_QUALITY = os.environ.get("OPENAI_IMAGE_QUALITY", "high")
# PNG — источник без потерь. Сжимаем в JPEG один раз, уже у себя.
OPENAI_IMAGE_OUTPUT_FORMAT = os.environ.get("OPENAI_IMAGE_OUTPUT_FORMAT", "png")
OPENAI_IMAGE_TIMEOUT = max(30, int(os.environ.get("OPENAI_IMAGE_TIMEOUT_MS", 180_000)) // 1000)
OPENAI_RETRY_DELAY = max(0, int(os.environ.get("OPENAI_IMAGE_RETRY_DELAY_MS", 15_000)) // 1000)

# gpt-image-2 принимает 1088x1440; у gpt-image-1 другой набор размеров.
RENDER_SIZE = "1088x1440" if re.match(r"^gpt-image-2(-|$)", OPENAI_IMAGE_MODEL) else "1024x1536"
SUPPORTS_INPUT_FIDELITY = bool(re.match(r"^gpt-image-1(-|$)", OPENAI_IMAGE_MODEL))

RETRYABLE_STATUSES = {400, 408, 409, 429, 500, 502, 503, 504}


class GenerationError(Exception):
    """Ошибка, которую уже можно показать пользователю."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _decode_data_url(data_url: str) -> tuple[bytes, str, str]:
    match = re.match(r"^data:(image/(png|jpeg|webp));base64,(.+)$", data_url or "", re.S)
    if not match:
        raise GenerationError("Mahsulot rasmi noto‘g‘ri formatda")
    mime, subtype, payload = match.group(1), match.group(2), match.group(3)
    extension = "jpg" if subtype == "jpeg" else subtype
    return base64.b64decode(payload), mime, extension


def _store(image_bytes: bytes, user_id: int, extension: str = "jpg") -> dict:
    # Расширение приходит из кодировщика: карточка — png, фото — jpg.
    name = f"generated/user_{user_id}/{uuid.uuid4().hex}.{extension}"
    path = default_storage.save(name, ContentFile(image_bytes))
    return {
        "id": uuid.uuid4().hex,
        "url": default_storage.url(path),
        "width": TARGET_WIDTH,
        "height": TARGET_HEIGHT,
    }


class MockGenerationProvider:
    """Заглушка: рисует однотонную карточку с названием товара."""

    name = "mock"

    def generate(self, job, index: int) -> dict:
        image = Image.new("RGB", (TARGET_WIDTH, TARGET_HEIGHT), (18, 20, 24))
        draw = ImageDraw.Draw(image)
        draw.rectangle([64, 64, TARGET_WIDTH - 64, TARGET_HEIGHT - 64], outline=(255, 92, 46), width=6)
        draw.text((96, 120), (job.title or "MAHSULOT").upper()[:24], fill=(245, 240, 232))
        draw.text((96, 200), f"variant {index}", fill=(160, 160, 160))
        draw.text((96, TARGET_HEIGHT - 160), "MOCK — real generatsiya o‘chirilgan", fill=(160, 160, 160))

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92)
        return _store(buffer.getvalue(), job.user_id)


class OpenAiGenerationProvider:
    """Реальная генерация через OpenAI Images API."""

    name = "openai"
    endpoint = "https://api.openai.com/v1/images/edits"

    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise GenerationError("OpenAI kaliti serverda sozlanmagan", status=503, code="OPENAI_NOT_CONFIGURED")

    # --- один запрос -------------------------------------------------------

    def _request(self, prompt: str, files: list[tuple[bytes, str, str]], output_format: str) -> bytes:
        data = {
            "model": OPENAI_IMAGE_MODEL,
            "prompt": prompt,
            "size": RENDER_SIZE,
            "output_format": output_format,
            "quality": OPENAI_IMAGE_QUALITY,
        }
        # output_compression имеет смысл только для jpeg/webp; с png API ругается.
        if output_format != "png":
            data["output_compression"] = "100"
        if SUPPORTS_INPUT_FIDELITY:
            data["input_fidelity"] = "high"

        field = "image[]" if len(files) > 1 else "image"
        multipart = [(field, (filename, content, mime)) for content, mime, filename in files]

        response = requests.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            data=data,
            files=multipart,
            timeout=OPENAI_IMAGE_TIMEOUT,
        )

        if not response.ok:
            payload = {}
            try:
                payload = response.json()
            except ValueError:
                pass
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code") or error.get("type") or "OPENAI_IMAGE_FAILED"
            logger.warning(
                "OpenAI rasm generatsiyasi xato: status=%s code=%s param=%s message=%s",
                response.status_code, code, error.get("param"), (error.get("message") or "")[:200],
            )
            raise GenerationError(error.get("message") or "OpenAI xatolik qaytardi", status=response.status_code, code=code)

        body = response.json()
        b64 = (body.get("data") or [{}])[0].get("b64_json")
        if not b64:
            raise GenerationError("AI rasm qaytarmadi", status=502, code="EMPTY_IMAGE_OUTPUT")
        return base64.b64decode(b64)

    def _request_with_format_fallback(self, prompt: str, files: list) -> bytes:
        """PNG — основной путь. Если модель его не принимает, пробуем jpeg."""
        try:
            return self._request(prompt, files, OPENAI_IMAGE_OUTPUT_FORMAT)
        except GenerationError as error:
            mentions_format = "output_format" in str(error) or "output_compression" in str(error)
            if not mentions_format or OPENAI_IMAGE_OUTPUT_FORMAT == "jpeg":
                raise
            logger.warning("output_format qabul qilinmadi, jpeg bilan qayta urinamiz")
            return self._request(prompt, files, "jpeg")

    # --- цепочка откатов ---------------------------------------------------

    def generate(self, job, index: int) -> dict:
        settings = job.settings or {}
        brief = {"title": job.title, "subtitle": job.subtitle, "category": job.category, "benefits": job.benefits}

        product_bytes, product_mime, product_ext = _decode_data_url(settings.get("assetDataUrl", ""))
        product = (product_bytes, product_mime, f"product.{product_ext}")

        reference_url = settings.get("designReferenceDataUrl") or ""
        has_reference = bool(reference_url) and settings.get("contentType") in ("card", "copyStyle")
        files = [product]
        if has_reference:
            ref_bytes, ref_mime, ref_ext = _decode_data_url(reference_url)
            files = [(ref_bytes, ref_mime, f"design-reference.{ref_ext}"), product]

        attempts = [
            ("primary", build_prompt(brief, settings, index, has_reference), files, has_reference),
            ("compact", compact_prompt(brief, settings, index, has_reference), files, has_reference),
            ("safe-minimal", safe_minimal_prompt(brief, settings, index), [product], False),
        ]

        last_error: GenerationError | None = None
        for tier, prompt, tier_files, reference_used in attempts:
            try:
                raw = self._request_with_format_fallback(prompt, tier_files)
                image_bytes, extension = normalize_generated_image(raw, job.content_type)
                result = _store(image_bytes, job.user_id, extension)
                result["promptTier"] = tier
                result["referenceUsed"] = reference_used
                return result
            except GenerationError as error:
                last_error = error
                if error.status not in RETRYABLE_STATUSES:
                    raise
                logger.warning("Tier %s bajarilmadi (%s), keyingisiga o‘tamiz", tier, error.code)
                time.sleep(OPENAI_RETRY_DELAY)

        raise last_error or GenerationError("Generatsiya bajarilmadi", status=502)


def get_provider():
    """mock по умолчанию: случайно потратить деньги не должно быть легко."""
    name = os.environ.get("GENERATION_PROVIDER", "mock").lower()
    if name == "openai":
        return OpenAiGenerationProvider()
    return MockGenerationProvider()
