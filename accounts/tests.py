"""Тесты входа и регистрации.

Отдельно проверяем подпись Telegram: если её сломать, войти сможет кто
угодно под любым аккаунтом.
"""

import hashlib
import hmac
import time
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from .models import User
from .social import SocialAuthError, verify_telegram_login

BOT_TOKEN = "123456:TEST-BOT-TOKEN"


def sign(fields: dict, token: str = BOT_TOKEN) -> str:
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()


class TelegramSignatureTests(TestCase):
    def setUp(self):
        self.fields = {"id": "99001122", "first_name": "Rasul", "auth_date": int(time.time())}

    def test_valid_signature_accepted(self):
        payload = {**self.fields, "hash": sign(self.fields)}
        profile = verify_telegram_login(payload, BOT_TOKEN)
        self.assertEqual(profile["provider_id"], "99001122")

    def test_forged_signature_rejected(self):
        with self.assertRaises(SocialAuthError) as context:
            verify_telegram_login({**self.fields, "hash": "a" * 64}, BOT_TOKEN)
        self.assertEqual(context.exception.reason, "BAD_SIGNATURE")

    def test_tampered_id_rejected(self):
        """Подпись настоящая, но id подменили — это должно отвалиться."""
        payload = {**self.fields, "hash": sign(self.fields), "id": "777"}
        with self.assertRaises(SocialAuthError) as context:
            verify_telegram_login(payload, BOT_TOKEN)
        self.assertEqual(context.exception.reason, "BAD_SIGNATURE")

    def test_stale_login_rejected(self):
        old = {**self.fields, "auth_date": int(time.time()) - 60 * 60 * 48}
        with self.assertRaises(SocialAuthError) as context:
            verify_telegram_login({**old, "hash": sign(old)}, BOT_TOKEN)
        self.assertEqual(context.exception.reason, "EXPIRED")

    def test_no_bot_token_configured(self):
        with self.assertRaises(SocialAuthError) as context:
            verify_telegram_login({**self.fields, "hash": sign(self.fields)}, "")
        self.assertEqual(context.exception.reason, "TELEGRAM_NOT_CONFIGURED")


class RegistrationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @mock.patch("accounts.serializers.email_domain_accepts_mail", return_value=True)
    def test_email_signup_gets_no_free_tokens(self, _mx):
        """Регистрация по почте не даёт бесплатных токенов.

        Именно через одноразовые ящики накручивали бесплатные генерации.
        """
        response = self.client.post(
            "/api/auth/register",
            {"name": "Fan", "email": "fan@example.com", "password": "parol12345"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["account"]["balance"], 0)

    @mock.patch("accounts.serializers.email_domain_accepts_mail", return_value=False)
    def test_fake_email_domain_rejected(self, _mx):
        response = self.client.post(
            "/api/auth/register",
            {"name": "Fan", "email": "dkemqmdq@dmwdqw.uz", "password": "parol12345"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @mock.patch("accounts.serializers.email_domain_accepts_mail", return_value=True)
    def test_duplicate_email_rejected(self, _mx):
        User.objects.create_user(username="a@b.uz", email="a@b.uz", password="parol12345")
        response = self.client.post(
            "/api/auth/register",
            {"name": "A", "email": "a@b.uz", "password": "parol12345"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
