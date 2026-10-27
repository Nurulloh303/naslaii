"""Тесты выдачи токенов по тарифам из админки."""

import os
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from billing.models import LedgerEntry
from billing.pricing import TOKENS_PER_IMAGE, tariff_by_id
from promos.models import Promo


@mock.patch.dict(os.environ, {"ADMIN_EMAILS": "admin@naslai.uz"})
class TariffAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin@naslai.uz", email="admin@naslai.uz", password="parol12345")
        self.user = User.objects.create_user(username="u@b.uz", email="u@b.uz", password="parol12345")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_tariffs_endpoint(self):
        response = self.client.get("/api/admin/tariffs")
        self.assertEqual(response.status_code, 200)
        ids = [t["id"] for t in response.data["tariffs"]]
        self.assertEqual(ids, ["start", "standart", "profi", "maksimum"])
        start = response.data["tariffs"][0]
        self.assertEqual(start["priceUzs"], 89_000)
        self.assertEqual(start["images"], 21)
        self.assertEqual(start["tokens"], 21 * TOKENS_PER_IMAGE)

    def test_tariffs_endpoint_admin_only(self):
        other = APIClient()
        other.force_authenticate(self.user)
        self.assertEqual(other.get("/api/admin/tariffs").status_code, 404)
        self.assertIn(APIClient().get("/api/admin/tariffs").status_code, (401, 403))

    def test_promo_by_tariff_ignores_client_value(self):
        response = self.client.post(
            "/api/admin/promos",
            {"code": "BLOGER-PROFI", "kind": "discount", "value": 5, "tariff": "profi"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        promo = Promo.objects.get(code="BLOGER-PROFI")
        self.assertEqual(promo.kind, "tokens")
        self.assertEqual(promo.value, tariff_by_id("profi")["tokens"])
        self.assertEqual(promo.tariff, "profi")
        self.assertEqual(response.data["promo"]["tariffName"], "Profi")

    def test_promo_by_tariff_without_value(self):
        response = self.client.post("/api/admin/promos", {"code": "START1", "tariff": "start"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Promo.objects.get(code="START1").value, 21)

    def test_custom_promo_still_works(self):
        response = self.client.post(
            "/api/admin/promos", {"code": "ANYNAME", "kind": "tokens", "value": 7, "tariff": "custom"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        promo = Promo.objects.get(code="ANYNAME")
        self.assertEqual((promo.value, promo.tariff), (7, ""))
        response = self.client.post("/api/admin/promos", {"code": "SALE20", "kind": "discount", "value": 20}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["promo"]["tariff"], "")

    def test_custom_promo_still_validates_value(self):
        response = self.client.post("/api/admin/promos", {"code": "ZERO1", "kind": "tokens"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_invalid_tariff_rejected(self):
        response = self.client.post("/api/admin/promos", {"code": "BAD1", "tariff": "gold"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Promo.objects.filter(code="BAD1").exists())

    def test_adjust_balance_by_tariff(self):
        response = self.client.post(
            f"/api/admin/users/{self.user.pk}/balance", {"tariff": "maksimum", "amount": 1}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["applied"], 195)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 195)
        self.assertEqual(LedgerEntry.objects.filter(user=self.user).latest("id").label, "Tarif: Maksimum")

    def test_adjust_balance_invalid_tariff(self):
        response = self.client.post(f"/api/admin/users/{self.user.pk}/balance", {"tariff": "gold"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_adjust_balance_custom_amount_unchanged(self):
        response = self.client.post(f"/api/admin/users/{self.user.pk}/balance", {"amount": 3}, format="json")
        self.assertEqual(response.data["applied"], 3)

    def test_overview_counts_by_tariff(self):
        Promo.objects.create(code="A-START", kind="tokens", value=21, tariff="start")
        Promo.objects.create(code="FREE1", kind="tokens", value=5)
        response = self.client.get("/api/admin/overview")
        self.assertEqual(response.data["promos"]["byTariff"], {"start": 1, "custom": 1})


class ApiKeyAdminOnlyTests(TestCase):
    """API kalitlar: faqat admin beradi, balans kalitda yuradi, qaytarish ham kalitga."""

    def setUp(self):
        from django.test import override_settings  # noqa: F401
        import os

        from accounts.models import User

        self._env = os.environ.get("ADMIN_EMAILS")
        os.environ["ADMIN_EMAILS"] = "boss@example.com"
        self.admin = User.objects.create_user(username="boss", email="boss@example.com", password="x-strong-pass-123")
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="x-strong-pass-123")

    def tearDown(self):
        import os

        if self._env is None:
            os.environ.pop("ADMIN_EMAILS", None)
        else:
            os.environ["ADMIN_EMAILS"] = self._env

    def _client(self, user):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_regular_user_cannot_create_keys(self):
        response = self._client(self.user).post("/api/api-keys", {}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._client(self.user).get("/api/api-keys").status_code, 404)

    def test_admin_creates_key_with_tariff_and_tops_up(self):
        from accounts.models import ApiKey, ApiKeyLedger

        client = self._client(self.admin)
        response = client.post("/api/admin/api-keys", {"userId": self.user.pk, "initialTariff": "start"}, format="json")
        self.assertEqual(response.status_code, 201)
        key = ApiKey.objects.get(pk=response.data["key"]["id"])
        self.assertEqual(key.balance, 21)
        secret = response.data["secret"]

        response = client.post(f"/api/admin/api-keys/{key.pk}", {"tariff": "standart"}, format="json")
        self.assertEqual(response.data["balance"], 73)
        self.assertEqual(ApiKeyLedger.objects.filter(key=key).count(), 2)

        from rest_framework.test import APIClient

        holder = APIClient()
        holder.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
        response = holder.get("/api/key/balance")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["balance"], 73)
        self.assertEqual(len(response.data["history"]), 2)

    def test_refund_goes_back_to_key(self):
        from accounts.models import ApiKey
        from billing.services import key_credit, key_debit, refund_job
        from generation.models import GenerationJob

        key = ApiKey.objects.create(user=self.user, name="k", prefix="nsk_live_x", digest="d" * 64)
        key_credit(key, 5, "start")
        key_debit(key, 2, "gen")
        job = GenerationJob.objects.create(user=self.user, content_type="card", tokens_charged=2, api_key=key)
        refund_job(job, 2, "qaytarish")
        key.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(key.balance, 5)
        self.assertEqual(self.user.balance, 0)


class SuperuserIsAdminTests(TestCase):
    def test_superuser_gets_admin_api_and_flag(self):
        import os

        os.environ.pop("ADMIN_EMAILS", None)
        boss = User.objects.create_superuser(username="su", email="su@example.com", password="x-strong-pass-123")
        plain = User.objects.create_user(username="pl", email="pl@example.com", password="x-strong-pass-123")
        client = APIClient()
        client.force_authenticate(boss)
        self.assertEqual(client.get("/api/admin/overview").status_code, 200)
        client.force_authenticate(plain)
        self.assertEqual(client.get("/api/admin/overview").status_code, 404)

        from accounts.serializers import AccountSerializer  # noqa: F401
