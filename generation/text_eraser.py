"""Magic Layers v3: matnni rasmdan AI'siz, lokal OpenCV bilan o'chirish.

Asl rasm (mahsulot!) o'zgarmasligi kerak — shuning uchun generativ inpaint
(flux fill) ishlatilmaydi: u bo'shagan joyga yangi "soxta" matn chizardi.
Bu yerda faqat harflar turgan piksellar almashtiriladi:

1. Har bir matn qutisi (``source`` = tor quti + ~6px) atrofidagi halqadan fon
   rangi olinadi (LAB'da median).
2. Quti ichida fondan sezilarli farq qiladigan piksellar = harflar maskasi
   (LAB masofa; yomon chiqsa — Otsu). Maska 2–4px kengaytiriladi, lekin
   ``source`` to'rtburchagidan chiqmaydi.
3. Halqa bir xil rangli bo'lsa (plashka, tekis fon) — maska median rang bilan
   bo'yaladi (chetlari yumshoq). Aks holda — ``cv2.inpaint`` (TELEA).

Maskadan tashqaridagi piksellar asl rasm bilan bayt-ma-bayt bir xil qoladi.
"""

from __future__ import annotations

import io
import logging

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

SOURCE_PAD_PX = 6          # tor quti → source to'rtburchagi
RING_WIDTH_PX = 5          # fon rangi olinadigan halqa qalinligi
MIN_THRESHOLD = 14.0       # LAB masofa: undan kichik farq — fon
UNIFORM_NOISE = 7.0        # halqa shovqini (p90) shundan past bo'lsa — tekis fon
FLAT_SPAN = 6.0            # LAB: quti bo'ylab fon modeli shundan ko'p o'zgarsa — gradient
INPAINT_RADIUS = 5


# ---------------------------------------------------------------------------
#  Yordamchilar
# ---------------------------------------------------------------------------

def decode_rgb(image_bytes: bytes) -> tuple[np.ndarray, np.ndarray | None]:
    """RGB massiv (H, W, 3) va alfa kanal (bo'lsa)."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        img.load()
        alpha = None
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            alpha = np.array(rgba)[:, :, 3].copy()
            rgb = np.array(rgba.convert("RGB"))
        else:
            rgb = np.array(img.convert("RGB"))
    return np.ascontiguousarray(rgb), alpha


def encode_png(rgb: np.ndarray, alpha: np.ndarray | None = None) -> bytes:
    if alpha is not None:
        img = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    else:
        img = Image.fromarray(rgb, "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _lab(rgb: np.ndarray) -> np.ndarray:
    """float32 LAB (L 0..100, a/b ~ -128..127) — masofalar sezgiga yaqin."""
    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)


def clamp_box(x0: int, y0: int, x1: int, y1: int, width: int, height: int) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) — x1/y1 eksklyuziv, rasm ichiga siqiladi."""
    x0 = max(0, min(int(x0), width - 1))
    y0 = max(0, min(int(y0), height - 1))
    x1 = max(x0 + 1, min(int(x1), width))
    y1 = max(y0 + 1, min(int(y1), height))
    return x0, y0, x1, y1


def pad_box(box: dict, pad: int, width: int, height: int) -> dict:
    x0, y0, x1, y1 = clamp_box(
        box["x"] - pad, box["y"] - pad,
        box["x"] + box["width"] + pad, box["y"] + box["height"] + pad,
        width, height,
    )
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def source_pad(box_height: int) -> int:
    """Katta harflarda soya/antialias kengroq — padding biroz o'sadi."""
    return SOURCE_PAD_PX + min(6, int(round(box_height * 0.05)))


def _ring_coords(shape, x0: int, y0: int, x1: int, y1: int, ring: int) -> tuple[np.ndarray, np.ndarray]:
    """Quti tashqarisidagi halqa piksellarining (ys, xs) koordinatalari."""
    height, width = shape[:2]
    ox0, oy0 = max(0, x0 - ring), max(0, y0 - ring)
    ox1, oy1 = min(width, x1 + ring), min(height, y1 + ring)
    keep = np.ones((oy1 - oy0, ox1 - ox0), dtype=bool)
    keep[y0 - oy0:y1 - oy0, x0 - ox0:x1 - ox0] = False
    ys, xs = np.nonzero(keep)
    if len(ys) < 8:
        # Quti butun rasmni egallagan — ichki chetki qatordan olamiz.
        keep = np.zeros((y1 - y0, x1 - x0), dtype=bool)
        keep[0, :] = keep[-1, :] = keep[:, 0] = keep[:, -1] = True
        ys, xs = np.nonzero(keep)
        return ys + y0, xs + x0
    return ys + oy0, xs + ox0


