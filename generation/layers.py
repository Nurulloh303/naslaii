"""Magic Layers — rasm qatlamlarini ajratish (fal-ai/qwen-image-layered).

Canva Magic Layers analogi: butun infografikani qabul qilib,
uni alohida shaffof (transparent PNG) qatlamlarga bo'lib beradi.
Natija lokal media'ga saqlanadi, shunda vaqtinchalik havolalar
o'chib ketmaydi.
"""

from __future__ import annotations

import base64
import logging
import os
import time
import uuid

import requests
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from .providers import GenerationError

logger = logging.getLogger(__name__)

FAL_ENDPOINT = "fal-ai/qwen-image-layered"
FAL_QUEUE_URL = f"https://queue.fal.run/{FAL_ENDPOINT}"
FAL_REQUEST_TIMEOUT = 120  # sekund
FAL_POLL_INTERVAL = 3     # sekund


def _get_fal_key() -> str:
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        raise GenerationError(
            "Fal.ai kaliti serverda sozlanmagan. .env fayliga FAL_KEY kiriting.",
            status=503,
            code="FAL_NOT_CONFIGURED",
        )
    return key


def _prepare_image_source(image_source: str) -> str:
    """Rasmni fal.ai qabul qiladigan formatga (ochiq URL yoki data:base64) keltiradi."""
    if not image_source:
        raise GenerationError("Rasm manzili ko'rsatilmadi", status=400)

    # Agar allaqachon to'liq internet havolasi yoki data URL bo'lsa
    if image_source.startswith(("http://", "https://", "data:image/")):
        return image_source

    # Agar lokal media yo'li bo'lsa (masalan: /media/generated/...)
    media_url = getattr(default_storage, "base_url", "/media/")
    relative_path = image_source
    if relative_path.startswith(media_url):
        relative_path = relative_path[len(media_url):].lstrip("/")

    if default_storage.exists(relative_path):
        with default_storage.open(relative_path, "rb") as f:
            raw_bytes = f.read()
        mime = "image/png" if relative_path.endswith(".png") else "image/jpeg"
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"

    return image_source


def _mock_decompose(image_source: str, user_id: int) -> list[dict]:
    """Lokal testlar va balans yo'qligida frontendni sinash uchun soxta qatlamlar."""
    logger.info("Mock rejimida qatlamlarga ajratilmoqda: user=%s", user_id)
    # Asl rasm bitta qatlam bo'lib beriladi
    return [
        {
            "id": f"layer_{uuid.uuid4().hex[:8]}",
            "order": 1,
            "name": "Asosiy qatlam (MOCK)",
            "url": image_source,
            "width": 1080,
            "height": 1440,
        }
    ]


def decompose_layers(image_source: str, user_id: int, num_layers: int = 4) -> list[dict]:
    """Rasmni fal.ai orqali shaffof qatlamlarga (transparent PNG) bo'lib beradi."""
    if os.environ.get("FAL_MOCK", "").lower() in ("1", "true", "yes"):
        return _mock_decompose(image_source, user_id)

    fal_key = _get_fal_key()
    prepared_url = _prepare_image_source(image_source)

    headers = {
        "Authorization": f"Key {fal_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "image_url": prepared_url,
        "num_layers": max(2, min(int(num_layers), 8)),
        "output_format": "png",
    }

    # 1. Navbatga so'rov yuborish
    try:
        response = requests.post(FAL_QUEUE_URL, json=payload, headers=headers, timeout=30)
    except requests.RequestException as error:
        logger.exception("Fal.ai bilan ulanishda xatolik: %s", error)
        raise GenerationError("Fal.ai serveriga ulanib bo'lmadi", status=502)

    if not response.ok:
        try:
            err_data = response.json()
        except ValueError:
            err_data = {}

        detail = err_data.get("detail") or err_data.get("message") or response.text
        logger.warning("Fal.ai xatolik: status=%s detail=%s", response.status_code, detail)

        if "Exhausted balance" in str(detail) or response.status_code == 403:
            raise GenerationError(
                "Fal.ai hisobingiz balansi tugagan. Iltimos, fal.ai/dashboard/billing orqali hisobingizni to'ldiring.",
                status=402,
                code="FAL_BALANCE_EXHAUSTED",
            )
        raise GenerationError(f"Fal.ai xatolik qaytardi: {detail}", status=response.status_code)

    queue_data = response.json()
    request_id = queue_data.get("request_id")
    if not request_id:
        raise GenerationError("Fal.ai vazifa identifikatorini qaytarmadi", status=502)

    # 2. Tayyor bo'lguncha kutish (Polling)
    status_url = f"{FAL_QUEUE_URL}/requests/{request_id}"
    start_time = time.time()
    result_data = None

    while time.time() - start_time < FAL_REQUEST_TIMEOUT:
        time.sleep(FAL_POLL_INTERVAL)
        try:
            poll_res = requests.get(status_url, headers=headers, timeout=20)
        except requests.RequestException:
            continue

        if not poll_res.ok:
            continue

        poll_data = poll_res.json()
        status = poll_data.get("status")

        if status == "COMPLETED":
            result_data = poll_data.get("response") or poll_data
            break
        elif status == "FAILED":
            err = poll_data.get("error") or "Noma'lum xatolik"
            raise GenerationError(f"Qatlamlarga ajratish bajarilmadi: {err}", status=502)

    if not result_data:
        raise GenerationError("Qatlamlarga ajratish vaqti tugadi (timeout)", status=504)

    # 3. Qatlamlarni yuklab olib, o'zimizning serverimizga saqlash
    # fal.ai javobi: {"images": [{"url": "...", "width": ..., "height": ...}, ...]}
    raw_images = result_data.get("images") or result_data.get("output", {}).get("images") or []
    if not raw_images:
        raise GenerationError("AI hech qanday qatlam qaytarmadi", status=502)

    stored_layers = []
    layer_names = ["Orqa fon", "Asosiy mahsulot", "Matnlar va belgilar", "Qo'shimcha elementlar"]

    for idx, layer_info in enumerate(raw_images):
        layer_url = layer_info.get("url")
        if not layer_url:
            continue

        try:
            layer_res = requests.get(layer_url, timeout=30)
            if not layer_res.ok:
                continue
            layer_bytes = layer_res.content
        except requests.RequestException:
            logger.warning("Qatlam rasmini yuklab bo'lmadi: %s", layer_url)
            continue

        filename = f"generated/user_{user_id}/layers/{uuid.uuid4().hex}.png"
        saved_path = default_storage.save(filename, ContentFile(layer_bytes))
        local_url = default_storage.url(saved_path)

        name = layer_names[idx] if idx < len(layer_names) else f"Qatlam {idx + 1}"
        stored_layers.append({
            "id": f"layer_{idx + 1}_{uuid.uuid4().hex[:6]}",
            "order": idx + 1,
            "name": name,
            "url": local_url,
            "width": layer_info.get("width", 1080),
            "height": layer_info.get("height", 1440),
        })

    if not stored_layers:
        raise GenerationError("Qatlamlarni serverga saqlab bo'lmadi", status=502)

    return stored_layers
