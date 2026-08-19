import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models


def generate_support_code() -> str:
    return f"NSL-{secrets.token_hex(3).upper()}"


class User(AbstractUser):
    """Аккаунт Naslai."""

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=120, blank=True)
    balance = models.PositiveIntegerField(default=0)
    language = models.CharField(max_length=8, default="uz")
    support_code = models.CharField(max_length=32, unique=True, default=generate_support_code)

    # Привязка к внешним способам входа. Пустая строка = не привязан.
    # Индекс уникальный: один аккаунт Google не может вести к двум нашим.
    google_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    telegram_id = models.CharField(max_length=64, blank=True, default="", db_index=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["google_id"],
                condition=~models.Q(google_id=""),
                name="unique_google_id",
            ),
            models.UniqueConstraint(
                fields=["telegram_id"],
                condition=~models.Q(telegram_id=""),
                name="unique_telegram_id",
            ),
        ]

    def __str__(self) -> str:
        return self.email

    @property
    def providers(self) -> list[str]:
        return [name for name, value in (("google", self.google_id), ("telegram", self.telegram_id)) if value]


class ApiKey(models.Model):
    """Ключ для доступа к генерации из своего сервиса.

    Хранится только хэш: показать ключ повторно нельзя, и утечка базы
    не даёт доступа к чужим генерациям.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=64)
    prefix = models.CharField(max_length=24)
    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.user_id}: {self.name}"
