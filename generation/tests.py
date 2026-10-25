from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from generation.models import GenerationJob

PIXEL_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
PIXEL_PNG_2 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class QuoteTests(TestCase):
    """Стоимость задачи.

    Модель простая: одна картинка — один токен. Старые тесты ждали
    отдельную цену для каждого режима (карточка 5, пакет 25) — от этого
    отказались: разная цена означала разную маржу, и её приходилось
    пересчитывать под каждую операцию.
    """

    def setUp(self):
        self.client = APIClient()

    def quote(self, settings):
        return self.client.post("/api/quote", {"settings": settings}, format="json").json()["tokens"]

    def test_one_image_is_one_token(self):
        self.assertEqual(self.quote({"contentType": "card", "marketplace": "wb", "variants": 1}), 1)
        self.assertEqual(self.quote({"contentType": "photo", "variants": 1}), 1)
        self.assertEqual(self.quote({"contentType": "fashion", "variants": 1}), 1)

    def test_variants_multiply(self):
        self.assertEqual(self.quote({"contentType": "fashion", "variants": 2}), 2)
        self.assertEqual(self.quote({"contentType": "card", "variants": 4}), 4)

    def test_pages_multiply_with_variants(self):
        self.assertEqual(self.quote({"contentType": "card", "variants": 2, "pages": 3}), 6)

    def test_marketplace_package_is_always_five(self):
        """Пакет — ровно 5 слайдов, варианты на цену не влияют."""
        self.assertEqual(self.quote({"contentType": "marketplacePackage", "variants": 1}), 5)
        self.assertEqual(self.quote({"contentType": "marketplacePackage", "variants": 4}), 5)


class GenerationFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User(username="rasul@test.uz", email="rasul@test.uz", name="Rasul", balance=100)
        self.user.set_password("testpass123")
        self.user.save()
        self.client.force_authenticate(self.user)

    def _post(self, key, payload):
        return self.client.post("/api/generations", payload, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_fashion_generation_debits_and_returns_1080x1440(self):
        r = self._post(
            "idem-fashion-0000000000000001",
            {"assetDataUrl": PIXEL_PNG, "brief": {"title": "", "category": "", "benefits": []}, "settings": {"contentType": "fashion", "variants": 1, "fashion": {"gender": "female"}}},
        )
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["balance"], 99)  # 1 картинка = 1 токен
        self.assertEqual(body["job"]["status"], "success")
        result = body["job"]["results"][0]
        self.assertEqual((result["width"], result["height"]), (1080, 1440))

    def test_copy_style_requires_reference_image(self):
        r = self._post(
            "idem-copystyle-noref-0000000001",
            {"assetDataUrl": PIXEL_PNG, "brief": {"title": "Krossovka", "category": "Poyabzal", "benefits": ["a", "b"]}, "settings": {"contentType": "copyStyle", "variants": 1}},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "MISSING_STYLE_REFERENCE")

    def test_copy_style_requires_two_verified_benefits(self):
        r = self._post(
            "idem-copystyle-nobenefits-0001",
            {"assetDataUrl": PIXEL_PNG, "brief": {"title": "Krossovka", "category": "Poyabzal", "benefits": []}, "settings": {"contentType": "copyStyle", "variants": 1, "designReferenceDataUrl": PIXEL_PNG_2}},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "MISSING_VERIFIED_BENEFITS")

    def test_copy_style_success_debits_one_token(self):
        r = self._post(
            "idem-copystyle-ok-0000000001",
            {"assetDataUrl": PIXEL_PNG, "brief": {"title": "Krossovka", "category": "Poyabzal", "benefits": ["yengil", "amortizatsiya"]}, "settings": {"contentType": "copyStyle", "variants": 1, "designReferenceDataUrl": PIXEL_PNG_2}},
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["balance"], 99)

    def test_marketplace_package_gives_five_slides(self):
        r = self._post(
            "idem-mp-0000000000000000001",
            {"assetDataUrl": PIXEL_PNG, "brief": {"title": "Termos", "category": "Uy", "benefits": ["issiqlik", "polat"]}, "settings": {"contentType": "marketplacePackage", "marketplacePackage": {"productDescription": "x"}}},
        )
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(len(body["job"]["results"]), 5)
        self.assertEqual(body["balance"], 95)  # 5 слайдов = 5 токенов

    def test_card_regression_no_reference_needed(self):
        r = self._post(
            "idem-card-0000000000000000001",
            {"assetDataUrl": PIXEL_PNG, "brief": {"title": "Ryukzak", "category": "Aksessuar", "benefits": ["a", "b", "c"]}, "settings": {"contentType": "card", "marketplace": "wb", "variants": 1}},
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["balance"], 99)

    def test_idempotency_key_prevents_double_charge(self):
        key = "idem-replay-000000000000000001"
        payload = {"assetDataUrl": PIXEL_PNG, "brief": {"title": "Ryukzak", "category": "Aksessuar", "benefits": ["a", "b", "c"]}, "settings": {"contentType": "card", "marketplace": "wb", "variants": 1}}
        first = self._post(key, payload)
        second = self._post(key, payload)
        self.assertEqual(first.json()["job"]["id"], second.json()["job"]["id"])
        self.assertEqual(first.json()["balance"], second.json()["balance"])

    def test_insufficient_balance_returns_402(self):
        self.user.balance = 1  # пакет стоит 5 токенов
        self.user.save()
        r = self._post(
            "idem-broke-0000000000000000001",
            {"assetDataUrl": PIXEL_PNG, "brief": {"title": "Termos", "category": "Uy", "benefits": ["a", "b"]}, "settings": {"contentType": "marketplacePackage", "marketplacePackage": {"productDescription": "x"}}},
        )
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"], "INSUFFICIENT_TOKENS")