def _design(xs: np.ndarray, ys: np.ndarray, scale: float) -> np.ndarray:
    return np.stack([np.ones_like(xs, dtype=np.float32), xs / scale, ys / scale], axis=1).astype(np.float32)


def fit_background(arr: np.ndarray, x0: int, y0: int, x1: int, y1: int, ring: int = RING_WIDTH_PX) -> dict:
    """Halqa bo'yicha fon modeli: har kanal uchun tekislik a + b·x + c·y (gradientga chidamli).

    Ikki bosqichli robust moslash: halqaga tushib qolgan harf/boshqa element
    piksellari ikkinchi bosqichda chiqarib tashlanadi.
    Qaytaradi: {"coef": (3, C), "noise": p90 qoldiq, "median": (C,)}.
    """
    ys, xs = _ring_coords(arr.shape, x0, y0, x1, y1, ring)
    values = arr[ys, xs].reshape(len(ys), -1).astype(np.float32)
    scale = float(max(arr.shape[:2]))
    design = _design(xs.astype(np.float32), ys.astype(np.float32), scale)
    median = np.median(values, axis=0)
    coef = np.zeros((3, values.shape[1]), dtype=np.float32)
    coef[0] = median
    # 1-bosqich: median'dan uzoq piksellar (harf bo'laklari) chiqariladi — median 50% gacha chidamli.
    to_median = np.linalg.norm(values - median, axis=1)
    keep = to_median <= max(8.0, float(np.percentile(to_median, 50)) * 2.0)
    for _ in range(3):
        if keep.sum() < 6:
            break
        solved, *_ = np.linalg.lstsq(design[keep], values[keep], rcond=None)
        coef = solved.astype(np.float32)
        residual = np.linalg.norm(values - design @ coef, axis=1)
        keep = residual <= max(3.0, float(np.percentile(residual, 75)) * 1.5)
    residual = np.linalg.norm(values - design @ coef, axis=1)
    noise = float(np.percentile(residual[keep] if keep.any() else residual, 90))
    return {"coef": coef, "noise": noise, "median": median, "scale": scale}


