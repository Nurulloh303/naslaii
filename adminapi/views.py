"""API админ-панели.

Доступ по списку почт в ADMIN_EMAILS. Постороннему отвечаем «не найдено»,
а не «нет доступа»: он не должен даже узнать, что раздел существует.

Порт блока /api/admin/* из server/index.mjs.
"""

from __future__ import annotations

import os
from datetime import timedelta

from django.db.models import Count, Sum
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import User
from billing.pricing import COST_PER_IMAGE_UZS, token_packs
from billing.services import credit, debit
from generation.models import GenerationJob
from promos.models import Promo, normalize_code
from promos.serializers import PromoSerializer, PromoWriteSerializer


def admin_emails() -> set[str]:
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def require_admin(request):
    """Пустой список = админка закрыта для всех. Это безопасное значение."""
    if str(request.user.email or "").lower() not in admin_emails():
        raise Http404


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def overview(request):
    require_admin(request)
    day_ago = timezone.now() - timedelta(days=1)
    jobs = GenerationJob.objects.all()

    users = User.objects.all()
    packs = token_packs()

    tokens_in_circulation = users.aggregate(total=Sum("balance"))["total"] or 0

    return Response(
        {
            "users": {
                "total": users.count(),
                "withGoogle": users.filter(google_id__gt="").count(),
                "withTelegram": users.filter(telegram_id__gt="").count(),
                "withBalance": users.filter(balance__gt=0).count(),
            },
            "tokens": {
                "inCirculation": tokens_in_circulation,
                "spent": jobs.aggregate(total=Sum("tokens_charged"))["total"] or 0,
            },
            "jobs": {
                "total": jobs.count(),
                "lastDay": jobs.filter(created_at__gte=day_ago).count(),
                "failed": jobs.filter(status="failed").count(),
            },
            "promos": {
                "total": Promo.objects.count(),
                "active": Promo.objects.filter(active=True).count(),
                "redemptions": Promo.objects.aggregate(total=Sum("used"))["total"] or 0,
            },
            # Платежи не подключены, заказов нет. Ноль здесь честнее, чем
            # отсутствующее поле: без него плитка в админке пустая.
            "orders": 0,
            "costs": {
                "perImageUzs": round(COST_PER_IMAGE_UZS),
                "tokenPriceUzs": packs[0]["perToken"],
                # Токены на руках — это наше обязательство: люди уже
                # заплатили, а картинки ещё не сгенерировали.
                "futureApiCostUzs": round(tokens_in_circulation * COST_PER_IMAGE_UZS),
            },
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def users_list(request):
    require_admin(request)
    query = str(request.query_params.get("q", "")).strip().lower()
    users = User.objects.annotate(jobs_count=Count("jobs"))
    if query:
        users = users.filter(email__icontains=query) | users.filter(name__icontains=query)

    return Response(
        {
            "total": User.objects.count(),
            "users": [
                {
                    # Строкой: в интерфейсе id подставляется в адрес и
                    # сравнивается со строками.
                    "id": str(user.pk),
                    "email": user.email,
                    "name": user.name,
                    "balance": user.balance,
                    "providers": [p for p in (("google" if user.google_id else None), ("telegram" if user.telegram_id else None)) if p],
                    "jobs": user.jobs_count,
                    # Платежи не подключены — заказов нет ни у кого.
                    "orders": 0,
                    "supportCode": user.support_code,
                }
                for user in users.order_by("-id")[:200]
            ],
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def adjust_balance(request, user_id: int):
    require_admin(request)
    target = User.objects.filter(pk=user_id).first()
    if not target:
        return Response({"error": "USER_NOT_FOUND", "message": "Foydalanuvchi topilmadi"}, status=status.HTTP_404_NOT_FOUND)

    try:
        amount = int(request.data.get("amount"))
    except (TypeError, ValueError):
        return Response({"error": "BAD_AMOUNT", "message": "Miqdor son bo‘lsin"}, status=status.HTTP_400_BAD_REQUEST)

    if amount == 0 or abs(amount) > 10_000:
        return Response({"error": "BAD_AMOUNT", "message": "Miqdor 1 dan 10000 gacha bo‘lsin"}, status=status.HTTP_400_BAD_REQUEST)

    reason = str(request.data.get("reason") or "").strip()[:80]
    if amount > 0:
        credit(target, amount, reason or "Admin tomonidan qo‘shildi")
        applied = amount
    else:
        # В минус баланс не уводим: с отрицательным балансом остальная
        # система считает неправильно.
        applied = -min(target.balance, abs(amount))
        if applied:
            debit(target, abs(applied), reason or "Admin tomonidan yechildi")

    target.refresh_from_db()
    return Response({"ok": True, "balance": target.balance, "applied": applied})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def promos_collection(request):
    require_admin(request)

    if request.method == "GET":
        return Response({"promos": PromoSerializer(Promo.objects.all(), many=True).data})

    serializer = PromoWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    code = serializer.validated_data["code"]
    if Promo.objects.filter(code=code).exists():
        return Response({"error": "CODE_EXISTS", "message": "Bunday kod allaqachon bor"}, status=status.HTTP_409_CONFLICT)
    promo = serializer.save()
    return Response({"promo": PromoSerializer(promo).data}, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def promo_detail(request, code: str):
    require_admin(request)
    promo = Promo.objects.filter(code=normalize_code(code)).first()
    if not promo:
        return Response({"error": "PROMO_NOT_FOUND", "message": "Promokod topilmadi"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        data = PromoSerializer(promo).data
        data["redemptions"] = [
            {"email": item.user.email, "at": item.created_at}
            for item in promo.redemptions.select_related("user")[:100]
        ]
        return Response({"promo": data})

    if request.method == "PATCH":
        if "active" in request.data:
            promo.active = bool(request.data["active"])
        if "note" in request.data:
            promo.note = str(request.data["note"])[:200]
        promo.save(update_fields=["active", "note"])
        return Response({"promo": PromoSerializer(promo).data})

    # Активированный код не удаляем: он часть истории и на нём держится
    # статистика блогера. Только выключаем.
    if promo.used > 0:
        promo.active = False
        promo.save(update_fields=["active"])
        return Response({"ok": True, "disabledInsteadOfDeleted": True, "promo": PromoSerializer(promo).data})

    promo.delete()
    return Response({"ok": True, "disabledInsteadOfDeleted": False})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def generations(request):
    require_admin(request)
    jobs = GenerationJob.objects.select_related("user").order_by("-created_at")[:100]
    return Response(
        {
            "generations": [
                {
                    "id": str(job.pk),
                    "email": job.user.email,
                    "status": job.status,
                    "tokens": job.tokens_charged,
                    "title": job.title,
                    "contentType": job.content_type,
                    "createdAt": job.created_at,
                    "error": job.error or None,
                }
                for job in jobs
            ]
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def orders(request):
    """Заказы на оплату. Платежи не подключены — список всегда пуст.

    Эндпоинт нужен, чтобы вкладка админки не падала на 404: пустой список
    она показывает как «заказов нет».
    """
    require_admin(request)
    return Response({"orders": []})
