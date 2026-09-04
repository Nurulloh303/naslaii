import io
import os
import re
import zipfile
from urllib.parse import quote, unquote

from django.conf import settings as django_settings
from django.core.files.storage import default_storage
from django.http import HttpResponse

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

import logging

from billing.pricing import TOKENS_PER_IMAGE, quote_for
from billing.services import InsufficientBalance, debit, refund
from config.throttling import AnalyzeThrottle, GenerationThrottle

from .analyze import analyze_image
from .imaging import normalize_generated_image
from .listing import generate_listing
from .models import GenerationJob
from .plan import build_plan
from .providers import GenerationError, _decode_data_url, _store, get_provider
from .style_templates import load_style_templates, resolve_style_template_path, style_template_mime
from .tasks import enqueue
from .serializers import (
    GenerationCreateSerializer,
    is_image_data_url,
    job_payload,
    project_payload,
)

CONTENT_TYPES = {"photo", "card", "video", "fashion", "copyStyle", "marketplacePackage"}
NEEDS_VERIFIED_BRIEF = {"card", "copyStyle", "marketplacePackage"}
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

logger = logging.getLogger(__name__)

# Сколько генераций храним на пользователя. Старые завершённые удаляются
# сами: в Node-версии человек упирался в «лимит исчерпан» и не мог ни
# продолжить работу, ни удалить старое из интерфейса.
MAX_JOBS_PER_USER = int(os.environ.get("MAX_JOBS_PER_USER", 50))


def prune_old_jobs(user) -> int:
    """Освобождает место под новую задачу, не трогая работающие."""
    total = GenerationJob.objects.filter(user=user).count()
    if total < MAX_JOBS_PER_USER:
        return 0
    extra = total - MAX_JOBS_PER_USER + 1
    stale = (
        GenerationJob.objects
        .filter(user=user, status__in=["success", "failed"])
        .order_by("created_at")
        .values_list("pk", flat=True)[:extra]
    )
    ids = list(stale)
    if not ids:
        return 0
    GenerationJob.objects.filter(pk__in=ids).delete()
    logger.info("old jobs pruned: user=%s removed=%s", user.pk, len(ids))
    return len(ids)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([GenerationThrottle])
