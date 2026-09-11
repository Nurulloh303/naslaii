"""Kie.ai orqali GPT Image 2.

OpenAI'dan farqi: rasm to'g'ridan-to'g'ri qaytmaydi. So'rov yuboriladi →
taskId qaytadi → natija tayyor bo'lguncha davriy so'raladi (polling) →
tayyor bo'lgach natija vaqtinchalik URL sifatida keladi, uni yuklab olamiz.

Ikkinchi farq: kirish rasmlari fayl sifatida emas, ochiq URL sifatida
yuboriladi. Bizda esa rasm brauzerdan data:base64 shaklida keladi —
shuning uchun avval o'z serverimizga (media/) saqlab, ochiq havolasini
Kie'ga beramiz.

https://docs.kie.ai/market/gpt/gpt-image-2-image-to-image
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import requests
from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from .prompts import build_prompt, compact_prompt
from .providers import GenerationError, _decode_data_url, _store
from .translate import translate_brief

logger = logging.getLogger(__name__)

KIE_API_BASE = "https://api.kie.ai"
KIE_CREATE_TASK_URL = f"{KIE_API_BASE}/api/v1/jobs/createTask"
KIE_TASK_INFO_URL = f"{KIE_API_BASE}/api/v1/jobs/recordInfo"

KIE_IMAGE_RESOLUTION = os.environ.get("KIE_IMAGE_RESOLUTION", "2K")
# Karta 1080x1440 — aynan 3:4. Kie shu nisbatlardan birini kutadi.
KIE_ASPECT_RATIO = os.environ.get("KIE_ASPECT_RATIO", "3:4")

KIE_TASK_TIMEOUT = max(30, int(os.environ.get("KIE_TASK_TIMEOUT_MS", 240_000)) // 1000)
KIE_POLL_INTERVAL = max(1, int(os.environ.get("KIE_POLL_INTERVAL_MS", 3_000)) // 1000)
KIE_REQUEST_TIMEOUT = max(10, int(os.environ.get("KIE_REQUEST_TIMEOUT_MS", 30_000)) // 1000)

# Rasmlarni Kie o'qishi uchun ochiq manzil kerak — bizning /media/ shu yerda.
PUBLIC_MEDIA_BASE = os.environ.get(
    "NASLAI_PUBLIC_MEDIA_BASE", f"https://api.{django_settings.SITE_DOMAIN}"
).rstrip("/")


def _public_url(path: str) -> str:
    return f"{PUBLIC_MEDIA_BASE}{path}" if path.startswith("/") else f"{PUBLIC_MEDIA_BASE}/{path}"


def _upload_input_image(data_url: str, user_id: int, label: str) -> str:
    """Data: URL'ni media'ga saqlaydi va Kie o'qiy oladigan ochiq havolani qaytaradi."""
    raw_bytes, _mime, extension = _decode_data_url(data_url)
    name = f"uploads/user_{user_id}/{label}-{uuid.uuid4().hex}.{extension}"
    path = default_storage.save(name, ContentFile(raw_bytes))
    return _public_url(default_storage.url(path))


def _create_task(prompt: str, input_urls: list[str], api_key: str) -> str:
    response = requests.post(
        KIE_CREATE_TASK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "gpt-image-2-image-to-image",
            "input": {
                "prompt": prompt,
                "input_urls": input_urls,
                "aspect_ratio": KIE_ASPECT_RATIO,
                "resolution": KIE_IMAGE_RESOLUTION,
            },
        },
        timeout=KIE_REQUEST_TIMEOUT,
    )
    body = response.json() if response.content else {}
    if not response.ok or body.get("code") != 200:
        message = body.get("msg") or "Kie so‘rovni qabul qilmadi"
        raise GenerationError(message, status=response.status_code, code="KIE_CREATE_FAILED")
    task_id = (body.get("data") or {}).get("taskId")
    if not task_id:
        raise GenerationError("Kie taskId qaytarmadi", status=502, code="KIE_NO_TASK_ID")
    return task_id


