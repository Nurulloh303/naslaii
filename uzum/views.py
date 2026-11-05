"""Uzum eksport API (``/api/uzum/...``). Hammasi kirish talab qiladi, faqat egasiga."""

from __future__ import annotations

from django.http import FileResponse
from django.utils import timezone
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response

from config.throttling import UzumExportThrottle, UzumPrefillThrottle
from generation.models import GenerationJob

from . import services
from .catalog import get_catalog
from .models import UzumExport
from .validation import validate_draft

MAX_RESULTS = 50
DICTIONARY_KINDS = ("brand", "color", "size", "country")
XLSM_MIME = "application/vnd.ms-excel.sheet.macroEnabled.12"


def _category_brief(cat: dict) -> dict:
    return {"id": cat["id"], "title": cat["title"], "path": cat["path"]}


@api_view(["GET"])
def categories(request):
    query = str(request.query_params.get("q") or "")[:100]
    found = get_catalog().search_categories(query, MAX_RESULTS)
    return Response({"categories": [_category_brief(c) for c in found]})


@api_view(["GET"])
def category_detail(request, category_id: int):
    cat = get_catalog().categories.get(category_id)
    if cat is None or not cat["leaf"]:
        return Response({"error": "CATEGORY_NOT_FOUND", "message": "Kategoriya topilmadi"}, status=404)
    return Response({
        **_category_brief(cat),
        "filters": [
            {"id": f["id"], "name": f["name"], "values": f["values"], "multiple": f["multiple"], "required": f["required"]}
            for f in cat["filters"]
        ],
    })


@api_view(["GET"])
def dictionary(request):
    kind = str(request.query_params.get("type") or "")
    if kind not in DICTIONARY_KINDS:
        return Response({"error": "BAD_DICTIONARY", "message": "type: brand, color, size yoki country"}, status=400)
    query = str(request.query_params.get("q") or "")[:100]
    return Response({"values": get_catalog().search_dictionary(kind, query, MAX_RESULTS)})


@api_view(["GET"])
@throttle_classes([UzumPrefillThrottle])
def prefill(request):
    raw = str(request.query_params.get("projectId") or "")
    job = GenerationJob.objects.filter(pk=int(raw), user=request.user).first() if raw.isdigit() else None
    if job is None:
        return Response({"error": "PROJECT_NOT_FOUND", "message": "Loyiha topilmadi"}, status=404)
    draft = services.prefill_draft(job)
    return Response({"draft": draft, "images": draft["images"]})


@api_view(["POST"])
def validate(request):
    draft = request.data.get("draft") if isinstance(request.data, dict) else None
    errors, _ = validate_draft(draft, request.user, get_catalog(), check_images=True)
    return Response({"ok": not errors, "errors": errors})


@api_view(["GET", "POST"])
def exports(request):
    if request.method == "GET":
        items = UzumExport.objects.filter(user=request.user)[:100]
        return Response({"exports": [services.export_payload(e) for e in items]})
    return _create_export(request)


def _create_export(request):
    # Faqat POST cheklanadi (GET ro'yxati — yo'q), shuning uchun throttle qo'lda.
    throttle = UzumExportThrottle()
    if not throttle.allow_request(request, None):
        from rest_framework.exceptions import Throttled

        raise Throttled(wait=throttle.wait())
    data = request.data if isinstance(request.data, dict) else {}
    draft = data.get("draft")
    key = str(data.get("idempotencyKey") or request.headers.get("Idempotency-Key") or "")
    try:
        outcome, value = services.create_export(request.user, draft, key)
    except services.ExportFailed as failure:
        return Response(
            {"error": "EXPORT_FAILED", "message": failure.export.errors[0]["message"], "export": services.export_payload(failure.export)},
            status=500,
        )
    if outcome == "invalid":
        return Response({"error": "VALIDATION_FAILED", "message": value[0]["message"], "errors": value}, status=400)
    if outcome == "conflict":
        return Response(
            {"error": "IDEMPOTENCY_CONFLICT", "message": "Bu kalit boshqa ma'lumot bilan ishlatilgan"}, status=409,
        )
    return Response({"export": services.export_payload(value)}, status=201)


def _own_export(request, pk: int):
    return UzumExport.objects.filter(pk=pk, user=request.user).first()


@api_view(["GET"])
def export_detail(request, pk: int):
    export = _own_export(request, pk)
    if export is None:
        return Response({"error": "EXPORT_NOT_FOUND", "message": "Eksport topilmadi"}, status=404)
    return Response({"export": services.export_payload(export)})


@api_view(["GET"])
def export_download(request, pk: int):
    export = _own_export(request, pk)
    if export is None or export.status != "ready" or not services.PRIVATE_NAME_RE.match(export.file_path or ""):
        return Response({"error": "EXPORT_NOT_FOUND", "message": "Eksport topilmadi"}, status=404)
    if export.expires_at and export.expires_at < timezone.now():
        return Response({"error": "EXPORT_EXPIRED", "message": "Eksport muddati tugagan — qaytadan yarating"}, status=410)
    storage = services.export_storage()
    try:
        handle = storage.open(export.file_path, "rb")
    except (OSError, ValueError):
        return Response({"error": "EXPORT_FILE_MISSING", "message": "Fayl topilmadi — qaytadan yarating"}, status=404)
    response = FileResponse(handle, content_type=XLSM_MIME, as_attachment=True, filename=export.file_name)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
