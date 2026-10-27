from django.db import transaction

from accounts.models import ApiKey, ApiKeyLedger, User
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


# ------------------------------------------------------------ API kalit balansi

@transaction.atomic
def key_debit(key: ApiKey, amount: int, label: str) -> ApiKey:
    locked = ApiKey.objects.select_for_update().get(pk=key.pk)
    if locked.balance < amount:
        raise InsufficientBalance(f"API kalit tokenlar yetarli emas: kerak {amount}, mavjud {locked.balance}")
    locked.balance -= amount
    locked.save(update_fields=["balance"])
    ApiKeyLedger.objects.create(key=locked, type=ApiKeyLedger.DEBIT, amount=amount, label=label, balance_after=locked.balance)
    return locked


@transaction.atomic
def key_credit(key: ApiKey, amount: int, label: str) -> ApiKey:
    locked = ApiKey.objects.select_for_update().get(pk=key.pk)
    locked.balance += amount
    locked.save(update_fields=["balance"])
    ApiKeyLedger.objects.create(key=locked, type=ApiKeyLedger.CREDIT, amount=amount, label=label, balance_after=locked.balance)
    return locked


def refund_job(job, amount: int, label: str) -> None:
    """Generatsiya uchun qaytarish — token qayerdan yechilgan bo'lsa, o'sha yerga.

    Kalit o'chirilgan bo'lsa (api_key=NULL bo'lib qoladi), lekin vazifa
    kalit orqali yaratilgan bo'lsa ham, foydalanuvchi balansiga qaytaramiz:
    tokenni yo'qotgandan ko'ra shu adolatliroq.
    """
    if not amount:
        return
    if job.api_key_id:
        key = ApiKey.objects.filter(pk=job.api_key_id).first()
        if key is not None:
            key_credit(key, amount, label)
            return
    refund(job.user, amount, label)

