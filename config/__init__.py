# Приложение Celery поднимается вместе с Django: иначе @shared_task
# в generation/tasks.py не к чему привязаться, и задачи не найдутся.
from .celery import app as celery_app

__all__ = ("celery_app",)
