from datetime import timedelta

from django.conf import settings as django_settings
from django.db import models
from django.utils import timezone

EXPORT_RETENTION_DAYS = 90


def default_expires_at():
    return timezone.now() + timedelta(days=EXPORT_RETENTION_DAYS)


class UzumExport(models.Model):
    """Uzum "ommaviy tovar yaratish" uchun tayyorlangan XLSM fayl.

    ``draft`` — eksport paytidagi o'zgarmas snapshot (rasmlar data URL'siz).
    Fayl shaxsiy saqlashda (``UZUM_EXPORT_ROOT``) — narx va sotuvchi
    ma'lumotlari bor, ommaviy emas. Rasmlar esa ommaviy (``photo_paths``).
    """

    STATUS_CHOICES = [("ready", "ready"), ("failed", "failed")]

    user = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="uzum_exports")
    job = models.ForeignKey(
        "generation.GenerationJob", on_delete=models.SET_NULL, null=True, blank=True, related_name="uzum_exports",
    )
    category_id = models.PositiveIntegerField()
    category_path = models.CharField(max_length=500, blank=True, default="")
    template_sha = models.CharField(max_length=64)
    draft = models.JSONField(default=dict)
    sku_count = models.PositiveSmallIntegerField(default=0)
    file_path = models.CharField(max_length=300, blank=True, default="")
    photo_paths = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="ready")
    errors = models.JSONField(default=list, blank=True)
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    draft_hash = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_expires_at)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="unique_uzum_export_idempotency_key",
            )
        ]

    def __str__(self) -> str:
        return f"uzum-export#{self.pk} cat={self.category_id} {self.status}"

    @property
    def file_name(self) -> str:
        return f"naslai-uzum-{self.pk}.xlsm"
