"""Magic Layers v5: Canva kabi "hammasi alohida" — mahsulot bitta qatlam, qolgani avtomatik.

1. Mahsulot — fal ``birefnet/v2`` (butun rasm, salient-object maskasi). GPT ``kind=product``
   qutilari faqat tanlash uchun: shu qutilarga tushgan bog'langan komponentlar
   olinadi va BITTA maskaga birlashtiriladi (mashina g'ildiraklari, sumka bandi alohida
   bo'lib ketmaydi), teshiklar to'ldiriladi. GPT mahsulot topmasa — eng katta salient
   komponent (rasmning 5–70%) mahsulot hisoblanadi. Mahsulot maskasidan hech narsa
   ayirilmaydi.
2. Qolgan hamma narsa — fal ``sam2/auto-segment`` (``individual_masks``). OpenCV bilan:
   juda mayda (<0.15%) va fonga o'xshash (>45%) segmentlar, asosan mahsulot ichidagilar
   (>60%) va asosan harf piksellaridan iboratlar tashlanadi; biri ikkinchisining
   (teshiklari to'ldirilgan) ichida >70% bo'lsa — kattasiga qo'shiladi (ikonka kartasi
   = karta + ikonka + ramka bitta element). Ko'pi bilan 24 ta. Nomi — eng ko'p ustma-ust
   tushgan GPT obyekti, aks holda "Element N".
3. Qoldiq ushlagich: fon bahosidan (kuchli median blur) LAB bo'yicha keskin farq qiladigan,
   hech qaysi qatlamga tegishli bo'lmagan joylar ("Qolgan element N").
4. Nuqta bo'yicha ajratish (``sam2/image`` point prompt) — ``extract`` endpoint uchun.

fal sxemalari (fal.ai llms.txt / API sahifalaridan tekshirilgan):
- ``fal-ai/birefnet/v2``: in ``image_url, model ("General Use (Light)"|"General Use (Light 2K)"|
  "General Use (Heavy)"|"Matting"|"Portrait"|"General Use (Dynamic)"), operating_resolution
  ("1024x1024"|"2048x2048"|"2304x2304"), output_mask, refine_foreground, output_format``;
  out ``image`` (fon olib tashlangan RGBA), ``mask_image`` (output_mask=true bo'lsa).
- ``fal-ai/sam2/auto-segment``: in ``image_url, points_per_side=32, pred_iou_thresh=0.88,
  stability_score_thresh=0.95, min_mask_region_area=100, output_format``;
  out ``combined_mask`` (Image), ``individual_masks`` (list[Image]).
- ``fal-ai/sam2/image``: in ``image_url, prompts [{x, y, label: 1|0}], box_prompts
  [{x_min,y_min,x_max,y_max}], apply_mask``; out ``image``.
"""

from __future__ import annotations

import io
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from PIL import Image

from . import object_layers as ol

logger = logging.getLogger(__name__)

FAL_BIREFNET_ENDPOINT = os.environ.get("FAL_BIREFNET_ENDPOINT", "").strip() or "fal-ai/birefnet/v2"
FAL_BIREFNET_FALLBACK = "fal-ai/birefnet"
FAL_AUTOSEG_ENDPOINT = os.environ.get("FAL_AUTOSEG_ENDPOINT", "").strip() or "fal-ai/sam2/auto-segment"
BIREFNET_MODEL = os.environ.get("FAL_BIREFNET_MODEL", "").strip() or "General Use (Heavy)"

