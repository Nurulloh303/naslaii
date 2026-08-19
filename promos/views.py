from __future__ import annotations

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.serializers import AccountSerializer
from billing.services import credit
from config.throttling import PromoThrottle

from .models import Promo, PromoRedemption, normalize_code

REJECTION_MESSAGES = {
    "NOT_FOUND": "Bunday promokod topilmadi",
    "DISABLED": "Bu promokod o‘chirilgan",
    "EXPIRED": "Promokod muddati tugagan",
    "LIMIT_REACHED": "Promokod faollashtirishlar chegarasiga yetdi",
    "ALREADY_USED": "Siz bu promokodni allaqachon ishlatgansiz",
}


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PromoThrottle])
def redeem(request):
    """Активация промокода пользователем."""
    code = normalize_code(request.data.get("code"))
    promo = Promo.objects.filter(code=code).first()

    reason = "NOT_FOUND" if promo is None else promo.check_for(request.user)
    if reason:
        return Response(
            {"error": reason, "message": REJECTION_MESSAGES.get(reason, "Promokodni qabul qilib bo‘lmadi")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        # Всё в одной транзакции: начисление и отметка об активации не
        # должны разъезжаться, иначе токены выдадутся дважды.
        with transaction.atomic():
            promo = Promo.objects.select_for_update().get(pk=promo.pk)
            reason = promo.check_for(request.user)
            if reason:
                raise IntegrityError(reason)

            PromoRedemption.objects.create(promo=promo, user=request.user)
            promo.used += 1
            promo.save(update_fields=["used"])

            granted = 0
            percent = 0
            if promo.kind == "tokens":
                granted = promo.value
                credit(request.user, granted, f"Promokod {promo.code}")
            else:
                percent = promo.value
    except IntegrityError as error:
        reason = str(error) if str(error) in REJECTION_MESSAGES else "ALREADY_USED"
        return Response(
            {"error": reason, "message": REJECTION_MESSAGES[reason]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    request.user.refresh_from_db()
    return Response(
        {
            "ok": True,
            "kind": promo.kind,
            "granted": granted,
            "percent": percent,
            "balance": request.user.balance,
            # Снимок целиком: кабинет подставляет его сразу и показывает
            # новый баланс и новую строку истории без перезагрузки.
            "account": AccountSerializer(request.user).data,
        }
    )
