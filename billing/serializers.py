from rest_framework import serializers

from .models import LedgerEntry


def ledger_entry_payload(entry: LedgerEntry) -> dict:
    """Одна операция в том виде, в каком её ждёт кабинет.

    Ключи именно camelCase: фронтенд читает `balanceAfter` и `createdAt`,
    и на snake_case показывал пустые значения без всякой ошибки.
    """
    return {
        "id": str(entry.pk),
        # Номер операции для поддержки: по нему находят запись в базе.
        "operationId": f"OP-{entry.pk:06d}",
        "label": entry.label,
        "amount": entry.amount,
        "type": entry.type,
        "balanceAfter": entry.balance_after,
        "createdAt": entry.created_at.isoformat(),
    }


class LedgerEntrySerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    operationId = serializers.SerializerMethodField()
    balanceAfter = serializers.IntegerField(source="balance_after", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = LedgerEntry
        fields = ["id", "operationId", "type", "amount", "label", "balanceAfter", "createdAt"]

    def get_id(self, obj: LedgerEntry) -> str:
        return str(obj.pk)

    def get_operationId(self, obj: LedgerEntry) -> str:
        return f"OP-{obj.pk:06d}"


class QuoteRequestSerializer(serializers.Serializer):
    settings = serializers.DictField()