def _wait_for_result(task_id: str, api_key: str) -> str:
    """Natija tayyor bo'lguncha kutadi, tayyor bo'lgach rasm URL'ini qaytaradi."""
    import json

    deadline = time.monotonic() + KIE_TASK_TIMEOUT
    headers = {"Authorization": f"Bearer {api_key}"}

    while time.monotonic() < deadline:
        response = requests.get(
            KIE_TASK_INFO_URL, params={"taskId": task_id}, headers=headers, timeout=KIE_REQUEST_TIMEOUT,
        )
        body = response.json() if response.content else {}
        data = body.get("data") or {}
        state = data.get("state")

        if state == "success":
            try:
                result = json.loads(data.get("resultJson") or "{}")
                url = (result.get("resultUrls") or [None])[0]
            except (ValueError, TypeError):
                url = None
            if not url:
                raise GenerationError("Kie natija havolasini qaytarmadi", status=502, code="KIE_EMPTY_RESULT")
            return url

        if state == "fail":
            message = data.get("failMsg") or "Kie generatsiyani bajara olmadi"
            raise GenerationError(message, status=502, code=data.get("failCode") or "KIE_TASK_FAILED")

        time.sleep(KIE_POLL_INTERVAL)

    raise GenerationError("Kie javob berishga ulgurmadi", status=504, code="KIE_TIMEOUT")


class KieGenerationProvider:
    """Gpt Image 2 — Kie.ai orqali. OpenAI'ga muqobil, xuddi shu interfeys bilan."""

    name = "kie"

    def __init__(self):
        self.api_key = os.environ.get("KIE_API_KEY", "")
        if not self.api_key:
            raise GenerationError("Kie kaliti serverda sozlanmagan", status=503, code="KIE_NOT_CONFIGURED")

    def generate(self, job, index: int) -> dict:
        settings = job.settings or {}
        brief = {"title": job.title, "subtitle": job.subtitle, "category": job.category, "benefits": job.benefits}
        # Tarjima alohida — matnli chaqiruv, rasm provayderidan qat'i nazar
        # xuddi shu OpenAI kalit bilan ishlaydi (analyze.py'dagi kabi).
        brief = translate_brief(brief, settings.get("language"), os.environ.get("OPENAI_API_KEY"))

        product_url = _upload_input_image(settings.get("assetDataUrl", ""), job.user_id, "product")

        reference_data_url = settings.get("designReferenceDataUrl") or ""
        has_reference = bool(reference_data_url) and settings.get("contentType") in ("card", "copyStyle")
        input_urls = [product_url]
        if has_reference:
            reference_url = _upload_input_image(reference_data_url, job.user_id, "reference")
            input_urls = [reference_url, product_url]

        attempts = [
            ("primary", build_prompt(brief, settings, index, has_reference)),
            ("compact", compact_prompt(brief, settings, index, has_reference)),
        ]

        last_error: GenerationError | None = None
        for tier, prompt in attempts:
            try:
                task_id = _create_task(prompt, input_urls, self.api_key)
                result_url = _wait_for_result(task_id, self.api_key)
                image_response = requests.get(result_url, timeout=KIE_REQUEST_TIMEOUT)
                image_response.raise_for_status()
                from .imaging import normalize_generated_image

                image_bytes, extension = normalize_generated_image(image_response.content, job.content_type)
                result = _store(image_bytes, job.user_id, extension)
                result["promptTier"] = tier
                result["referenceUsed"] = has_reference
                return result
            except (GenerationError, requests.RequestException) as error:
                last_error = error if isinstance(error, GenerationError) else GenerationError(str(error), status=502)
                logger.warning("Kie tier %s bajarilmadi (%s), keyingisiga o‘tamiz", tier, last_error.code)

        raise last_error or GenerationError("Generatsiya bajarilmadi", status=502)
