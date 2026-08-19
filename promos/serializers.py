from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from .models import CODE_RE, Promo, normalize_code


class PromoSerializer(serializers.ModelSerializer):
    """Чтение: то, что видит админ в таблице промокодов."""

    lastUsedAt = serializers.DateTimeField(source="last_used_at", read_only=True)
    maxUses = serializers.IntegerField(source="max_uses", read_only=True)
    expiresAt = serializers.DateTimeField(source="expires_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Promo
        fields = ["code", "kind", "value", "used", "maxUses", "active", "owner", "note", "expiresAt", "createdAt", "lastUsedAt"]


class PromoWriteSerializer(serializers.ModelSerializer):
    """Запись: создание промокода админом.

    Проверки жёсткие — ошибка в промокоде стоит реальных денег.
    """

    maxUses = serializers.IntegerField(source="max_uses", required=False, min_value=0, max_value=100_000)
    expiresAt = serializers.DateTimeField(source="expires_at", required=False, allow_null=True)

    class Meta:
        model = Promo
        fields = ["code", "kind", "value", "maxUses", "active", "owner", "note", "expiresAt"]

    def validate_code(self, value: str) -> str:
        code = normalize_code(value)
        if not CODE_RE.match(code):
            raise serializers.ValidationError("Kod 3–24 ta harf yoki raqamdan iborat bo‘lsin")
        return code

    def validate_expiresAt(self, value):
        if value and value < timezone.now():
            raise serializers.ValidationError("Sana o‘tib ketgan")
        return value

    def validate(self, attrs):
        kind = attrs.get("kind", "tokens")
        value = attrs.get("value")
        if not value or value <= 0:
            raise serializers.ValidationError({"value": "Qiymat noldan katta bo‘lsin"})
        # Ограничения сверху — защита от опечатки в лишний ноль.
        if kind == "tokens" and value > 1000:
            raise serializers.ValidationError({"value": "1000 tadan ortiq token berib bo‘lmaydi"})
        if kind == "discount" and value > 90:
            raise serializers.ValidationError({"value": "Chegirma 90% dan oshmasin"})
        return attrs
