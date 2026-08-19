"""Промокоды для рекламы у блогеров.

Каждому блогеру — свой код. В админке видно, кто сколько людей привёл,
и по этому можно решать, куда вкладывать рекламный бюджет.

Порт server/promos.mjs.
"""

from __future__ import annotations

import re

from django.conf import settings as django_settings
from django.db import models
from django.utils import timezone

KIND_CHOICES = [
    ("tokens", "Токены на счёт"),
    ("discount", "Скидка на покупку, %"),
]

CODE_RE = re.compile(r"^[A-Z0-9-]{3,24}$")


def normalize_code(value: str) -> str:
    """Регистр и пробелы не должны мешать: код диктуют голосом в сторис."""
    return re.sub(r"\s+", "", str(value or "").strip().upper())


class Promo(models.Model):
    code = models.CharField(max_length=24, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="tokens")
    value = models.PositiveIntegerField()
    # 0 — без ограничения. Здесь ограничиваем, сколько активаций отдали блогеру.
    max_uses = models.PositiveIntegerField(default=0)
    used = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    owner = models.CharField(max_length=80, blank=True, default="")
    note = models.CharField(max_length=200, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.code

    def save(self, *args, **kwargs):
        self.code = normalize_code(self.code)
        super().save(*args, **kwargs)

    # --- проверки ---------------------------------------------------------

    def check_for(self, user) -> str | None:
        """Причина отказа или None, если код можно применить."""
        if not self.active:
            return "DISABLED"
        if self.expires_at and self.expires_at < timezone.now():
            return "EXPIRED"
        if self.max_uses and self.used >= self.max_uses:
            return "LIMIT_REACHED"
        # Один человек — одна активация. Иначе код нажимают по кругу
        # и получают бесконечные токены.
        if self.redemptions.filter(user=user).exists():
            return "ALREADY_USED"
        return None

    @property
    def last_used_at(self):
        last = self.redemptions.order_by("-created_at").first()
        return last.created_at if last else None


class PromoRedemption(models.Model):
    promo = models.ForeignKey(Promo, on_delete=models.CASCADE, related_name="redemptions")
    user = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="promo_redemptions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Защита на уровне базы: даже при двух одновременных запросах
            # вторая активация не пройдёт.
            models.UniqueConstraint(fields=["promo", "user"], name="unique_promo_per_user"),
        ]

    def __str__(self) -> str:
        return f"{self.promo_id} → {self.user_id}"