def create_generation(request):
    idempotency_key = request.headers.get("Idempotency-Key", "")
    if idempotency_key and not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        return Response({"error": "INVALID_IDEMPOTENCY_KEY", "message": "Idempotency-Key noto'g'ri formatda"}, status=400)

    if idempotency_key:
        existing = GenerationJob.objects.filter(user=request.user, idempotency_key=idempotency_key).first()
        if existing:
            # Always re-read the balance from the DB rather than trusting
            # request.user's in-memory value: it may have been loaded before
            # an earlier request (with the same key) already debited tokens.
            request.user.refresh_from_db(fields=["balance"])
            return Response(
                {
                    "job": job_payload(existing, request),
                    "balance": request.user.balance,
                    "idempotentReplay": True,
                },
                status=status.HTTP_200_OK,
            )

    serializer = GenerationCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    settings_payload = data["settings"]
    content_type = settings_payload.get("contentType") or "card"
    if content_type not in CONTENT_TYPES:
        return Response({"error": "INVALID_CONTENT_TYPE", "message": f"Noma'lum kontent turi: {content_type}"}, status=400)

    if content_type == "copyStyle" and not is_image_data_url(settings_payload.get("designReferenceDataUrl")):
        return Response({"error": "MISSING_STYLE_REFERENCE", "message": "Uslub namunasi rasmini yuklang"}, status=400)

    brief = data.get("brief") or {}
    title = (brief.get("title") or "").strip()
    subtitle = (brief.get("subtitle") or "").strip()[:40]
    category = (brief.get("category") or "").strip()
    benefits = [b.strip() for b in (brief.get("benefits") or []) if b and b.strip()]

    needs_verified_brief = content_type in NEEDS_VERIFIED_BRIEF
    if needs_verified_brief and (not title or not category):
        return Response(
            {"error": "MISSING_BRIEF", "message": "Generatsiyadan oldin mahsulot nomi va kategoriyasini tekshiring."},
            status=400,
        )
    if needs_verified_brief and len(benefits) < 2:
        return Response(
            {
                "error": "MISSING_VERIFIED_BENEFITS",
                "message": "Generatsiyadan oldin AI bergan yoki o'zingiz tekshirgan kamida 2 ta aniq afzallikni kiriting.",
            },
            status=400,
        )

    prune_old_jobs(request.user)

    quote = quote_for(settings_payload)

    try:
        user = debit(request.user, quote["tokens"], f"Generatsiya · {content_type}")
    except InsufficientBalance as exc:
        return Response({"error": "INSUFFICIENT_TOKENS", "message": str(exc)}, status=402)

    # Провайдер читает фото из settings: так у него один источник данных
    # и не нужно тащить отдельный аргумент через всю цепочку.
    settings_payload = {**settings_payload, "assetDataUrl": data.get("assetDataUrl", "")}

    job = GenerationJob.objects.create(
        user=user,
        content_type=content_type,
        title=title,
        subtitle=subtitle,
        category=category,
        benefits=benefits,
        settings=settings_payload,
        tokens_charged=quote["tokens"],
        idempotency_key=idempotency_key,
        status="queued",
        message="Vazifa navbatda",
    )

    try:
        enqueue(job)
    except Exception:
        # Токены уже списаны, поэтому возвращаем их здесь же: оставить
        # человека без картинки и без токенов — худшее, что можно сделать.
        logger.exception("Vazifani navbatga qo‘shib bo‘lmadi: job=%s", job.pk)

        # Перечитываем из базы обязательно. Без брокера задача выполняется
        # прямо здесь и могла уже вернуть токены сама — второй возврат
        # выдал бы их бесплатно.
        job.refresh_from_db()
        if job.tokens_charged and not job.refunded:
            refund(user, job.tokens_charged, f"Navbatga qo‘shilmadi · {content_type}")
            job.refunded = True
        if job.status not in {"success", "failed"}:
            job.status = "failed"
            job.progress = 100
            job.message = "Navbat ishlamayapti"
            job.error = "QUEUE_UNAVAILABLE"
        job.save()

        user.refresh_from_db(fields=["balance"])
        return Response(
            {
                "error": "QUEUE_UNAVAILABLE",
                "message": "Server band. Tokenlar qaytarildi, birozdan so‘ng qayta urinib ko‘ring.",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # В синхронном режиме (без брокера) задача уже отработала, и здесь
    # окажется готовый результат. С очередью — статус «в очереди», а
    # дальше фронтенд опрашивает /api/generations/<id>.
    job.refresh_from_db()
    user.refresh_from_db(fields=["balance"])

    return Response(
        {"job": job_payload(job, request), "balance": user.balance, "idempotentReplay": False},
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def generation_detail(request, pk: int):
    """Статус задачи. Фронтенд опрашивает его, пока идёт генерация.

    Баланс отдаём вместе с задачей: он меняется в тот же момент, и без
    него счётчик токенов в шапке отставал до перезагрузки страницы.
    """
    job = GenerationJob.objects.filter(pk=pk, user=request.user).first()
    if job is None:
        return Response({"error": "JOB_NOT_FOUND", "message": "Vazifa topilmadi"}, status=404)
    return Response({"job": job_payload(job, request), "balance": request.user.balance})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def projects_list(request):
    """Список проектов. Ключ `projects` обязателен: кабинет читает
    `payload.projects`, а не голый массив."""
    jobs = GenerationJob.objects.filter(user=request.user)
    return Response({"projects": [project_payload(job, request) for job in jobs]})


def _brief_text_file(job) -> str:
    """Бриф текстом — чтобы папка была понятна и без нашего сайта."""
    lines = [job.title or "Mahsulot"]
    if job.subtitle:
        lines.append(f"Subtitr: {job.subtitle}")
    if job.category:
        lines.append(f"Kategoriya: {job.category}")
    lines += ["", "AFZALLIKLAR:"]
    lines += [f"- {item}" for item in (job.benefits or []) if item]
    plan = (job.settings or {}).get("plan") or []
    if plan:
        lines += ["", "SLAYDLARDAGI MATN:"]
        for index, slide in enumerate(plan):
            head = slide.get("headline", "")
            sub = slide.get("subtitle", "")
            lines.append(f"{index + 1}. {head}" + (f" — {sub}" if sub else ""))
            for line in slide.get("lines") or []:
                lines.append(f"   · {line}")
    lines += ["", f"Naslai · {job.created_at.date().isoformat()}"]
    return "\n".join(lines)


@api_view(["GET", "DELETE"])
@permission_classes([IsAuthenticated])
def project_detail(request, pk: int):
    """Loyiha — bitta papka sifatida (GET) yoki o'chirish (DELETE).

    Фильтр по пользователю обязателен: без него чужой проект открывается
    или удаляется по одному лишь номеру.
    """
    job = GenerationJob.objects.filter(pk=pk, user=request.user).first()
    if job is None:
        return Response({"error": "PROJECT_NOT_FOUND", "message": "Loyiha topilmadi"}, status=404)

    if request.method == "DELETE":
        if job.status in {"queued", "processing"} and not job.cancelled:
            return Response(
                {"error": "PROJECT_RUNNING", "message": "Vazifa hali bajarilmoqda. Avval uni bekor qiling."},
                status=409,
            )
        job.delete()
        remaining = GenerationJob.objects.filter(user=request.user)
        return Response({"ok": True, "projects": [project_payload(item, request) for item in remaining]})

    # GET: bриф, matn rejasi, sozlamalar va barcha rasmlar bitta joyda —
    # loyihani qayta ochganda odam nimadan yig'ilganini yana ko'radi.
    card = project_payload(job, request)
    detail = job_payload(job, request)
    data = {
        **card,
        "summary": detail.get("context", {}).get("summary", ""),
        "contentType": job.content_type,
        # Asl mahsulot rasmi settings ichida data: URL sifatida saqlangan —
        # alohida fayl kerak emas, to'g'ridan-to'g'ri <img src> bo'ladi.
        "sourceUrl": (job.settings or {}).get("assetDataUrl") or None,
        "archiveUrl": f"/api/projects/{job.pk}/archive.zip",
        "brief": {
            "title": job.title,
            "subtitle": job.subtitle,
            "category": job.category,
            "benefits": [b for b in (job.benefits or []) if b],
        },
        "plan": (job.settings or {}).get("plan") or [],
        "listing": (job.settings or {}).get("listing"),
        "results": detail.get("results", []),
    }
    return Response({"project": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def project_archive(request, pk: int):
    """Vazifaning barcha rasmlari bitta arxivda.

    Nomlar raqamlangan: marketplace rasmlarni fayl nomi tartibida yuklaydi,
    raqamsiz slaydlar aralashib ketadi.
    """
    job = GenerationJob.objects.filter(pk=pk, user=request.user).first()
    if job is None or job.status != "success":
        return Response({"error": "PROJECT_NOT_FOUND", "message": "Loyiha topilmadi"}, status=404)

    slide_types = (job.settings or {}).get("slideTypes") or []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, result in enumerate(job.results or []):
            url = str(result.get("url") or "")
            relative = url.replace(django_settings.MEDIA_URL, "", 1).lstrip("/")
            if not default_storage.exists(relative):
                continue
            label = slide_types[index] if index < len(slide_types) else f"slayd-{index + 1}"
            extension = relative.rsplit(".", 1)[-1] if "." in relative else "jpg"
            with default_storage.open(relative, "rb") as source:
                # Rasmlar allaqachon siqilgan — deflate foiz uchun soniyalar sarflaydi.
                archive.writestr(
                    zipfile.ZipInfo(f"{index + 1:02d}-{label}.{extension}"),
                    source.read(),
                    compress_type=zipfile.ZIP_STORED,
                )
        archive.writestr("brief.txt", _brief_text_file(job))

    payload = buffer.getvalue()
    response = HttpResponse(payload, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="naslai-{job.pk}.zip"'
    response["Content-Length"] = str(len(payload))
    return response


@api_view(["GET"])
@permission_classes([AllowAny])
def style_templates_list(request):
    """Tayyor shablonlar galereyasi manifesti — kirishsiz.

    Public: bu shaxsiy ma'lumot emas, barcha sotuvchilar uchun ochiq vitrina.
    """
    templates = [
        {
            "id": entry["file"],
            "category": entry["category"],
            "url": f"/api/style-templates/{quote(entry['file'])}",
        }
        for entry in load_style_templates()
    ]
    return Response({"templates": templates})


@api_view(["GET"])
@permission_classes([AllowAny])
def style_template_file(request, file_name: str):
    """Shablon rasmining o'zi. Faqat manifestdagi fayllar beriladi — yo'l
    hech qachon papkadan tashqariga chiqmaydi, hatto "../" qo'yilsa ham."""
    path = resolve_style_template_path(unquote(file_name))
    if path is None:
        return Response({"error": "TEMPLATE_NOT_FOUND", "message": "Shablon topilmadi"}, status=404)
    with open(path, "rb") as handle:
        payload = handle.read()
    response = HttpResponse(payload, content_type=style_template_mime(path))
    response["Cache-Control"] = "public, max-age=86400"
    return response


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([AnalyzeThrottle])
def analyze(request):
    """Разбор фотографии товара до генерации.

    Ключ OpenAI остаётся на сервере: браузер отправляет картинку нам, а не
    провайдеру напрямую.
    """
    image = request.data.get("imageDataUrl") or ""
    if not is_image_data_url(image):
        return Response(
            {"error": "INVALID_IMAGE", "message": "To'g'ri mahsulot rasmini yuklang"},
            status=400,
        )

    marketplace = str(request.data.get("marketplace") or "uzum")
    if marketplace not in {"uzum", "wb", "ozon"}:
        marketplace = "uzum"

    return Response(analyze_image(image, marketplace, request.data.get("language")))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_generation(request, pk: int):
    """Отмена запущенной генерации.

    Возвращаем токены только за НЕотрисованные картинки: за готовые мы уже
    заплатили провайдеру, и человек их получает. Если не готово ничего —
    возврат полный.

    С очередью (CELERY_BROKER_URL задан) отмена работает по-настоящему:
    флаг читается между картинками. Без брокера генерация выполняется
    прямо в запросе и успевает закончиться раньше, чем придёт отмена, —
    тогда честно отвечаем 409.
    """
    job = GenerationJob.objects.filter(pk=pk, user=request.user).first()
    if job is None:
        return Response({"error": "JOB_NOT_FOUND", "message": "Vazifa topilmadi"}, status=404)
    if job.cancelled or job.status in {"success", "failed"}:
        return Response({"error": "JOB_FINISHED", "message": "Vazifa allaqachon tugagan"}, status=409)

    job.cancelled = True
    delivered = len(job.results or [])
    unused = max(0, job.tokens_charged - delivered)

    if unused and not job.refunded:
        refund(request.user, unused, f"Bekor qilingan generatsiya uchun qaytarish · {job.content_type}")
    job.refunded = True

    if delivered:
        job.status = "success"
        job.message = f"Bekor qilindi · {delivered} ta rasm tayyor"
    else:
        job.status = "failed"
        job.message = "Bekor qilindi"
        job.error = "Generatsiya bekor qilindi"
    job.progress = 100
    job.save()

    request.user.refresh_from_db(fields=["balance"])
    return Response({
        "ok": True,
        "refunded": unused,
        "delivered": delivered,
        "job": job_payload(job, request),
        "balance": request.user.balance,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([GenerationThrottle])
def regenerate_slide(request, pk: int, variant: int):
    """Tayyor vazifadan bitta kadrni qayta chizish.

    Avval bitta yaroqsiz slaydni tuzatish uchun butun buyurtmani qaytadan
    boshlash kerak edi — 8 slaydli paketda bitta rasm uchun 8 token.
    Bu yerda aynan 1 token yechiladi, qolgan slaydlar tegilmaydi.

    Sotuvchining izohi (`note`) promptga qo'shimcha qoida sifatida
    qo'shiladi, urinishlar soni esa modeldan sezilarli boshqacha
    kompozitsiya so'raydi.
    """
    job = GenerationJob.objects.filter(pk=pk, user=request.user).first()
    if job is None:
        return Response({"error": "JOB_NOT_FOUND", "message": "Vazifa topilmadi"}, status=404)
    if job.status != "success":
        return Response({"error": "JOB_NOT_READY", "message": "Avval generatsiya tugashi kerak"}, status=409)

    results = list(job.results or [])
    if variant < 1 or variant > len(results):
        return Response({"error": "RESULT_NOT_FOUND", "message": "Bunday slayd yo'q"}, status=404)

    note = " ".join(str(request.data.get("note") or "").split())[:300]
    settings = dict(job.settings or {})
    counts = dict(settings.get("regenerateCount") or {})
    notes = dict(settings.get("slideNotes") or {})
    counts[str(variant)] = int(counts.get(str(variant), 0)) + 1
    notes[str(variant)] = note
    settings["regenerateCount"] = counts
    settings["slideNotes"] = notes

    label = f"Slaydni qayta chizish · {job.content_type} · {variant}-slayd"
    try:
        debit(request.user, TOKENS_PER_IMAGE, label)
    except InsufficientBalance:
        return Response(
            {"error": "INSUFFICIENT_BALANCE", "message": f"Slaydni qayta chizish uchun {TOKENS_PER_IMAGE} token kerak"},
            status=402,
        )

    job.settings = settings
    job.save(update_fields=["settings", "updated_at"])

    try:
        results[variant - 1] = get_provider().generate(job, variant)
    except Exception as error:  # включая GenerationError
        logger.exception("Slaydni qayta chizib bo'lmadi")
        # Token allaqachon yechilgan. Rasm yo'q — demak qaytaramiz, aks
        # holda odam olmagan narsasi uchun to'laydi.
        refund(request.user, TOKENS_PER_IMAGE, f"Qayta chizish uchun qaytarish · {job.content_type} · {variant}-slayd")
        counts[str(variant)] -= 1
        job.settings = settings
        job.save(update_fields=["settings", "updated_at"])
        request.user.refresh_from_db(fields=["balance"])
        return Response(
            {"error": "REGENERATION_FAILED", "message": str(error)[:300], "balance": request.user.balance},
            status=502,
        )

    job.results = results
    job.save(update_fields=["results", "updated_at"])
    request.user.refresh_from_db(fields=["balance"])
    return Response({"job": job_payload(job, request), "balance": request.user.balance}, status=202)


@api_view(["POST"])
@permission_classes([AllowAny])
def plan_view(request):
    """Matn rejasi: brif bo'yicha, modelsiz, bepul va mgnovenno."""
    brief = request.data.get("brief") or {}
    settings_payload = request.data.get("settings") or {}
    return Response({"plan": build_plan(brief, settings_payload)})


@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def compose_slide(request, pk: int, variant: int):
    """Brauzerda o'z matni bilan yig'ilgan slaydni saqlaydi.

    Fonni model chizadi (`textMode=own` — matnsiz), harflarni esa
    brauzer o'zi chizadi (haqiqiy shrift bilan, AI xato yozmasin deb).
    Bu yerda faqat tayyor rasm saqlanadi — token yechilmaydi.
    """
    job = GenerationJob.objects.filter(pk=pk, user=request.user).first()
    if job is None:
        return Response({"error": "JOB_NOT_FOUND", "message": "Vazifa topilmadi"}, status=404)

    results = list(job.results or [])
    if variant < 1 or variant > len(results):
        return Response({"error": "RESULT_NOT_FOUND", "message": "Bunday slayd yo'q"}, status=404)

    image_data_url = str(request.data.get("imageDataUrl") or "")
    try:
        raw_bytes, _mime, _ext = _decode_data_url(image_data_url)
    except GenerationError as error:
        return Response({"error": "INVALID_IMAGE", "message": str(error)}, status=400)

    image_bytes, extension = normalize_generated_image(raw_bytes, job.content_type)
    stored = _store(image_bytes, job.user_id, extension)

    current = dict(results[variant - 1])
    current.update(stored)
    current["composed"] = True
    results[variant - 1] = current

    job.results = results
    job.save(update_fields=["results", "updated_at"])
    return Response({"job": job_payload(job, request)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([AnalyzeThrottle])
def generate_listing_view(request):
    """Uzum uchun bepul tavsif — istalgan rasmdan, loyihasiz.

    Token yechilmaydi: bu — mahsulotga kirish nuqtasi, karta generatsiyasi
    bilan bog'liq emas.
    """
    image = request.data.get("imageDataUrl") or ""
    if not is_image_data_url(image):
        return Response({"error": "INVALID_IMAGE", "message": "To'g'ri mahsulot rasmini yuklang"}, status=400)
    hint = " ".join(str(request.data.get("hint") or "").split())[:200]
    return Response({"listing": generate_listing(image, hint)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([AnalyzeThrottle])
def project_listing(request, pk: int):
    """Xuddi shu tavsif, lekin tayyor loyihaning o'z rasmidan.

    Natija loyiha ichida saqlanadi — keyingi safar `project_detail` uni
    qayta so'ramasdan qaytaradi.
    """
    job = GenerationJob.objects.filter(pk=pk, user=request.user).first()
    if job is None:
        return Response({"error": "PROJECT_NOT_FOUND", "message": "Loyiha topilmadi"}, status=404)
    results = job.results or []
    if not results:
        return Response({"error": "PROJECT_NOT_READY", "message": "Avval karta tayyor bo'lishi kerak"}, status=409)

    image_url = str(results[0].get("url") or "")
    # Nisbiy "/media/..." OpenAI'ga yaramaydi — to'liq manzilga aylantiramiz.
    if image_url.startswith(("http://", "https://", "data:")):
        image_source = image_url
    else:
        image_source = request.build_absolute_uri(image_url)

    listing = generate_listing(image_source)

    settings_payload = dict(job.settings or {})
    settings_payload["listing"] = listing
    job.settings = settings_payload
    job.save(update_fields=["settings", "updated_at"])
    return Response({"listing": listing})