# Mahsulot
PRODUCT_MIN_FRACTION = 0.05      # GPT mahsulot topmasa — eng katta salient komponent shu oraliqda
PRODUCT_MAX_FRACTION = 0.70
# Segmentlar
SEG_MIN_FRACTION = 0.0015        # rasmning 0.15% idan kichik — shovqin
SEG_MAX_FRACTION = 0.45          # undan katta — fon
SEG_IN_PRODUCT = 0.60            # segmentning shuncha qismi mahsulot ichida — mahsulot bo'lagi
SEG_GLYPH = 0.50                 # shuncha qismi harf — matn qatlami o'zi
SEG_NESTED = 0.70                # biri ikkinchisi ichida shuncha — bitta element
MAX_SEGMENTS = 24
MASK_DOWNLOAD_WORKERS = 12
MAX_RAW_MASKS = 160
# Qoldiq ushlagich
LEFTOVER_MIN_FRACTION = 0.001
LEFTOVER_MAX_FRACTION = 0.15
LEFTOVER_LAB_THRESHOLD = 24.0
LEFTOVER_TEXTURED = 0.25         # qoplanmagan joyning shuncha qismi "farqli" — fon tekstura/foto
MAX_LEFTOVERS = 10
# Nuqta bo'yicha ajratish
EXTRACT_MAX_FRACTION = 0.60


# ---------------------------------------------------------------------------
#  Segment: maska kesmasi (xotira tejash uchun — 100 ta to'liq maska 150 MB bo'lardi)
# ---------------------------------------------------------------------------

class Seg:
    """Bool maska, chegara qutisi bo'yicha kesilgan. (x0, y0, x1, y1) — x1/y1 eksklyuziv."""

    __slots__ = ("x0", "y0", "x1", "y1", "m", "area", "name", "kind", "z")

    def __init__(self, crop: np.ndarray, x0: int, y0: int, name: str | None = None,
                 kind: str = "graphic", z: float = 3.0):
        self.m = crop.astype(bool)
        self.x0, self.y0 = int(x0), int(y0)
        self.y1, self.x1 = self.y0 + crop.shape[0], self.x0 + crop.shape[1]
        self.area = int(self.m.sum())
        self.name, self.kind, self.z = name, kind, z

    @classmethod
    def from_full(cls, mask: np.ndarray, **kw) -> "Seg | None":
        ys, xs = np.nonzero(mask)
        if len(ys) == 0:
            return None
        y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
        return cls(mask[y0:y1, x0:x1], x0, y0, **kw)

    def full(self, shape) -> np.ndarray:
        out = np.zeros(shape[:2], dtype=bool)
        out[self.y0:self.y1, self.x0:self.x1] = self.m
        return out

    def count_in(self, full_mask: np.ndarray) -> int:
        """Shu segment piksellarining nechtasi ``full_mask`` ichida."""
        return int(full_mask[self.y0:self.y1, self.x0:self.x1][self.m].sum())

    def overlap(self, other: "Seg") -> int:
        x0, y0 = max(self.x0, other.x0), max(self.y0, other.y0)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if x0 >= x1 or y0 >= y1:
            return 0
        a = self.m[y0 - self.y0:y1 - self.y0, x0 - self.x0:x1 - self.x0]
        b = other.m[y0 - other.y0:y1 - other.y0, x0 - other.x0:x1 - other.x0]
        return int((a & b).sum())

    def union(self, other: "Seg") -> "Seg":
        x0, y0 = min(self.x0, other.x0), min(self.y0, other.y0)
        x1, y1 = max(self.x1, other.x1), max(self.y1, other.y1)
        crop = np.zeros((y1 - y0, x1 - x0), dtype=bool)
        crop[self.y0 - y0:self.y1 - y0, self.x0 - x0:self.x1 - x0] |= self.m
        crop[other.y0 - y0:other.y1 - y0, other.x0 - x0:other.x1 - x0] |= other.m
        return Seg(crop, x0, y0, name=self.name, kind=self.kind, z=self.z)

    def filled(self) -> "Seg":
        return Seg(ol._fill_holes(self.m), self.x0, self.y0, name=self.name, kind=self.kind, z=self.z)

    def bbox(self) -> tuple[int, int, int, int]:
        return self.x0, self.y0, self.x1, self.y1


def _box_mask(objects: list[dict], shape, pad_fraction: float = 0.0) -> np.ndarray:
    height, width = shape[:2]
    out = np.zeros((height, width), dtype=bool)
    for obj in objects:
        px, py = int(obj["width"] * pad_fraction), int(obj["height"] * pad_fraction)
        x0, y0 = max(0, obj["x"] - px), max(0, obj["y"] - py)
        x1, y1 = min(width, obj["x"] + obj["width"] + px), min(height, obj["y"] + obj["height"] + py)
        out[y0:y1, x0:x1] = True
    return out


