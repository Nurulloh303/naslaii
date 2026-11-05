"""Uzum eksportini yig'ish: tekshirish → rasm nusxalari → XLSM → saqlash."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid

from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from . import photos as photo_mod
from .catalog import current_template_sha, get_catalog, template_bytes
from .models import UzumExport
from .ooxml import col_to_index
from .validation import validate_draft
from .writer import COLUMNS, FILTER_START_COL, MULTI_LABEL, SINGLE_LABEL, Number, SheetPlan, Text, build_xlsm

logger = logging.getLogger(__name__)

PRIVATE_NAME_RE = re.compile(r"^user_\d+/[0-9a-f]{32}\.xlsm$")


def export_storage() -> FileSystemStorage:
    return FileSystemStorage(location=django_settings.UZUM_EXPORT_ROOT)


def photo_separator() -> str:
    return getattr(django_settings, "UZUM_PHOTO_SEPARATOR", ";") or ";"


def build_plan(normalized: dict, photo_urls: list[str]) -> SheetPlan:
    category = normalized["category"]
    c1 = f"{category['path']} | {category['id']}"
    headers = []
    filters = {f["id"]: f for f in category["filters"]}
    col_of: dict[str, int] = {}
    for index, fid in enumerate(category["macroColumns"]):
        col = FILTER_START_COL + index
        if fid:
            spec = filters[fid]
            headers.append((col, spec["name"], MULTI_LABEL if spec["multiple"] else SINGLE_LABEL))
            col_of[fid] = col
        else:
            headers.append((col, "", MULTI_LABEL))  # fantom ustun: makros aynan shunday yozadi

    c = {key: col_to_index(letter) for key, letter in COLUMNS.items()}
    photos_text = photo_separator().join(photo_urls)
    rows = []
    for sku in normalized["skus"]:
        row = {
            c["titleRu"]: Text(normalized["titleRu"]),
            c["sellerId"]: Text(sku["sellerId"]),
            c["titleUz"]: Text(normalized["titleUz"]),
            c["groupKey"]: Text(normalized["groupKey"]),
            c["categoryTitle"]: Text(category["title"]),
            c["categoryId"]: Number(category["id"]),
            c["brand"]: Text(normalized["brand"]),
            c["model"]: Text(normalized["model"]),
            c["country"]: Text(normalized["country"]),
            c["descriptionRu"]: Text(normalized["descriptionRu"]),
            c["descriptionUz"]: Text(normalized["descriptionUz"]),
            c["shortRu"]: Text(normalized["shortRu"]),
            c["shortUz"]: Text(normalized["shortUz"]),
            c["compositionRu"]: Text(normalized["compositionRu"]),
            c["compositionUz"]: Text(normalized["compositionUz"]),
            c["careRu"]: Text(normalized["careRu"]),
            c["careUz"]: Text(normalized["careUz"]),
            c["sizeChartRu"]: Text(normalized["sizeChartRu"]),
            c["sizeChartUz"]: Text(normalized["sizeChartUz"]),
            c["photos"]: Text(photos_text),
            c["barcode"]: Text(sku["barcode"]),
            c["ikpu"]: Text(normalized["ikpu"]),
            c["color"]: Text(sku["color"]),
            c["size"]: Text(sku["size"]),
            c["price"]: Number(sku["price"]),
            c["oldPrice"]: Number(sku["oldPrice"]),
            c["weightG"]: Number(sku["weightG"]),
            c["heightMm"]: Number(sku["heightMm"]),
            c["widthMm"]: Number(sku["widthMm"]),
            c["lengthMm"]: Number(sku["lengthMm"]),
        }
        for fid, values in normalized["filters"].items():
            row[col_of[fid]] = Text(";".join(values))  # HandleMultipleChoice: ";" bilan
        rows.append(row)
    return SheetPlan(c1=c1, filter_headers=headers, rows=rows)


def snapshot(draft: dict, normalized: dict) -> dict:
    """Bazaga yoziladigan o'zgarmas nusxa: data URL'lar o'rniga faqat belgi."""
    snap = {k: v for k, v in draft.items() if k not in ("photos", "images")}
    snap["photos"] = [
        {"jobId": p["jobId"], "resultIndex": p["resultIndex"]} if "jobId" in p else {"dataUrl": "(upload)"}
        for p in normalized.get("photos", [])
    ]
    return json.loads(json.dumps(snap, ensure_ascii=False, default=str))


