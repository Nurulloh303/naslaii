"""Uzum eksport qoralamasini tekshirish (``validate_draft``).

Xato shakli: ``{"field": "price", "sku": 0, "message": "..."}`` — ``sku``
faqat SKU maydonlarida. Xabarlar o'zbekcha (interfeys ularni to'g'ridan-to'g'ri
ko'rsatadi).

Qoidalar shablon (Лист1 2/3-qator izohlari, Инструкция_RU) va Лист2/Лист3
asosida. AI tasdiqlamaydigan faktlar (IKPU, brend, mamlakat, narx, og'irlik,
o'lchamlar) majburiy — ular sotuvchidan keladi, biz o'ylab topmaymiz.
Chegirma yo'q bo'lsa ``oldPrice = price`` (soxta chegirma yaratilmaydi).
"""

from __future__ import annotations

import re
import unicodedata

from generation.models import GenerationJob

from .catalog import Catalog, allowed_size_types, size_type
from .ooxml import INVALID_XML_RE

MAX_SKUS = 50
MAX_PHOTOS = 10
TITLE_MAX = 255
GROUP_KEY_MAX = 100  # "Группировка SKU (до 100 символов)"
DESCRIPTION_MAX = 10000
SHORT_MAX = 1000
OPTIONAL_TEXT_MAX = 5000
MODEL_MAX = 255
SELLER_ID_MAX = 100
CELL_MAX = 32767  # Excel katak chegarasi
PRICE_MAX = 1_000_000_000
WEIGHT_MAX = 1_000_000  # g
DIM_MAX = 10_000  # mm

TEXT_FIELDS = (
    # (kalit, majburiy, chegarasi, xabar nomi)
    ("titleRu", True, TITLE_MAX, "Nomi (RU)"),
    ("titleUz", True, TITLE_MAX, "Nomi (UZ)"),
    ("descriptionRu", True, DESCRIPTION_MAX, "To'liq tavsif (RU)"),
    ("descriptionUz", True, DESCRIPTION_MAX, "To'liq tavsif (UZ)"),
    ("shortRu", True, SHORT_MAX, "Qisqa tavsif (RU)"),
    ("shortUz", True, SHORT_MAX, "Qisqa tavsif (UZ)"),
    ("compositionRu", False, OPTIONAL_TEXT_MAX, "Tarkib (RU)"),
    ("compositionUz", False, OPTIONAL_TEXT_MAX, "Tarkib (UZ)"),
    ("careRu", False, OPTIONAL_TEXT_MAX, "Parvarish (RU)"),
    ("careUz", False, OPTIONAL_TEXT_MAX, "Parvarish (UZ)"),
    ("sizeChartRu", False, OPTIONAL_TEXT_MAX, "O'lcham jadvali (RU)"),
    ("sizeChartUz", False, OPTIONAL_TEXT_MAX, "O'lcham jadvali (UZ)"),
    ("model", False, MODEL_MAX, "Model"),
)

INT_FIELDS = (
    ("price", PRICE_MAX, "Sotuv narxi"),
    ("weightG", WEIGHT_MAX, "Og'irlik (g)"),
    ("heightMm", DIM_MAX, "Balandlik (mm)"),
    ("widthMm", DIM_MAX, "Kenglik (mm)"),
    ("lengthMm", DIM_MAX, "Uzunlik (mm)"),
)


def price_step() -> int:
    """Uzum: "Значение цены не кратно 1000" — narx va eski narx 1000 so'mga karrali."""
    from django.conf import settings

    try:
        return max(1, int(getattr(settings, "UZUM_PRICE_STEP", 1000)))
    except (TypeError, ValueError):
        return 1000


def _round_price(value: int, step: int) -> int:
    return max(step, int(round(value / step)) * step)


def ikpu_lengths() -> set[int]:
    """Ruxsat etilgan IKPU uzunliklari.

    Shablon izohi "16 символов" deydi, lekin davlat klassifikatori (MXIK,
    tasnif.soliq.uz) 2021-yildan beri 17 xonali. Ikkalasini qabul qilamiz;
    ``UZUM_IKPU_LENGTHS=16`` bilan qat'iylashtirish mumkin.
    """
    from django.conf import settings

    raw = str(getattr(settings, "UZUM_IKPU_LENGTHS", "16,17"))
    lengths = {int(x) for x in re.findall(r"\d+", raw)}
    return lengths or {16, 17}