def _components(mask: np.ndarray):
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    return count, labels, stats


def _drop_specks(mask: np.ndarray, rel: float = 0.05, min_px: int = 16) -> np.ndarray:
    """Mayda dog'lar (eng katta komponentning 5% idan kichik) olib tashlanadi."""
    count, labels, stats = _components(mask)
    if count <= 2:
        return mask.astype(bool)
    areas = stats[1:, cv2.CC_STAT_AREA]
    keep = np.zeros(count, dtype=bool)
    keep[1:] = areas >= max(min_px, float(areas.max()) * rel)
    return keep[labels]


# ---------------------------------------------------------------------------
#  1. Mahsulot (BiRefNet)
# ---------------------------------------------------------------------------

def birefnet_payload(image_url: str) -> dict:
    return {
        "image_url": image_url,
        "model": BIREFNET_MODEL,
        "operating_resolution": "1024x1024",
        "output_mask": True,
        "refine_foreground": False,
        "output_format": "png",
    }


def run_birefnet(image_url: str, size: tuple[int, int], fal_run, download, timeout: float) -> np.ndarray | None:
    """Butun rasm bo'yicha salient maska (H, W) bool. v2 ishlamasa — ``fal-ai/birefnet``."""
    last_error = None
    for endpoint in dict.fromkeys([FAL_BIREFNET_ENDPOINT, FAL_BIREFNET_FALLBACK]):
        started = time.time()
        try:
            result = fal_run(endpoint, birefnet_payload(image_url), timeout=timeout) or {}
            mask_image = result.get("mask_image") if isinstance(result.get("mask_image"), dict) else None
            url = (mask_image or {}).get("url")
            if not url:
                image = result.get("image") if isinstance(result.get("image"), dict) else {}
                url = image.get("url")  # RGBA — decode_mask alfa kanalni o'qiydi
            if not url:
                raise ValueError("birefnet maska qaytarmadi")
            mask = ol.decode_mask(download(url), size)
            logger.info("Magic layers: %s %.1fs, salient=%.1f%%", endpoint, time.time() - started,
                        100.0 * mask.mean())
            return mask
        except Exception as error:
            last_error = error
            logger.warning("Magic layers: %s ishlamadi: %s", endpoint, error)
            timeout = max(5.0, timeout - (time.time() - started))
    logger.warning("Magic layers: BiRefNet umuman ishlamadi: %s", last_error)
    return None


def select_product(salient: np.ndarray, objects: list[dict], glyph: np.ndarray | None = None) -> Seg | None:
    """Salient maskadan mahsulot — BITTA segment.

    GPT mahsulot qutilari (birlashmasi) bilan ustma-ust tushgan barcha komponentlar olinadi:
    mashina kuzovi va g'ildiraklari alohida komponent bo'lsa ham bitta qatlam bo'ladi.
    Quti tashqarisiga keskin chiqib ketgan komponent (mahsulotga yopishgan qo'shni karta)
    qutining 15% kengaytmasiga kesiladi. Harflardan iborat komponentlar tashlanadi.
    """
    height, width = salient.shape[:2]
    total = float(width * height)
    mask = cv2.morphologyEx(salient.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) > 0
    products = [o for o in objects if o.get("kind") == "product"]
    count, labels, stats = _components(mask)
    if count <= 1:
        return None
    glyph = glyph if glyph is not None else np.zeros_like(mask)

    if products:
        boxes = _box_mask(products, mask.shape)
        wide = _box_mask(products, mask.shape, pad_fraction=0.06)
        clip = _box_mask(products, mask.shape, pad_fraction=0.15)
        box_area = float(boxes.sum())
        keep = np.zeros_like(mask)
        for idx in range(1, count):
            area = int(stats[idx, cv2.CC_STAT_AREA])
            if area < max(32, total * 0.0002):
                continue
            comp = labels == idx
            inside = int((comp & wide).sum())
            if inside == 0:
                continue
            if int((comp & glyph).sum()) > SEG_GLYPH * area:
                continue  # sarlavha harflari — matn qatlami
            if inside < 0.4 * area and (comp & boxes).sum() < 0.25 * box_area:
                continue  # asosan tashqarida — qo'shni element
            if area - inside > 1.5 * inside:
                comp = comp & clip  # mahsulotga yopishgan katta qo'shni bo'lak kesiladi
            keep |= comp
        if not keep.any():
            return None
        keep = ol._fill_holes(_drop_specks(keep, rel=0.01))
        if keep.sum() > PRODUCT_MAX_FRACTION * total:
            keep = keep & clip
        name = next((o.get("name") for o in products if o.get("name")), None) or "Mahsulot"
        z = max(float(o.get("z", 5.0)) for o in products)
        return Seg.from_full(keep, name=name, kind="product", z=z)

    # GPT mahsulot bermadi: eng katta salient komponent, agar mahsulotga o'xshasa.
    areas = stats[1:, cv2.CC_STAT_AREA]
    idx = int(np.argmax(areas)) + 1
    fraction = float(areas[idx - 1]) / total
    if not (PRODUCT_MIN_FRACTION <= fraction <= PRODUCT_MAX_FRACTION):
        return None
    comp = labels == idx
    if int((comp & glyph).sum()) > SEG_GLYPH * int(areas[idx - 1]):
        return None
    return Seg.from_full(ol._fill_holes(comp), name="Mahsulot", kind="product", z=5.0)


