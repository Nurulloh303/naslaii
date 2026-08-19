import os
import re

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

import logging

from billing.pricing import quote_for
from billing.services import InsufficientBalance, debit, refund
from config.throttling import AnalyzeThrottle, GenerationThrottle

from .analyze import analyze_image
from .models import GenerationJob
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


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_project(request, pk: int):
    """Удаление проекта из кабинета.

    Фильтр по пользователю обязателен: без него чужой проект удаляется по
    одному лишь номеру.
    """
    job = GenerationJob.objects.filter(pk=pk, user=request.user).first()
    if job is None:
        return Response({"error": "PROJECT_NOT_FOUND", "message": "Loyiha topilmadi"}, status=404)
    if job.status in {"queued", "processing"} and not job.cancelled:
        return Response(
            {"error": "PROJECT_RUNNING", "message": "Vazifa hali bajarilmoqda. Avval uni bekor qiling."},
            status=409,
        )
    job.delete()
    remaining = GenerationJob.objects.filter(user=request.user)
    return Response({"ok": True, "projects": [project_payload(item, request) for item in remaining]})


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