class CancelGenerationTests(TestCase):
    """Отмена генерации.

    Главное здесь — деньги: возврат должен случиться ровно один раз и
    только за неотрисованные картинки.
    """

    def setUp(self):
        from accounts.models import User
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(username="c@n.uz", email="c@n.uz", password="parol12345")
        self.user.balance = 10
        self.user.save(update_fields=["balance"])
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.user).key}")

    def _job(self, **kwargs):
        from generation.models import GenerationJob

        defaults = dict(user=self.user, content_type="card", status="processing", tokens_charged=4,
                        settings={"contentType": "card", "variants": 4, "pages": 1})
        defaults.update(kwargs)
        return GenerationJob.objects.create(**defaults)

    def test_cancel_with_nothing_rendered_refunds_everything(self):
        job = self._job()
        response = self.client.post(f"/api/generations/{job.pk}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["refunded"], 4)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 14)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")

    def test_cancel_refunds_only_unrendered_images(self):
        """Две картинки уже отрисованы — за них мы заплатили, возврат за две."""
        job = self._job(results=[{"id": "1"}, {"id": "2"}])
        response = self.client.post(f"/api/generations/{job.pk}/cancel")
        self.assertEqual(response.data["refunded"], 2)
        self.assertEqual(response.data["delivered"], 2)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 12)
        job.refresh_from_db()
        # Готовые картинки человек получает.
        self.assertEqual(job.status, "success")

    def test_second_cancel_does_not_refund_twice(self):
        job = self._job()
        self.client.post(f"/api/generations/{job.pk}/cancel")
        second = self.client.post(f"/api/generations/{job.pk}/cancel")
        self.assertEqual(second.status_code, 409)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 14)

    def test_finished_job_cannot_be_cancelled(self):
        job = self._job(status="success")
        response = self.client.post(f"/api/generations/{job.pk}/cancel")
        self.assertEqual(response.status_code, 409)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 10)

    def test_other_users_job_is_invisible(self):
        from accounts.models import User

        stranger = User.objects.create_user(username="s@n.uz", email="s@n.uz", password="parol12345")
        job = self._job(user=stranger)
        response = self.client.post(f"/api/generations/{job.pk}/cancel")
        self.assertEqual(response.status_code, 404)


class SceneWishTests(TestCase):
    """Пожелание по сцене.

    Оно должно попадать в промпт как указание про ФОН и при этом нести
    запрет на превращение себя в текст на карточке — иначе «покажи
    машину» рискует стать надписью «МАШИНА».
    """

    BRIEF = {"title": "Signalizasiya pulti", "category": "Avto", "benefits": ["Katta ekran", "Uzoq masofa"]}
    SETTINGS = {"contentType": "card", "marketplace": "uzum", "language": "uz", "variants": 1, "pages": 1}

    def test_wish_reaches_prompt_with_limits(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, {**self.SETTINGS, "sceneWish": "mashina ichida ko'rsatilsin"}, 1)
        self.assertIn("mashina ichida ko'rsatilsin", prompt)
        self.assertIn("Apply it to the SCENE only", prompt)
        self.assertIn("never be written on the card as text", prompt)

    def test_empty_wish_adds_nothing(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self.SETTINGS, 1)
        self.assertNotIn("SCENE WISH", prompt)

    def test_wish_is_trimmed_to_300_characters(self):
        """Длинный текст модель начинает читать как список фактов и выносит словами."""
        from generation.prompts import scene_wish_rule

        rule = scene_wish_rule({"sceneWish": "a" * 500})
        self.assertIn("a" * 300, rule)
        self.assertNotIn("a" * 301, rule)

    def test_wish_works_in_style_copy_mode(self):
        from generation.prompts import style_copy_prompt

        prompt = style_copy_prompt(self.BRIEF, {**self.SETTINGS, "sceneWish": "mashina fonda"}, 1)
        self.assertIn("mashina fonda", prompt)