# ---------------------------------------------------------------------------
#  2. Avtomatik segmentlar (SAM2 auto-segment)
# ---------------------------------------------------------------------------

def autoseg_payload(image_url: str) -> dict:
    return {
        "image_url": image_url,
        "points_per_side": 32,
        "pred_iou_thresh": 0.86,
        "stability_score_thresh": 0.92,
        "min_mask_region_area": 100,
        "output_format": "png",
    }


def split_combined_mask(raw: bytes, size: tuple[int, int], min_px: int) -> list[np.ndarray]:
    """``combined_mask`` (har segment o'z rangida, fon qora) → alohida maskalar."""
    width, height = size
    with Image.open(io.BytesIO(raw)) as img:
        rgb = np.array(img.convert("RGB"))
    if rgb.shape[1::-1] != (width, height):
        rgb = cv2.resize(rgb, (width, height), interpolation=cv2.INTER_NEAREST)
    q = (rgb // 16).astype(np.int32)
    keys = q[..., 0] * 256 + q[..., 1] * 16 + q[..., 2]
    values, counts = np.unique(keys, return_counts=True)
    masks = []
    for value, cnt in sorted(zip(values.tolist(), counts.tolist()), key=lambda vc: -vc[1]):
        if value == 0 or cnt < min_px:
            continue  # qora — fon
        masks.append(keys == value)
        if len(masks) >= MAX_RAW_MASKS:
            break
    if len(masks) == 1 and masks[0].mean() > 0.0:
        # Ikki rangli (oq/qora) maska — komponentlar alohida segment.
        count, labels, stats = _components(masks[0])
        masks = [labels == i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= min_px]
    return masks


def run_autoseg(image_url: str, size: tuple[int, int], fal_run, download, timeout: float,
                deadline: float | None = None) -> list[Seg] | None:
    """fal auto-segment → xom segmentlar (kesma ko'rinishida). Xatoda None (zaxira yo'l)."""
    width, height = size
    min_px = max(16, int(SEG_MIN_FRACTION * width * height * 0.5))
    started = time.time()
    try:
        result = fal_run(FAL_AUTOSEG_ENDPOINT, autoseg_payload(image_url), timeout=timeout) or {}
    except Exception as error:
        logger.warning("Magic layers: auto-segment ishlamadi: %s", error)
        return None
    urls = [m.get("url") for m in (result.get("individual_masks") or []) if isinstance(m, dict) and m.get("url")]
    segs: list[Seg] = []
    if urls:
        urls = urls[:MAX_RAW_MASKS]

        def one(url):
            if deadline is not None and time.time() > deadline:
                return None
            try:
                mask = ol.decode_mask(download(url), size)
            except Exception as error:
                logger.info("Magic layers: auto-segment maskasi o'qilmadi: %s", error)
                return None
            if mask.sum() < min_px:
                return None
            return Seg.from_full(mask)

        with ThreadPoolExecutor(max_workers=MASK_DOWNLOAD_WORKERS) as pool:
            segs = [s for s in pool.map(one, urls) if s is not None]
    else:
        combined = result.get("combined_mask") if isinstance(result.get("combined_mask"), dict) else None
        if combined and combined.get("url"):
            try:
                segs = [Seg.from_full(m) for m in split_combined_mask(download(combined["url"]), size, min_px)]
            except Exception as error:
                logger.warning("Magic layers: combined_mask o'qilmadi: %s", error)
                return None
        else:
            logger.warning("Magic layers: auto-segment maska qaytarmadi")
            return None
    logger.info("Magic layers: auto-segment %.1fs, %d/%d maska", time.time() - started, len(segs), len(urls))
    return segs


def _merge_nested(segs: list[Seg]) -> list[Seg]:
    """Biri ikkinchisining (teshiklari to'ldirilgan) ichida >70% bo'lsa — kattasiga qo'shiladi."""
    segs = sorted(segs, key=lambda s: -s.area)
    for _round in range(3):
        kept: list[Seg] = []
        filled: list[Seg] = []
        changed = False
        for seg in segs:
            seg_filled = seg.filled()
            target = None
            for idx, big in enumerate(kept):
                if seg.overlap(filled[idx]) > SEG_NESTED * seg.area or \
                        big.overlap(seg_filled) > SEG_NESTED * big.area:
                    target = idx
                    break
            if target is None:
                kept.append(seg)
                filled.append(seg_filled)
            else:
                merged = kept[target].union(seg)
                kept[target] = merged
                filled[target] = merged.filled()
                changed = True
        segs = sorted(kept, key=lambda s: -s.area)
        if not changed:
            break
    return segs


def _name_segment(seg: Seg, graphics: list[dict]) -> dict | None:
    """Eng ko'p ustma-ust tushgan GPT obyekti (segmentning ≥50% qutida va quti ≥20% qoplangan)."""
    best, best_score = None, 0.0
    for obj in graphics:
        x0, y0 = max(seg.x0, obj["x"]), max(seg.y0, obj["y"])
        x1, y1 = min(seg.x1, obj["x"] + obj["width"]), min(seg.y1, obj["y"] + obj["height"])
        if x0 >= x1 or y0 >= y1:
            continue
        inside = int(seg.m[y0 - seg.y0:y1 - seg.y0, x0 - seg.x0:x1 - seg.x0].sum())
        frac = inside / max(1, seg.area)
        bbox_area = max(1, (seg.x1 - seg.x0) * (seg.y1 - seg.y0))
        cover = min(1.0, (x1 - x0) * (y1 - y0) / max(1, obj["width"] * obj["height"]))
        cover = min(cover, bbox_area / max(1, obj["width"] * obj["height"]))
        if frac < 0.5 or cover < 0.2:
            continue
        score = frac * cover
        if score > best_score:
            best, best_score = obj, score
    return best


def postprocess_segments(raw: list[Seg], shape, product: Seg | None, glyph: np.ndarray | None,
                         objects: list[dict]) -> list[Seg]:
    """Xom auto-segment maskalaridan tahrirlanadigan elementlar (yuqoridagi qoidalar)."""
    height, width = shape[:2]
    total = float(width * height)
    product_full = product.full(shape) if product is not None else None
    glyph_d = ol.dilate(glyph, 2) if glyph is not None and glyph.any() else None
    good: list[Seg] = []
    for seg in raw:
        if seg.area < SEG_MIN_FRACTION * total or seg.area > SEG_MAX_FRACTION * total:
            continue
        if product_full is not None and seg.count_in(product_full) > SEG_IN_PRODUCT * seg.area:
            continue
        if glyph_d is not None and seg.count_in(glyph_d) > SEG_GLYPH * seg.area:
            continue
        cleaned = _drop_specks(seg.m)
        good.append(Seg(cleaned, seg.x0, seg.y0) if cleaned.sum() != seg.area else seg)

    merged = _merge_nested(good)
    final: list[Seg] = []
    for seg in merged:
        filled = seg.filled()
        seg = filled if filled.area <= SEG_MAX_FRACTION * total else seg
        if product_full is not None and seg.count_in(product_full) > SEG_IN_PRODUCT * seg.area:
            continue  # birlashgandan keyin ham asosan mahsulot
        final.append(seg)
    final = sorted(final, key=lambda s: -s.area)[:MAX_SEGMENTS]
    graphics = [o for o in objects if o.get("kind") != "product"]
    for number, seg in enumerate(final, start=1):
        obj = _name_segment(seg, graphics)
        seg.kind = "graphic"
        seg.name = (obj.get("name") if obj else None) or f"Element {number}"
        seg.z = float(obj.get("z", 3.0)) if obj else 3.0
    return final


def uncovered_objects(objects: list[dict], segs: list[Seg], shape) -> list[dict]:
    """GPT grafik obyektlaridan auto-segment hech narsa bermaganlari (quti <30% qoplangan)."""
    if not objects:
        return []
    covered = np.zeros(shape[:2], dtype=bool)
    for seg in segs:
        covered[seg.y0:seg.y1, seg.x0:seg.x1] |= seg.m
    missing = []
    for obj in objects:
        if obj.get("kind") == "product":
            continue
        box = covered[obj["y"]:obj["y"] + obj["height"], obj["x"]:obj["x"] + obj["width"]]
        if box.size and box.mean() < 0.3:
            missing.append(obj)
    return missing


# ---------------------------------------------------------------------------
#  3. Qoldiq ushlagich
# ---------------------------------------------------------------------------

def estimate_background(rgb: np.ndarray, covered: np.ndarray | None = None) -> np.ndarray:
    """Fon bahosi: 1/4 o'lchamda qatlamlar o'rni inpaint qilinadi, keyin kuchli median blur (≈120px).

    Inpaint bo'lmasa, katta elementlar (oq kartalar) yonidagi fon bahosi ularning rangiga
    "bo'yalib", kartalar orasidagi tirqishlar soxta "qoldiq" bo'lib chiqardi.
    """
    height, width = rgb.shape[:2]
    scale = 4
    size = (max(1, width // scale), max(1, height // scale))
    small = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
    if covered is not None and covered.any():
        small_mask = cv2.resize(covered.astype(np.uint8) * 255, size, interpolation=cv2.INTER_AREA)
        small_mask = cv2.dilate((small_mask > 0).astype(np.uint8) * 255, np.ones((3, 3), np.uint8))
        if 0 < small_mask.mean() < 255 * 0.95:
            small = cv2.inpaint(small, small_mask, 5, cv2.INPAINT_TELEA)
    k = 31 if min(small.shape[:2]) > 40 else max(3, (min(small.shape[:2]) // 2) | 1)
    blurred = cv2.medianBlur(small, k)
    blurred = cv2.medianBlur(blurred, k)
    return cv2.resize(blurred, (width, height), interpolation=cv2.INTER_LINEAR)


def catch_leftovers(rgb: np.ndarray, covered: np.ndarray, start_number: int = 1) -> list[Seg]:
    """Fondan keskin farq qiladigan, hech bir qatlamga kirmagan joylar → "Qolgan element N".

    Fon foto/tekstura bo'lsa (farqli piksellar qoplanmagan joyning >25%) — hech narsa
    qo'shilmaydi: aks holda manzara bo'laklari "element" bo'lib ketardi.
    """
    from .text_eraser import _lab

    height, width = rgb.shape[:2]
    total = float(width * height)
    bg = estimate_background(rgb, covered)
    dist = np.linalg.norm(_lab(rgb) - _lab(bg), axis=2)
    free = ~ol.dilate(covered, 4)
    residual = (dist > LEFTOVER_LAB_THRESHOLD) & free
    free_count = max(1, int(free.sum()))
    if residual.sum() > LEFTOVER_TEXTURED * free_count:
        logger.info("Magic layers: qoldiq ushlagich o'tkazildi — fon teksturali (%.0f%%)",
                    100.0 * residual.sum() / free_count)
        return []
    residual = cv2.morphologyEx(residual.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    residual = cv2.morphologyEx(residual, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)) > 0
    residual &= ~covered  # yopish (close) qatlamlar ichiga kirmasin
    count, labels, stats = _components(residual)
    found: list[Seg] = []
    order = (np.argsort(-stats[1:, cv2.CC_STAT_AREA]) + 1) if count > 1 else []
    for idx in order:
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < LEFTOVER_MIN_FRACTION * total:
            break
        if area > LEFTOVER_MAX_FRACTION * total:
            continue
        x, y, w, h = (int(v) for v in stats[idx, :4])
        crop = labels[y:y + h, x:x + w] == idx
        # Antialias cheti ham elementga kirsin (fon bilan farqi kichikroq piksellar).
        pad = 3
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(width, x + w + pad), min(height, y + h + pad)
        grown = np.zeros((y1 - y0, x1 - x0), dtype=bool)
        grown[y - y0:y - y0 + h, x - x0:x - x0 + w] = crop
        near = cv2.dilate(grown.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        grown = ol._fill_holes(grown | (near & (dist[y0:y1, x0:x1] > LEFTOVER_LAB_THRESHOLD * 0.5)))
        grown &= ~covered[y0:y1, x0:x1]
        seg = Seg(grown, x0, y0, name=f"Qolgan element {start_number + len(found)}", kind="graphic", z=3.0)
        if seg.area:
            found.append(seg)
        if len(found) >= MAX_LEFTOVERS:
            break
    return found


# ---------------------------------------------------------------------------
#  4. Nuqta bo'yicha ajratish (extract endpoint)
# ---------------------------------------------------------------------------

def point_payload(image_url: str, x: int, y: int, label=1) -> dict:
    return {"image_url": image_url, "prompts": [{"x": int(x), "y": int(y), "label": label}],
            "apply_mask": False, "output_format": "png"}


def run_point_sam(image_url: str, x: int, y: int, size, fal_run, download, timeout: float) -> np.ndarray:
    """SAM2 nuqta prompti → (H, W) bool maska. ``label`` ba'zi versiyalarda satr — qayta urinish."""
    last_error: Exception | None = None
    for label in (1, "1"):
        try:
            result = fal_run(ol.FAL_SAM_ENDPOINT, point_payload(image_url, x, y, label), timeout=timeout)
            url = ol._sam_mask_url(result)
            if not url:
                raise ValueError("SAM2 maska qaytarmadi")
            return ol.decode_mask(download(url), size)
        except Exception as error:  # noqa: PERF203
            last_error = error
            logger.warning("Magic layers extract: SAM2 (label=%r) ishlamadi: %s", label, error)
    raise last_error or ValueError("SAM2 ishlamadi")


def clean_point_mask(mask: np.ndarray, x: int, y: int) -> np.ndarray:
    """Nuqta tushgan (yoki unga eng yaqin, ≤24px) komponent, teshiklari to'ldirilgan."""
    count, labels, stats = _components(mask)
    if count <= 1:
        return np.zeros_like(mask, dtype=bool)
    height, width = mask.shape[:2]
    x, y = min(max(0, x), width - 1), min(max(0, y), height - 1)
    idx = int(labels[y, x])
    if idx == 0:
        y0, y1, x0, x1 = max(0, y - 24), min(height, y + 25), max(0, x - 24), min(width, x + 25)
        window = labels[y0:y1, x0:x1]
        ys, xs = np.nonzero(window)
        if len(ys) == 0:
            return np.zeros_like(mask, dtype=bool)
        nearest = int(np.argmin((ys + y0 - y) ** 2 + (xs + x0 - x) ** 2))
        idx = int(window[ys[nearest], xs[nearest]])
    comp = labels == idx
    # Shu elementning yaqin bo'laklari (masalan, ikonka ichidagi ajralgan chiziq) ham.
    near = ol.dilate(comp, 6)
    for other in range(1, count):
        if other != idx and stats[other, cv2.CC_STAT_AREA] >= 16 and (near & (labels == other)).any():
            comp |= labels == other
    return ol._fill_holes(comp)
