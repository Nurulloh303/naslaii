from __future__ import annotations

import hashlib
import os
import secrets

from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.db import transaction
from django.middleware.csrf import get_token
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from billing.pricing import STARTER_TOKENS
from config.throttling import LoginThrottle, RegisterThrottle

from .models import ApiKey, User
from .serializers import (
    AccountSerializer,
    ApiKeySerializer,
    LoginSerializer,
    ProfileSerializer,
    RegisterSerializer,
)
from .social import SocialAuthError, verify_google_id_token, verify_telegram_login

# Пароля у аккаунтов Google и Telegram нет, поэтому при входе нужно прямо
# указать, каким способом проверять пользователя дальше.
MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _issue(request, user: User, created: bool = False) -> Response:
    """Открываем сессию и отдаём снимок аккаунта.

    Два способа входа сразу:

    * cookie сессии — для браузера. Она httponly, поэтому чужой скрипт на
      странице её не прочитает;
    * токен DRF — для обращений из своего кода, заголовком
      `Authorization: Token <ключ>`.
    """
    django_login(request._request, user, backend=MODEL_BACKEND)
    # После входа Django выдаёт новый идентификатор сессии, значит и
    # значение CSRF нужно обновить — иначе следующий POST получит 403.
    get_token(request._request)

    token, _ = Token.objects.get_or_create(user=user)
    return Response(
        {"token": token.key, "account": AccountSerializer(user).data},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([RegisterThrottle])
def register(request):
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    return _issue(request, serializer.save(), created=True)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def login(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    return _issue(request, serializer.validated_data["user"])


@api_view(["POST"])
@permission_classes([AllowAny])
def logout(request):
    """Выход. Отвечаем телом, а не пустым 204: фронтенд читает JSON и на
    пустом ответе считает выход неудавшимся."""
    if request.user.is_authenticated:
        Token.objects.filter(user=request.user).delete()
        django_logout(request._request)
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([AllowAny])
def session(request):
    """Кто вошёл. Вызывается при загрузке страницы.

    Отвечает 200 и для гостя: «не вошёл» — это не ошибка, а обычное
    состояние, и показывать из-за него красное сообщение не нужно.

    Заодно ставит cookie csrftoken — без неё первый же POST получит 403.
    """
    get_token(request._request)
    if not request.user.is_authenticated:
        return Response({"authenticated": False, "account": None})
    return Response({"authenticated": True, "account": AccountSerializer(request.user).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(AccountSerializer(request.user).data)


@api_view(["PATCH", "PUT"])
@permission_classes([IsAuthenticated])
def profile(request):
    """Изменение имени, почты и языка."""
    serializer = ProfileSerializer(request.user, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    request.user.refresh_from_db()
    return Response(AccountSerializer(request.user).data)


# ---------------------------------------------------- вход через провайдера

@api_view(["GET"])
@permission_classes([AllowAny])
def providers(request):
    """Фронтенд отсюда узнаёт, какие кнопки показывать."""
    google_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    bot_name = os.environ.get("TELEGRAM_BOT_NAME", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    # Страница входа обычно первая, поэтому cookie csrftoken ставим и здесь.
    get_token(request._request)
    return Response(
        {
            "google": bool(google_id),
            "googleClientId": google_id or None,
            "telegram": bool(bot_name and bot_token),
            "telegramBot": bot_name or None,
        }
    )


@transaction.atomic
def _login_with_provider(profile: dict) -> tuple[User, bool]:
    """Три случая: уже привязан, есть аккаунт с той же почтой, новый человек.

    Второй случай важен: без него у одного человека появятся два аккаунта
    с двумя разными балансами.
    """
    field = f"{profile['provider']}_id"
    existing = User.objects.filter(**{field: profile["provider_id"]}).first()
    if existing:
        return existing, False

    if profile.get("email"):
        by_email = User.objects.filter(email__iexact=profile["email"]).first()
        if by_email:
            setattr(by_email, field, profile["provider_id"])
            by_email.save(update_fields=[field])
            return by_email, False

    # Telegram почту не отдаёт — делаем внутренний адрес, он нигде не виден.
    email = profile.get("email") or f"{profile['provider']}.{profile['provider_id']}@users.naslai.uz"
    user = User(
        username=email,
        email=email,
        name=profile["name"],
        balance=STARTER_TOKENS,
        **{field: profile["provider_id"]},
    )
    # Пароля нет: войти по паролю в такой аккаунт нельзя.
    user.set_unusable_password()
    user.save()
    return user, True


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def google_login(request):
    try:
        verified = verify_google_id_token(request.data.get("credential"))
    except SocialAuthError as error:
        return Response(
            {"error": "GOOGLE_LOGIN_FAILED", "message": "Google orqali kirishni tasdiqlab bo‘lmadi", "reason": error.reason},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    user, created = _login_with_provider(verified)
    return _issue(request, user, created)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def telegram_login(request):
    try:
        verified = verify_telegram_login(request.data)
    except SocialAuthError as error:
        return Response(
            {"error": "TELEGRAM_LOGIN_FAILED", "message": "Telegram orqali kirishni tasdiqlab bo‘lmadi", "reason": error.reason},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    user, created = _login_with_provider(verified)
    return _issue(request, user, created)


# ------------------------------------------------------------- API-ключи

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def api_keys(request):
    limit = max(1, int(os.environ.get("MAX_API_KEYS_PER_USER", 3)))

    if request.method == "GET":
        return Response({"keys": ApiKeySerializer(request.user.api_keys.all(), many=True).data, "limit": limit})

    if request.user.api_keys.count() >= limit:
        return Response(
            {
                "error": "API_KEY_LIMIT",
                "message": f"Ko‘pi bilan {limit} ta faol kalit bo‘lishi mumkin. Yangisini yaratish uchun eskisini bekor qiling.",
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    secret = f"nsk_live_{secrets.token_urlsafe(24)}"
    # Нумерация сквозная: после удаления ключа имена не должны повторяться,
    # иначе в списке окажутся два «Kalit 2» и их не различить.
    next_number = request.user.api_keys.count() + 1
    while request.user.api_keys.filter(name=f"Kalit {next_number}").exists():
        next_number += 1

    key = ApiKey.objects.create(
        user=request.user,
        name=f"Kalit {next_number}",
        prefix=secret[:18],
        digest=hashlib.sha256(secret.encode()).hexdigest(),
    )
    return Response({"key": ApiKeySerializer(key).data, "secret": secret}, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def revoke_api_key(request, key_id: int):
    deleted, _ = ApiKey.objects.filter(pk=key_id, user=request.user).delete()
    if not deleted:
        return Response({"error": "KEY_NOT_FOUND", "message": "Kalit topilmadi"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"ok": True})
