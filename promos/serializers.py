from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from billing.pricing import tariff_by_id

from .models import CODE_RE, Promo, normalize_code


class PromoSerializer(serializers.ModelSerializer):
    """Чтение: то, что видит админ в таблице промокодов."""

    lastUsedAt = serializers.DateTimeField(source="last_used_at", read_only=True)
    maxUses = serializers.IntegerField(source="max_uses", read_only=True)
    expiresAt = serializers.DateTimeField(source="expires_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    tariffName = serializers.SerializerMethodField()

    class Meta:
        model = Promo
        fields = ["code", "kind", "value", "used", "maxUses", "active", "owner", "note", "expiresAt", "createdAt", "lastUsedAt", "tariff", "tariffName"]

    def get_tariffName(self, promo) -> str:
        if not promo.tariff:
            return ""
        tariff = tariff_by_id(promo.tariff)
        # Тариф могли убрать из окружения — показываем хотя бы id.
        return tariff["name"] if tariff else promo.tariff


class PromoWriteSerializer(serializers.ModelSerializer):
    """Запись: создание промокода админом.

    Проверки жёсткие — ошибка в промокоде стоит реальных денег.
    """

    maxUses = serializers.IntegerField(source="max_uses", required=False, min_value=0, max_value=100_000)
    expiresAt = serializers.DateTimeField(source="expires_at", required=False, allow_null=True)
    # «custom» или пусто — свободный код. Иначе id тарифа: тогда kind и
    # value берутся из тарифа, присланное клиентом value игнорируется.
    tariff = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=32)
    value = serializers.IntegerField(required=False, min_value=0)

    class Meta:
        model = Promo
        fields = ["code", "kind", "value", "maxUses", "active", "owner", "note", "expiresAt", "tariff"]

    def validate_code(self, value: str) -> str:
        code = normalize_code(value)
        if not CODE_RE.match(code):
            raise serializers.ValidationError("Kod 3–24 ta harf yoki raqamdan iborat bo‘lsin")
        return code

    def validate_expiresAt(self, value):
        if value and value < timezone.now():
            raise serializers.ValidationError("Sana o‘tib ketgan")
        return value

    def validate_tariff(self, value):
        key = str(value or "").strip().lower()
        if key in ("", "custom"):
            return ""
        if not tariff_by_id(key):
            raise serializers.ValidationError("Bunday tarif yo‘q")
        return key

    def validate(self, attrs):
        tariff_id = attrs.get("tariff") or ""
        attrs["tariff"] = tariff_id
        if tariff_id:
            tariff = tariff_by_id(tariff_id)
            attrs["kind"] = "tokens"
            attrs["value"] = tariff["tokens"]
            return attrs

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