def background_field(model: dict, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """Model bo'yicha (y1-y0, x1-x0, C) fon maydoni."""
    ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    scale = model["scale"]
    coef = model["coef"]
    return coef[0] + (xs / scale)[..., None] * coef[1] + (ys / scale)[..., None] * coef[2]


def _otsu(dist: np.ndarray) -> np.ndarray:
    scaled = np.clip(dist * (255.0 / max(1.0, float(dist.max()))), 0, 255).astype(np.uint8)
    _, mask = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return mask > 0


def glyph_mask(lab_crop: np.ndarray, bg: np.ndarray, noise: float, text_lab: np.ndarray | None = None,
               strict: bool = False) -> np.ndarray:
    """Quti ichidagi harf piksellari (bool).

    ``bg`` — fon rangi (3,) yoki fon maydoni (h, w, 3), LAB.
    Matn rangi ma'lum va fondan yetarli farq qilsa — asosiy belgi "matn rangiga
    yaqinlik" (donador/gradient fonda ham ishonchli). ``strict`` (qutini
    toraytirish) — faqat matn rangidagi piksellar; o'chirishda esa ularning
    atrofidagi kontur/soya/antialias ham qo'shiladi.
    Matn rangi yo'q yoki yaroqsiz bo'lsa — fondan farq (LAB masofa), yomon
    chiqsa Otsu.
    """
    dist = np.linalg.norm(lab_crop - bg, axis=2)
    threshold = max(MIN_THRESHOLD, noise * 1.5 + 4.0)
    bg_mean = bg.reshape(-1, 3).mean(axis=0)

    if text_lab is not None:
        contrast = float(np.linalg.norm(text_lab - bg_mean))
        if contrast > 20.0:
            to_text = np.linalg.norm(lab_crop - text_lab, axis=2)
            color_mask = (to_text < dist) & (to_text < max(18.0, 0.5 * contrast))
            if color_mask.sum() >= 12:
                # GPT rangi taxminiy — haqiqiy matn rangini piksellardan aniqlab, bir marta takrorlaymiz.
                refined = np.median(lab_crop[color_mask], axis=0)
                contrast = float(np.linalg.norm(refined - bg_mean))
                if contrast > 20.0:
                    to_text = np.linalg.norm(lab_crop - refined, axis=2)
                    color_mask = (to_text < dist) & (to_text < max(16.0, 0.45 * contrast))
                fraction = float(color_mask.mean())
                if 0.002 <= fraction <= 0.75:
                    if strict:
                        return color_mask
                    # Kontur/soya/antialias: matn piksellariga yaqin (≤5px) va fondan farqli.
                    near = cv2.dilate(color_mask.astype(np.uint8), np.ones((11, 11), np.uint8)) > 0
                    edge_threshold = min(threshold, max(MIN_THRESHOLD, 0.35 * contrast))
                    return color_mask | (near & (dist > edge_threshold))

    mask = dist > threshold
    fraction = float(mask.mean()) if mask.size else 0.0
    if fraction > 0.75 or (fraction < 0.003 and float(dist.max()) > MIN_THRESHOLD):
        mask = _otsu(dist)
        if mask.mean() > 0.75:  # fon — ozchilik; demak teskari
            mask = ~mask
    return mask


# ---------------------------------------------------------------------------
#  Bog'langan komponentlar filtri
# ---------------------------------------------------------------------------

def filter_components(mask: np.ndarray, core: tuple[int, int, int, int], min_px: int = 2,
                      extend_lines: bool = False) -> np.ndarray:
    """Harfga o'xshamagan komponentlarni olib tashlaydi.

    ``core`` — (x0, y0, x1, y1) maska koordinatalarida matnning taxminiy qutisi.
    Tashlanadi: juda mayda dog'lar; ``core``ga umuman tegmaydiganlar (boshqa
    element); maska chetiga tegib turgan va asosan ``core``dan tashqarida
    yotganlar (plashka cheti, mahsulot, fon tuzilmasi).

    ``extend_lines`` — GPT qutisi so'zning oxirini kesib qo'ygan bo'lsa: shu
    qatorda (vertikal kesishadigan), yonma-yon turgan harflar ham qo'shiladi.
    """
    height, width = mask.shape[:2]
    kx0, ky0, kx1, ky1 = core
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = np.zeros(count, dtype=bool)
    candidates = []
    for idx in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[idx])
        if area < min_px:
            continue
        touches_edge = x == 0 or y == 0 or x + w >= width or y + h >= height
        ix = max(0, min(x + w, kx1) - max(x, kx0))
        iy = max(0, min(y + h, ky1) - max(y, ky0))
        if ix == 0 or iy == 0:
            if not touches_edge:
                candidates.append((idx, x, y, w, h))
            continue
        inside_ratio = (ix * iy) / max(1, w * h)
        if touches_edge and inside_ratio < 0.6:
            continue
        if inside_ratio < 0.25 and w * h > (kx1 - kx0) * (ky1 - ky0) * 0.5:
            continue  # katta va asosan tashqarida — harf emas
        keep[idx] = True

    if extend_lines and keep.any() and candidates:
        kept = stats[keep]
        letter_h = float(np.median(kept[:, cv2.CC_STAT_HEIGHT]))
        letter_area = float(np.median(kept[:, cv2.CC_STAT_AREA]))
        bx0 = int(kept[:, 0].min()); bx1 = int((kept[:, 0] + kept[:, 2]).max())
        by0 = int(kept[:, 1].min()); by1 = int((kept[:, 1] + kept[:, 3]).max())
        gap_limit = max(6.0, letter_h * 0.9)
        changed = True
        while changed:
            changed = False
            for item in list(candidates):
                idx, x, y, w, h = item
                if h > letter_h * 2.2 or w > letter_h * 4 or h < letter_h * 0.4:
                    continue  # harf o'lchamida emas
                if stats[idx, cv2.CC_STAT_AREA] < letter_area * 0.1:
                    continue  # mayda dog'
                overlap = min(y + h, by1) - max(y, by0)
                if overlap < 0.5 * min(h, letter_h):
                    continue
                gap = max(bx0 - (x + w), x - bx1, 0)
                if gap > gap_limit:
                    continue
                keep[idx] = True
                candidates.remove(item)
                bx0, bx1 = min(bx0, x), max(bx1, x + w)
                changed = True
    return keep[labels]


