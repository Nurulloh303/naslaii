"""Тесты промокодов.

Проверяем не «работает ли вообще», а те места, где ошибка стоит денег:
повторная активация, лимит, срок, начисление ровно один раз.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User

from .models import Promo


class PromoRedeemTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="a@b.uz", email="a@b.uz", password="parol12345")
        self.other = User.objects.create_user(username="c@d.uz", email="c@d.uz", password="parol12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def redeem(self, code="TEST10", client=None):
        return (client or self.client).post("/api/promo/redeem", {"code": code}, format="json")

    def test_tokens_credited_once(self):
        Promo.objects.create(code="TEST10", kind="tokens", value=10)
        response = self.redeem()
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 10)

    def test_same_user_cannot_redeem_twice(self):
        Promo.objects.create(code="TEST10", kind="tokens", value=10)
        self.redeem()
        response = self.redeem()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "ALREADY_USED")
        self.user.refresh_from_db()
        # Главное: второй раз токены НЕ начислились.
        self.assertEqual(self.user.balance, 10)

    def test_limit_is_respected(self):
        Promo.objects.create(code="TEST10", kind="tokens", value=10, max_uses=1)
        self.redeem()

        other_client = APIClient()
        other_client.force_authenticate(self.other)
        response = self.redeem(client=other_client)
        self.assertEqual(response.data["error"], "LIMIT_REACHED")

    def test_expired_promo_rejected(self):
        Promo.objects.create(
            code="OLD10", kind="tokens", value=10,
            expires_at=timezone.now() - timedelta(days=1),
        )
        response = self.redeem("OLD10")
        self.assertEqual(response.data["error"], "EXPIRED")

    def test_disabled_promo_rejected(self):
        Promo.objects.create(code="OFF10", kind="tokens", value=10, active=False)
        self.assertEqual(self.redeem("OFF10").data["error"], "DISABLED")

    def test_unknown_code(self):
        self.assertEqual(self.redeem("NOPE").data["error"], "NOT_FOUND")

    def test_code_is_case_insensitive(self):
        Promo.objects.create(code="DILBAR10", kind="tokens", value=5)
        self.assertEqual(self.redeem("dilbar10").status_code, 200)
