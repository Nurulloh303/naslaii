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