# ---------------------------------------------------------------------------
#  Qutini toraytirish (GPT/OCR qutisi → harflarning aniq chegarasi)
# ---------------------------------------------------------------------------

def tighten_box(rgb: np.ndarray, box: dict, margin: int | tuple[int, int] | None = None,
                text_color: str | None = None, lab: np.ndarray | None = None) -> dict | None:
    """Quti atrofida harflar maskasini topib, tor qutini qaytaradi. Topilmasa — None.

    ``margin`` — (gorizontal, vertikal) qidiruv chegarasi; GPT qutisi biroz
    siljigan/kesilgan bo'lsa ham harflar to'liq topiladi. Topilgan matn qidiruv
    chegarasiga yopishib qolsa (so'z oxiri hali ham tashqarida) — bir marta
    kengroq qidiriladi.
    """
    height, width = rgb.shape[:2]
    lab = _lab(rgb) if lab is None else lab
    result, search = _tighten_once(rgb, box, margin, text_color, lab)
    if result is not None:
        sx0, _, sx1, _ = search
        at_left = result["x"] <= sx0 + 1 and sx0 > 0
        at_right = result["x"] + result["width"] >= sx1 - 1 and sx1 < width
        if at_left or at_right:
            wider = (int(result["height"] * 0.8 + result["width"] * 0.1) + 10, 6)
            again, _ = _tighten_once(rgb, result, wider, text_color, lab)
            if again is not None and again["width"] >= result["width"]:
                result = again
    return result


def _tighten_once(rgb, box, margin, text_color, lab):
    height, width = rgb.shape[:2]
    cx0, cy0, cx1, cy1 = clamp_box(box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"], width, height)
    bw, bh = cx1 - cx0, cy1 - cy0
    if margin is None:
        margin = (int(bh * 0.3 + bw * 0.08) + 10, int(bh * 0.35) + 10)
    elif isinstance(margin, int):
        margin = (margin, margin)
    mx, my = margin
    sx0, sy0, sx1, sy1 = clamp_box(cx0 - mx, cy0 - my, cx1 + mx, cy1 + my, width, height)

    # Fon — asl qutini quchoqlagan ingichka halqadan (katta margin plashkadan chiqib ketishi mumkin).
    text_lab = _hex_to_lab(text_color)
    model = fit_background(lab, cx0, cy0, cx1, cy1, ring=4)
    if _looks_like_text(model, text_lab):
        # Quti harflar ichida qolgan (halqa ham matn rangida) — tashqi halqadan olamiz.
        model = fit_background(lab, sx0, sy0, sx1, sy1, ring=4)
    noise = model["noise"]
    bg = background_field(model, sx0, sy0, sx1, sy1)
    mask = glyph_mask(lab[sy0:sy1, sx0:sx1], bg, noise, text_lab, strict=True)
    if noise > UNIFORM_NOISE:
        # Donador fondagi mayda dog'lar — harf emas (harf chizig'i kamida 2–4px).
        k = 2 + int(bh >= 40) + int(bh >= 120)
        mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((k, k), np.uint8)) > 0
    search = (sx0, sy0, sx1, sy1)
    if not mask.any():
        return None, search
    min_px = max(3, int(bw * bh * 0.0003))
    mask = filter_components(mask, (cx0 - sx0, cy0 - sy0, cx1 - sx0, cy1 - sy0), min_px=min_px,
                             extend_lines=True)
    if not mask.any():
        return None, search
    ys, xs = np.nonzero(mask)
    tx0, ty0, tx1, ty1 = sx0 + int(xs.min()), sy0 + int(ys.min()), sx0 + int(xs.max()) + 1, sy0 + int(ys.max()) + 1
    if (tx1 - tx0) < 3 or (ty1 - ty0) < 3:
        return None, search
    # Topilgan quti asl qutidan juda katta bo'lsa — nimadir noto'g'ri (fon/plashka).
    if (tx1 - tx0) * (ty1 - ty0) > bw * bh * 3:
        return None, search
    return {"x": tx0, "y": ty0, "width": tx1 - tx0, "height": ty1 - ty0}, search


