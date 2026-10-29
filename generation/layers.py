"""Magic Layers v5 — tekis rasmni Canva "Magic Layers" kabi qatlamlarga ajratish.

Quvur (pipeline):
1. Parallel: OpenAI vision (matn bloklari + obyekt qutilari/nomlari/z), Florence OCR,
   fal BiRefNet v2 (butun rasm — salient maska) va fal SAM2 auto-segment (hamma segmentlar).
2. Matn: lokal OpenCV harflarni o'chiradi → ``textClean``; har bir matn qatlami uchun
   asl harflarning RGBA kesmasi (``imageUrl`` / ``imageBox``); harf maskalari birlashmasi.
3. Mahsulot = BITTA element: BiRefNet komponentlaridan GPT mahsulot qutilariga
   tushganlari (``auto_layers.select_product``). Zaxira — SAM2 box prompt.
4. Qolgan elementlar: auto-segment → filtr/birlashtirish/nomlash
   (``auto_layers.postprocess_segments``); GPT obyekti qoplanmasa — SAM2 box prompt.
   auto-segment ishlamasa — butunlay v4 yo'li (GPT qutisi + SAM2 box prompt).
5. Qoldiq ushlagich: fondan keskin farqli, qatlamga kirmagan joylar → "Qolgan element N".
6. Elementlar to'liq maskalar bilan kesiladi ("tishlash" yo'q). Fon: barcha maskalar +
   harflar (8px) fal bria/eraser bilan to'ldiriladi (faqat maska ichiga), zaxira — OpenCV.
7. ``extract_element`` — foydalanuvchi bosgan nuqtadan (SAM2 point prompt) yangi element.

Barcha fayllar ``generated/user_*/layers/`` ostida, tasodifiy uuid nomlar bilan;
brauzerga ``/api/layer-files/<yo'l>`` orqali (CORS sarlavhalari bilan) beriladi.
Hech qanday bosqich xatosi 500 bermaydi.
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

from . import auto_layers as al
from . import object_layers as ol
from . import text_eraser as te
from . import text_layers as tl
from .providers import GenerationError

logger = logging.getLogger(__name__)

LAYERS_VERSION = 5

FAL_OCR_ENDPOINT = os.environ.get("FAL_OCR_ENDPOINT", "").strip() or "fal-ai/florence-2-large/ocr-with-region"
FAL_QUEUE_BASE = "https://queue.fal.run"
FAL_REQUEST_TIMEOUT = 120  # sekund
FAL_OCR_TIMEOUT = max(10, int(os.environ.get("MAGIC_LAYERS_OCR_TIMEOUT_MS", 40_000)) // 1000)
FAL_POLL_INTERVAL = 1.5    # sekund

# Butun quvur uchun umumiy vaqt. gunicorn/nginx --timeout 300 dan kam bo'lishi shart.
PIPELINE_BUDGET = max(60, int(os.environ.get("MAGIC_LAYERS_BUDGET_SECONDS", 250)))

SAM_TIMEOUT = 75          # sekund — bitta obyekt
BIREFNET_TIMEOUT = 70     # sekund
AUTOSEG_TIMEOUT = 110     # sekund (+ maskalarni yuklab olish)
EXTRACT_SAM_TIMEOUT = 45  # sekund
ERASER_TIMEOUT = 100      # sekund

# Qatlam fayllari shu prefiks ostida saqlanadi va faqat shu yerdan beriladi.
LAYER_FILES_URL = "/api/layer-files/"


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


# ---------------------------------------------------------------------------
#  Saqlash
# ---------------------------------------------------------------------------

def layers_dir(user_id) -> str:
    return f"generated/user_{int(user_id)}/layers/"


def store_layer_file(raw: bytes, user_id, kind: str, ext: str = "png") -> str:
    """Faylni ``generated/user_<id>/layers/<kind>_<uuid>.<ext>`` ga saqlaydi.

    Qaytaradi: ``/api/layer-files/<storage yo'li>`` (nisbiy; javobda _absolute).
    Nom 128-bit tasodifiy uuid — taxmin qilib bo'lmaydi, shuning uchun endpoint
    autentifikatsiyasiz (brauzer <img crossOrigin="anonymous"> cookie yubormaydi).
    """
    name = f"{layers_dir(user_id)}{kind}_{uuid.uuid4().hex}.{ext}"
    saved = default_storage.save(name, ContentFile(raw))
    return LAYER_FILES_URL + saved.replace("\\", "/")


def _image_ext(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return "png"


def _store_base(image_bytes: bytes, user_id) -> str:
    """Asl rasm nusxasi qatlamlar prefiksi ostida (boshqa media papkasida bo'lishi mumkin)."""
    ext = _image_ext(image_bytes)
    if ext == "png" and image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        image_bytes = te.encode_png(*te.decode_rgb(image_bytes))
    return store_layer_file(image_bytes, user_id, "base", ext)


# ---------------------------------------------------------------------------
#  Bosqichlar
# ---------------------------------------------------------------------------

def _detect_and_ocr(image_bytes: bytes, text_timeout: float) -> tuple[list[dict], list[dict], list[dict]]:
    """GPT (matn + obyektlar) va Florence OCR — parallel. Xatoda bo'sh ro'yxatlar."""
    use_ocr = bool(_fal_key())
    with ThreadPoolExecutor(max_workers=2) as pool:
        gpt_future = pool.submit(tl.detect_layout, image_bytes, text_timeout)
        ocr_future = (
            pool.submit(tl.run_ocr, image_bytes, _fal_run, FAL_OCR_ENDPOINT, min(FAL_OCR_TIMEOUT, text_timeout))
            if use_ocr else None
        )
        try:
            blocks, objects = gpt_future.result()
        except Exception as error:
            logger.warning("Magic layers: aniqlash xatosi: %s", error)
            blocks, objects = [], []
        try:
            ocr_lines = ocr_future.result() if ocr_future is not None else []
        except Exception as error:
            logger.warning("Magic layers: OCR xatosi: %s", error)
            ocr_lines = []
    return blocks, objects, ocr_lines


def _attach_text_cutouts(text_layers: list[dict], original_rgb, masks: dict, user_id) -> None:
    """Har bir matn qatlamiga asl harflarning RGBA kesmasi (imageUrl/imageBox)."""
    height, width = original_rgb.shape[:2]
    for layer in text_layers:
        layer["imageUrl"] = None
        layer["imageBox"] = None
        mask = masks.get(layer.get("id"))
        source = layer.get("source")
        if mask is None or not source:
            continue
        try:
            x0, y0, x1, y1 = te.clamp_box(source["x"], source["y"], source["x"] + source["width"],
                                          source["y"] + source["height"], width, height)
            box = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
            png = ol.box_cutout(original_rgb, mask, box)
            layer["imageUrl"] = store_layer_file(png, user_id, "txt")
            layer["imageBox"] = box
        except Exception as error:
            logger.warning("Magic layers: matn kesmasi saqlanmadi (%s): %s", layer.get("id"), error)


def _glyph_union(text_layers: list[dict], masks: dict, shape) -> "np.ndarray":
    """Har bir matn qatlamining harf maskasi (source qutisida) → butun rasm bo'yicha birlashma."""
    import numpy as np

    height, width = shape[:2]
    out = np.zeros((height, width), dtype=bool)
    for layer in text_layers:
        crop = masks.get(layer.get("id"))
        source = layer.get("source")
        if crop is None or not source:
            continue
        x0, y0, x1, y1 = te.clamp_box(source["x"], source["y"], source["x"] + source["width"],
                                      source["y"] + source["height"], width, height)
        h, w = min(crop.shape[0], y1 - y0), min(crop.shape[1], x1 - x0)
        out[y0:y0 + h, x0:x0 + w] |= crop[:h, :w]
    return out


def _build_elements(segs: list, clean_rgb, user_id) -> list[dict]:
    """Segmentlar → saqlangan RGBA elementlar.

    v5: "tishlash" yo'q — har bir element o'zining TO'LIQ maskasi bilan kesiladi; ustma-ust
    joyda oldingisi ustidan chiziladi. order: z (GPT ishorasi), keyin kattaroq pastda (1 = eng pastki).
    """
    ranked = sorted(segs, key=lambda s: (float(s.z), -s.area))
    elements: list[dict] = []
    for seg in ranked:
        if seg.area < 64:
            continue
        try:
            cut = ol.rgba_cutout(clean_rgb, seg.full(clean_rgb.shape))
            if cut is None:
                continue
            png, box = cut
            elements.append({
                "id": f"el_{uuid.uuid4().hex[:10]}",
                "name": seg.name or "Element",
                "kind": seg.kind if seg.kind in ("product", "graphic") else "graphic",
                "url": store_layer_file(png, user_id, "el"),
                **box,
                "order": len(elements) + 1,
            })
        except Exception as error:
            logger.warning("Magic layers: element saqlanmadi (%s): %s", seg.name, error)
    return elements

def _result(base_url, background_url, clean_url, size, text_layers, elements, text_removed, objects_removed):
    width, height = size
    return {
        "baseUrl": base_url,
        "backgroundUrl": background_url,
        "cleanUrl": clean_url,
        "width": int(width),
        "height": int(height),
        "elements": elements,
        "textLayers": text_layers,
        "layers": [],  # v1/v2 maydoni — eski frontend uchun bo'sh
        "textRemoved": bool(text_removed),
        "objectsRemoved": bool(objects_removed),
        "layersVersion": LAYERS_VERSION,
    }


def _mock_decompose(image_source: str, user_id: int) -> dict:
    """Lokal testlar va balans yo'qligida frontendni sinash uchun soxta natija.

    Fon = asl rasm, 3 ta element (markaz kesmasi — mahsulot, 2 ta grafik), 2 ta matn qatlami.
    """
    logger.info("Mock rejimida qatlamlarga ajratilmoqda: user=%s", user_id)
    width, height = 1080, 1440
    try:
        image_bytes = _load_image_bytes(image_source) if (
            image_source.startswith("data:image/") or "/media/" in image_source
            or image_source.startswith(LAYER_FILES_URL)) else None
        rgb = te.decode_rgb(image_bytes)[0] if image_bytes else None
    except Exception:
        image_bytes, rgb = None, None
    if rgb is None:
        return _result(image_source, image_source, None, (width, height),
                       [dict(layer, imageUrl=None, imageBox=None) for layer in tl.mock_text_layers(width, height)],
                       [], True, False)

    import numpy as np

    height, width = rgb.shape[:2]
    base_url = _store_base(image_bytes, user_id)
    elements = []
    boxes = [
        ("Mahsulot (MOCK)", "product", (width // 4, height // 4, width * 3 // 4, height * 3 // 4)),
        ("Element 1", "graphic", (width // 20, height * 4 // 5, width // 20 + width // 6, height * 4 // 5 + height // 10)),
        ("Element 2", "graphic", (width * 3 // 4, height * 4 // 5, width * 3 // 4 + width // 6, height * 4 // 5 + height // 10)),
    ]
    for name, kind, (x0, y0, x1, y1) in boxes:
        mask = np.zeros((height, width), dtype=bool)
        mask[y0:y1, x0:x1] = True
        png, box = ol.rgba_cutout(rgb, mask, pad=0)
        elements.append({"id": f"el_{uuid.uuid4().hex[:10]}", "name": name, "kind": kind,
                         "url": store_layer_file(png, user_id, "el"), **box, "order": 0})
    # Grafikalar pastda, mahsulot ustida.
    elements = elements[1:] + elements[:1]
    for order, element in enumerate(elements, start=1):
        element["order"] = order
    text_layers = tl.mock_text_layers(width, height)
    for layer in text_layers:
        src = layer["source"]
        crop_mask = np.ones((src["height"], src["width"]), dtype=bool)
        layer["imageUrl"] = store_layer_file(ol.box_cutout(rgb, crop_mask, src), user_id, "txt")
        layer["imageBox"] = dict(src)
    return _result(base_url, base_url, base_url, (width, height), text_layers, elements, True, False)


def _segs_from_pairs(pairs: list, kind: str = "graphic") -> list:
    """(GPT obyekti, maska) juftlari (SAM2 box prompt zaxirasi) → Seg."""
    segs = []
    for obj, mask in pairs:
        seg = al.Seg.from_full(mask, name=obj.get("name") or "Element", kind=kind,
                               z=float(obj.get("z", 5.0 if kind == "product" else 3.0)))
        if seg is not None:
            segs.append(seg)
    return segs


def decompose_layers(image_source: str, user_id: int, num_layers: int = 4,
                     separate_objects: bool = False) -> dict:
    """Rasmni tahrirlanadigan qatlamlarga ajratadi (v5).

    ``num_layers`` / ``separate_objects`` — eski API, e'tiborga olinmaydi (obyektlar har doim).
    Qaytaradi: {"baseUrl", "backgroundUrl", "cleanUrl", "width", "height", "elements",
    "textLayers", "layers": [], "textRemoved", "objectsRemoved", "layersVersion": 5}.
    URL'lar — nisbiy ``/api/layer-files/...``.
    """
    if _is_mock():
        return _mock_decompose(image_source, user_id)

    import numpy as np

    started = time.time()
    deadline = started + PIPELINE_BUDGET
    timings: dict[str, float] = {}

    def remaining(reserve: float = 0) -> float:
        return max(5.0, deadline - time.time() - reserve)

    image_bytes = _load_image_bytes(image_source)
    try:
        rgb, _alpha = te.decode_rgb(image_bytes)
    except Exception:
        raise GenerationError("Slayd rasmini o'qib bo'lmadi", status=400)
    height, width = rgb.shape[:2]
    size = (width, height)
    base_url = _store_base(image_bytes, user_id)
    use_fal = bool(_fal_key())

    # 1. Parallel: GPT (matn + obyektlar) + OCR, BiRefNet (mahsulot), SAM2 auto-segment (hammasi).
    # Segmentatsiya asl rasm ustida — GPT ni kutmaydi; harflar keyin glif maskalari bilan ajratiladi.
    t0 = time.time()
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        original_url = ol.jpeg_data_url(rgb, 95) if use_fal else None
        salient_future = pool.submit(
            al.run_birefnet, original_url, size, _fal_run, _download, min(BIREFNET_TIMEOUT, remaining(120)),
        ) if use_fal else None
        autoseg_future = pool.submit(
            al.run_autoseg, original_url, size, _fal_run, _download, min(AUTOSEG_TIMEOUT, remaining(110)),
            deadline - 100,
        ) if use_fal else None
        blocks, objects, ocr_lines = _detect_and_ocr(image_bytes, min(tl.TEXT_DETECT_TIMEOUT, remaining(150)))
        timings["detect"] = time.time() - t0

        text_layers: list[dict] = []
        if blocks:
            try:
                text_layers = tl.refine_layers(rgb, blocks, ocr_lines)
            except Exception as error:
                logger.warning("Magic layers: qutilarni aniqlashtirib bo'lmadi, GPT qutilari: %s", error)
                text_layers = blocks

        # 2. Matnni lokal o'chirish + asl harflar kesmalari (segmentatsiya shu payt davom etadi)
        t0 = time.time()
        clean_rgb, clean_url, text_removed = rgb, None, False
        masks: dict = {}
        if text_layers:
            try:
                clean_png, masks = te.erase_text_with_masks(image_bytes, text_layers)
                clean_rgb = te.decode_rgb(clean_png)[0]
                clean_url = store_layer_file(clean_png, user_id, "clean")
                text_removed = True
            except Exception as error:
                logger.warning("Magic layers: matnni o'chirib bo'lmadi: %s", error)
                clean_rgb, clean_url, masks = rgb, None, {}
        _attach_text_cutouts(text_layers, rgb, masks, user_id)
        glyph = _glyph_union(text_layers, masks, rgb.shape)
        timings["text"] = time.time() - t0

        t0 = time.time()
        salient = _future_result(salient_future, "birefnet")
        timings["salient_wait"] = time.time() - t0
        t0 = time.time()
        raw_segs = _future_result(autoseg_future, "auto-segment")
        timings["autoseg_wait"] = time.time() - t0
    finally:
        pool.shutdown(wait=False)

    # 3. Mahsulot — BITTA qatlam
    t0 = time.time()
    product = None
    product_objects = [o for o in objects if o.get("kind") == "product"]
    graphic_objects = [o for o in objects if o.get("kind") != "product"]
    if salient is not None:
        try:
            product = al.select_product(salient, objects, glyph)
        except Exception as error:
            logger.warning("Magic layers: mahsulot maskasi tanlanmadi: %s", error)
    clean_data_url = None
    if product is None and product_objects and use_fal:
        # Zaxira: v4 yo'li — SAM2 box prompt, barcha mahsulot qutilari bitta maskaga.
        clean_data_url = ol.jpeg_data_url(clean_rgb)
        pairs = ol.segment_objects(product_objects, clean_data_url, size, _fal_run, _download,
                                   timeout=min(SAM_TIMEOUT, remaining(60)), deadline=deadline - 60)
        if pairs:
            union = np.zeros((height, width), dtype=bool)
            for _obj, mask in pairs:
                union |= mask
            product = al.Seg.from_full(union, name=product_objects[0].get("name") or "Mahsulot", kind="product",
                                       z=max(float(o.get("z", 5.0)) for o in product_objects))
    timings["product"] = time.time() - t0

    # 4. Qolgan elementlar: auto-segment (+ GPT obyektlari topilmaganlari uchun box prompt)
    t0 = time.time()
    segs: list = []
    fallback_objects: list[dict] = []
    if raw_segs is not None:
        try:
            segs = al.postprocess_segments(raw_segs, rgb.shape, product, glyph, objects)
            fallback_objects = al.uncovered_objects(graphic_objects, segs, rgb.shape)
        except Exception as error:
            logger.warning("Magic layers: segmentlarni qayta ishlab bo'lmadi: %s", error)
            segs, fallback_objects = [], graphic_objects
    elif use_fal:
        fallback_objects = graphic_objects
    if fallback_objects and use_fal and remaining() > 70:
        clean_data_url = clean_data_url or ol.jpeg_data_url(clean_rgb)
        try:
            pairs = ol.segment_objects(fallback_objects, clean_data_url, size, _fal_run, _download,
                                       timeout=min(SAM_TIMEOUT, remaining(50)), deadline=deadline - 50)
        except Exception as error:
            logger.warning("Magic layers: SAM2 box zaxirasi ishlamadi: %s", error)
            pairs = []
        product_full = product.full(rgb.shape) if product is not None else None
        for seg in _segs_from_pairs(pairs):
            if product_full is not None and seg.count_in(product_full) > al.SEG_IN_PRODUCT * seg.area:
                continue
            segs.append(seg)
    timings["segments"] = time.time() - t0

    # 5. Qoldiq ushlagich: hech bir qatlamga kirmagan, fondan keskin farqli joylar
    t0 = time.time()
    items = ([product] if product is not None else []) + segs
    leftovers: list = []
    if use_fal:
        covered = glyph.copy()
        for seg in items:
            covered[seg.y0:seg.y1, seg.x0:seg.x1] |= seg.m
        try:
            leftovers = al.catch_leftovers(clean_rgb, covered)
        except Exception as error:
            logger.warning("Magic layers: qoldiq ushlagich ishlamadi: %s", error)
    items += leftovers
    elements = _build_elements(items, clean_rgb, user_id) if items else []
    timings["leftover+cut"] = time.time() - t0

    # 6. Fon: barcha element + mahsulot + harf maskalari (8px kengaytirilgan) to'ldiriladi
    t0 = time.time()
    background_url = clean_url or base_url
    objects_removed = False
    method = "-"
    if elements:
        union = glyph.copy()
        for seg in items:
            union[seg.y0:seg.y1, seg.x0:seg.x1] |= seg.m
        union = ol.dilate(union, ol.BACKGROUND_DILATE_PX)
        try:
            bg_rgb, method = ol.fill_background(
                clean_rgb, union, _fal_run, _download, timeout=min(ERASER_TIMEOUT, remaining(5)),
                use_fal=use_fal,
            )
            background_url = store_layer_file(te.encode_png(bg_rgb), user_id, "bg")
            objects_removed = True
        except Exception as error:
            logger.warning("Magic layers: fonni tiklab bo'lmadi: %s", error)
    timings["background"] = time.time() - t0

    logger.info(
        "Magic layers v5: jami %.1fs (%s) user=%s matn=%d mahsulot=%s segment=%d qoldiq=%d gpt_obyekt=%d fon=%s",
        time.time() - started, " ".join(f"{k}={v:.1f}s" for k, v in timings.items()),
        user_id, len(text_layers), "ha" if product is not None else "yo'q", len(segs), len(leftovers),
        len(objects), method,
    )
    return _result(base_url, background_url, clean_url, size, text_layers, elements,
                   text_removed, objects_removed)


def _future_result(future, label: str):
    if future is None:
        return None
    try:
        return future.result()
    except Exception as error:
        logger.warning("Magic layers: %s xatosi: %s", label, error)
        return None


# ---------------------------------------------------------------------------
#  Nuqta bo'yicha element ajratish (extract endpoint)
# ---------------------------------------------------------------------------

class ExtractError(Exception):
    """Foydalanuvchiga 422 bilan qaytadigan xato (NOTHING_FOUND / TOO_LARGE)."""

    MESSAGES = {
        "NOTHING_FOUND": "Bu joyda ajratiladigan obyekt topilmadi. Obyektning o'ziga bosing.",
        "TOO_LARGE": "Tanlangan joy juda katta (bu fon). Kichikroq obyektga bosing.",
    }

    def __init__(self, code: str):
        super().__init__(self.MESSAGES.get(code, code))
        self.code = code


def read_layer_image(url: str) -> bytes:
    """``/api/layer-files/...`` (yoki to'liq URL ichidagi shu yo'l) → baytlar diskdan."""
    url = str(url or "")
    marker = url.find(LAYER_FILES_URL)
    if marker >= 0:
        path = url[marker + len(LAYER_FILES_URL):].split("?", 1)[0]
        if ".." in path:
            raise GenerationError("Fon fayli topilmadi", status=404)
        with default_storage.open(path, "rb") as handle:
            return handle.read()
    return _load_image_bytes(url)


def extract_element(background_bytes: bytes, x: int, y: int, user_id, number: int):
    """Joriy fondan (x, y) nuqtadagi obyektni ajratadi.

    Qaytaradi: (element dict — url saqlangan, to'ldirilgan fon RGB, to'ldirilgan joy maskasi).
    Fon to'ldirish faqat maska (+8px) ichida — tashqarisi bayt-ma-bayt o'zgarmaydi.
    ``ExtractError`` — 422 (hech narsa topilmadi / juda katta). Boshqa xatolar — GenerationError.
    """
    import numpy as np

    started = time.time()
    rgb = te.decode_rgb(background_bytes)[0]
    height, width = rgb.shape[:2]
    if not (0 <= x < width and 0 <= y < height):
        raise ExtractError("NOTHING_FOUND")

    if _is_mock():
        half = max(4, min(width, height) // 20)
        mask = np.zeros((height, width), dtype=bool)
        mask[max(0, y - half):y + half, max(0, x - half):x + half] = True
        filled = ol.opencv_fill(rgb, ol.dilate(mask, 2))
        fill_mask = ol.dilate(mask, 2)
    else:
        raw = al.run_point_sam(ol.jpeg_data_url(rgb, 95), x, y, (width, height), _fal_run, _download,
                               timeout=EXTRACT_SAM_TIMEOUT)
        mask = al.clean_point_mask(raw, x, y)
        area = int(mask.sum())
        if area < 64:
            raise ExtractError("NOTHING_FOUND")
        if area > al.EXTRACT_MAX_FRACTION * width * height:
            raise ExtractError("TOO_LARGE")
        fill_mask = ol.dilate(mask, ol.BACKGROUND_DILATE_PX)
        filled, _method = ol.fill_background(rgb, fill_mask, _fal_run, _download, timeout=ERASER_TIMEOUT,
                                             use_fal=bool(_fal_key()))
    cut = ol.rgba_cutout(rgb, mask)
    if cut is None:
        raise ExtractError("NOTHING_FOUND")
    png, box = cut
    element = {
        "id": f"el_{uuid.uuid4().hex[:10]}",
        "name": f"Element {number}",
        "kind": "graphic",
        "url": store_layer_file(png, user_id, "el"),
        **box,
        "order": number,
    }
    logger.info("Magic layers extract: (%d,%d) %.1fs maska=%.2f%% user=%s", x, y, time.time() - started,
                100.0 * mask.mean(), user_id)
    return element, filled, fill_mask
