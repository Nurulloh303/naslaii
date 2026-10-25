"""Magic Layers v3 — rasmdagi matnni joyida tahrirlash (Canva / ChatGPT image edit kabi).

Asosiy qoida: asl rasm (mahsulot!) O'ZGARMAYDI. Generativ inpaint ishlatilmaydi.

Quvur (pipeline):
1. ``baseUrl`` — asl rasm, tegilmaydi.
2. OpenAI vision matn bloklarini topadi (aniq matn, uslub, taxminiy quti);
   fal ``florence-2-large/ocr-with-region`` qator qutilari bilan aniqlashtiriladi;
   har bir quti lokal harf maskasi bo'yicha toraytiriladi.
3. Lokal OpenCV harflarni o'chiradi (``text_eraser``): tekis fonda median rang,
   aks holda ``cv2.inpaint`` (TELEA). Natija — ``cleanUrl`` (asl o'lchamdagi PNG,
   maskadan tashqarisi asl rasm bilan bir xil).
4. ``separateObjects: true`` bo'lsagina toza rasm fal-ai/qwen-image-layered bilan
   rasm qatlamlariga bo'linadi (aks holda ``layers: []``).

OCR/OpenAI/eraser xatosi hech qachon 500 bermaydi: matn bo'lmasa
``textLayers: []``, ``cleanUrl: null``, ``textRemoved: false``.
"""

from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from . import text_eraser as te
from . import text_layers as tl
from .providers import GenerationError

logger = logging.getLogger(__name__)

LAYERS_VERSION = 3

