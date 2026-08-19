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
from billing.services import refund

from .models import GenerationJob
from .providers import get_provider

logger = logging.getLogger(__name__)


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

    provider = get_provider()
    total = total_renders(job.settings or {})

    try:
        results = []
        for index in range(total):
            # Отмену проверяем между картинками. Читаем из базы: флаг ставит
            # другой запрос, значение в памяти о нём не знает. Запрос, уже
            # ушедший в OpenAI, прервать нельзя — за него мы платим в любом
            # случае.
            if GenerationJob.objects.filter(pk=job.pk, cancelled=True).exists():
                job.results = results
                job.save(update_fields=["results", "updated_at"])
                return
            results.append(provider.generate(job, index + 1))
            job.progress = min(96, 32 + round((index + 1) / total * 60))
            job.message = f"{index + 1}/{total} tayyorlanmoqda"
            job.save(update_fields=["progress", "message", "updated_at"])

        job.results = results
        job.status = "success"
        job.progress = 100
        job.message = "Tayyor"
        job.save()
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
        refund(job.user, job.tokens_charged, f"Generatsiya uchun qaytarish · {job.content_type}")
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
