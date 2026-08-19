from django.conf import settings as django_settings
from django.db import models


class AuditRun(models.Model):
    """Запись о запуске аудита.

    Нужна для дневного лимита бесплатных проверок и для того, чтобы отчёт
    не пропадал при обновлении страницы. Храним ТОЛЬКО текст отчёта —
    фотографии товара здесь не сохраняются.
    """

    user = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="audit_runs")
    link = models.CharField(max_length=300, blank=True, default="")
    report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self) -> str:
        return f"audit#{self.pk} user={self.user_id}"