FAL_ENDPOINT = "fal-ai/qwen-image-layered"
FAL_OCR_ENDPOINT = os.environ.get("FAL_OCR_ENDPOINT", "").strip() or "fal-ai/florence-2-large/ocr-with-region"
FAL_QUEUE_BASE = "https://queue.fal.run"
FAL_QUEUE_URL = f"{FAL_QUEUE_BASE}/{FAL_ENDPOINT}"
FAL_REQUEST_TIMEOUT = 120  # sekund — qwen uchun
FAL_OCR_TIMEOUT = max(10, int(os.environ.get("MAGIC_LAYERS_OCR_TIMEOUT_MS", 40_000)) // 1000)
FAL_POLL_INTERVAL = 1.5    # sekund

# Butun quvur uchun umumiy vaqt. gunicorn/nginx --timeout 300 dan kam bo'lishi shart.
PIPELINE_BUDGET = max(60, int(os.environ.get("MAGIC_LAYERS_BUDGET_SECONDS", 250)))

LAYER_NAMES = ["Orqa fon", "Asosiy mahsulot", "Bezaklar va belgilar", "Qo'shimcha elementlar"]
LAYER_NAMES_WITH_TEXT = ["Orqa fon", "Asosiy mahsulot", "Matnlar va belgilar", "Qo'shimcha elementlar"]


def _fal_key() -> str:
    return os.environ.get("FAL_KEY", "").strip()


def _get_fal_key() -> str:
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        raise GenerationError(
            "Fal.ai kaliti serverda sozlanmagan. .env fayliga FAL_KEY kiriting.",
            status=503,
            code="FAL_NOT_CONFIGURED",
        )
    return key


def _is_mock() -> bool:
    return os.environ.get("FAL_MOCK", "").lower() in ("1", "true", "yes")


def _prepare_image_source(image_source: str) -> str:
    """Rasmni fal.ai qabul qiladigan formatga (ochiq URL yoki data:base64) keltiradi."""
    if not image_source:
        raise GenerationError("Rasm manzili ko'rsatilmadi", status=400)

    if image_source.startswith("data:image/"):
        return image_source

    # Agar lokal media bo'lsa (URL ichida /media/ bo'lsa), to'g'ridan-to'g'ri diskdan o'qiymiz
    if "/media/" in image_source:
        relative_path = image_source.split("/media/", 1)[1].lstrip("/")
        if default_storage.exists(relative_path):
            with default_storage.open(relative_path, "rb") as f:
                raw_bytes = f.read()
            mime = "image/png" if relative_path.endswith(".png") else "image/jpeg"
            b64 = base64.b64encode(raw_bytes).decode("ascii")
            return f"data:{mime};base64,{b64}"

    return image_source


def _bytes_to_data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _download(url: str, timeout: float = 30) -> bytes:
    """URL yoki data URI dan baytlarni oladi."""
    if url.startswith("data:"):
        _, _, payload = url.partition(",")
        return base64.b64decode(payload)
    res = requests.get(url, timeout=timeout)
    if not res.ok:
        raise GenerationError(f"Rasmni yuklab bo'lmadi: {res.status_code}", status=502)
    return res.content


def _load_image_bytes(image_source: str) -> bytes:
    try:
        return _download(_prepare_image_source(image_source))
    except requests.RequestException as error:
        raise GenerationError(f"Slayd rasmini o'qib bo'lmadi: {error}", status=502)


# ---------------------------------------------------------------------------
#  fal.ai navbati (queue submit + poll)
# ---------------------------------------------------------------------------

def _fal_run(endpoint: str, payload: dict, timeout: float = FAL_REQUEST_TIMEOUT) -> dict:
    """fal.ai navbatiga vazifa yuborib, natija JSON'ini kutib oladi."""
    fal_key = _get_fal_key()
    queue_url = f"{FAL_QUEUE_BASE}/{endpoint}"
    headers = {"Authorization": f"Key {fal_key}", "Content-Type": "application/json"}

    # 1. Navbatga so'rov yuborish
    try:
        response = requests.post(queue_url, json=payload, headers=headers, timeout=30)
    except requests.RequestException as error:
        logger.exception("Fal.ai bilan ulanishda xatolik (%s): %s", endpoint, error)
        raise GenerationError("Fal.ai serveriga ulanib bo'lmadi", status=502)

    if not response.ok:
        try:
            err_data = response.json()
        except ValueError:
            err_data = {}
        if isinstance(err_data, dict):
            detail = err_data.get("detail") or err_data.get("message") or response.text
        else:
            detail = response.text
        logger.warning("Fal.ai xatolik (%s): status=%s detail=%s", endpoint, response.status_code, str(detail)[:300])

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
    # status_url IN_QUEUE/IN_PROGRESS/COMPLETED qaytaradi; response_url esa faqat
    # COMPLETED bo'lgach natijani beradi (bajarilayotganda 400 qaytaradi).
    status_url = queue_data.get("status_url") or f"{queue_url}/requests/{request_id}/status"
    response_url = queue_data.get("response_url") or f"{queue_url}/requests/{request_id}"
    start_time = time.time()

    while time.time() - start_time < timeout:
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
            try:
                res_resp = requests.get(response_url, headers=headers, timeout=30)
            except requests.RequestException as e:
                logger.warning("Fal.ai response_url ulanish xatosi: %s", e)
                continue
            if res_resp.ok:
                return res_resp.json()
            logger.warning("Fal.ai response_url xatosi: %s %s", res_resp.status_code, res_resp.text[:200])
            if res_resp.status_code == 422 or res_resp.status_code >= 500:
                # Model ichki xato bilan tugagan — qayta so'rash foyda bermaydi.
                raise GenerationError(f"Fal.ai natija bermadi: {res_resp.text[:200]}", status=502)
        elif status in ("FAILED", "ERROR"):
            err = poll_data.get("error") or "Noma'lum xatolik"
            raise GenerationError(f"Fal.ai vazifasi bajarilmadi: {err}", status=502)

    raise GenerationError("Qatlamlarga ajratish vaqti tugadi (timeout)", status=504)


def _result_images(result_data: dict) -> list[dict]:
    return result_data.get("images") or (result_data.get("output") or {}).get("images") or []


# ---------------------------------------------------------------------------
#  Bosqichlar
# ---------------------------------------------------------------------------

def _split_layers(image_url: str, num_layers: int, timeout: float) -> list[dict]:
    try:
        count = int(num_layers)
    except (TypeError, ValueError):
        count = 4
    result_data = _fal_run(
        FAL_ENDPOINT,
        {
            "image_url": image_url,
            "num_layers": max(2, min(count, 8)),
            "output_format": "png",
        },
        timeout=timeout,
    )
    raw_images = _result_images(result_data)
    if not raw_images:
        raise GenerationError("AI hech qanday qatlam qaytarmadi", status=502)
    return raw_images


def _store_layers(raw_images: list[dict], user_id: int, size: tuple[int, int], text_removed: bool) -> list[dict]:
    from .serializers import _absolute

    names = LAYER_NAMES if text_removed else LAYER_NAMES_WITH_TEXT
    width, height = size
    stored_layers = []

    for idx, layer_info in enumerate(raw_images):
        layer_url = layer_info.get("url")
        if not layer_url:
            continue
        try:
            layer_bytes = _download(layer_url)
        except (requests.RequestException, GenerationError):
            logger.warning("Qatlam rasmini yuklab bo'lmadi: %s", str(layer_url)[:120])
            continue

        filename = f"generated/user_{user_id}/layers/{uuid.uuid4().hex}.png"
        saved_path = default_storage.save(filename, ContentFile(layer_bytes))
        abs_url = _absolute(default_storage.url(saved_path))

        stored_layers.append({
            "id": f"layer_{idx + 1}_{uuid.uuid4().hex[:6]}",
            "order": idx + 1,
            "name": names[idx] if idx < len(names) else f"Qatlam {idx + 1}",
            "url": abs_url,
            "width": layer_info.get("width") or width,
            "height": layer_info.get("height") or height,
        })

    if not stored_layers:
        raise GenerationError("Qatlamlarni serverga saqlab bo'lmadi", status=502)
    return stored_layers


def _store_clean(png_bytes: bytes, user_id: int) -> str:
    """Toza (matnsiz) rasmni media'ga saqlaydi. Nisbiy URL (/media/...) qaytaradi."""
    filename = f"generated/user_{user_id}/layers/clean_{uuid.uuid4().hex}.png"
    saved_path = default_storage.save(filename, ContentFile(png_bytes))
    return default_storage.url(saved_path)


def _detect_and_ocr(image_bytes: bytes, text_timeout: float) -> tuple[list[dict], list[dict]]:
    """GPT matn aniqlash va Florence OCR — parallel. Ikkalasi ham xatoda bo'sh ro'yxat."""
    use_ocr = bool(_fal_key())
    with ThreadPoolExecutor(max_workers=2) as pool:
        gpt_future = pool.submit(tl.detect_text_blocks, image_bytes, text_timeout)
        ocr_future = (
            pool.submit(tl.run_ocr, image_bytes, _fal_run, FAL_OCR_ENDPOINT, min(FAL_OCR_TIMEOUT, text_timeout))
            if use_ocr else None
        )
        try:
            blocks = gpt_future.result()
        except Exception as error:
            logger.warning("Magic layers: matn aniqlash xatosi: %s", error)
            blocks = []
        try:
            ocr_lines = ocr_future.result() if ocr_future is not None else []
        except Exception as error:
            logger.warning("Magic layers: OCR xatosi: %s", error)
            ocr_lines = []
    return blocks, ocr_lines


def _mock_decompose(image_source: str, user_id: int, separate_objects: bool = False) -> dict:
    """Lokal testlar va balans yo'qligida frontendni sinash uchun soxta natija."""
    logger.info("Mock rejimida qatlamlarga ajratilmoqda: user=%s", user_id)
    width, height = 1080, 1440
    if image_source.startswith("data:image/") or "/media/" in image_source:
        try:
            width, height = tl.image_size(_load_image_bytes(image_source))
        except Exception:
            pass
    layers = []
    if separate_objects:
        layers = [{
            "id": f"layer_{uuid.uuid4().hex[:8]}",
            "order": 1,
            "name": "Asosiy qatlam (MOCK)",
            "url": image_source,
            "width": width,
            "height": height,
        }]
    return {
        "baseUrl": image_source,
        "cleanUrl": image_source,
        "width": width,
        "height": height,
        "textLayers": tl.mock_text_layers(width, height),
        "layers": layers,
        "textRemoved": True,
        "layersVersion": LAYERS_VERSION,
    }


def decompose_layers(image_source: str, user_id: int, num_layers: int = 4,
                     separate_objects: bool = False) -> dict:
    """Rasm matnini tahrirlanadigan qatlamlarga aylantiradi (v3).

    Qaytaradi: {"baseUrl", "cleanUrl", "width", "height", "textLayers", "layers",
    "textRemoved", "layersVersion": 3}. ``cleanUrl`` — nisbiy media URL yoki None.
    """
    if _is_mock():
        return _mock_decompose(image_source, user_id, separate_objects)

    if separate_objects:
        _get_fal_key()  # kalit yo'q bo'lsa — darhol 503, OpenAI'ga pul sarflamasdan

    started = time.time()
    deadline = started + PIPELINE_BUDGET

    def remaining(reserve: float = 0) -> float:
        return max(5.0, deadline - time.time() - reserve)

    image_bytes = _load_image_bytes(image_source)
    try:
        rgb, _alpha = te.decode_rgb(image_bytes)
    except Exception:
        raise GenerationError("Slayd rasmini o'qib bo'lmadi", status=400)
    height, width = rgb.shape[:2]

    # 1. Matn bloklari + OCR qutilari (parallel; xatolikda bo'sh ro'yxat)
    t0 = time.time()
    reserve = 140 if separate_objects else 20
    blocks, ocr_lines = _detect_and_ocr(image_bytes, min(tl.TEXT_DETECT_TIMEOUT, remaining(reserve)))
    logger.info("Magic layers: aniqlash %.1fs, bloklar=%d, ocr=%d", time.time() - t0, len(blocks), len(ocr_lines))

    text_layers: list[dict] = []
    if blocks:
        try:
            text_layers = tl.refine_layers(rgb, blocks, ocr_lines)
        except Exception as error:
            logger.warning("Magic layers: qutilarni aniqlashtirib bo'lmadi, GPT qutilari: %s", error)
            text_layers = blocks

    # 2. Matnni lokal o'chirish (asl rasm tegilmaydi; toza nusxa alohida fayl)
    clean_url = None
    clean_png = None
    if text_layers:
        t0 = time.time()
        try:
            clean_png = te.erase_text(image_bytes, text_layers)
            clean_url = _store_clean(clean_png, user_id)
        except Exception as error:
            logger.warning("Magic layers: matnni o'chirib bo'lmadi: %s", error)
            clean_png, clean_url = None, None
        logger.info("Magic layers: o'chirish %.1fs, ok=%s", time.time() - t0, bool(clean_url))

    # 3. Ixtiyoriy: obyektlarni ajratish (qwen) — toza rasmdan
    stored_layers: list[dict] = []
    if separate_objects:
        split_source = _bytes_to_data_url(clean_png) if clean_png else _prepare_image_source(image_source)
        t0 = time.time()
        raw_images = _split_layers(split_source, num_layers, timeout=min(FAL_REQUEST_TIMEOUT, remaining(15)))
        logger.info("Magic layers: qwen %.1fs, qatlamlar=%d", time.time() - t0, len(raw_images))
        stored_layers = _store_layers(raw_images, user_id, (width, height), bool(clean_png))

    logger.info("Magic layers: jami %.1fs user=%s", time.time() - started, user_id)
    return {
        "baseUrl": image_source,
        "cleanUrl": clean_url,
        "width": int(width),
        "height": int(height),
        "textLayers": text_layers,
        "layers": stored_layers,
        "textRemoved": bool(clean_url),
        "layersVersion": LAYERS_VERSION,
    }
