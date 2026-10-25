"""Magic Layers v4: vizual obyektlarni alohida qatlamlarga ajratish va fonni tiklash.

1. Har bir obyekt qutisi uchun fal ``sam2/image`` (box prompt) — maska.
   Kirish rasmi — matni o'chirilgan nusxa (``textClean``): kesilgan ikonka
   kartalarida harflar qolmaydi (ular alohida matn qatlami).
2. Maska OpenCV bilan tozalanadi: quti tashqarisi kesiladi, eng katta
   komponent(lar) qoladi, teshiklar to'ldiriladi; RGBA PNG (yumshoq chet).
3. Barcha obyekt maskalari (8px kengaytirilgan) birlashmasi fal ``bria/eraser``
   bilan to'ldiriladi (yangi matn/obyekt chizmaydigan object-removal modeli).
   Natija faqat maska ichiga qo'yiladi — tashqaridagi piksellar o'zgarmaydi.
   fal ishlamasa — ``cv2.inpaint`` (TELEA): katta joylar xira chiqadi, lekin
   hech qachon yiqilmaydi.

Nega SAM2 (birefnet emas): box prompt modelni aynan shu obyektga bog'laydi;
birefnet esa kesmadagi "eng ko'zga tashlanadigan" narsani oladi — mahsulot
ustida badge bo'lsa yoki kesmaga qo'shni karta tushsa, noto'g'ri obyektni
qaytarishi mumkin. Bitta model — bitta xatolik rejimi, oddiyroq.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

FAL_SAM_ENDPOINT = os.environ.get("FAL_SAM_ENDPOINT", "").strip() or "fal-ai/sam2/image"
FAL_ERASER_ENDPOINT = os.environ.get("FAL_ERASER_ENDPOINT", "").strip() or "fal-ai/bria/eraser"
SAM_WORKERS = 6
MAX_IMAGE_FRACTION = 0.60     # undan katta maska — fon, obyekt emas
MIN_BOX_FRACTION = 0.02       # maska qutining 2% idan kichik — bo'sh
BACKGROUND_DILATE_PX = 8


# ---------------------------------------------------------------------------
#  Kodlash yordamchilari
# ---------------------------------------------------------------------------

def jpeg_data_url(rgb: np.ndarray, quality: int = 92) -> str:
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def mask_data_url(mask: np.ndarray) -> str:
    buf = io.BytesIO()
    Image.fromarray(mask.astype(np.uint8) * 255, "L").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def encode_rgba(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def decode_mask(raw: bytes, size: tuple[int, int]) -> np.ndarray:
    """Model qaytargan maska rasmi → (H, W) bool, asl o'lchamda.

    SAM2 ``apply_mask: false`` — oq/qora maska; RGBA bo'lsa va alfa o'zgaruvchan
    bo'lsa — alfa kanal ishlatiladi.
    """
    width, height = size
    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            alpha = np.array(img.convert("RGBA"))[:, :, 3]
            if int(alpha.min()) < 128 <= int(alpha.max()):
                gray = alpha
            else:
                gray = np.array(img.convert("L"))
        else:
            gray = np.array(img.convert("L"))
    if gray.shape[::-1] != (width, height):
        gray = cv2.resize(gray, (width, height), interpolation=cv2.INTER_LINEAR)
    return gray > 127


# ---------------------------------------------------------------------------
#  Maskani tozalash va kesish
# ---------------------------------------------------------------------------

def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Tashqaridan yetib bo'lmaydigan bo'shliqlar (teshiklar) to'ldiriladi."""
    padded = np.pad(mask.astype(np.uint8), 1)
    flood = padded.copy()
    ff_mask = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), np.uint8)
    cv2.floodFill(flood, ff_mask, (0, 0), 1)
    holes = flood == 0
    return (padded.astype(bool) | holes)[1:-1, 1:-1]


