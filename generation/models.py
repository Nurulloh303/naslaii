from django.conf import settings as django_settings
from django.db import models

CONTENT_TYPE_CHOICES = [
    ("photo", "photo"),
    ("card", "card"),
    ("video", "video"),
    ("fashion", "fashion"),
    ("copyStyle", "copyStyle"),
    ("marketplacePackage", "marketplacePackage"),
]

STATUS_CHOICES = [
    ("queued", "queued"),
    ("processing", "processing"),
    ("success", "success"),
    ("failed", "failed"),
]


class GenerationJob(models.Model):
    user = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="jobs")
    content_type = models.CharField(max_length=32, choices=CONTENT_TYPE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="queued")
    progress = models.PositiveSmallIntegerField(default=0)
    message = models.CharField(max_length=200, blank=True, default="Vazifa navbatda")
    error = models.CharField(max_length=300, blank=True, default="")

    title = models.CharField(max_length=200, blank=True)
    subtitle = models.CharField(max_length=40, blank=True, default="")
    category = models.CharField(max_length=120, blank=True)
    benefits = models.JSONField(default=list, blank=True)

    settings = models.JSONField(default=dict)
    results = models.JSONField(default=list, blank=True)

    tokens_charged = models.PositiveIntegerField(default=0)
    # Отмена. Флаг читает цикл генерации между картинками: запрос, уже
    # ушедший в OpenAI, прервать нельзя — за него мы всё равно платим.
    cancelled = models.BooleanField(default=False)
    refunded = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=128, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="unique_idempotency_key_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"job#{self.pk} {self.content_type} {self.status}"