def draft_hash(draft: dict) -> str:
    return hashlib.sha256(json.dumps(draft, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class ExportFailed(Exception):
    def __init__(self, export):
        super().__init__("export failed")
        self.export = export


def create_export(user, draft: dict, idempotency_key: str = ""):
    """Qaytaradi: ``("ok", export)`` | ``("invalid", errors)`` | ``("replay", export)`` | ``("conflict", export)``."""
    key = (idempotency_key or "").strip()[:128]
    digest = draft_hash(draft) if isinstance(draft, dict) else ""
    if key:
        existing = UzumExport.objects.filter(user=user, idempotency_key=key).first()
        if existing is not None:
            return ("replay" if existing.draft_hash == digest else "conflict"), existing

    sha = current_template_sha()
    catalog = get_catalog(sha)
    errors, normalized = validate_draft(draft, user, catalog, check_images=True)
    if errors:
        return "invalid", errors

    category = normalized["category"]
    images = [p["image"] for p in normalized["photos"]]
    photo_paths: list[str] = []
    export = UzumExport(
        user=user,
        job=normalized.get("job"),
        category_id=category["id"],
        category_path=category["path"][:500],
        template_sha=sha,
        draft=snapshot(draft, normalized),
        sku_count=len(normalized["skus"]),
        idempotency_key=key,
        draft_hash=digest,
    )
    try:
        photo_paths = photo_mod.store_export_photos(user.pk, images)
        urls = [photo_mod.public_url(p) for p in photo_paths]
        plan = build_plan(normalized, urls)
        data = build_xlsm(template_bytes(sha), category, plan)
        storage = export_storage()
        name = f"user_{user.pk}/{uuid.uuid4().hex}.xlsm"
        saved = storage.save(name, ContentFile(data))
        if saved != name:
            raise RuntimeError("Saqlangan fayl nomi kutilganidek emas")
        export.file_path = saved
        export.photo_paths = photo_paths
        export.status = "ready"
    except photo_mod.PhotoError as error:
        return "invalid", [{"field": "photos", "message": str(error)}]
    except Exception as error:  # noqa: BLE001 — sabab admin'da ko'rinsin
        logger.exception("Uzum eksportini yasab bo'lmadi")
        export.status = "failed"
        export.idempotency_key = ""  # qayta urinish yangi eksport yasasin
        export.photo_paths = photo_paths
        export.errors = [{"field": "export", "message": "Faylni tayyorlab bo'lmadi. Birozdan so'ng qayta urinib ko'ring."}]
        export.draft["_error"] = str(error)[:300]
        try:
            export.save()
        except IntegrityError:
            pass
        raise ExportFailed(export)

    try:
        with transaction.atomic():
            export.save()
    except IntegrityError:
        # Bir vaqtda kelgan ikki so'rov bir xil kalit bilan: g'olibini qaytaramiz.
        existing = UzumExport.objects.filter(user=user, idempotency_key=key).first()
        if existing is None:
            raise
        _discard_files(export)
        return ("replay" if existing.draft_hash == digest else "conflict"), existing
    return "ok", export


def _discard_files(export: UzumExport) -> None:
    try:
        if export.file_path:
            export_storage().delete(export.file_path)
        for path in export.photo_paths or []:
            default_storage.delete(path)
    except OSError:
        logger.warning("Ortiqcha eksport fayllarini o'chirib bo'lmadi")


def export_payload(export: UzumExport) -> dict:
    return {
        "id": export.pk,
        "status": export.status,
        "categoryId": export.category_id,
        "categoryPath": export.category_path,
        "skuCount": export.sku_count,
        "photoUrls": [photo_mod.public_url(p) for p in export.photo_paths or []],
        "createdAt": export.created_at.isoformat() if export.created_at else None,
        "expiresAt": export.expires_at.isoformat() if export.expires_at else None,
        "fileName": export.file_name,
        "errors": export.errors or [],
    }


# ------------------------------------------------------------------ prefill

def suggest_group_key(title: str, job_id) -> str:
    slug = slugify(title or "")[:80].strip("-") or "naslai"
    return f"{slug}-{job_id}-{uuid.uuid4().hex[:6]}"[:100]


def job_images(job) -> list[dict]:
    from generation.serializers import _absolute

    images = []
    for index, result in enumerate(job.results or []):
        try:
            photo_mod.result_storage_path(job, index)
        except photo_mod.PhotoError:
            continue
        images.append({"jobId": job.pk, "resultIndex": index, "url": _absolute(str(result.get("url") or ""))})
    return images


def prefill_draft(job) -> dict:
    """Loyihadan qoralama: tavsif (``job.settings.listing`` yoki listing.py), rasmlar, guruh kaliti."""
    listing = (job.settings or {}).get("listing")
    if not isinstance(listing, dict) or not _listing_has_text(listing):
        listing = _generate_listing_for(job) or listing or {}

    def pick(key, lang):
        value = listing.get(key) if isinstance(listing, dict) else None
        return str((value or {}).get(lang) or "").strip() if isinstance(value, dict) else ""

    title_ru = pick("title", "ru") or (job.title or "")
    title_uz = pick("title", "uz") or (job.title or "")
    images = job_images(job)
    return {
        "projectId": job.pk,
        "categoryId": None,
        "brand": "",
        "model": "",
        "country": "",
        "titleRu": title_ru,
        "titleUz": title_uz,
        "descriptionRu": pick("fullDescription", "ru"),
        "descriptionUz": pick("fullDescription", "uz"),
        "shortRu": pick("shortDescription", "ru"),
        "shortUz": pick("shortDescription", "uz"),
        "compositionRu": "", "compositionUz": "",
        "careRu": "", "careUz": "",
        "sizeChartRu": "", "sizeChartUz": "",
        "ikpu": "",
        "groupKey": suggest_group_key(title_uz or title_ru, job.pk),
        "photos": [{"jobId": i["jobId"], "resultIndex": i["resultIndex"]} for i in images],
        "images": images,
        "filters": {},
        "skus": [{
            "sellerId": "", "barcode": "", "color": "", "size": "",
            "price": None, "oldPrice": None, "weightG": None,
            "heightMm": None, "widthMm": None, "lengthMm": None,
        }],
        "listingNotice": str(listing.get("notice") or "") if isinstance(listing, dict) else "",
    }


def _listing_has_text(listing: dict) -> bool:
    title = listing.get("title")
    return isinstance(title, dict) and any(str(v or "").strip() for v in title.values())


def _generate_listing_for(job) -> dict | None:
    """Mavjud ``generation.listing.generate_listing`` ni loyihaning 1-rasmi bilan chaqiradi."""
    import base64

    from generation.listing import generate_listing

    images = job_images(job)
    if not images:
        return None
    try:
        path = photo_mod.result_storage_path(job, images[0]["resultIndex"])
        with default_storage.open(path, "rb") as handle:
            raw = handle.read()
    except (OSError, photo_mod.PhotoError):
        return None
    ext = path.rsplit(".", 1)[-1].lower()
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}[ext]
    listing = generate_listing(f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}")
    if _listing_has_text(listing):
        settings_payload = dict(job.settings or {})
        settings_payload["listing"] = listing
        job.settings = settings_payload
        job.save(update_fields=["settings", "updated_at"])
    return listing
