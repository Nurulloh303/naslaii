"""Очередь фоновых задач.

Зачем она вообще нужна. Одна картинка у OpenAI рисуется до трёх минут, а
пакет из пяти — до пятнадцати. Пока это делалось прямо в HTTP-запросе,
рабочий процесс gunicorn был занят всё это время: трое одновременно
генерирующих людей занимали все три процесса, и сайт замирал для
остальных.

Настройка целиком лежит в settings.py с префиксом CELERY_.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("naslai")

# Все параметры берём из настроек Django — второго места для конфигурации
# заводить не стоит, разъедутся.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Находит tasks.py во всех приложениях из INSTALLED_APPS.
app.autodiscover_tasks()