def clean_text(value, multiline: bool = True) -> str:
    """Matnni tozalaydi: NFC, taqiqlangan boshqaruv belgilari olib tashlanadi, chetlari kesiladi."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    text = INVALID_XML_RE.sub("", text).replace("\t", " ")
    if not multiline:
        text = " ".join(text.split())
    else:
        text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def _int(value):
    """Butun musbat son yoki None. ``"120000"`` qabul qilinadi; ``1.5``/``True`` — yo'q."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,12}\s*", value):
        return int(value)
    return None


def _gtin_ok(code: str) -> bool:
    if len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def validate_draft(draft, user, catalog: Catalog, *, check_images: bool = True):
    """Qoralamani tekshiradi. Qaytaradi: ``(errors, normalized)``.

    ``normalized`` — yozuvchi uchun tozalangan qiymatlar (xato bo'lsa ham qisman).
    ``check_images=True`` bo'lsa rasmlar haqiqatan ochiladi (data URL dekod qilinadi).
    """
    errors: list[dict] = []

    def err(field, message, sku=None):
        item = {"field": field, "message": message}
        if sku is not None:
            item["sku"] = sku
        errors.append(item)

    if not isinstance(draft, dict):
        return [{"field": "draft", "message": "Ma'lumot noto'g'ri yuborildi"}], {}

    out: dict = {}

    # --- kategoriya
    category = None
    cat_id = _int(draft.get("categoryId"))
    if cat_id is None:
        err("categoryId", "Kategoriyani tanlang")
    else:
        category = catalog.categories.get(cat_id)
        if category is None:
            err("categoryId", "Bunday kategoriya Uzum katalogida yo'q")
        elif not category["leaf"]:
            err("categoryId", "Eng oxirgi (ichki) kategoriyani tanlang")
            category = None
    out["category"] = category

    # --- loyiha (ixtiyoriy)
    project_id = draft.get("projectId")
    out["job"] = None
    if not _blank(project_id):
        pid = _int(project_id)
        job = GenerationJob.objects.filter(pk=pid, user=user).first() if pid else None
        if job is None:
            err("projectId", "Loyiha topilmadi")
        out["job"] = job

    # --- matnlar
    for key, required, limit, label in TEXT_FIELDS:
        raw = draft.get(key)
        if raw is not None and not isinstance(raw, str):
            err(key, f"{label}: matn bo'lishi kerak")
            continue
        text = clean_text(raw, multiline=key not in ("titleRu", "titleUz", "model"))
        if required and not text:
            err(key, f"{label}ni to'ldiring")
        elif len(text) > limit:
            err(key, f"{label} {limit} belgidan oshmasin (hozir {len(text)})")
        out[key] = text

    # --- ma'lumotnomadan qiymatlar
    brand_raw = draft.get("brand")
    if _blank(brand_raw):
        err("brand", "Brendni ma'lumotnomadan tanlang (brend bo'lmasa — «Отсутствует бренд»)")
        out["brand"] = ""
    else:
        brand = catalog.canonical("brand", brand_raw)
        if brand is None:
            err("brand", "Bu brend Uzum ma'lumotnomasida yo'q — ro'yxatdan tanlang")
        out["brand"] = brand or ""

    country_raw = draft.get("country")
    if _blank(country_raw):
        err("country", "Ishlab chiqarilgan mamlakatni tanlang")
        out["country"] = ""
    else:
        country = catalog.canonical("country", country_raw)
        if country is None:
            err("country", "Bu mamlakat Uzum ma'lumotnomasida yo'q — ro'yxatdan tanlang")
        out["country"] = country or ""

    # --- IKPU
    ikpu = re.sub(r"\s+", "", str(draft.get("ikpu") or ""))
    if not ikpu:
        err("ikpu", "IKPU kodini kiriting (tasnif.soliq.uz dagi raqamli kod)")
    elif not ikpu.isdigit() or len(ikpu) not in ikpu_lengths():
        err("ikpu", "IKPU " + " yoki ".join(str(n) for n in sorted(ikpu_lengths(), reverse=True)) + " ta raqamdan iborat bo'lishi kerak (tasnif.soliq.uz)")
    out["ikpu"] = ikpu

    # --- guruh kaliti
    group_key = clean_text(draft.get("groupKey"), multiline=False)
    if not group_key:
        err("groupKey", "SKU guruhlash kalitini kiriting")
    elif len(group_key) > GROUP_KEY_MAX:
        err("groupKey", f"Guruhlash kaliti {GROUP_KEY_MAX} belgidan oshmasin")
    out["groupKey"] = group_key

    # --- rasmlar
    photos = draft.get("photos")
    out["photos"] = []
    if not isinstance(photos, list) or not photos:
        err("photos", "Kamida bitta rasm tanlang (birinchisi asosiy bo'ladi)")
        photos = []
    elif len(photos) > MAX_PHOTOS:
        err("photos", f"{MAX_PHOTOS} tadan ko'p rasm bo'lmasin")
        photos = []
    seen_refs = set()
    jobs_cache: dict[int, GenerationJob | None] = {}
    from . import photos as photo_mod

    for index, photo in enumerate(photos):
        field = f"photos.{index}"
        if not isinstance(photo, dict):
            err(field, "Rasm noto'g'ri ko'rsatilgan")
            continue
        if photo.get("dataUrl"):
            data_url = photo.get("dataUrl")
            if not isinstance(data_url, str):
                err(field, "Rasm noto'g'ri ko'rsatilgan")
                continue
            image = None
            if check_images:
                try:
                    image = photo_mod.load_data_url(data_url)
                except photo_mod.PhotoError as error:
                    err(field, str(error))
                    continue
            out["photos"].append({"dataUrl": data_url, "image": image})
            continue
        job_id = _int(photo.get("jobId"))
        result_index = _int(photo.get("resultIndex"))
        if job_id is None or result_index is None:
            err(field, "Rasm noto'g'ri ko'rsatilgan")
            continue
        if (job_id, result_index) in seen_refs:
            err(field, "Bu rasm ikki marta tanlangan")
            continue
        seen_refs.add((job_id, result_index))
        if job_id not in jobs_cache:
            jobs_cache[job_id] = GenerationJob.objects.filter(pk=job_id, user=user).first()
        job = jobs_cache[job_id]
        if job is None:
            err(field, "Rasm topilmadi")
            continue
        try:
            path = photo_mod.result_storage_path(job, result_index)
            image = photo_mod.load_result_image(job, result_index) if check_images else None
        except photo_mod.PhotoError as error:
            err(field, str(error))
            continue
        out["photos"].append({"jobId": job_id, "resultIndex": result_index, "path": path, "image": image})

    # --- filtrlar
    filters_in = draft.get("filters") or {}
    out["filters"] = {}
    if not isinstance(filters_in, dict):
        err("filters", "Filtrlar noto'g'ri yuborildi")
        filters_in = {}
    if category is not None:
        known = {f["id"]: f for f in category["filters"]}
        for key in filters_in:
            if str(key) not in known:
                err(f"filters.{key}", "Bu filtr tanlangan kategoriyaga tegishli emas")
        for fid, spec in known.items():
            raw = filters_in.get(fid, filters_in.get(int(fid)) if fid.isdigit() else None)
            if isinstance(raw, str):
                values = [v for v in (part.strip() for part in raw.split(";")) if v] if spec["multiple"] else ([raw.strip()] if raw.strip() else [])
            elif isinstance(raw, list) and all(isinstance(v, str) for v in raw):
                values = [v.strip() for v in raw if v.strip()]
            elif raw is None:
                values = []
            else:
                err(f"filters.{fid}", f"«{spec['name']}»: qiymat noto'g'ri")
                continue
            if not values:
                if spec["required"]:
                    err(f"filters.{fid}", f"«{spec['name']}» majburiy filtr — qiymat tanlang")
                continue
            if not spec["multiple"] and len(values) > 1:
                err(f"filters.{fid}", f"«{spec['name']}» uchun faqat bitta qiymat tanlanadi")
                continue
            allowed = {v.casefold(): v for v in spec["values"]}
            canonical = []
            bad = [v for v in values if v.casefold() not in allowed]
            if bad:
                err(f"filters.{fid}", f"«{spec['name']}»: «{bad[0]}» ro'yxatda yo'q")
                continue
            for v in values:
                c = allowed[v.casefold()]
                if c not in canonical:
                    canonical.append(c)
            out["filters"][fid] = canonical
    elif filters_in:
        pass  # kategoriya xatosi allaqachon aytilgan

    # --- SKU lar
    skus = draft.get("skus")
    out["skus"] = []
    if not isinstance(skus, list) or not skus:
        err("skus", "Kamida bitta SKU (variant) kiriting")
        skus = []
    elif len(skus) > MAX_SKUS:
        err("skus", f"Bitta eksportda {MAX_SKUS} tadan ko'p SKU bo'lmasin")
        skus = []

    variants_seen: dict[tuple, int] = {}
    seller_ids: dict[str, int] = {}
    barcodes: dict[str, int] = {}
    for index, sku in enumerate(skus):
        if not isinstance(sku, dict):
            err("skus", "SKU noto'g'ri yuborildi", index)
            continue
        item: dict = {}
        seller_id = clean_text(sku.get("sellerId"), multiline=False)
        if len(seller_id) > SELLER_ID_MAX:
            err("sellerId", f"Sotuvchi ID {SELLER_ID_MAX} belgidan oshmasin", index)
        elif seller_id:
            if seller_id in seller_ids:
                err("sellerId", "Sotuvchi ID takrorlangan", index)
            seller_ids.setdefault(seller_id, index)
        item["sellerId"] = seller_id

        barcode = re.sub(r"\s+", "", str(sku.get("barcode") or ""))
        if barcode:
            if not re.fullmatch(r"\d{8,14}", barcode) or not _gtin_ok(barcode):
                err("barcode", "Shtrixkod EAN-13/EAN-8/UPC-A/GTIN-14 formatida emas (bo'sh qoldirsangiz Uzum o'zi yaratadi)", index)
            elif barcode in barcodes:
                err("barcode", "Shtrixkod takrorlangan", index)
            barcodes.setdefault(barcode, index)
        item["barcode"] = barcode

        color = ""
        if not _blank(sku.get("color")):
            color = catalog.canonical("color", sku.get("color")) or ""
            if not color:
                err("color", "Bu rang Uzum ma'lumotnomasida yo'q", index)
        item["color"] = color
        size = ""
        if not _blank(sku.get("size")):
            size = catalog.canonical("size", sku.get("size")) or ""
            if not size:
                err("size", "O'lcham «Tur:Qiymat» ko'rinishida ma'lumotnomadan tanlanadi", index)
            elif category is not None and size_type(size) not in allowed_size_types(catalog, category):
                allowed = allowed_size_types(catalog, category)
                if allowed:
                    err("size", f"«{size_type(size)}» bu kategoriyaga mos emas — Uzum qabul qilmaydi", index)
                else:
                    err("size", "Bu kategoriyaga Uzum o'lcham qo'shtirmaydi — o'lchamni bo'sh qoldiring", index)
        item["size"] = size

        for key, limit, label in INT_FIELDS:
            raw = sku.get(key)
            value = _int(raw)
            if _blank(raw):
                err(key, f"{label}ni kiriting", index)
            elif value is None:
                err(key, f"{label} butun son bo'lishi kerak (faqat raqamlar)", index)
            elif value <= 0:
                err(key, f"{label} 0 dan katta bo'lishi kerak", index)
            elif value > limit:
                err(key, f"{label} juda katta", index)
            item[key] = value

        price = item.get("price")
        step = price_step()
        if isinstance(price, int) and price > 0 and price % step:
            err("price", f"Uzum narxni {step:,} so'mga karrali qabul qiladi (masalan {_round_price(price, step):,} so'm)".replace(",", " "), index)
        raw_old = sku.get("oldPrice")
        if _blank(raw_old):
            old = price  # chegirma yo'q — eski narx = narx
        else:
            old = _int(raw_old)
            if old is None:
                err("oldPrice", "Chegirmagacha narx butun son bo'lishi kerak", index)
            elif old <= 0 or old > PRICE_MAX:
                err("oldPrice", "Chegirmagacha narx noto'g'ri", index)
            elif isinstance(price, int) and old < price:
                err("oldPrice", "Chegirmagacha narx sotuv narxidan kam bo'lmasin", index)
            elif old % step:
                err("oldPrice", f"Uzum narxni {step:,} so'mga karrali qabul qiladi (masalan {_round_price(old, step):,} so'm)".replace(",", " "), index)
        item["oldPrice"] = old

        variant = (color, size)
        if len(skus) > 1:
            if not color and not size:
                err("color", "Bir nechta SKU bo'lsa, har biriga rang yoki o'lcham kerak", index)
            elif variant in variants_seen:
                err("size" if size else "color", f"Bu rang/o'lcham {variants_seen[variant] + 1}-SKU bilan bir xil", index)
            variants_seen.setdefault(variant, index)
        out["skus"].append(item)

    # T ustuni va boshqa matnlar Excel katak chegarasidan oshmasin.
    for key, *_ in TEXT_FIELDS:
        if len(out.get(key) or "") > CELL_MAX:
            err(key, "Matn juda uzun")

    return errors, out
