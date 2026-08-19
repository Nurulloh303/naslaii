from __future__ import annotations

import os
import socket

from django.contrib.auth import authenticate
from rest_framework import serializers

from billing.pricing import (
    STARTER_TOKENS_UNVERIFIED,
    public_pricing,
    token_packs,
)

from .models import ApiKey, User


def email_domain_accepts_mail(email: str) -> bool:
    """Проверяем, что домен почты вообще принимает письма.

    Формат `что-угодно@что-угодно.uz` проходит любую регулярку, поэтому
    выдуманный адрес вида dkemqmdq@dmwdqw.uz раньше спокойно
    регистрировался. У живого почтового домена есть MX-запись.

    При сбое DNS пропускаем: блокировать живого человека из-за проблем
    на нашей стороне хуже, чем пустить сомнительный адрес.
    """
    domain = str(email or "").split("@")[-1].strip().lower()
    if not domain or "." not in domain:
        return False

    if os.environ.get("SKIP_EMAIL_MX_CHECK") == "1":
        return True

    try:
        import dns.resolver  # type: ignore

        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        return len(answers) > 0
    except ImportError:
        # dnspython не установлен — обходимся проверкой, что домен вообще
        # существует. Слабее, но лучше, чем ничего.
        try:
            socket.getaddrinfo(domain, None)
            return True
        except socket.gaierror:
            return False
    except Exception as error:  # NXDOMAIN, NoAnswer, таймаут
        name = type(error).__name__
        if name in {"NXDOMAIN", "NoAnswer", "NoNameservers"}:
            return False
        return True


class RegisterSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, write_only=True)

    def validate_email(self, value: str) -> str:
        email = value.lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("Bu email allaqachon ro‘yxatdan o‘tgan.")
        if not email_domain_accepts_mail(email):
            raise serializers.ValidationError(
                "Bu pochta manzili mavjud emas. Haqiqiy email kiriting yoki Google/Telegram orqali kiring."
            )
        return email

    def create(self, validated_data) -> User:
        user = User(
            username=validated_data["email"],
            email=validated_data["email"],
            name=validated_data["name"],
            # Ноль намеренно: одноразовую почту делают за десять секунд,
            # и именно так накручивали бесплатные генерации.
            balance=STARTER_TOKENS_UNVERIFIED,
        )
        user.set_password(validated_data["password"])
        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = authenticate(username=attrs["email"].lower(), password=attrs["password"])
        if not user:
            raise serializers.ValidationError("Email yoki parol noto‘g‘ri.")
        attrs["user"] = user
        return attrs


class ProfileSerializer(serializers.ModelSerializer):
    """Правка своего профиля: имя, почта, язык интерфейса."""

    class Meta:
        model = User
        fields = ["name", "email", "language"]
        extra_kwargs = {
            "name": {"required": False},
            "email": {"required": False},
            "language": {"required": False},
        }

    def validate_email(self, value: str) -> str:
        email = str(value or "").strip().lower()
        if not email:
            raise serializers.ValidationError("Email bo‘sh bo‘lmasin.")
        if email == str(self.instance.email or "").lower():
            return email
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("Bu email allaqachon band.")
        if not email_domain_accepts_mail(email):
            raise serializers.ValidationError("Bu pochta manzili mavjud emas.")
        return email

    def validate_language(self, value: str) -> str:
        # Незнакомый код языка ломает подбор текстов на фронтенде.
        if value not in {"uz", "ru", "tg"}:
            raise serializers.ValidationError("Til noto‘g‘ri.")
        return value

    def update(self, instance: User, validated_data) -> User:
        email = validated_data.get("email")
        # username в этой модели равен почте — иначе останется старый
        # логин и вход по паролю перестанет находить аккаунт.
        if email and email != instance.email:
            instance.username = email
        return super().update(instance, validated_data)


