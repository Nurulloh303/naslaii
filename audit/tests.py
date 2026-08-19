from django.test import TestCase


class AuditSectionTests(TestCase):
    """Раздел «Плохие продажи?» целиком.

    Проверяем то, где ошибка стоит доверия: язык отчёта, источники правил
    (чужое требование против нашего совета) и сохранность отчёта.
    """

    def setUp(self):
        from accounts.models import User
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(username="a@n.uz", email="a@n.uz", password="parol12345")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.user).key}")
        self.png = (
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQ"
            "GAhKmMIQAAAABJRU5ErkJggg=="
        )

    def _run(self, language="uz", answers=None):
        return self.client.post(
            "/api/audit/run",
            {"images": [self.png], "link": "", "language": language, "answers": answers or {}},
            format="json",
        )

    def test_report_language_follows_the_request(self):
        ru = self._run("ru").data["report"]
        self.assertRegex(ru["verdict"], r"[А-Яа-я]")
        uz = self._run("uz").data["report"]
        self.assertNotRegex(uz["verdict"], r"[А-Яа-я]")

    def test_official_rules_carry_a_link_and_recommendations_do_not(self):
        """Нельзя выдавать наш совет за требование площадки."""
        problems = self._run().data["report"]["problems"]
        self.assertTrue(problems)
        for problem in problems:
            self.assertIn(problem["source"], {"uzum", "practice"})
            if problem["source"] == "uzum":
                self.assertTrue(problem["ruleUrl"].startswith("https://seller.uzum.uz/manual/"))
            else:
                self.assertIsNone(problem["ruleUrl"])

    def test_unknown_rule_id_does_not_get_a_link(self):
        from audit.views import with_rule_sources

        report = with_rule_sources({"problems": [{"ruleId": "made_up_rule"}]})
        self.assertEqual(report["problems"][0]["source"], "practice")
        self.assertIsNone(report["problems"][0]["ruleUrl"])

    def test_not_checked_is_returned_as_keys(self):
        meta = self._run().data["report"]["meta"]
        self.assertEqual(meta["notChecked"], ["price", "reviews", "search", "delivery", "season"])

    def test_category_is_cleaned_for_generation(self):
        from audit.views import sanitize_report

        good = sanitize_report({"suggested": {"category": "Salomatlik"}, "score": 58})
        self.assertEqual(good["suggested"]["category"], "Salomatlik")
        # Придуманной категории в списке нет — генерация всё равно её потеряет.
        invented = sanitize_report({"suggested": {"category": "Витамины"}, "score": 58})
        self.assertEqual(invented["suggested"]["category"], "")

    def test_score_is_clamped(self):
        from audit.views import sanitize_report

        self.assertEqual(sanitize_report({"score": 900})["score"], 100)
        self.assertEqual(sanitize_report({"score": "нет"})["score"], 0)

    def test_last_report_survives_a_page_reload(self):
        first = self._run("ru").data["report"]
        questions = self.client.get("/api/audit/questions").data
        self.assertEqual(questions["lastReport"]["verdict"], first["verdict"])
        self.assertTrue(questions["lastReportAt"])

    def test_external_reasons_are_named_when_answers_point_away_from_the_card(self):
        report = self._run("ru", {"reviews": "none", "impressions": "noImpressions", "boost": "boostOff"}).data["report"]
        self.assertEqual(len(report["likelyNotTheCard"]), 3)