def _looks_like_text(model: dict, text_lab: np.ndarray | None) -> bool:
    """Fon modeli matn rangiga juda yaqin — demak halqa harflar ustida."""
    if text_lab is None:
        return False
    return float(np.linalg.norm(model["median"] - text_lab)) < 15.0


def _hex_to_lab(value: str | None) -> np.ndarray | None:
    if not value or not isinstance(value, str) or not value.startswith("#") or len(value) != 7:
        return None
    try:
        rgb = np.array([[[int(value[i:i + 2], 16) for i in (1, 3, 5)]]], dtype=np.uint8)
    except ValueError:
        return None
    return _lab(rgb)[0, 0]


# ---------------------------------------------------------------------------
#  O'chirish
# ---------------------------------------------------------------------------

def erase_region(work: np.ndarray, source: dict, text_color: str | None = None,
                 core: dict | None = None, dilate: int = 3) -> np.ndarray:
    """``work`` (RGB, joyida o'zgaradi) ichida ``source`` to'rtburchagidagi harflarni o'chiradi.

    ``core`` — matnning tor qutisi (bo'lsa, undan asosan tashqaridagi
    komponentlar — plashka cheti va h.k. — tegilmaydi).
    Qaytaradi: o'zgargan piksellar maskasi (H, W) bool.
    """
    height, width = work.shape[:2]
    x0, y0, x1, y1 = clamp_box(source["x"], source["y"], source["x"] + source["width"],
                               source["y"] + source["height"], width, height)
    changed = np.zeros((height, width), dtype=bool)

    # Kontekst bilan kesma: halqa + inpaint uchun qo'shni piksellar.
    ctx = RING_WIDTH_PX + INPAINT_RADIUS * 3
    cx0, cy0, cx1, cy1 = clamp_box(x0 - ctx, y0 - ctx, x1 + ctx, y1 + ctx, width, height)
    crop = work[cy0:cy1, cx0:cx1]
    lab = _lab(crop)
    bx0, by0, bx1, by1 = x0 - cx0, y0 - cy0, x1 - cx0, y1 - cy0

    text_lab = _hex_to_lab(text_color)
    model = fit_background(lab, bx0, by0, bx1, by1)
    if _looks_like_text(model, text_lab):
        # Halqa harf ustiga tushgan — fonni kontekst chetidan baholaymiz.
        model = fit_background(lab, max(0, bx0 - ctx // 2), max(0, by0 - ctx // 2),
                               min(crop.shape[1], bx1 + ctx // 2), min(crop.shape[0], by1 + ctx // 2), ring=4)
    noise = model["noise"]
    bg = background_field(model, bx0, by0, bx1, by1)
    inner = glyph_mask(lab[by0:by1, bx0:bx1], bg, noise, text_lab)
    if core is not None and inner.any():
        k = clamp_box(core["x"] - x0, core["y"] - y0, core["x"] + core["width"] - x0,
                      core["y"] + core["height"] - y0, bx1 - bx0, by1 - by0)
        inner = filter_components(inner, k, min_px=1)
    if not inner.any():
        return changed

    mask = np.zeros(crop.shape[:2], dtype=np.uint8)
    mask[by0:by1, bx0:bx1] = inner.astype(np.uint8) * 255
    if dilate > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilate + 1, 2 * dilate + 1))
        mask = cv2.dilate(mask, kernel)
    # Maska source to'rtburchagidan chiqmaydi — frontend aynan shu joyni ko'chiradi.
    limit = np.zeros_like(mask)
    limit[by0:by1, bx0:bx1] = 255
    mask = cv2.bitwise_and(mask, limit)

    ring_sample = np.zeros(crop.shape[:2], dtype=bool)
    ring_ys, ring_xs = _ring_coords(crop.shape, bx0, by0, bx1, by1, RING_WIDTH_PX + 3)
    ring_sample[ring_ys, ring_xs] = True

    # Plashka/tekis fon: halqa shovqini past VA quti bo'ylab rang deyarli o'zgarmaydi.
    # Gradient yoki notekis fonda — cv2.inpaint (atrofdan silliq davom ettiradi).
    span = float(np.ptp(bg.reshape(-1, 3), axis=0).max()) if bg.ndim == 3 else 0.0
    if noise <= UNIFORM_NOISE and span <= FLAT_SPAN:
        # Tekis fon/plashka: median rang, chetlari biroz yumshoq.
        # Fon rangi: halqa median'i; plashkada yengil gradient bo'lsa — tekislik modeli.
        rgb_model = fit_background(crop.astype(np.float32), bx0, by0, bx1, by1)
        fill = np.empty(crop.shape, dtype=np.float32)
        fill[:] = rgb_model["median"]
        fill[by0:by1, bx0:bx1] = background_field(rgb_model, bx0, by0, bx1, by1)
        alpha = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), 1.0)
        alpha[mask > 0] = np.maximum(alpha[mask > 0], 0.85)
        # Maska yadrosi to'liq bo'yaladi, faqat tashqi chet aralashadi.
        alpha[cv2.erode(mask, np.ones((3, 3), np.uint8)) > 0] = 1.0
        alpha[limit == 0] = 0.0
        blended = crop.astype(np.float32) * (1 - alpha[..., None]) + fill * alpha[..., None]
        result = np.clip(np.rint(blended), 0, 255).astype(np.uint8)
        touched = alpha > 0.002
        # Mayda donador tekstura (mato, qog'oz) bo'lsa — tekis dog' ko'rinmasin.
        result = _add_grain(result, crop, mask > 0, ring_sample)
    else:
        result = cv2.inpaint(crop, mask, INPAINT_RADIUS, cv2.INPAINT_TELEA)
        touched = mask > 0
        result = _add_grain(result, crop, touched, ring_sample)

    crop[touched] = result[touched]
    changed[cy0:cy1, cx0:cx1] = touched
    return changed