class HeadlineLanguageTests(TestCase):
    """Заголовок должен быть на языке карточки.

    Продавец выбирал русский, а получал "RYUKZAK" латиницей — название
    приходило из брифа на узбекском, а промпт требовал печатать дословно.
    """

    BRIEF = {"title": "Ryukzak", "category": "Aksessuarlar", "benefits": ["Keng asosiy bo'lim", "Yon cho'ntaklar"]}

    def _settings(self, language):
        return {"contentType": "card", "marketplace": "uzum", "language": language, "variants": 1, "pages": 1}

    def test_script_mismatch_detected(self):
        from generation.prompts import headline_matches_language

        self.assertFalse(headline_matches_language("RYUKZAK", "ru"))
        self.assertTrue(headline_matches_language("РЮКЗАК", "ru"))
        self.assertTrue(headline_matches_language("RYUKZAK", "uz"))
        self.assertFalse(headline_matches_language("РЮКЗАК", "uz"))
        self.assertTrue(headline_matches_language("БОРХАЛТА", "tg"))

    def test_numbers_only_headline_is_not_a_mismatch(self):
        from generation.prompts import headline_matches_language

        self.assertTrue(headline_matches_language("1080", "ru"))

    def test_russian_card_asks_for_translation(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self._settings("ru"), 1)
        # Заголовок нельзя печатать латиницей: просим написать его кириллицей.
        self.assertIn("write it in Russian Cyrillic and print ONLY that version", prompt)
        self.assertNotIn('headline exactly: "RYUKZAK"', prompt)

    def test_brand_names_are_transliterated_not_translated(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(
            {"title": "Melatonin 3 mg", "category": "Salomatlik", "benefits": ["3 mg doza", "90 tabletka"]},
            self._settings("ru"),
            1,
        )
        self.assertIn("transliterate it into", prompt)
        # Дозировка не должна становиться заголовком: "Melatonin 3 mg" -> MELATONIN.
        self.assertIn('"MELATONIN"', prompt)

    def test_package_text_is_a_source_of_facts(self):
        """Надписи на упаковке — самый надёжный источник, они уже на фото."""
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self._settings("uz"), 1)
        self.assertIn("READ the text printed on the product packaging", prompt)

    def test_uzbek_card_keeps_headline_verbatim(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self._settings("uz"), 1)
        self.assertIn('exactly: "RYUKZAK"', prompt)
        self.assertNotIn("TRANSLATE", prompt)

    def test_style_copy_mode_follows_the_same_rule(self):
        from generation.prompts import style_copy_prompt

        prompt = style_copy_prompt(self.BRIEF, self._settings("ru"), 1)
        self.assertIn("write it in Russian Cyrillic and print ONLY that version", prompt)


class DeliveredFormatTests(TestCase):
    """Формат готовой картинки.

    JPEG режет цветность вдвое (4:2:0). У инфографики белые буквы на
    цветном фоне, и вокруг них появляются «пятна» — это было видно при
    увеличении реальной карточки. PNG для такой картинки и чище, и легче.
    """

    def _card(self):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1088, 1440), (26, 79, 116))
        draw = ImageDraw.Draw(image)
        for y in range(0, 1440, 180):
            draw.rectangle([80, y, 1000, y + 60], fill=(255, 255, 255))
        return image

    def _raw(self, image):
        import io as _io

        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_card_is_delivered_as_lossless_png(self):
        import io as _io

        from PIL import Image

        from generation.imaging import normalize_generated_image

        source = self._card()
        data, extension = normalize_generated_image(self._raw(source), "card")
        self.assertEqual(extension, "png")

        # Обрезка 1088 -> 1080 идёт по центру, поэтому сравниваем с ней же.
        expected = source.crop((4, 0, 1084, 1440))
        delivered = Image.open(_io.BytesIO(data)).convert("RGB")
        self.assertEqual(delivered.size, (1080, 1440))
        self.assertEqual(list(delivered.getdata()), list(expected.getdata()))

    def test_png_is_smaller_than_jpeg_for_flat_cards(self):
        import io as _io

        from generation.imaging import normalize_generated_image

        raw = self._raw(self._card())
        png, _ = normalize_generated_image(raw, "card")

        from PIL import Image

        buffer = _io.BytesIO()
        Image.open(_io.BytesIO(raw)).convert("RGB").crop((4, 0, 1084, 1440)).save(buffer, format="JPEG", quality=96, optimize=True)
        self.assertLess(len(png), len(buffer.getvalue()))

    def test_photo_modes_stay_jpeg(self):
        from generation.imaging import normalize_generated_image

        raw = self._raw(self._card())
        for content_type in ("photo", "fashion", "video"):
            _, extension = normalize_generated_image(raw, content_type)
            self.assertEqual(extension, "jpg", content_type)

    def test_card_modes_all_use_png(self):
        from generation.imaging import normalize_generated_image

        raw = self._raw(self._card())
        for content_type in ("card", "copyStyle", "marketplacePackage"):
            _, extension = normalize_generated_image(raw, content_type)
            self.assertEqual(extension, "png", content_type)


class LanguageIsNotAlphabetTests(TestCase):
    """«Русская кириллица» — это язык, а не алфавит.

    На реальной карточке модель записала узбекские слова русскими буквами:
    «УЙҚУНИ ҚЎЛЛАБ-ҚУВВАТЛАЙДИ», «90 ТАБЛЕТКА МАВЖУД». Формально кириллица,
    по факту покупатель это не прочитает.
    """

    BRIEF = {
        "title": "Melatonin 3 mg",
        "category": "Salomatlik",
        "benefits": ["Uyquni qo'llab-quvvatlaydi", "90 tabletka mavjud", "Glyutensiz tarkib"],
    }

    def _settings(self, language):
        return {"contentType": "card", "marketplace": "uzum", "language": language, "variants": 1, "pages": 1}

    def test_russian_card_demands_russian_words_not_just_cyrillic(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self._settings("ru"), 1)
        self.assertIn("words themselves must be RUSSIAN", prompt)
        self.assertIn("Cyrillic alphabet alone is NOT enough", prompt)

    def test_uzbek_only_letters_are_banned_on_a_russian_card(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self._settings("ru"), 1)
        self.assertIn("Never use the letters ў, қ, ғ, ҳ, ҷ", prompt)

    def test_translation_asks_for_meaning_not_transliteration(self):
        from generation.prompts import translate_note

        note = translate_note(self._settings("ru"))
        self.assertIn("TRANSLATE their MEANING", note)
        self.assertIn("do not transliterate", note)

    def test_product_label_must_not_be_repainted(self):
        """Этикетку на самом флаконе переписывать нельзя: придёт другой товар."""
        from generation.prompts import generation_prompt, style_copy_prompt

        for builder in (generation_prompt, style_copy_prompt):
            prompt = builder(self.BRIEF, self._settings("ru"), 1)
            self.assertIn("NEVER repaint, translate or re-typeset", prompt, builder.__name__)

    def test_uzbek_card_also_gets_a_word_rule(self):
        from generation.prompts import translate_note

        self.assertIn("Uzbek Latin words", translate_note(self._settings("uz")))


