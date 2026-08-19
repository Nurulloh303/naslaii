from django.conf import settings as django_settings
from django.db import models


class LedgerEntry(models.Model):
    CREDIT = "credit"
    DEBIT = "debit"
    TYPE_CHOICES = [(CREDIT, "Credit"), (DEBIT, "Debit")]

    user = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ledger_entries")
    type = models.CharField(max_length=8, choices=TYPE_CHOICES)
    amount = models.PositiveIntegerField()
    label = models.CharField(max_length=200)
    balance_after = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user_id} {self.type} {self.amount} -> {self.balance_after}"
