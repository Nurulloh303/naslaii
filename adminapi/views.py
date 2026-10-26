"""API админ-панели.

Доступ по списку почт в ADMIN_EMAILS. Постороннему отвечаем «не найдено»,
а не «нет доступа»: он не должен даже узнать, что раздел существует.

Порт блока /api/admin/* из server/index.mjs.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta

from django.db.models import Count, Sum
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import ApiKey, User
from billing.pricing import COST_PER_IMAGE_UZS, public_tariffs, tariff_by_id, token_packs
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
                # Сколько кодов создано по каждому тарифу (свободные — "custom").
                "byTariff": {
                    (row["tariff"] or "custom"): row["n"]
                    for row in Promo.objects.values("tariff").annotate(n=Count("id"))
                },
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

    reason = str(request.data.get("reason") or "").strip()[:80]
    tariff_id = str(request.data.get("tariff") or "").strip().lower()
    if tariff_id and tariff_id != "custom":
        # По тарифу: сумму берём из тарифа, присланный amount игнорируем.
        tariff = tariff_by_id(tariff_id)
        if not tariff:
            return Response({"error": "BAD_TARIFF", "message": "Bunday tarif yo‘q"}, status=status.HTTP_400_BAD_REQUEST)
        amount = tariff["tokens"]
        reason = reason or f"Tarif: {tariff['name']}"
    else:
        try:
            amount = int(request.data.get("amount"))
        except (TypeError, ValueError):
            return Response({"error": "BAD_AMOUNT", "message": "Miqdor son bo‘lsin"}, status=status.HTTP_400_BAD_REQUEST)

        if amount == 0 or abs(amount) > 10_000:
            return Response({"error": "BAD_AMOUNT", "message": "Miqdor 1 dan 10000 gacha bo‘lsin"}, status=status.HTTP_400_BAD_REQUEST)

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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tariffs(request):
    """Тарифы, по которым админ выдаёт токены (промокодом или начислением)."""
    require_admin(request)
    return Response({"tariffs": public_tariffs()})


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


# ================================================================ ApiKey admin

def _api_key_payload(key: ApiKey) -> dict:
    return {
        "id": key.pk,
        "name": key.name,
        "prefix": key.prefix,
        "balance": key.balance,
        "note": key.note,
        "createdAt": key.created_at,
        "lastUsedAt": key.last_used_at,
        "userEmail": key.user.email,
        "userId": key.user_id,
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def api_keys_collection(request):
    """Admin: barcha ApiKey larni ko'rish yoki yangi kalit yaratish.

    POST body: { "userId": 42, "name": "Sarvar uchun", "note": "...", "initialBalance": 10 }
    """
    require_admin(request)

    if request.method == "GET":
        keys = ApiKey.objects.select_related("user").order_by("-created_at")[:200]
        return Response({"keys": [_api_key_payload(k) for k in keys]})

    # POST — yangi kalit yaratish
    user_id = request.data.get("userId")
    target = User.objects.filter(pk=user_id).first() if user_id else None
    if not target:
        return Response({"error": "USER_NOT_FOUND", "message": "Foydalanuvchi topilmadi"}, status=404)

    name = str(request.data.get("name") or "").strip()[:64]
    if not name:
        name = f"API kalit {target.api_keys.count() + 1}"

    try:
        initial_balance = max(0, int(request.data.get("initialBalance") or 0))
    except (TypeError, ValueError):
        initial_balance = 0

    note = str(request.data.get("note") or "").strip()[:200]

    secret = f"nsk_live_{secrets.token_urlsafe(24)}"
    key = ApiKey.objects.create(
        user=target,
        name=name,
        prefix=secret[:18],
        digest=hashlib.sha256(secret.encode()).hexdigest(),
        balance=initial_balance,
        note=note,
    )
    return Response(
        {"key": _api_key_payload(key), "secret": secret},
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def api_key_detail(request, key_id: int):
    """Admin: ApiKey ni ko'rish, credit qo'shish yoki o'chirish.

    POST body credit uchun: { "amount": 50, "note": "Birinchi to'lov" }
    """
    require_admin(request)
    key = ApiKey.objects.select_related("user").filter(pk=key_id).first()
    if not key:
        return Response({"error": "KEY_NOT_FOUND", "message": "API kalit topilmadi"}, status=404)

    if request.method == "GET":
        return Response({"key": _api_key_payload(key)})

    if request.method == "DELETE":
        key.delete()
        return Response({"ok": True})

    # POST — credit qo'shish
    try:
        amount = int(request.data.get("amount"))
    except (TypeError, ValueError):
        return Response({"error": "BAD_AMOUNT", "message": "Miqdor son bo'lsin"}, status=400)

    if amount <= 0 or amount > 100_000:
        return Response({"error": "BAD_AMOUNT", "message": "Miqdor 1 dan 100000 gacha bo'lsin"}, status=400)

    from django.db import transaction
    with transaction.atomic():
        locked = ApiKey.objects.select_for_update().get(pk=key.pk)
        locked.balance += amount
        locked.save(update_fields=["balance"])

    locked.refresh_from_db()
    return Response({"ok": True, "balance": locked.balance, "added": amount})
