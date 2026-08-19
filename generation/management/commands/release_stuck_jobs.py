"""Освобождает зависшие генерации и возвращает за них токены.

Зачем это нужно. С очередью появился отказ, которого не было при
синхронной работе: воркер может умереть посреди задачи — kill -9, нехватка
памяти, перезагрузка сервера. Сообщение к тому моменту уже подтверждено
(CELERY_TASK_ACKS_LATE = False — иначе задача вернулась бы в очередь и мы
заплатили бы провайдеру второй раз за одну оплаченную генерацию).

Итог: задача навсегда остаётся в состоянии queued или processing. Человек
списал токены и не получил ничего, а интерфейс бесконечно показывает
«генерируется». Эта команда доводит такие задачи до failed и возвращает
токены.

Запускать по расписанию, раз в 10–15 минут (systemd timer или cron):

    python manage.py release_stuck_jobs

Порог берётся из STUCK_JOB_MINUTES (по умолчанию 30 минут) и должен быть
заметно больше самой долгой честной генерации — пакет из пяти картинок при
медленном провайдере идёт до пятнадцати минут.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from billing.services import refund
from generation.models import GenerationJob

RUNNING_STATUSES = ["queued", "processing"]


class Command(BaseCommand):
    help = "Зависшие генерации переводит в failed и возвращает токены владельцу."

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes",
            type=int,
            default=settings.STUCK_JOB_MINUTES,
            help="Через сколько минут считать задачу зависшей.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только показать, ничего не менять.",
        )

    def handle(self, *args, **options):
        minutes = max(1, options["minutes"])
        dry_run = options["dry_run"]
        deadline = timezone.now() - timedelta(minutes=minutes)

        # updated_at, а не created_at: работающая задача обновляет прогресс
        # после каждой картинки, и живой длинный пакет мы не тронем.
        stuck = GenerationJob.objects.filter(status__in=RUNNING_STATUSES, updated_at__lt=deadline)

        released = 0
        refunded_tokens = 0

        for job in stuck.select_related("user"):
            self.stdout.write(
                f"job#{job.pk} user={job.user_id} status={job.status} "
                f"tokens={job.tokens_charged} updated={job.updated_at:%Y-%m-%d %H:%M}"
            )
            if dry_run:
                released += 1
                continue

            if job.tokens_charged and not job.refunded:
                refund(job.user, job.tokens_charged, f"Bajarilmagan generatsiya uchun qaytarish · {job.content_type}")
                job.refunded = True
                refunded_tokens += job.tokens_charged

            job.status = "failed"
            job.progress = 100
            job.message = "Vazifa bajarilmadi"
            job.error = "STUCK_JOB_RELEASED"
            job.save()
            released += 1

        if dry_run:
            self.stdout.write(self.style.WARNING(f"dry-run: {released} ta vazifa topildi, hech narsa o‘zgartirilmadi"))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"{released} ta vazifa yopildi, {refunded_tokens} token qaytarildi")
            )
