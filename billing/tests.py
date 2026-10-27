"""Тесты экономики.

Цены здесь должны совпадать с Node-версией до сума. Если разойдутся,
один и тот же пакет будет стоить по-разному в двух местах.
"""

from django.test import TestCase

from .pricing import COST_PER_IMAGE_UZS, quote_for, token_packs


class PricingTests(TestCase):
    def test_packs_are_tariffs(self):
        expected = {
            "start": (21, 89_000, 4_238, 0),
            "standart": (52, 199_000, 3_827, 10),
            "profi": (117, 399_000, 3_410, 20),
            "maksimum": (195, 599_000, 3_072, 28),
        }
        packs = token_packs()
        self.assertEqual([pack["id"] for pack in packs], list(expected))
        for pack in packs:
            tokens, price, per_token, discount = expected[pack["id"]]
            self.assertEqual(pack["tokens"], tokens, pack["id"])
            self.assertEqual(pack["priceUzs"], price, pack["id"])
            self.assertEqual(pack["perToken"], per_token, pack["id"])
            self.assertEqual(pack["discount"], discount, pack["id"])
        self.assertEqual([pack["id"] for pack in packs if pack["popular"]], ["standart"])

    def test_price_per_token_strictly_decreases(self):
        """Большой пакет обязан быть выгоднее маленького.

        Иначе клиенту дешевле купить два маленьких, и апгрейд не продаётся —
        ровно эта ошибка сейчас на infografikaai.uz.
        """
        per_token = [pack["perToken"] for pack in token_packs()]
        self.assertEqual(per_token, sorted(per_token, reverse=True))
        self.assertEqual(len(set(per_token)), len(per_token))

    def test_margin_never_below_target(self):
        payment_load = 0.025
        for pack in token_packs():
            net = pack["priceUzs"] * (1 - payment_load)
            profit = net - pack["tokens"] * COST_PER_IMAGE_UZS
            self.assertGreaterEqual(profit / net, 0.75, f"маржа просела на пакете {pack['id']}")

    def test_quote_counts_variants_times_pages(self):
        quote = quote_for({"contentType": "card", "variants": 2, "pages": 3})
        self.assertEqual(quote["tokens"], 6)

    def test_package_is_flat_five_slides(self):
        quote = quote_for({"contentType": "marketplacePackage", "variants": 4, "pages": 5})
        self.assertEqual(quote["tokens"], 5)


class InfographicRulesTests(TestCase):
    def test_headline_keeps_apostrophe(self):
        from generation.infographic_rules import to_headline

        # «Ko‘ylak» не должно превращаться в «KO YLAK».
        self.assertEqual(to_headline("Ko‘ylak", "", "uz"), "KO‘YLAK")

    def test_headline_picks_head_noun_by_language(self):
        from generation.infographic_rules import to_headline

        self.assertEqual(to_headline("maktab uchun ryukzak", "", "uz"), "RYUKZAK")
        self.assertEqual(to_headline("сумка для ноутбука", "", "ru"), "СУМКА")

    def test_only_three_callouts(self):
        from generation.infographic_rules import rank_callouts

        result = rank_callouts(["Chiroyli dizayn", "100% paxta", "2 ta cho‘ntak", "Yengil", "Har kuni"])
        self.assertEqual(len(result), 3)
        # С цифрами идут первыми.
        self.assertIn("100% paxta", result)