def clean_mask(mask: np.ndarray, box: dict, image_size: tuple[int, int]) -> np.ndarray | None:
    """Maskani obyekt qutisi atrofiga cheklaydi, mayda dog'larni tashlaydi, teshiklarni to'ldiradi.

    Bo'sh (qutining <2%) yoki juda katta (rasmning >60%) maska — None.
    """
    width, height = image_size
    pad_x = int(box["width"] * 0.04) + int(width * 0.01) + 2
    pad_y = int(box["height"] * 0.04) + int(height * 0.01) + 2
    x0, y0 = max(0, box["x"] - pad_x), max(0, box["y"] - pad_y)
    x1 = min(width, box["x"] + box["width"] + pad_x)
    y1 = min(height, box["y"] + box["height"] + pad_y)
    limited = np.zeros_like(mask, dtype=bool)
    limited[y0:y1, x0:x1] = mask[y0:y1, x0:x1]

    box_area = max(1, box["width"] * box["height"])
    if limited.sum() < max(64, box_area * MIN_BOX_FRACTION):
        return None

    crop = limited[y0:y1, x0:x1].astype(np.uint8)
    # Mayda tirqish/shovqin: yengil ochish-yopish
    crop = cv2.morphologyEx(crop, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(crop, connectivity=8)
    if count <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = float(areas.max())
    keep = np.zeros(count, dtype=bool)
    keep[1:] = areas >= max(32, largest * 0.15)
    crop = keep[labels]
    crop = _fill_holes(crop)

    out = np.zeros_like(mask, dtype=bool)
    out[y0:y1, x0:x1] = crop
    total = float(out.sum())
    if total < max(64, box_area * MIN_BOX_FRACTION) or total > MAX_IMAGE_FRACTION * width * height:
        return None
    return out


def feather_alpha(mask: np.ndarray, sigma: float = 0.8) -> np.ndarray:
    """Maska → uint8 alfa: ichi to'liq 255, faqat tashqariga ~1px yumshoq o'tish."""
    hard = mask.astype(np.float32)
    soft = cv2.GaussianBlur(hard, (0, 0), sigma)
    soft[soft < 0.03] = 0.0  # Gauss "dumi" — ko'rinmas, lekin PNG'ni va to'qnashuvni buzadi
    return np.clip(np.rint(np.maximum(hard, soft) * 255.0), 0, 255).astype(np.uint8)


def rgba_cutout(rgb: np.ndarray, mask: np.ndarray, pad: int = 2) -> tuple[bytes, dict] | None:
    """Rasmning maska bo'yicha RGBA kesmasi (maska chegarasi + pad). (png, {x,y,width,height})."""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    height, width = mask.shape[:2]
    x0, y0 = max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad)
    x1, y1 = min(width, int(xs.max()) + 1 + pad), min(height, int(ys.max()) + 1 + pad)
    alpha = feather_alpha(mask[y0:y1, x0:x1])
    rgba = np.dstack([rgb[y0:y1, x0:x1], alpha])
    return encode_rgba(np.ascontiguousarray(rgba)), {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def box_cutout(rgb: np.ndarray, mask_crop: np.ndarray, box: dict) -> bytes:
    """Berilgan to'rtburchak (``box``) ustidan RGBA kesma, alfa = ``mask_crop`` (yumshoq chet)."""
    x, y = box["x"], box["y"]
    region = rgb[y:y + box["height"], x:x + box["width"]]
    alpha = feather_alpha(mask_crop[:region.shape[0], :region.shape[1]])
    return encode_rgba(np.ascontiguousarray(np.dstack([region, alpha])))


# ---------------------------------------------------------------------------
#  SAM2 segmentatsiya
# ---------------------------------------------------------------------------

def sam_payload(image_url: str, box: dict) -> dict:
    """fal-ai/sam2/image: image_url + box_prompts [{x_min,y_min,x_max,y_max}] (px)."""
    return {
        "image_url": image_url,
        "box_prompts": [{
            "x_min": int(box["x"]),
            "y_min": int(box["y"]),
            "x_max": int(box["x"] + box["width"]),
            "y_max": int(box["y"] + box["height"]),
        }],
        "apply_mask": False,
        "output_format": "png",
    }


def _sam_mask_url(result: dict) -> str | None:
    image = result.get("image") if isinstance(result, dict) else None
    if isinstance(image, dict) and image.get("url"):
        return image["url"]
    images = (result or {}).get("images") or []
    if images and isinstance(images[0], dict):
        return images[0].get("url")
    return None


def segment_objects(objects: list[dict], image_url: str, size: tuple[int, int], fal_run, download,
                    timeout: float, deadline: float | None = None) -> list[tuple[dict, np.ndarray]]:
    """Har bir obyekt uchun SAM2 (parallel, ko'pi bilan 6 ta). Xato bergan obyekt — o'tkazib yuboriladi.

    ``timeout`` — bitta chaqiruv chegarasi; ``deadline`` (time.time()) — butun bosqich
    chegarasi: 16 obyekt 6 oqimda 3 navbat bo'ladi, vaqt tugasa qolganlari o'tkaziladi.
    Qaytaradi: [(obyekt, toza maska)], GPT tartibida.
    """
    def one(obj: dict):
        call_timeout = timeout
        if deadline is not None:
            call_timeout = min(timeout, deadline - time.time())
            if call_timeout < 5:
                logger.warning("SAM2: vaqt tugadi, %s o'tkazildi", obj.get("name"))
                return None
        try:
            result = fal_run(FAL_SAM_ENDPOINT, sam_payload(image_url, obj), timeout=call_timeout)
            url = _sam_mask_url(result)
            if not url:
                logger.warning("SAM2: %s uchun maska qaytmadi", obj.get("name"))
                return None
            mask = decode_mask(download(url), size)
            cleaned = clean_mask(mask, obj, size)
            if cleaned is None:
                logger.info("SAM2: %s maskasi bo'sh yoki juda katta — o'tkazildi", obj.get("name"))
            return cleaned
        except Exception as error:
            logger.warning("SAM2: %s segmentatsiyasi ishlamadi: %s", obj.get("name"), error)
            return None

    if not objects:
        return []
    with ThreadPoolExecutor(max_workers=min(SAM_WORKERS, len(objects))) as pool:
        masks = list(pool.map(one, objects))
    return [(obj, mask) for obj, mask in zip(objects, masks) if mask is not None]


# ---------------------------------------------------------------------------
#  Fon: obyektlar o'rnini to'ldirish
# ---------------------------------------------------------------------------

def dilate(mask: np.ndarray, px: int = BACKGROUND_DILATE_PX) -> np.ndarray:
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.dilate(mask.astype(np.uint8), kernel) > 0


def paste_inside(original: np.ndarray, filled: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """``filled`` piksellari faqat ``mask`` ichiga qo'yiladi; ichki chet ~3px yumshoq.

    Maskadan tashqaridagi piksellar ``original`` bilan bayt-ma-bayt bir xil.
    """
    if filled.shape != original.shape:
        filled = cv2.resize(filled, (original.shape[1], original.shape[0]), interpolation=cv2.INTER_LANCZOS4)
    m = mask.astype(np.uint8)
    inner = cv2.erode(m, np.ones((5, 5), np.uint8))
    alpha = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 1.5)
    alpha[inner > 0] = 1.0
    alpha[m == 0] = 0.0
    blended = original.astype(np.float32) * (1 - alpha[..., None]) + filled.astype(np.float32) * alpha[..., None]
    out = np.clip(np.rint(blended), 0, 255).astype(np.uint8)
    out[m == 0] = original[m == 0]
    return out


def opencv_fill(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Zaxira: cv2 TELEA inpaint. Katta maskada tezlik uchun yarim o'lchamda."""
    m = mask.astype(np.uint8) * 255
    if mask.sum() > 150_000:
        small = cv2.resize(rgb, (rgb.shape[1] // 2, rgb.shape[0] // 2), interpolation=cv2.INTER_AREA)
        small_mask = cv2.resize(m, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
        small_mask = cv2.dilate(small_mask, np.ones((3, 3), np.uint8))
        filled_small = cv2.inpaint(small, small_mask, 7, cv2.INPAINT_TELEA)
        filled = cv2.resize(filled_small, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_CUBIC)
    else:
        filled = cv2.inpaint(rgb, m, 7, cv2.INPAINT_TELEA)
    return paste_inside(rgb, filled, mask)


def eraser_payload(image_url: str, mask_url: str) -> dict:
    """fal-ai/bria/eraser: image_url + mask_url (oq = o'chiriladigan joy)."""
    return {"image_url": image_url, "mask_url": mask_url, "mask_type": "automatic"}


def fill_background(rgb: np.ndarray, mask: np.ndarray, fal_run, download, timeout: float,
                    use_fal: bool = True) -> tuple[np.ndarray, str]:
    """Obyektlar o'rnini to'ldirilgan fon. (rgb, usul: "bria" | "opencv")."""
    if use_fal:
        try:
            result = fal_run(FAL_ERASER_ENDPOINT, eraser_payload(jpeg_data_url(rgb, 95), mask_data_url(mask)),
                             timeout=timeout)
            image = (result or {}).get("image") or {}
            url = image.get("url") if isinstance(image, dict) else None
            if not url:
                raise ValueError("eraser rasm qaytarmadi")
            with Image.open(io.BytesIO(download(url))) as img:
                filled = np.array(img.convert("RGB"))
            return paste_inside(rgb, filled, mask), "bria"
        except Exception as error:
            logger.warning("Magic layers: bria eraser ishlamadi, OpenCV zaxira: %s", error)
    return opencv_fill(rgb, mask), "opencv"
