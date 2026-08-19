from django.db import transaction

from accounts.models import User
from .models import LedgerEntry


class InsufficientBalance(Exception):
    pass


@transaction.atomic
def debit(user: User, amount: int, label: str) -> User:
    locked = User.objects.select_for_update().get(pk=user.pk)
    if locked.balance < amount:
        raise InsufficientBalance(f"Token yetarli emas: kerak {amount}, mavjud {locked.balance}")
    locked.balance -= amount
    locked.save(update_fields=["balance"])
    LedgerEntry.objects.create(user=locked, type=LedgerEntry.DEBIT, amount=amount, label=label, balance_after=locked.balance)
    return locked


@transaction.atomic
def credit(user: User, amount: int, label: str) -> User:
    locked = User.objects.select_for_update().get(pk=user.pk)
    locked.balance += amount
    locked.save(update_fields=["balance"])
    LedgerEntry.objects.create(user=locked, type=LedgerEntry.CREDIT, amount=amount, label=label, balance_after=locked.balance)
    return locked


@transaction.atomic
def refund(user: User, amount: int, label: str) -> User:
    return credit(user, amount, label)