def _add_grain(result: np.ndarray, original: np.ndarray, touched: np.ndarray, sample: np.ndarray) -> np.ndarray:
    """Inpaint/bo'yoq silliq dog' qoldiradi; donador fonda atrofdagi shovqinni qaytaramiz.

    ``sample`` — shovqin o'lchanadigan piksellar (source atrofidagi halqa):
    tekis plashkada shovqin ~0 bo'ladi va hech narsa qo'shilmaydi.
    """
    near = cv2.dilate(touched.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    known = sample & ~near
    if known.sum() < 50:
        return result
    src = original.astype(np.float32)
    detail = src - cv2.GaussianBlur(src, (0, 0), 1.5)
    sigma = detail[known].std(axis=0)
    if float(sigma.max()) < 1.5:
        return result
    rng = np.random.default_rng(int(touched.sum()))
    grain = rng.normal(0.0, 1.0, result.shape).astype(np.float32) * sigma[None, None, :]
    grain = cv2.GaussianBlur(grain, (0, 0), 0.7) * 1.6  # blur amplitudani pasaytiradi
    out = result.astype(np.float32)
    out[touched] += grain[touched]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def erase_text_with_masks(image_bytes: bytes, text_layers: list[dict]) -> tuple[bytes, dict[str, np.ndarray]]:
    """``erase_text`` + har bir qatlam uchun harflar maskasi.

    Maska — o'zgartirilgan piksellar (harf + kontur/soya/antialias, 3px kengaytirilgan),
    qatlamning ``source`` to'rtburchagiga kesilgan bool massiv. v4 frontend tegilmagan
    matnni aynan shu maska bo'yicha asl rasmdan kesilgan PNG sifatida ko'rsatadi.
    """
    rgb, alpha = decode_rgb(image_bytes)
    height, width = rgb.shape[:2]
    work = rgb.copy()
    masks: dict[str, np.ndarray] = {}
    for layer in text_layers:
        source = layer.get("source") or layer
        core = layer if all(k in layer for k in ("x", "y", "width", "height")) else None
        try:
            changed = erase_region(work, source, layer.get("color"), core=core)
        except Exception as error:  # bitta qutidagi xato butun rasmni buzmasin
            logger.warning("Matnni o'chirib bo'lmadi (%s): %s", layer.get("id"), error)
            continue
        x0, y0, x1, y1 = clamp_box(source["x"], source["y"], source["x"] + source["width"],
                                   source["y"] + source["height"], width, height)
        crop = changed[y0:y1, x0:x1]
        if crop.any() and layer.get("id"):
            masks[layer["id"]] = crop.copy()
    return encode_png(work, alpha), masks


def erase_text(image_bytes: bytes, text_layers: list[dict]) -> bytes:
    """Har bir qatlamning ``source`` qutisidagi harflarni o'chirib, PNG qaytaradi (asl o'lcham)."""
    return erase_text_with_masks(image_bytes, text_layers)[0]
