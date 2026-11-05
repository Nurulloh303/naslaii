"""Фоновая генерация.

Раньше это выполнялось прямо в HTTP-запросе. Один пакет из пяти картинок
держал рабочий процесс gunicorn до пятнадцати минут, и трёх одновременных
генераций хватало, чтобы сайт перестал отвечать всем остальным.

Договор с фронтендом при переносе НЕ изменился: он и раньше опрашивал
`GET /api/generations/<id>` до статуса success или failed.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from billing.pricing import total_renders
from billing.services import refund_job

from .models import GenerationJob
from .providers import get_provider

logger = logging.getLogger(__name__)


def _was_cancelled(job: GenerationJob) -> bool:
    """Bazadan o'qiymiz: bekor qilish flagini boshqa so'rov qo'yadi."""
    return GenerationJob.objects.filter(pk=job.pk, cancelled=True).exists()


def _ensure_page_copy(job: GenerationJob) -> None:
    """Ko'p sahifali to'plamda har sahifaning matni har xil bo'lishi kerak.

    Reja yo'q yoki eski (hamma sahifada bir xil) bo'lsa — yangisini yozamiz.
    Xato hech qachon generatsiyani to'xtatmaydi.
    """
    settings = dict(job.settings or {})
    pages = int(settings.get("pages") or 1)
    if pages <= 1 or settings.get("contentType", "card") not in ("card", "copyStyle") or settings.get("textMode") == "own":
        return
    try:
        from .page_copy import build_page_copy, plan_is_repetitive

        plan = settings.get("plan")
        if isinstance(plan, list) and len(plan) >= pages and not plan_is_repetitive(plan[:pages]):
            return
        brief = {"title": job.title, "subtitle": job.subtitle, "category": job.category, "benefits": job.benefits}
        settings["plan"] = build_page_copy(brief, settings, min(pages, 5), use_ai=True)
        job.settings = settings
        job.save(update_fields=["settings", "updated_at"])
    except Exception:  # noqa: BLE001 — matn rejasi generatsiyani buzmasligi kerak
        logger.exception("Sahifa matnini tayyorlab bo'lmadi")


def run_job(job: GenerationJob) -> None:
    """Собственно прогон. Вынесен из задачи, чтобы его можно было вызвать
    напрямую — из тестов и из команды перезапуска.

    ВАЖНО: при провале токены возвращаются. Раньше они просто сгорали —
    человек платил, картинку не получал, и это выглядело как обман.
    """
    job.status = "processing"
    job.progress = 40
    job.message = "AI generatsiya qilinmoqda"
    job.save(update_fields=["status", "progress", "message", "updated_at"])

    if job.content_type == "resize":
        from .resize import ResizeProvider

        provider = ResizeProvider()
        job.message = "Rasm 1080×1440 ga moslanmoqda"
        job.save(update_fields=["message", "updated_at"])
    else:
        provider = get_provider()
    total = total_renders(job.settings or {})
    _ensure_page_copy(job)

    try:
        results = []
        for index in range(total):
            # Отмену проверяем между картинками. Читаем из базы: флаг ставит
            # другой запрос, значение в памяти о нём не знает. Запрос, уже
            # ушедший в OpenAI, прервать нельзя — за него мы платим в любом
            # случае.
            if _was_cancelled(job):
                return
            results.append(provider.generate(job, index + 1, previous=results))
            # Rasm chizilayotganda bekor qilingan bo'lishi mumkin. Unda bekor qilish
            # so'rovi allaqachon holat, xabar va tokenlarni hal qilgan — ularni
            # eski (xotiradagi) qiymatlar bilan ustidan yozmaymiz.
            if _was_cancelled(job):
                return
            job.progress = min(96, 32 + round((index + 1) / total * 60))
            job.message = f"{index + 1}/{total} tayyorlanmoqda"
            job.save(update_fields=["progress", "message", "updated_at"])

        if _was_cancelled(job):
            return
        job.results = results
        job.status = "success"
        job.progress = 100
        job.message = "Tayyor"
        job.save(update_fields=["results", "status", "progress", "message", "updated_at"])
        return
    except Exception as error:  # включая GenerationError
        if GenerationJob.objects.filter(pk=job.pk, cancelled=True).exists():
            return
        logger.exception("Generatsiya bajarilmadi")
        job.status = "failed"
        job.progress = 100
        job.message = "Generatsiya bajarilmadi"
        job.error = str(error)[:300]
        job.save()

    # Возврат вне try: если упадёт сам возврат, статус задачи уже сохранён.
    if job.tokens_charged and not job.refunded:
        refund_job(job, job.tokens_charged, f"Generatsiya uchun qaytarish · {job.content_type}")
        job.refunded = True
        job.save(update_fields=["refunded", "updated_at"])


@shared_task(name="generation.run_job", ignore_result=True)
def run_generation_job(job_id: int) -> None:
    """Точка входа для очереди.

    Повторов нет намеренно (см. CELERY_TASK_ACKS_LATE в настройках): каждый
    повтор — ещё один платный вызов провайдера за одну оплаченную
    генерацию.
    """
    job = GenerationJob.objects.filter(pk=job_id).first()
    if job is None:
        # Проект удалили, пока задача ждала очереди. Это не ошибка.
        logger.info("Vazifa topilmadi, o‘tkazib yuborildi: job=%s", job_id)
        return

    # Защита от повторной доставки сообщения: заново прогонять задачу,
    # которая уже выполняется или выполнена, — значит платить дважды.
    if job.status != "queued":
        logger.warning("Vazifa allaqachon %s holatida, qayta ishga tushirilmadi: job=%s", job.status, job.pk)
        return

    if job.cancelled:
        return

    run_job(job)


def enqueue(job: GenerationJob) -> None:
    """Отправляет задачу в очередь.

    Без брокера (CELERY_TASK_ALWAYS_EAGER) выполнится сразу и вернётся
    только после завершения — ровно прежнее поведение.
    """
    run_generation_job.delay(job.pk)


def queue_enabled() -> bool:
    return not settings.CELERY_TASK_ALWAYS_EAGER