class SubtitleIsProductSpecificTests(TestCase):
    """Подзаголовок обязан относиться к ЭТОМУ товару.

    На реальных карточках вышло одинаково: автомобильная лампа и мелатонин
    получили «НА КАЖДЫЙ ДЕНЬ». Это была наша заготовка на любой случай.
    """

    def _settings(self, language="ru"):
        return {"contentType": "card", "marketplace": "uzum", "language": language, "variants": 1, "pages": 1}

    def test_generic_product_has_no_canned_subtitle(self):
        from generation.prompts import visible_text_plan

        plan = visible_text_plan({"title": "Signalizasiya pulti", "category": "Boshqa"}, self._settings())
        self.assertEqual(plan["subtitle"], "")

    def test_category_gives_a_meaningful_subtitle(self):
        from generation.prompts import visible_text_plan

        plan = visible_text_plan({"title": "Melatonin 3 mg", "category": "Salomatlik"}, self._settings())
        self.assertEqual(plan["subtitle"], "пищевая добавка")

    def test_filler_phrases_are_banned_in_the_prompt(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(
            {"title": "Signalizasiya pulti", "category": "Boshqa", "benefits": ["Katta ekran", "Uzoq masofa"]},
            self._settings(),
            1,
        )
        self.assertIn("на каждый день", prompt)
        self.assertIn("Never write filler that would fit anything", prompt)

    def test_known_category_subtitle_is_fixed_not_invented(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(
            {"title": "Melatonin 3 mg", "category": "Salomatlik", "benefits": ["3 mg doza", "90 tabletka"]},
            self._settings(),
            1,
        )
        self.assertIn('Subtitle: "пищевая добавка"', prompt)
        self.assertNotIn("Write the subtitle yourself", prompt)

    def test_two_different_products_do_not_share_a_subtitle(self):
        from generation.prompts import visible_text_plan

        lamp = visible_text_plan({"title": "Лампа для авто", "category": "Elektronika"}, self._settings())
        pills = visible_text_plan({"title": "Melatonin 3 mg", "category": "Salomatlik"}, self._settings())
        self.assertNotEqual(lamp["subtitle"], pills["subtitle"])


class SubtitleFromAnalysisTests(TestCase):
    """Подзаголовок пишет тот, кто видел фото.

    Наши словари — только запас: они угадывают по названию, а анализ
    смотрит на упаковку.
    """

    def _settings(self, language="ru"):
        return {"contentType": "card", "marketplace": "uzum", "language": language, "variants": 1, "pages": 1}

    def test_brief_subtitle_wins_over_dictionary(self):
        from generation.prompts import visible_text_plan

        plan = visible_text_plan(
            {"title": "Melatonin 3 mg", "subtitle": "поддержка сна с этикетки", "category": "Salomatlik"},
            self._settings(),
        )
        self.assertEqual(plan["subtitle"], "поддержка сна с этикетки")

    def test_dictionary_is_used_when_analysis_gave_nothing(self):
        from generation.prompts import visible_text_plan

        plan = visible_text_plan({"title": "Melatonin 3 mg", "subtitle": "", "category": "Salomatlik"}, self._settings())
        self.assertEqual(plan["subtitle"], "пищевая добавка")

    def test_filler_from_the_model_is_dropped(self):
        from generation.prompts import looks_like_filler_subtitle, visible_text_plan

        for filler in ("на каждый день", "kundalik foydalanish uchun", "har kuni uchun", "Качественный товар", "marketplace uchun"):
            self.assertTrue(looks_like_filler_subtitle(filler), filler)

        plan = visible_text_plan(
            {"title": "Signalizasiya pulti", "subtitle": "на каждый день", "category": "Boshqa"},
            self._settings(),
        )
        self.assertEqual(plan["subtitle"], "")

    def test_meaningful_subtitles_pass(self):
        from generation.prompts import looks_like_filler_subtitle

        for good in ("avto uchun", "светодиодная", "для школы", "пищевая добавка"):
            self.assertFalse(looks_like_filler_subtitle(good), good)

    def test_subtitle_reaches_the_prompt_verbatim(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(
            {"title": "Signalizasiya pulti", "subtitle": "avto uchun", "category": "Elektronika", "benefits": ["Katta ekran", "Uzoq masofa"]},
            self._settings("uz"),
            1,
        )
        self.assertIn('Subtitle: "avto uchun"', prompt)


class BriefLanguageDoesNotLeakTests(TestCase):
    """Строка из брифа не должна печататься на чужом языке.

    Живой случай: бриф написан по-узбекски (анализ шёл при узбекском
    интерфейсе), карточка выбрана русская — и подзаголовок
    «mini linzalar avto uchun» уехал на русскую карточку как есть,
    потому что указание «дословно» сильнее общего правила о переводе.
    """

    BRIEF = {
        "title": "Lampa",
        "subtitle": "mini linzalar avto uchun",
        "category": "Elektronika",
        "benefits": ["Bluetooth 5.0 simsiz ulanish", "3000k dan 10000k gacha yorug‘lik"],
    }

    def _settings(self, language, **extra):
        return {"contentType": "card", "marketplace": "uzum", "language": language, "variants": 1, "pages": 1, **extra}

    def test_uzbek_subtitle_is_translated_on_a_russian_card(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self._settings("ru"), 1)
        self.assertNotIn('Subtitle: "mini linzalar avto uchun"', prompt)
        self.assertIn('Russian Cyrillic translation of "mini linzalar avto uchun"', prompt)

    def test_same_language_subtitle_stays_verbatim(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self._settings("uz"), 1)
        self.assertIn('Subtitle: "mini linzalar avto uchun"', prompt)
        self.assertNotIn("translation of", prompt)

    def test_badges_are_translated_not_transliterated(self):
        from generation.prompts import generation_prompt

        prompt = generation_prompt(self.BRIEF, self._settings("ru"), 1)
        self.assertIn("translate its meaning", prompt)
        self.assertIn("never respell it with another alphabet", prompt)


class ReferenceIsCopiedStructurallyTests(TestCase):
    """Референс — это шаблон, а не настроение.

    Продавцу нужен тот же макет со своим товаром и своими словами;
    «скопируй систему» модель понимала как «сделай в похожем духе».
    """

    BRIEF = {"title": "Lampa", "subtitle": "avto uchun", "category": "Elektronika", "benefits": ["65 Vt", "12 V"]}

    def _settings(self, mode):
        return {"contentType": "copyStyle", "marketplace": "uzum", "language": "ru", "variants": 1, "pages": 1, "referenceMode": mode}

    def test_copy_mode_demands_the_same_layout(self):
        from generation.prompts import style_copy_prompt

        prompt = style_copy_prompt(self.BRIEF, self._settings("copy"), 1)
        for needle in (
            "STRUCTURAL COPY",
            "SAME number of text blocks",
            "SAME positions",
            "SAME background",
            "SAME typography",
            "Change ONLY two things",
            "Never move, resize or delete a block",
            "IMAGE 1 is the TEMPLATE",
        ):
            self.assertIn(needle, prompt, needle)

    def test_inspire_mode_stays_soft(self):
        from generation.prompts import style_copy_prompt

        prompt = style_copy_prompt(self.BRIEF, self._settings("inspire"), 1)
        self.assertNotIn("STRUCTURAL COPY", prompt)


class OldJobsArePrunedTests(TestCase):
    """История генераций не должна упираться в глухой лимит.

    В Node-версии на 50-й генерации человек получал «лимит исчерпан» и не
    мог ни продолжить, ни удалить старое из интерфейса.
    """

    def setUp(self):
        from accounts.models import User

        self.user = User.objects.create_user(username="p@n.uz", email="p@n.uz", password="parol12345")

    def _job(self, status="success"):
        from generation.models import GenerationJob

        return GenerationJob.objects.create(user=self.user, content_type="card", status=status, tokens_charged=1)

    def test_oldest_finished_job_is_removed(self):
        from generation.models import GenerationJob
        from generation.views import MAX_JOBS_PER_USER, prune_old_jobs

        jobs = [self._job() for _ in range(MAX_JOBS_PER_USER)]
        removed = prune_old_jobs(self.user)
        self.assertEqual(removed, 1)
        self.assertFalse(GenerationJob.objects.filter(pk=jobs[0].pk).exists())
        self.assertTrue(GenerationJob.objects.filter(pk=jobs[-1].pk).exists())

    def test_running_jobs_are_never_touched(self):
        from generation.models import GenerationJob
        from generation.views import MAX_JOBS_PER_USER, prune_old_jobs

        running = [self._job("processing") for _ in range(MAX_JOBS_PER_USER)]
        self.assertEqual(prune_old_jobs(self.user), 0)
        self.assertEqual(GenerationJob.objects.filter(user=self.user).count(), len(running))

    def test_nothing_happens_below_the_limit(self):
        from generation.views import prune_old_jobs

        self._job()
        self.assertEqual(prune_old_jobs(self.user), 0)


class QueueTests(TestCase):
    """Очередь генерации.

    Проверяем не «работает ли Celery» — это забота самой Celery. Проверяем
    места, где ошибка стоит денег: списанные и не возвращённые токены,
    повторный прогон уже оплаченной задачи, задача, застрявшая навсегда.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User(username="navbat@test.uz", email="navbat@test.uz", name="Navbat", balance=10)
        self.user.set_password("testpass123")
        self.user.save()
        self.client.force_authenticate(self.user)

    def _payload(self):
        return {
            "assetDataUrl": PIXEL_PNG,
            "brief": {"title": "Sumka", "category": "Aksessuarlar", "benefits": ["Keng", "Mustahkam"]},
            "settings": {"contentType": "card", "variants": 1},
        }

    def _post(self, key="idem-queue-000000000000000001"):
        return self.client.post("/api/generations", self._payload(), format="json", HTTP_IDEMPOTENCY_KEY=key)

    # --- обычный режим с брокером ----------------------------------------

    def test_with_queue_job_comes_back_queued_and_tokens_are_debited(self):
        """С очередью ответ приходит сразу, картинок в нём ещё нет."""
        from unittest.mock import patch

        with patch("generation.views.enqueue") as enqueue:
            response = self._post()

        self.assertTrue(enqueue.called)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["job"]["status"], "queued")
        self.assertEqual(body["job"]["results"], [])
        # Токены списываются при постановке в очередь, а не при выполнении:
        # иначе за время ожидания их можно потратить второй раз.
        self.assertEqual(body["balance"], 9)

    # --- брокер недоступен ------------------------------------------------

    def test_broker_failure_refunds_tokens(self):
        """Redis лежит — человек не должен остаться без токенов."""
        from unittest.mock import patch

        with patch("generation.views.enqueue", side_effect=RuntimeError("redis down")):
            response = self._post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "QUEUE_UNAVAILABLE")

        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 10, "токены обязаны вернуться полностью")

        job = GenerationJob.objects.get(user=self.user)
        self.assertEqual(job.status, "failed")
        self.assertTrue(job.refunded)

    def test_broker_failure_does_not_refund_twice(self):
        """Задача успела вернуть токены сама — второй возврат выдал бы их даром."""
        from unittest.mock import patch

        from billing.services import refund

        def fail_after_refund(job):
            refund(job.user, job.tokens_charged, "test")
            job.refunded = True
            job.status = "failed"
            job.save()
            raise RuntimeError("upala pri otpravke")

        with patch("generation.views.enqueue", side_effect=fail_after_refund):
            self._post()

        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 10)

    # --- повторная доставка сообщения -------------------------------------

    def test_task_refuses_to_rerun_a_job_that_is_not_queued(self):
        """Повторная доставка не должна оплачивать генерацию второй раз."""
        from unittest.mock import patch

        from generation.tasks import run_generation_job

        job = GenerationJob.objects.create(
            user=self.user, content_type="card", status="processing", tokens_charged=1, settings={"variants": 1}
        )

        with patch("generation.tasks.get_provider") as provider:
            run_generation_job(job.pk)

        self.assertFalse(provider.called, "провайдер не должен вызываться повторно")
        job.refresh_from_db()
        self.assertEqual(job.status, "processing")

    def test_task_on_deleted_job_is_not_an_error(self):
        from generation.tasks import run_generation_job

        run_generation_job(999999)  # проект удалили, пока задача ждала очереди

    # --- зависшие задачи ---------------------------------------------------

    def test_stuck_job_is_released_and_refunded(self):
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils import timezone

        job = GenerationJob.objects.create(
            user=self.user, content_type="card", status="processing", tokens_charged=3
        )
        # updated_at стоит auto_now, поэтому подменяем запросом.
        GenerationJob.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(hours=2))

        call_command("release_stuck_jobs", verbosity=0)

        job.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertTrue(job.refunded)
        self.assertEqual(self.user.balance, 13, "3 токена обязаны вернуться")

    def test_running_job_within_the_window_is_left_alone(self):
        """Живой пакет из пяти картинок идёт долго — трогать его нельзя."""
        from django.core.management import call_command

        job = GenerationJob.objects.create(
            user=self.user, content_type="card", status="processing", tokens_charged=5
        )

        call_command("release_stuck_jobs", verbosity=0)

        job.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(job.status, "processing")
        self.assertEqual(self.user.balance, 10)

    def test_dry_run_changes_nothing(self):
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils import timezone

        job = GenerationJob.objects.create(
            user=self.user, content_type="card", status="processing", tokens_charged=2
        )
        GenerationJob.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(hours=2))

        call_command("release_stuck_jobs", "--dry-run", verbosity=0)

        job.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(job.status, "processing")
        self.assertEqual(self.user.balance, 10)


def _png_bytes(width=108, height=144, color=(30, 90, 200)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _png_data_url(width=108, height=144, color=(30, 90, 200)):
    import base64

    return "data:image/png;base64," + base64.b64encode(_png_bytes(width, height, color)).decode("ascii")


class TextLayersParsingTests(TestCase):
    """Magic Layers v2: GPT javobi → textLayers (px), maska."""

    def test_normalized_boxes_become_pixels(self):
        from generation.text_layers import parse_text_blocks

        payload = {"blocks": [{
            "text": "Oʻzbekcha gʻaroyib\\nmatn", "x": 0.1, "y": 0.05, "w": 0.8, "h": 0.1,
            "font_size_px": 60, "font_weight": 750, "color": "fff", "align": "CENTER",
            "on_plate": True, "plate_color": "#ff0000", "uppercase": "true",
        }]}
        [layer] = parse_text_blocks(payload, 1080, 1440)

        self.assertTrue(layer["id"].startswith("text_"))
        self.assertEqual(layer["text"], "Oʻzbekcha gʻaroyib\nmatn")
        self.assertEqual((layer["x"], layer["y"], layer["width"], layer["height"]), (108, 72, 864, 144))
        self.assertEqual(layer["fontSize"], 60)
        self.assertIn(layer["fontWeight"], (700, 800))
        self.assertEqual(layer["color"], "#FFFFFF")
        self.assertEqual(layer["align"], "center")
        self.assertEqual(layer["background"], "#FF0000")
        self.assertTrue(layer["uppercase"])
        self.assertEqual(layer["fontFamily"], "Inter")
        self.assertEqual(layer["lineHeight"], 1.15)
        self.assertEqual(layer["rotation"], 0)

    def test_json_string_pixels_fallback_and_clamping(self):
        from generation.text_layers import parse_text_blocks

        raw = '```json\n{"blocks": [{"text": "Привет", "x": 100, "y": 1400, "w": 2000, "h": 100, "font_weight": 400, "color": "red-ish", "align": "justify"}, {"text": "  "}]}\n```'
        [layer] = parse_text_blocks(raw, 1080, 1440)
        self.assertEqual(layer["text"], "Привет")
        self.assertEqual(layer["x"], 100)
        self.assertEqual(layer["y"], 1400)
        self.assertLessEqual(layer["x"] + layer["width"], 1080)
        self.assertLessEqual(layer["y"] + layer["height"], 1440)
        self.assertEqual(layer["color"], "#000000")
        self.assertEqual(layer["align"], "left")
        self.assertIsNone(layer["background"])
        self.assertGreaterEqual(layer["fontSize"], 8)

    def test_detect_text_blocks_calls_openai_and_converts(self):
        import json
        import os
        from unittest.mock import MagicMock, patch

        from generation.text_layers import detect_text_blocks

        content = json.dumps({"blocks": [{"text": "Chegirma 50%", "x": 0.5, "y": 0.5, "w": 0.25, "h": 0.05,
                                          "font_size_px": 5, "font_weight": 800, "color": "#112233",
                                          "align": "right", "on_plate": False, "plate_color": "#000000",
                                          "uppercase": False}]})
        response = MagicMock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": content}}]}

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
                patch("generation.text_layers.requests.post", return_value=response) as post:
            [layer] = detect_text_blocks(_png_bytes(200, 400))

        body = post.call_args.kwargs["json"]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("200x400", body["messages"][0]["content"][0]["text"])
        self.assertEqual((layer["x"], layer["y"], layer["width"], layer["height"]), (100, 200, 50, 20))
        self.assertIsNone(layer["background"], "on_plate=false bo'lsa plashka rangi yo'q")
        self.assertEqual(layer["align"], "right")

    def test_detect_without_key_or_on_error_returns_empty(self):
        import os
        from unittest.mock import patch

        import requests

        from generation.text_layers import detect_text_blocks

        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(detect_text_blocks(_png_bytes()), [])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
                patch("generation.text_layers.requests.post", side_effect=requests.ConnectionError("down")):
            self.assertEqual(detect_text_blocks(_png_bytes()), [])

    def test_mask_covers_only_dilated_glyph_boxes(self):
        from generation.text_layers import build_text_mask

        mask = build_text_mask((200, 200), [{"x": 50, "y": 50, "width": 40, "height": 20}], dilate=10)
        self.assertEqual(mask.mode, "L")
        self.assertEqual(mask.size, (200, 200))
        self.assertEqual(mask.getpixel((60, 60)), 255)   # harf ichida
        self.assertEqual(mask.getpixel((42, 42)), 255)   # kengaytirilgan chet
        self.assertEqual(mask.getpixel((35, 60)), 0)     # chetdan tashqari
        self.assertEqual(mask.getpixel((150, 150)), 0)   # fon / plashka


class MagicLayersPipelineTests(TestCase):
    def setUp(self):
        import tempfile

        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)

    def _env(self):
        import os
        from unittest.mock import patch

        return patch.dict(os.environ, {"FAL_KEY": "fal-test", "FAL_MOCK": "0"})

    def _blocks(self):
        return [{"id": "text_abc", "text": "Salom", "x": 10, "y": 10, "width": 40, "height": 20,
                 "fontSize": 18, "fontWeight": 700, "color": "#FFFFFF", "align": "left",
                 "fontFamily": "Inter", "lineHeight": 1.15, "uppercase": False, "background": None, "rotation": 0}]

    def test_inpaint_failure_falls_back_to_original_image(self):
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers
        from generation.providers import GenerationError

        source = _png_data_url()
        layer_png = _png_data_url(color=(0, 0, 0))
        calls = []

        def fake_fal(endpoint, payload, timeout=0):
            calls.append((endpoint, payload))
            if endpoint == layers.FAL_INPAINT_ENDPOINT:
                raise GenerationError("inpaint down", status=502)
            return {"images": [{"url": layer_png}, {"url": layer_png}]}

        with self._env(), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=self._blocks()), \
                patch("generation.layers._fal_run", side_effect=fake_fal):
            out = layers.decompose_layers(source, user_id=1)

        self.assertFalse(out["textRemoved"])
        self.assertEqual(out["layersVersion"], 2)
        self.assertEqual(len(out["textLayers"]), 1)
        self.assertEqual(len(out["layers"]), 2)
        self.assertEqual(out["layers"][0]["name"], "Orqa fon")
        qwen_payload = calls[-1][1]
        self.assertEqual(calls[-1][0], layers.FAL_ENDPOINT)
        self.assertEqual(qwen_payload["image_url"], source, "qwen asl rasmda ishlashi kerak")

    def test_successful_inpaint_feeds_clean_image_to_qwen_at_original_size(self):
        import base64
        import io
        from unittest.mock import patch

        from django.test import override_settings
        from PIL import Image

        from generation import layers

        source = _png_data_url(108, 144)
        # Fill modeli boshqa o'lchamda qaytardi — biz asl o'lchamga qaytaramiz.
        inpainted = _png_data_url(64, 64, color=(255, 0, 0))
        layer_png = _png_data_url(color=(0, 0, 0))
        calls = []

        def fake_fal(endpoint, payload, timeout=0):
            calls.append((endpoint, payload))
            if endpoint == layers.FAL_INPAINT_ENDPOINT:
                return {"images": [{"url": inpainted}]}
            return {"images": [{"url": layer_png}]}

        with self._env(), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=self._blocks()), \
                patch("generation.layers._fal_run", side_effect=fake_fal):
            out = layers.decompose_layers(source, user_id=1)

        self.assertTrue(out["textRemoved"])
        inpaint_payload = calls[0][1]
        self.assertTrue(inpaint_payload["mask_url"].startswith("data:image/png;base64,"))
        self.assertIn("no text", inpaint_payload["prompt"])
        clean_url = calls[1][1]["image_url"]
        self.assertNotEqual(clean_url, source)
        clean = Image.open(io.BytesIO(base64.b64decode(clean_url.split(",", 1)[1])))
        self.assertEqual(clean.size, (108, 144))
        self.assertEqual(clean.getpixel((25, 18))[:3], (255, 0, 0))     # matn joyi — inpaint
        self.assertEqual(clean.getpixel((100, 130))[:3], (30, 90, 200))  # qolgani — asl rasm

    def test_text_detection_failure_returns_image_layers_only(self):
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers

        layer_png = _png_data_url(color=(0, 0, 0))
        with self._env(), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=[]), \
                patch("generation.layers._fal_run", return_value={"images": [{"url": layer_png}]}) as fal:
            out = layers.decompose_layers(_png_data_url(), user_id=1)

        self.assertEqual(out["textLayers"], [])
        self.assertFalse(out["textRemoved"])
        self.assertEqual(fal.call_count, 1, "matn bo'lmasa inpaint chaqirilmaydi")

    def test_mock_mode_returns_sample_text_layers(self):
        import os
        from unittest.mock import patch

        from generation import layers

        with patch.dict(os.environ, {"FAL_MOCK": "1"}):
            out = layers.decompose_layers(_png_data_url(108, 144), user_id=1)
        self.assertEqual(len(out["layers"]), 1)
        self.assertEqual(out["layers"][0]["width"], 108)
        self.assertGreaterEqual(len(out["textLayers"]), 1)
        self.assertEqual(out["layersVersion"], 2)


class MagicLayersViewCacheTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User(username="layers@test.uz", email="layers@test.uz", name="Layers", balance=5)
        self.user.set_password("testpass123")
        self.user.save()
        self.client.force_authenticate(self.user)

    def _job(self, result_extra):
        result = {"id": "r1", "url": PIXEL_PNG, "width": 1, "height": 1, **result_extra}
        return GenerationJob.objects.create(user=self.user, content_type="card", status="success", results=[result])

    def _decomposed(self):
        return {
            "layers": [{"id": "layer_1", "order": 1, "name": "Orqa fon", "url": PIXEL_PNG, "width": 1, "height": 1}],
            "textLayers": [{"id": "text_1", "text": "Yangi"}],
            "textRemoved": True,
            "layersVersion": 2,
        }

    def test_old_cache_without_version_is_recomputed(self):
        from unittest.mock import patch

        job = self._job({"layers": [{"id": "old", "url": PIXEL_PNG}]})
        with patch("generation.views.decompose_layers", return_value=self._decomposed()) as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {}, format="json")

        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(decompose.called)
        body = r.json()
        self.assertEqual(body["textLayers"][0]["text"], "Yangi")
        self.assertTrue(body["textRemoved"])
        self.assertEqual(body["layersVersion"], 2)
        job.refresh_from_db()
        self.assertEqual(job.results[0]["layersVersion"], 2)
        self.assertEqual(job.results[0]["textLayers"][0]["id"], "text_1")
        self.assertTrue(job.results[0]["textRemoved"])

    def test_v2_cache_is_returned_with_text_layers(self):
        from unittest.mock import patch

        job = self._job(self._decomposed())
        with patch("generation.views.decompose_layers") as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {}, format="json")

        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(decompose.called)
        body = r.json()
        self.assertTrue(body["cached"])
        self.assertEqual(body["textLayers"], [{"id": "text_1", "text": "Yangi"}])
        self.assertTrue(body["textRemoved"])
        self.assertEqual(body["layersVersion"], 2)

    def test_force_recomputes_v2_cache(self):
        from unittest.mock import patch

        job = self._job(self._decomposed())
        with patch("generation.views.decompose_layers", return_value=self._decomposed()) as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {"force": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(decompose.called)


class StylePresetPromptTests(TestCase):
    """Karta uslubi (Minimalizm, Premium, ...) promptga haqiqatan tushishi kerak."""

    brief = {"title": "Soat", "subtitle": "", "category": "Aksessuar", "benefits": ["Suvga chidamli"]}

    def test_preset_added_without_reference(self):
        from .prompts import build_prompt, compact_prompt, safe_minimal_prompt

        settings = {"contentType": "card", "stylePreset": "premium"}
        self.assertIn("PREMIUM", build_prompt(self.brief, settings, 1, False))
        self.assertIn("PREMIUM", compact_prompt(self.brief, settings, 1, False))
        self.assertIn("PREMIUM", safe_minimal_prompt(self.brief, settings, 1))

    def test_auto_and_reference_have_no_preset(self):
        from .prompts import build_prompt

        self.assertNotIn("DESIGN STYLE CHOSEN", build_prompt(self.brief, {"contentType": "card", "stylePreset": ""}, 1, False))
        self.assertNotIn(
            "DESIGN STYLE CHOSEN",
            build_prompt(self.brief, {"contentType": "card", "stylePreset": "tech", "designReferenceDataUrl": "x"}, 1, True),
        )