class UserSerializer(serializers.ModelSerializer):
    # id строкой: фронтенд подставляет его в адреса и сравнивает со
    # строками. Число там иногда сравнивалось как "3" !== 3.
    id = serializers.SerializerMethodField()
    supportCode = serializers.CharField(source="support_code", read_only=True)

    class Meta:
        model = User
        fields = ["id", "name", "email", "language", "supportCode"]

    def get_id(self, obj: User) -> str:
        return str(obj.pk)


class ApiKeySerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = ApiKey
        fields = ["id", "name", "prefix", "createdAt"]


# Сколько последних операций кладём в снимок аккаунта. Вся история
# целиком отдаётся отдельным запросом GET /api/ledger.
LEDGER_IN_SNAPSHOT = 50
PROJECTS_IN_SNAPSHOT = 60


class AccountSerializer(serializers.ModelSerializer):
    """Полный снимок аккаунта — то, что читает фронтенд.

    Набор полей повторяет тип `AccountSnapshot` в
    `src/app/account/accountApi.ts`. Убираете поле здесь — соответствующий
    раздел кабинета падает на `undefined`, причём молча.
    """

    user = serializers.SerializerMethodField()
    pricing = serializers.SerializerMethodField()
    packs = serializers.SerializerMethodField()
    apiKeys = serializers.SerializerMethodField()
    apiKeyLimit = serializers.SerializerMethodField()
    isAdmin = serializers.SerializerMethodField()
    providers = serializers.ReadOnlyField()
    ledger = serializers.SerializerMethodField()
    projects = serializers.SerializerMethodField()
    referrals = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()
    orders = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "balance", "user", "pricing", "packs", "apiKeys", "apiKeyLimit",
            "isAdmin", "providers", "ledger", "projects", "referrals",
            "payments", "orders",
        ]

    def get_user(self, obj: User):
        return UserSerializer(obj).data

    def get_pricing(self, obj: User):
        return public_pricing()

    def get_packs(self, obj: User):
        return token_packs()

    def get_apiKeys(self, obj: User):
        return ApiKeySerializer(obj.api_keys.all(), many=True).data

    def get_apiKeyLimit(self, obj: User):
        return max(1, int(os.environ.get("MAX_API_KEYS_PER_USER", 3)))

    def get_isAdmin(self, obj: User) -> bool:
        # Флаг только для отрисовки меню. Настоящая проверка — на каждом
        # запросе к /api/admin/*, фронтенду доверять нельзя.
        raw = os.environ.get("ADMIN_EMAILS", "")
        allowed = {item.strip().lower() for item in raw.split(",") if item.strip()}
        return str(obj.email or "").lower() in allowed

    def get_ledger(self, obj: User):
        from billing.serializers import ledger_entry_payload

        return [ledger_entry_payload(entry) for entry in obj.ledger_entries.all()[:LEDGER_IN_SNAPSHOT]]

    def get_projects(self, obj: User):
        from generation.serializers import project_payload

        return [project_payload(job) for job in obj.jobs.all()[:PROJECTS_IN_SNAPSHOT]]

    def get_referrals(self, obj: User):
        # Реферальной программы пока нет: считать некому и не из чего.
        # Возвращаем нули честно — так в кабинете видно «0 приглашённых»,
        # а не выдуманное число. Код берём из support_code: он уже
        # уникален и человеку его всё равно диктовать.
        return {"invited": 0, "credited": 0, "level": 1, "code": obj.support_code}

    def get_payments(self, obj: User):
        # Пока false. Включать только вместе с обработчиком уведомлений
        # от Click/Payme: токены нельзя начислять до подтверждения оплаты.
        enabled = os.environ.get("PAYMENTS_ENABLED", "0") == "1"
        raw = os.environ.get("PAYMENT_PROVIDERS", "click,payme")
        providers = [p.strip() for p in raw.split(",") if p.strip() in {"click", "payme"}]
        return {"enabled": enabled, "providers": providers if enabled else []}

    def get_orders(self, obj: User):
        # Заказов не храним, пока не подключены платежи.
        return []
