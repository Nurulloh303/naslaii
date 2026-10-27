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
    """Magic Layers v3: GPT javobi → textLayers (px), shrift xaritasi, o'lcham."""

    def test_normalized_boxes_become_pixels(self):
        from generation.text_layers import parse_text_blocks

        payload = {"blocks": [{
            "text": "Oʻzbekcha gʻaroyib\\nmatn", "x": 0.1, "y": 0.05, "w": 0.8, "h": 0.1,
            "font_style": "sans", "geometric": True, "font_weight": 750, "color": "fff", "align": "CENTER",
            "on_plate": True, "plate_color": "#ff0000", "uppercase": "true",
        }]}
        [layer] = parse_text_blocks(payload, 1080, 1440)

        self.assertTrue(layer["id"].startswith("text_"))
        self.assertEqual(layer["text"], "Oʻzbekcha gʻaroyib\nmatn")
        self.assertEqual((layer["x"], layer["y"], layer["width"], layer["height"]), (108, 72, 864, 144))
        self.assertIn(layer["fontWeight"], (700, 800))
        self.assertEqual(layer["color"], "#FFFFFF")
        self.assertEqual(layer["align"], "center")
        self.assertIsNone(layer["background"], "v3: plashka rasmda qoladi")
        self.assertTrue(layer["uppercase"])
        self.assertEqual(layer["fontFamily"], "Montserrat")
        self.assertEqual(layer["letterSpacing"], 0.0)
        self.assertEqual(layer["rotation"], 0)
        self.assertEqual(layer["source"], {"x": 108, "y": 72, "width": 864, "height": 144})
        # 2 qator, bosh harflar: 144 = (1·1.15 + 0.70)·fs → fs ≈ 78
        self.assertTrue(70 <= layer["fontSize"] <= 85, layer["fontSize"])

    def test_product_labels_skipped_and_script_kept(self):
        from generation.text_layers import parse_text_blocks

        payload = {"blocks": [
            {"text": "BRAND", "x": 0.4, "y": 0.5, "w": 0.1, "h": 0.02, "product_label": True},
            {"text": "Dam olish uchun ideal", "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.04, "font_style": "condensed"},
        ]}
        [layer] = parse_text_blocks(payload, 1080, 1440)
        self.assertEqual(layer["text"], "Dam olish uchun ideal")
        self.assertEqual(layer["fontFamily"], "Oswald")

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
        self.assertEqual(layer["fontFamily"], "Inter")
        self.assertGreaterEqual(layer["fontSize"], 8)

    def test_font_family_mapping(self):
        from generation.text_layers import FONT_FAMILIES, map_font_family

        self.assertEqual(map_font_family("condensed", 700), "Oswald")
        self.assertEqual(map_font_family("condensed", 800, display=True), "Anton")
        self.assertEqual(map_font_family("condensed", 900), "Anton")
        self.assertEqual(map_font_family("sans", 400), "Inter")
        self.assertEqual(map_font_family("sans", 800), "Montserrat")
        self.assertEqual(map_font_family("sans", 800, geometric=False), "Inter")
        self.assertEqual(map_font_family("serif", 700), "Playfair Display")
        self.assertEqual(map_font_family("rounded", 700), "Nunito")
        self.assertEqual(map_font_family("script", 400), "Caveat")
        self.assertEqual(map_font_family("comic", 400), "Inter")
        self.assertEqual(map_font_family(None, 400), "Inter")
        for style in ("condensed", "sans", "serif", "rounded", "script", "x"):
            self.assertIn(map_font_family(style, 700), FONT_FAMILIES)

    def test_font_size_from_box_height(self):
        from generation.text_layers import compute_font_size

        # Bir qator bosh harf: siyoh balandligi ≈ 0.73·fs (Inter)
        size, lh = compute_font_size("SALOM", 73, "Inter", True)
        self.assertTrue(95 <= size <= 105, size)
        self.assertEqual(lh, 1.15)
        # Tushirmali kichik harflar: ≈ 0.97·fs
        size, _ = compute_font_size("yetkazib", 97, "Inter", False)
        self.assertTrue(92 <= size <= 108, size)
        # OCR qator qadami ma'lum: lineHeight shundan
        size, lh = compute_font_size("AAA\nBBB", 73 + 120, "Inter", True, line_pitch=120)
        self.assertTrue(95 <= size <= 105, size)
        self.assertAlmostEqual(lh, 1.2, delta=0.05)

    def test_prompt_forbids_transliteration(self):
        from generation.text_layers import build_prompt

        prompt = build_prompt(1080, 1440)
        self.assertIn("1080x1440", prompt)
        self.assertIn("NEVER transliterate", prompt)
        self.assertIn("font_style", prompt)
        self.assertIn("product_label", prompt)

    def test_detect_text_blocks_calls_openai_and_converts(self):
        import json
        import os
        from unittest.mock import MagicMock, patch

        from generation.text_layers import detect_text_blocks

        content = json.dumps({"blocks": [{"text": "Chegirma 50%", "x": 0.5, "y": 0.5, "w": 0.25, "h": 0.05,
                                          "font_style": "serif", "font_weight": 800, "color": "#112233",
                                          "align": "right", "uppercase": False}]})
        response = MagicMock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": content}}]}

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
                patch("generation.text_layers.requests.post", return_value=response) as post:
            [layer] = detect_text_blocks(_png_bytes(200, 400))

        body = post.call_args.kwargs["json"]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("200x400", body["messages"][0]["content"][0]["text"])
        self.assertEqual((layer["x"], layer["y"], layer["width"], layer["height"]), (100, 200, 50, 20))
        self.assertEqual(layer["fontFamily"], "Playfair Display")
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


class OcrMatchingTests(TestCase):
    """Florence-2 OCR javobini o'qish va GPT bloklariga biriktirish."""

    def _layer(self, text, x, y, w, h):
        return {"id": "t", "text": text, "x": x, "y": y, "width": w, "height": h, "color": "#000000",
                "fontFamily": "Inter", "uppercase": False}

    def test_parse_ocr_formats(self):
        from generation.text_layers import parse_ocr_results

        dict_boxes = {"results": {"quad_boxes": [{"x": 10, "y": 20, "w": 100, "h": 30, "label": "</s>SALOM"}]}}
        [line] = parse_ocr_results(dict_boxes, 1080, 1440)
        self.assertEqual((line["x0"], line["y0"], line["x1"], line["y1"]), (10, 20, 110, 50))
        self.assertEqual(line["text"], "SALOM")

        flat = {"results": {"quad_boxes": [[10, 20, 110, 21, 109, 50, 9, 49]], "labels": ["Salom"]}}
        [line] = parse_ocr_results(flat, 1080, 1440)
        self.assertEqual((line["x0"], line["y0"], line["x1"], line["y1"]), (9, 20, 110, 50))
        self.assertEqual(line["text"], "Salom")

        points = {"results": {"quad_boxes": [{"points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.1},
                                                         {"x": 0.2, "y": 0.2}, {"x": 0.1, "y": 0.2}],
                                              "label": "x"}]}}
        [line] = parse_ocr_results(points, 1000, 1000)
        self.assertEqual((round(line["x0"]), round(line["y1"])), (100, 200))

        self.assertEqual(parse_ocr_results({"results": "broken"}, 10, 10), [])
        self.assertEqual(parse_ocr_results(None, 10, 10), [])

    def test_lines_matched_by_position_and_text_then_unioned(self):
        from generation.text_layers import match_ocr_lines

        layers = [
            self._layer("DAM OLISH UCHUN\nIDEAL TANLOV", 100, 100, 800, 200),
            self._layer("Yetkazib berish bepul", 100, 1200, 600, 50),
        ]
        ocr = [
            {"text": "DAM OLISH UCHUN", "x0": 80, "y0": 95, "x1": 930, "y1": 190},
            {"text": "IDEAL TANLOV", "x0": 200, "y0": 210, "x1": 800, "y1": 300},
            # GPT qutisi butunlay siljigan, lekin matn deyarli bir xil
            {"text": "Yetkazib berish bepul", "x0": 120, "y0": 1100, "x1": 720, "y1": 1150},
            # Mahsulotdagi yozuv — hech kimga mos emas
            {"text": "BRANDX", "x0": 500, "y0": 700, "x1": 600, "y1": 730},
        ]
        matches = match_ocr_lines(layers, ocr)
        self.assertEqual([ln["text"] for ln in matches[0]], ["DAM OLISH UCHUN", "IDEAL TANLOV"])
        self.assertEqual([ln["text"] for ln in matches[1]], ["Yetkazib berish bepul"])
        self.assertEqual(sum(len(v) for v in matches.values()), 3)

    def test_garbled_ocr_inside_box_still_matches(self):
        from generation.text_layers import match_ocr_lines

        layers = [self._layer("Oʻzbekiston boʻylab", 100, 100, 600, 60)]
        ocr = [{"text": "Ozbekiston bo'ylab", "x0": 110, "y0": 105, "x1": 690, "y1": 150}]
        self.assertEqual(len(match_ocr_lines(layers, ocr)[0]), 1)

    def test_refine_uses_ocr_union_then_tightens(self):
        import numpy as np
        from PIL import Image, ImageDraw

        from generation.text_layers import refine_layers

        img = Image.new("RGB", (600, 400), (240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.rectangle([100, 100, 400, 140], fill=(0, 0, 0))   # "1-qator"
        draw.rectangle([100, 170, 300, 210], fill=(0, 0, 0))   # "2-qator"
        rgb = np.array(img)
        layer = self._layer("SALOM\nDUNYO", 50, 50, 200, 50)  # GPT qutisi noto'g'ri
        layer["uppercase"] = True
        ocr = [
            {"text": "SALOM", "x0": 96, "y0": 96, "x1": 404, "y1": 144},
            {"text": "DUNYO", "x0": 96, "y0": 166, "x1": 304, "y1": 214},
        ]
        [out] = refine_layers(rgb, [layer], ocr)
        self.assertEqual((out["x"], out["y"], out["width"], out["height"]), (100, 100, 301, 111))
        self.assertLessEqual(out["source"]["x"], 100 - 6)
        self.assertGreaterEqual(out["source"]["x"] + out["source"]["width"], 401 + 6)
        self.assertGreater(out["lineHeight"], 1.0)

    def test_refine_without_ocr_tightens_gpt_box(self):
        import numpy as np
        from PIL import Image, ImageDraw

        from generation.text_layers import refine_layers

        img = Image.new("RGB", (600, 400), (20, 60, 200))
        ImageDraw.Draw(img).rectangle([100, 100, 300, 140], fill=(255, 255, 255))
        layer = self._layer("SALOM", 110, 108, 170, 28)   # glyflarni kesib qo'ygan
        layer["color"] = "#FFFFFF"
        [out] = refine_layers(np.array(img), [layer], [])
        self.assertEqual((out["x"], out["y"], out["width"], out["height"]), (100, 100, 201, 41))


class TextEraserTests(TestCase):
    """Lokal OpenCV eraser: harflar o'chadi, qolgan piksellar bayt-ma-bayt bir xil."""

    def _plate_image(self):
        import io

        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", (400, 300), (30, 120, 200))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([60, 80, 340, 200], 20, fill=(255, 212, 0))
        font = ImageFont.load_default(size=48)
        draw.text((90, 110), "SALE 50%", font=font, fill=(0, 0, 0))
        bbox = draw.textbbox((90, 110), "SALE 50%", font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), bbox

    def test_plate_text_erased_and_rest_identical(self):
        import io

        import numpy as np
        from PIL import Image

        from generation.text_eraser import erase_text, pad_box, source_pad

        png, bbox = self._plate_image()
        box = {"x": bbox[0], "y": bbox[1], "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1]}
        layer = {"id": "t", "color": "#000000", **box, "source": pad_box(box, source_pad(box["height"]), 400, 300)}
        out = erase_text(png, [layer])

        before = np.array(Image.open(io.BytesIO(png)).convert("RGB")).astype(int)
        after = np.array(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)
        self.assertEqual(after.shape, before.shape)

        src = layer["source"]
        region = after[src["y"]:src["y"] + src["height"], src["x"]:src["x"] + src["width"]]
        diff = np.abs(region - np.array([255, 212, 0])).max(axis=2)
        self.assertGreater((diff <= 12).mean(), 0.995, "matn joyi plashka rangida bo'lishi kerak")
        self.assertFalse((region.sum(axis=2) < 200).any(), "qora harf qolmasligi kerak")

        outside = np.ones(before.shape[:2], dtype=bool)
        outside[src["y"]:src["y"] + src["height"], src["x"]:src["x"] + src["width"]] = False
        self.assertTrue((after[outside] == before[outside]).all(), "source'dan tashqarisi o'zgarmaydi")
        # Plashka chetlari ham joyida
        self.assertEqual(tuple(after[85, 200]), (255, 212, 0))
        self.assertEqual(tuple(after[10, 10]), (30, 120, 200))

    def test_textured_background_uses_inpaint_and_stays_inside_source(self):
        import io

        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        from generation.text_eraser import erase_text

        rng = np.random.default_rng(0)
        yy, xx = np.mgrid[0:300, 0:400]
        base = np.stack([80 + yy * 0.4, 100 + xx * 0.2, 160 - yy * 0.2], axis=2) + rng.normal(0, 10, (300, 400, 3))
        img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
        ImageDraw.Draw(img).text((80, 120), "Yangi", font=ImageFont.load_default(size=56), fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        source = {"x": 70, "y": 110, "width": 200, "height": 80}
        out = erase_text(buf.getvalue(), [{"id": "t", "color": "#FFFFFF", "source": source}])

        before = np.array(img).astype(int)
        after = np.array(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)
        region = after[110:190, 70:270]
        self.assertLess(int((region.min(axis=2) > 235).sum()), 15, "oq harflar qolmasligi kerak")
        outside = np.ones((300, 400), dtype=bool)
        outside[110:190, 70:270] = False
        self.assertTrue((after[outside] == before[outside]).all())

    def test_tighten_ignores_plate_edges(self):
        import numpy as np
        from PIL import Image, ImageDraw

        from generation.text_eraser import tighten_box

        img = Image.new("RGB", (500, 300), (240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.rectangle([50, 80, 450, 220], fill=(220, 30, 60))      # plashka
        draw.rectangle([120, 120, 380, 180], fill=(255, 255, 255))  # "matn"
        rgb = np.array(img)
        tight = tighten_box(rgb, {"x": 130, "y": 128, "width": 230, "height": 45}, text_color="#FFFFFF")
        self.assertEqual(tight, {"x": 120, "y": 120, "width": 261, "height": 61})


def _data_url_png(img):
    import base64
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _decode_data_url_rgb(url):
    import base64
    import io

    import numpy as np
    from PIL import Image

    return np.array(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB"))


class TextDedupeTests(TestCase):
    """v4: takroriy matnlar, OCR qatori faqat bitta blokka, fontSize siqilishi (209px)."""

    def _layer(self, text, x, y, w, h, **extra):
        return {"id": f"t_{text}_{x}_{y}", "text": text, "x": x, "y": y, "width": w, "height": h,
                "color": "#FFFFFF", "fontFamily": "Inter", "uppercase": True, **extra}

    def test_same_text_same_place_dropped_other_place_kept(self):
        from generation.text_layers import dedupe_layers

        layers = [
            self._layer("Yuqori", 100, 100, 200, 40),
            self._layer("Yuqori", 105, 102, 190, 38),      # takror
            self._layer("Yuqori", 600, 900, 200, 40),      # boshqa kartadagi — qoladi
            self._layer("Sport", 100, 300, 150, 40),
            self._layer("SPORT", 110, 305, 140, 36),       # takror (katta-kichik harf)
        ]
        out = dedupe_layers(layers)
        self.assertEqual(sorted((l["text"], l["x"]) for l in out),
                         [("Sport", 100), ("Yuqori", 100), ("Yuqori", 600)])

    def test_parse_drops_gpt_duplicates(self):
        from generation.text_layers import parse_text_blocks

        payload = {"blocks": [
            {"text": "Sport", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.03},
            {"text": "Sport", "x": 0.105, "y": 0.1, "w": 0.2, "h": 0.03},
        ]}
        self.assertEqual(len(parse_text_blocks(payload, 1080, 1440)), 1)

    def test_ocr_line_assigned_to_one_block_only(self):
        from generation.text_layers import match_ocr_lines

        layers = [self._layer("Yuqori tezlik", 100, 100, 300, 40), self._layer("Yuqori", 100, 100, 150, 40)]
        ocr = [{"text": "Yuqori tezlik", "x0": 98, "y0": 98, "x1": 402, "y1": 142}]
        matches = match_ocr_lines(layers, ocr)
        self.assertEqual(sum(len(v) for v in matches.values()), 1)
        self.assertEqual(list(matches), [0])

    def test_ocr_line_of_wrong_size_not_matched(self):
        from generation.text_layers import match_ocr_lines

        # Kichik "Sport" qutisi katta sarlavha qatori ichida — sarlavha qatori unga tegishli emas.
        layers = [self._layer("Sport", 400, 150, 120, 40)]
        ocr = [{"text": "AVTOMOBIL", "x0": 100, "y0": 100, "x1": 980, "y1": 260}]
        self.assertEqual(match_ocr_lines(layers, ocr), {})

    def test_clamp_font_size(self):
        from generation.text_layers import clamp_font_size

        self.assertEqual(clamp_font_size("Sport", 209, 40), 58)
        self.assertEqual(clamp_font_size("A\nB", 209, 100), 72)
        self.assertEqual(clamp_font_size("Sport", 50, 40), 50)

    def test_regression_209px_misplaced_box_on_headline(self):
        """Production: "Sport" qutisi sarlavha ustiga tushdi, OCR uni topmadi → fontSize 209 va takror."""
        import numpy as np
        from PIL import Image, ImageDraw

        from generation.text_layers import refine_layers

        img = Image.new("RGB", (1080, 1440), (12, 16, 30))
        draw = ImageDraw.Draw(img)
        for i in range(9):  # "AVTOMOBIL" — 9 ta harf, balandligi 160px
            draw.rectangle([100 + i * 98, 100, 170 + i * 98, 260], fill=(255, 255, 255))
        draw.rectangle([100, 1000, 250, 1032], fill=(255, 255, 255))  # haqiqiy "Sport"
        rgb = np.array(img)
        layers = [
            self._layer("AVTOMOBIL", 100, 100, 880, 160),
            self._layer("Sport", 400, 150, 120, 40),   # noto'g'ri joy (sarlavha ichida)
            self._layer("Sport", 405, 152, 118, 38),   # GPT takrori
        ]
        ocr = [{"text": "AVTOMOBIL", "x0": 98, "y0": 98, "x1": 982, "y1": 262}]
        out = refine_layers(rgb, layers, ocr)
        texts = [l["text"] for l in out]
        self.assertEqual(texts.count("AVTOMOBIL"), 1)
        self.assertLessEqual(texts.count("Sport"), 1)
        for layer in out:
            single = layer["height"] / len(layer["text"].split("\n"))
            self.assertLessEqual(layer["fontSize"], 1.4 * single + 1, layer)
            if layer["text"] == "Sport":
                self.assertLess(layer["fontSize"], 100, "Sport sarlavha o'lchamini olmasligi kerak")
        headline = next(l for l in out if l["text"] == "AVTOMOBIL")
        self.assertTrue(180 <= headline["fontSize"] <= 225, headline["fontSize"])

    def test_regression_single_line_block_does_not_absorb_lines_below(self):
        """Production 209px: bir qatorli "Sport" bloki pastdagi OCR qatorlarini ham olib, balandligi oshardi."""
        import numpy as np
        from PIL import Image, ImageDraw

        from generation.text_layers import refine_layers

        img = Image.new("RGB", (1080, 1440), (12, 16, 30))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([80, 960, 420, 1200], 20, fill=(40, 50, 80))
        rows = [(1000, 1040), (1056, 1096), (1112, 1152)]
        for y0, y1 in rows:
            for i in range(5):
                draw.rectangle([110 + i * 40, y0, 140 + i * 40, y1], fill=(255, 255, 255))
        layer = self._layer("Sport", 100, 990, 220, 170, uppercase=False)  # GPT qutisi 3 qatorni qamragan
        ocr = [{"text": t, "x0": 108, "y0": y0 - 2, "x1": 302, "y1": y1 + 2}
               for t, (y0, y1) in zip(("Sport", "Sport", "rejim"), rows)]
        [out] = refine_layers(np.array(img), [layer], ocr)
        self.assertLess(out["height"], 50, out)
        self.assertLess(out["fontSize"], 60, out)

        # Gorizontal bo'lingan bitta qator esa birlashtiriladi
        from generation.text_layers import match_ocr_lines

        split = [{"text": "Yuqori", "x0": 100, "y0": 100, "x1": 220, "y1": 140},
                 {"text": "tezlik", "x0": 230, "y0": 101, "x1": 340, "y1": 141}]
        matches = match_ocr_lines([self._layer("Yuqori tezlik", 100, 100, 240, 40)], split)
        self.assertEqual(len(matches[0]), 2)

    def test_font_family_from_gpt_and_fuzzy_styles(self):
        from generation.text_layers import map_font_family, parse_text_blocks

        self.assertEqual(map_font_family("sans", 800, geometric=False, font_family="Montserrat"), "Montserrat")
        self.assertEqual(map_font_family(None, 700, font_family="Bebas Neue"), "Anton")
        self.assertEqual(map_font_family(None, 700, font_family="Helvetica"), "Inter")
        self.assertEqual(map_font_family("bold sans-serif", 800), "Montserrat")
        self.assertEqual(map_font_family("geometric sans", 500), "Montserrat")
        self.assertEqual(map_font_family("condensed display", 700), "Anton")
        self.assertEqual(map_font_family(None, 700, font_family="Comic Sans"), "Inter")
        [layer] = parse_text_blocks({"blocks": [{"text": "AVTOMOBIL", "x": 0.1, "y": 0.1, "w": 0.8, "h": 0.1,
                                                 "font_family": "Oswald", "font_style": "sans"}]}, 1080, 1440)
        self.assertEqual(layer["fontFamily"], "Oswald")

    def test_parse_objects(self):
        from generation.text_layers import parse_objects

        payload = {"objects": [
            {"name": "Mashina", "kind": "product", "x": 0.1, "y": 0.4, "w": 0.8, "h": 0.3, "z": 5},
            {"name": "Ikonka kartasi", "kind": "card", "x": 0.05, "y": 0.2, "w": 0.2, "h": 0.1, "z": 2},
            {"name": "Fon", "x": 0, "y": 0, "w": 1, "h": 1},          # fon — tashlanadi
            {"name": "Dog'", "x": 0.5, "y": 0.5, "w": 0.001, "h": 0.001},  # juda mayda
            "broken",
        ]}
        objects = parse_objects(payload, 1080, 1440)
        self.assertEqual([o["name"] for o in objects], ["Mashina", "Ikonka kartasi"])
        self.assertEqual(objects[0]["kind"], "product")
        self.assertEqual(objects[1]["kind"], "graphic")
        self.assertEqual((objects[0]["x"], objects[0]["y"], objects[0]["width"], objects[0]["height"]),
                         (108, 576, 864, 432))
        self.assertEqual(parse_objects("not json", 10, 10), [])
        many = {"objects": [{"name": f"o{i}", "x": 0.01 * i, "y": 0.0, "w": 0.05, "h": 0.05} for i in range(30)]}
        self.assertEqual(len(parse_objects(many, 1080, 1440)), 16)

    def test_detect_layout_returns_blocks_and_objects(self):
        import json
        import os
        from unittest.mock import MagicMock, patch

        from generation.text_layers import detect_layout

        content = json.dumps({
            "blocks": [{"text": "AVTOMOBIL", "x": 0.1, "y": 0.05, "w": 0.8, "h": 0.1, "font_family": "Montserrat"}],
            "objects": [{"name": "Mashina", "kind": "product", "x": 0.1, "y": 0.4, "w": 0.8, "h": 0.3}],
        })
        response = MagicMock(ok=True)
        response.json.return_value = {"choices": [{"message": {"content": content}}]}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), \
                patch("generation.text_layers.requests.post", return_value=response) as post:
            blocks, objects = detect_layout(_png_bytes(200, 400))
        self.assertIn("objects", post.call_args.kwargs["json"]["messages"][0]["content"][0]["text"])
        self.assertEqual(blocks[0]["fontFamily"], "Montserrat")
        self.assertEqual(objects[0]["name"], "Mashina")


class ObjectLayersTests(TestCase):
    """Maska → RGBA kesma, fonni qaytarish, SAM/eraser sxemalari."""

    def test_rgba_cutout_from_mask(self):
        import io

        import numpy as np
        from PIL import Image

        from generation.object_layers import rgba_cutout

        rng = np.random.default_rng(1)
        rgb = rng.integers(0, 255, (100, 120, 3), dtype=np.uint8)
        mask = np.zeros((100, 120), dtype=bool)
        mask[20:60, 30:90] = True
        png, box = rgba_cutout(rgb, mask, pad=2)
        self.assertEqual(box, {"x": 28, "y": 18, "width": 64, "height": 44})
        out = np.array(Image.open(io.BytesIO(png)))
        self.assertEqual(out.shape, (44, 64, 4))
        self.assertTrue((out[2:42, 2:62, 3] == 255).all())
        self.assertTrue((out[2:42, 2:62, :3] == rgb[20:60, 30:90]).all())
        self.assertEqual(int(out[0, 0, 3]), 0)
        self.assertIsNone(rgba_cutout(rgb, np.zeros_like(mask)))

    def test_clean_mask_fills_holes_drops_specks_and_rejects_bad(self):
        import numpy as np

        from generation.object_layers import clean_mask

        size = (200, 200)
        box = {"x": 50, "y": 50, "width": 100, "height": 100}
        mask = np.zeros((200, 200), dtype=bool)
        mask[60:140, 60:140] = True
        mask[90:110, 90:110] = False        # teshik
        mask[5:8, 5:8] = True               # quti tashqarisidagi dog'
        mask[145:147, 60:62] = True         # quti ichidagi mayda dog'
        out = clean_mask(mask, box, size)
        self.assertTrue(out[100, 100], "teshik to'ldirilishi kerak")
        self.assertFalse(out[6, 6])
        self.assertFalse(out[146, 61])
        self.assertIsNone(clean_mask(np.zeros((200, 200), dtype=bool), box, size))
        full = np.ones((200, 200), dtype=bool)
        self.assertIsNone(clean_mask(full, {"x": 0, "y": 0, "width": 200, "height": 200}, size),
                          "rasmning >60% — fon")

    def test_decode_mask_resizes_and_reads_alpha(self):
        import io

        import numpy as np
        from PIL import Image

        from generation.object_layers import decode_mask

        gray = Image.new("L", (50, 50), 0)
        gray.paste(255, (10, 10, 30, 30))
        buf = io.BytesIO()
        gray.save(buf, format="PNG")
        mask = decode_mask(buf.getvalue(), (100, 100))
        self.assertEqual(mask.shape, (100, 100))
        self.assertTrue(mask[40, 40])
        self.assertFalse(mask[80, 80])

        rgba = np.zeros((20, 20, 4), dtype=np.uint8)
        rgba[..., :3] = 255
        rgba[5:10, 5:10, 3] = 255
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
        mask = decode_mask(buf.getvalue(), (20, 20))
        self.assertTrue(mask[7, 7])
        self.assertFalse(mask[15, 15])

    def test_paste_inside_keeps_outside_identical(self):
        import numpy as np

        from generation.object_layers import opencv_fill, paste_inside

        rng = np.random.default_rng(2)
        original = rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)
        filled = np.zeros((60, 80, 3), dtype=np.uint8)  # boshqa o'lcham — moslashtiriladi
        mask = np.zeros((120, 160), dtype=bool)
        mask[30:90, 40:120] = True
        out = paste_inside(original, filled, mask)
        self.assertTrue((out[~mask] == original[~mask]).all())
        self.assertTrue((out[40:80, 50:110] == 0).all())
        out = opencv_fill(original, mask)
        self.assertTrue((out[~mask] == original[~mask]).all())

    def test_payload_schemas(self):
        from generation.object_layers import eraser_payload, sam_payload

        payload = sam_payload("data:x", {"x": 10, "y": 20, "width": 30, "height": 40})
        self.assertEqual(payload["box_prompts"], [{"x_min": 10, "y_min": 20, "x_max": 40, "y_max": 60}])
        self.assertFalse(payload["apply_mask"])
        self.assertEqual(eraser_payload("a", "b"), {"image_url": "a", "mask_url": "b", "mask_type": "automatic"})

    def test_fill_background_falls_back_to_opencv(self):
        import numpy as np

        from generation.object_layers import fill_background
        from generation.providers import GenerationError

        rgb = np.full((80, 80, 3), 100, dtype=np.uint8)
        rgb[20:60, 20:60] = (255, 0, 0)
        mask = np.zeros((80, 80), dtype=bool)
        mask[15:65, 15:65] = True

        def failing(*args, **kwargs):
            raise GenerationError("down", status=502)

        out, method = fill_background(rgb, mask, failing, lambda url: b"", timeout=5)
        self.assertEqual(method, "opencv")
        self.assertTrue((np.abs(out[40, 40].astype(int) - 100) <= 3).all(), out[40, 40])
        self.assertTrue((out[~mask] == rgb[~mask]).all())


class MagicLayersPipelineTests(TestCase):
    """v4 quvuri: matn + obyektlar + fon (fal/OpenAI soxta)."""

    def setUp(self):
        import tempfile

        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)

    def _env(self, **extra):
        import os
        from unittest.mock import patch

        return patch.dict(os.environ, {"FAL_KEY": "fal-test", "FAL_MOCK": "0", **extra})

    def _card(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (300, 200), (30, 90, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([40, 40, 200, 70], fill=(255, 255, 255))        # "matn"
        draw.rectangle([150, 110, 260, 180], fill=(220, 20, 20))       # "mahsulot"
        return img, _data_url_png(img)

    def _blocks(self):
        return [{"id": "text_abc", "text": "SALOM", "x": 45, "y": 45, "width": 140, "height": 22,
                 "fontSize": 18, "fontWeight": 700, "color": "#FFFFFF", "align": "left",
                 "fontFamily": "Inter", "lineHeight": 1.15, "letterSpacing": 0.0, "uppercase": True,
                 "background": None, "rotation": 0, "source": {"x": 45, "y": 45, "width": 140, "height": 22}}]

    def _objects(self):
        return [{"name": "Mashina", "kind": "product", "x": 145, "y": 105, "width": 120, "height": 80, "z": 5}]

    def _read(self, url):
        import io

        import numpy as np
        from django.core.files.storage import default_storage
        from PIL import Image

        self.assertTrue(url.startswith("/api/layer-files/generated/user_1/layers/"), url)
        with default_storage.open(url[len("/api/layer-files/"):], "rb") as handle:
            return np.array(Image.open(io.BytesIO(handle.read())))

    def _fake_fal(self, calls, eraser_ok=True, sam_ok=True):
        from PIL import Image

        from generation import layers, object_layers

        def fake(endpoint, payload, timeout=0):
            calls.append((endpoint, payload))
            if endpoint == layers.FAL_OCR_ENDPOINT:
                return {"results": {"quad_boxes": [{"x": 38, "y": 38, "w": 165, "h": 35, "label": "SALOM"}]}}
            if endpoint == object_layers.FAL_SAM_ENDPOINT:
                if not sam_ok:
                    raise layers.GenerationError("sam down", status=502)
                box = payload["box_prompts"][0]
                mask = Image.new("L", (300, 200), 0)
                mask.paste(255, (150, 110, 261, 181))
                assert box["x_min"] <= 150 and box["x_max"] >= 260
                return {"image": {"url": _data_url_png(mask)}}
            if endpoint == object_layers.FAL_ERASER_ENDPOINT:
                if not eraser_ok:
                    raise layers.GenerationError("eraser down", status=502)
                return {"image": {"url": _data_url_png(Image.new("RGB", (300, 200), (30, 90, 200)))}}
            raise AssertionError(f"kutilmagan fal chaqiruvi: {endpoint}")
        return fake

    def _run(self, fake, detect=None, **env):
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers

        detect = detect if detect is not None else (self._blocks(), self._objects())
        _img, source = self._card()
        with self._env(**env), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_layout", return_value=detect), \
                patch("generation.layers._fal_run", side_effect=fake):
            out = layers.decompose_layers(source, user_id=1)
            files = {
                "base": self._read(out["baseUrl"]),
                "background": self._read(out["backgroundUrl"]),
                "elements": [self._read(el["url"]) for el in out["elements"]],
                "texts": [self._read(t["imageUrl"]) for t in out["textLayers"] if t["imageUrl"]],
                "clean": self._read(out["cleanUrl"]) if out["cleanUrl"] else None,
            }
        return out, files

    def test_full_pipeline_text_objects_background(self):
        import numpy as np

        from generation import layers, object_layers

        calls = []
        out, files = self._run(self._fake_fal(calls))
        endpoints = [c[0] for c in calls]
        self.assertEqual(sorted(endpoints), sorted([layers.FAL_OCR_ENDPOINT, object_layers.FAL_SAM_ENDPOINT,
                                                    object_layers.FAL_ERASER_ENDPOINT]))
        self.assertEqual(out["layersVersion"], 4)
        self.assertTrue(out["textRemoved"])
        self.assertTrue(out["objectsRemoved"])
        self.assertEqual((out["width"], out["height"]), (300, 200))
        img, _ = self._card()
        original = np.array(img)
        self.assertTrue((files["base"][..., :3] == original).all(), "baseUrl — asl rasm nusxasi")

        # SAM matnsiz (textClean) rasmda ishlaydi
        sam_input = _decode_data_url_rgb(next(p for e, p in calls if e == object_layers.FAL_SAM_ENDPOINT)["image_url"])
        self.assertLess(int(np.abs(sam_input[55, 120].astype(int) - [30, 90, 200]).max()), 12)

        [text] = out["textLayers"]
        self.assertEqual((text["x"], text["y"], text["width"], text["height"]), (40, 40, 161, 31))
        box = text["imageBox"]
        self.assertEqual(box, text["source"])
        cut = files["texts"][0]
        self.assertEqual(cut.shape, (box["height"], box["width"], 4))
        inside = cut[55 - box["y"], 120 - box["x"]]
        self.assertEqual(tuple(inside), (255, 255, 255, 255), "asl harf pikseli to'liq ko'rinadi")

        [element] = out["elements"]
        self.assertTrue(element["id"].startswith("el_"))
        self.assertEqual((element["name"], element["kind"], element["order"]), ("Mashina", "product", 1))
        el = files["elements"][0]
        self.assertEqual(el.shape, (element["height"], element["width"], 4))
        self.assertEqual(tuple(el[145 - element["y"], 200 - element["x"]]), (220, 20, 20, 255))

        bg = files["background"]
        self.assertEqual(tuple(bg[145, 200]), (30, 90, 200), "mahsulot o'rni to'ldirilgan")
        self.assertEqual(tuple(bg[55, 120]), tuple(files["clean"][55, 120]))
        clean = files["clean"]
        far = np.ones((200, 300), dtype=bool)
        far[95:196, 135:276] = False
        self.assertTrue((bg[far] == clean[far]).all(), "maskadan tashqarisi o'zgarmaydi")

    def test_eraser_failure_uses_opencv(self):
        out, files = self._run(self._fake_fal([], eraser_ok=False))
        self.assertTrue(out["objectsRemoved"])
        self.assertEqual(len(out["elements"]), 1)
        self.assertLess(int(abs(int(files["background"][145, 200][0]) - 30)), 40)

    def test_sam_failure_skips_object(self):
        out, files = self._run(self._fake_fal([], sam_ok=False))
        self.assertEqual(out["elements"], [])
        self.assertFalse(out["objectsRemoved"])
        self.assertTrue(out["textRemoved"])
        self.assertEqual(out["backgroundUrl"], out["cleanUrl"])

    def test_openai_failure_returns_original_background(self):
        import numpy as np

        calls = []
        out, files = self._run(self._fake_fal(calls), detect=([], []))
        self.assertEqual(out["textLayers"], [])
        self.assertEqual(out["elements"], [])
        self.assertFalse(out["textRemoved"])
        self.assertFalse(out["objectsRemoved"])
        self.assertEqual(out["backgroundUrl"], out["baseUrl"])
        img, _ = self._card()
        self.assertTrue((files["background"][..., :3] == np.array(img)).all())

    def test_no_fal_key_still_edits_text(self):
        calls = []
        out, files = self._run(self._fake_fal(calls), FAL_KEY="")
        self.assertEqual(calls, [])
        self.assertEqual(len(out["textLayers"]), 1)
        self.assertTrue(out["textLayers"][0]["imageUrl"])
        self.assertEqual(out["elements"], [])
        self.assertEqual(out["backgroundUrl"], out["cleanUrl"])

    def test_text_eraser_crash_never_500(self):
        from unittest.mock import patch

        with patch("generation.layers.te.erase_text_with_masks", side_effect=RuntimeError("boom")):
            out, files = self._run(self._fake_fal([]))
        self.assertIsNone(out["cleanUrl"])
        self.assertFalse(out["textRemoved"])
        self.assertEqual(len(out["textLayers"]), 1)
        self.assertIsNone(out["textLayers"][0]["imageUrl"])
        self.assertEqual(len(out["elements"]), 1, "obyektlar baribir ajratiladi")

    def test_mock_mode(self):
        import os
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers

        source = _png_data_url(108, 144)
        with patch.dict(os.environ, {"FAL_MOCK": "1"}), override_settings(MEDIA_ROOT=self._media.name):
            out = layers.decompose_layers(source, user_id=1)
            self.assertEqual(self._read(out["backgroundUrl"]).shape[:2], (144, 108))
            self.assertEqual(self._read(out["elements"][0]["url"]).shape[:2], (72, 54))
        self.assertEqual(out["backgroundUrl"], out["baseUrl"])
        self.assertEqual((out["width"], out["height"]), (108, 144))
        self.assertEqual(len(out["textLayers"]), 2)
        self.assertTrue(all(t["imageUrl"] and t["imageBox"] for t in out["textLayers"]))
        self.assertEqual(len(out["elements"]), 1)
        self.assertEqual(out["layersVersion"], 4)


class LayerFileEndpointTests(TestCase):
    def setUp(self):
        import tempfile

        from django.test import override_settings

        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        override = override_settings(MEDIA_ROOT=self._media.name)
        override.enable()
        self.addCleanup(override.disable)

    def _store(self, name="generated/user_1/layers/el_0123456789abcdef.png"):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        return default_storage.save(name, ContentFile(_png_bytes(4, 4)))

    def test_serves_file_with_cors_and_cache(self):
        from django.conf import settings

        path = self._store()
        origin = settings.CORS_ALLOWED_ORIGINS[0]
        r = self.client.get(f"/api/layer-files/{path}", HTTP_ORIGIN=origin)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/png")
        self.assertEqual(r["Access-Control-Allow-Origin"], origin)
        self.assertIn("max-age=2592000", r["Cache-Control"])
        self.assertEqual(b"".join(r.streaming_content), _png_bytes(4, 4))

    def test_foreign_origin_gets_no_cors_header(self):
        path = self._store()
        r = self.client.get(f"/api/layer-files/{path}", HTTP_ORIGIN="https://evil.example")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", r)

    def test_rejects_traversal_and_other_prefixes(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        default_storage.save("generated/user_1/secret_0123456789.png", ContentFile(b"x"))
        default_storage.save("generated/user_1/layers/sub/el_0123456789abcdef.png", ContentFile(b"x"))
        for bad in (
            "generated/user_1/layers/../secret_0123456789.png",
            "generated/user_1/layers/..%2Fsecret_0123456789.png",
            "generated/user_1/secret_0123456789.png",
            "generated/user_1/layers/sub/el_0123456789abcdef.png",
            "/etc/passwd",
            "%2Fetc%2Fpasswd",
            "generated/user_1/layers/el_0123456789abcdef.py",
            "generated/user_1/layers/missing_0123456789abcdef.png",
        ):
            r = self.client.get(f"/api/layer-files/{bad}")
            self.assertEqual(r.status_code, 404, bad)

    def test_post_not_allowed(self):
        path = self._store()
        self.assertEqual(self.client.post(f"/api/layer-files/{path}").status_code, 405)


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

    def _decomposed(self, **extra):
        prefix = "/api/layer-files/generated/user_1/layers/"
        return {
            "baseUrl": prefix + "base_x.png",
            "backgroundUrl": prefix + "bg_x.png",
            "cleanUrl": prefix + "clean_x.png",
            "width": 1, "height": 1,
            "elements": [{"id": "el_1", "name": "Mashina", "kind": "product", "url": prefix + "el_x.png",
                          "x": 0, "y": 0, "width": 1, "height": 1, "order": 1}],
            "layers": [],
            "textLayers": [{"id": "text_1", "text": "Yangi", "imageUrl": prefix + "txt_x.png",
                            "imageBox": {"x": 0, "y": 0, "width": 1, "height": 1}}],
            "textRemoved": True,
            "objectsRemoved": True,
            "layersVersion": 4,
            **extra,
        }

    def test_v3_cache_is_recomputed_and_v4_stored(self):
        from unittest.mock import patch

        job = self._job({"textLayers": [{"id": "x"}], "cleanUrl": "/media/c.png", "layersVersion": 3})
        with patch("generation.views.decompose_layers", return_value=self._decomposed()) as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {}, format="json")

        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(decompose.called)
        body = r.json()
        self.assertEqual(body["layersVersion"], 4)
        self.assertTrue(body["textRemoved"])
        self.assertTrue(body["objectsRemoved"])
        for url in (body["baseUrl"], body["backgroundUrl"], body["elements"][0]["url"],
                    body["textLayers"][0]["imageUrl"]):
            self.assertTrue(url.startswith("http"), url)
            self.assertIn("/api/layer-files/generated/user_1/layers/", url)
        self.assertEqual(body["elements"][0]["name"], "Mashina")
        self.assertEqual(body["textLayers"][0]["imageBox"], {"x": 0, "y": 0, "width": 1, "height": 1})
        self.assertEqual((body["width"], body["height"]), (1, 1))

        job.refresh_from_db()
        stored = job.results[0]
        self.assertEqual(stored["layersVersion"], 4)
        self.assertEqual(stored["backgroundUrl"], "/api/layer-files/generated/user_1/layers/bg_x.png")
        self.assertEqual(stored["baseUrl"], "/api/layer-files/generated/user_1/layers/base_x.png")
        self.assertEqual(stored["url"], PIXEL_PNG, "asl natija URL'i o'zgarmaydi")
        result = body["job"]["results"][0]
        self.assertTrue(result["backgroundUrl"].endswith("bg_x.png"))
        self.assertTrue(result["elements"][0]["url"].startswith("http"))
        self.assertTrue(result["textLayers"][0]["imageUrl"].startswith("http"))
        self.assertTrue(result["objectsRemoved"])
        self.assertEqual(result["layersVersion"], 4)

    def test_v4_cache_is_returned(self):
        from unittest.mock import patch

        job = self._job(self._decomposed())
        with patch("generation.views.decompose_layers") as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {}, format="json")

        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(decompose.called)
        body = r.json()
        self.assertTrue(body["cached"])
        self.assertEqual(body["layersVersion"], 4)
        self.assertTrue(body["backgroundUrl"].endswith("bg_x.png"))
        self.assertEqual(len(body["elements"]), 1)

    def test_force_recomputes_v4_cache(self):
        from unittest.mock import patch

        job = self._job(self._decomposed())
        with patch("generation.views.decompose_layers", return_value=self._decomposed()) as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {"force": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(decompose.called)

    def test_nothing_found_returns_empty_lists(self):
        from unittest.mock import patch

        job = self._job({})
        empty = self._decomposed(cleanUrl=None, textLayers=[], elements=[], textRemoved=False,
                                 objectsRemoved=False, backgroundUrl="/api/layer-files/generated/user_1/layers/base_x.png")
        with patch("generation.views.decompose_layers", return_value=empty):
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["textLayers"], [])
        self.assertEqual(body["elements"], [])
        self.assertIsNone(body["cleanUrl"])
        self.assertEqual(body["backgroundUrl"], body["baseUrl"])
        self.assertFalse(body["textRemoved"])
        self.assertFalse(body["objectsRemoved"])

    def test_pipeline_error_is_not_500(self):
        from unittest.mock import patch

        job = self._job({})
        with patch("generation.views.decompose_layers", side_effect=RuntimeError("boom")):
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {}, format="json")
        self.assertEqual(r.status_code, 502)


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


class ShortTitleTests(TestCase):
    """Production: nom uzun gap bo'lib keldi, sarlavha esa oxirgi so'z «BILAN» bo'ldi."""

    def test_short_title_cuts_description(self):
        from .analyze import short_title

        self.assertEqual(short_title("Yoga mat, qulay va ko‘pgina yutuqlar bilan"), "Yoga mat")
        self.assertEqual(short_title("Soat"), "Soat")
        self.assertEqual(short_title("maktab uchun ryukzak"), "maktab uchun ryukzak")

    def test_headline_never_a_stopword(self):
        from .infographic_rules import to_headline

        self.assertEqual(to_headline("Yoga mat, qulay va ko‘pgina yutuqlar bilan"), "YOGA MAT")
        self.assertEqual(to_headline("maktab uchun ryukzak"), "RYUKZAK")
        self.assertEqual(to_headline("Сумка для ноутбука", language="ru"), "СУМКА")
        self.assertEqual(to_headline("Melatonin 3 mg"), "MELATONIN")


class StyleTemplateGalleryTests(TestCase):
    """Galereya git'dagi style_templates/ papkasidan beriladi (media/ emas)."""

    def setUp(self):
        self.client = APIClient()

    def test_default_dir_is_tracked_repo_folder(self):
        import os
        from django.conf import settings
        self.assertEqual(os.path.normpath(settings.STYLE_TEMPLATES_DIR), os.path.normpath(str(settings.BASE_DIR / "style_templates")))
        self.assertTrue(os.path.isfile(os.path.join(settings.STYLE_TEMPLATES_DIR, "manifest.json")))

    def test_list_has_one_cover_per_style(self):
        response = self.client.get("/api/style-templates")
        self.assertEqual(response.status_code, 200)
        templates = response.json()["templates"]
        self.assertGreater(len(templates), 100)
        for item in templates:
            self.assertEqual(set(item), {"id", "category", "url", "thumb", "cover"})
        covers = {item["category"] for item in templates if item["cover"]}
        self.assertEqual(covers, {"premium", "lifestyle", "bright", "bold", "minimal", "tech", "nature"})
        self.assertEqual(sum(1 for item in templates if item["cover"]), 7)

    def test_file_served_and_traversal_blocked(self):
        first = self.client.get("/api/style-templates").json()["templates"][0]
        response = self.client.get(first["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        thumb = self.client.get(first["thumb"])
        self.assertEqual(thumb.status_code, 200)
        self.assertLess(len(thumb.content), len(response.content))
        self.assertEqual(self.client.get("/api/style-templates/..%2Fmanage.py").status_code, 404)

    def test_gallery_thumbnails_are_not_throttled(self):
        # Galereya ~150 prevyuni birdaniga so'raydi — anon 60/min ularni kesmasligi kerak.
        from django.core.cache import cache
        cache.clear()
        templates = self.client.get("/api/style-templates").json()["templates"]
        statuses = {self.client.get(item["thumb"]).status_code for item in templates}
        self.assertEqual(statuses, {200})


class GeneratedFileEndpointTests(TestCase):
    """«Yuklab olish»: natija fayli /api/files/ orqali CORS bilan, ?download=1 da attachment."""

    def setUp(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        self.path = default_storage.save("generated/user_1/abcdef0123456789.png", ContentFile(b"\x89PNG\r\n\x1a\nxx"))

    def tearDown(self):
        from django.core.files.storage import default_storage

        default_storage.delete(self.path)

    def test_serves_file_inline_and_as_attachment(self):
        response = self.client.get(f"/api/files/{self.path}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))

        response = self.client.get(f"/api/files/{self.path}?download=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])

    def test_cors_header_for_allowed_origin(self):
        from django.conf import settings

        origin = settings.CORS_ALLOWED_ORIGINS[0]
        response = self.client.get(f"/api/files/{self.path}", HTTP_ORIGIN=origin)
        self.assertEqual(response.get("Access-Control-Allow-Origin"), origin)

    def test_rejects_other_paths(self):
        for bad in ("uploads/user_1/abcdef0123456789.png", "generated/user_1/../../settings.py", "generated/user_1/x.png"):
            self.assertEqual(self.client.get(f"/api/files/{bad}").status_code, 404, bad)


class DjangoAdminPagesTests(TestCase):
    """Admin sahifalari (foydalanuvchilar, promokodlar, generatsiyalar) xatosiz ochiladi."""

    def setUp(self):
        from accounts.models import User
        from promos.models import Promo, PromoRedemption

        self.admin = User.objects.create_superuser(username="boss", email="boss@example.com", password="x-strong-pass-123")
        user = User.objects.create_user(username="u1", email="u1@example.com", password="x-strong-pass-123", name="Ali")
        promo = Promo.objects.create(code="START21", kind="tokens", value=21, tariff="start")
        PromoRedemption.objects.create(promo=promo, user=user)
        self.job = GenerationJob.objects.create(
            user=user, content_type="card", status="success", title="Yoga mat", tokens_charged=1,
            settings={"stylePreset": "premium", "assetDataUrl": "data:image/png;base64,AAAA",
                      "designReferenceDataUrl": "data:image/png;base64,BBBB", "designReferenceName": "Premium",
                      "designReferenceUrl": "https://api.naslai.uz/api/style-templates/premium-01.jpg"},
            results=[{"url": "/media/generated/user_1/abc.png", "referenceUsed": True, "promptTier": "primary"}],
        )
        self.client.force_login(self.admin)

    def test_pages_open(self):
        from django.conf import settings

        base = f"/{settings.DJANGO_ADMIN_PATH}"
        for url in (
            f"{base}/", f"{base}/accounts/user/", f"{base}/promos/promo/", f"{base}/promos/promoredemption/",
            f"{base}/generation/generationjob/", f"{base}/generation/generationjob/?style=premium&reference=yes",
            f"{base}/generation/generationjob/?style=auto&reference=no",
            f"{base}/generation/generationjob/{self.job.pk}/change/", f"{base}/billing/ledgerentry/",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        user_id = GenerationJob.objects.get(pk=self.job.pk).user_id
        self.assertEqual(self.client.get(f"{base}/accounts/user/{user_id}/change/").status_code, 200)
        page = self.client.get(f"{base}/generation/generationjob/{self.job.pk}/change/").content.decode()
        self.assertIn("Premium", page)
        self.assertIn("Referens ishlatilgan", page)


class ApiKeyDjangoAdminTests(TestCase):
    def test_add_key_from_django_admin_shows_secret_and_credits_tariff(self):
        from django.conf import settings

        from accounts.models import ApiKey

        boss = User.objects.create_superuser(username="boss2", email="boss2@example.com", password="x-strong-pass-123")
        owner = User.objects.create_user(username="o1", email="o1@example.com", password="x-strong-pass-123")
        self.client.force_login(boss)
        base = f"/{settings.DJANGO_ADMIN_PATH}/accounts/apikey"
        self.assertEqual(self.client.get(f"{base}/add/").status_code, 200)
        response = self.client.post(
            f"{base}/add/",
            {"user": owner.pk, "name": "Sarvar", "note": "", "tariff": "profi", "amount": "",
             "ledger-TOTAL_FORMS": "0", "ledger-INITIAL_FORMS": "0", "ledger-MIN_NUM_FORMS": "0", "ledger-MAX_NUM_FORMS": "1000"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        key = ApiKey.objects.get(name="Sarvar")
        self.assertEqual(key.balance, 117)
        self.assertIn("nsk_live_", response.content.decode())
        self.assertEqual(self.client.get(f"{base}/{key.pk}/change/").status_code, 200)
        response = self.client.post(
            f"{base}/{key.pk}/change/",
            {"name": "Sarvar", "note": "", "top_up_tariff": "", "top_up_amount": "10", "top_up_note": "chek 12",
             "ledger-TOTAL_FORMS": "1", "ledger-INITIAL_FORMS": "1", "ledger-MIN_NUM_FORMS": "0", "ledger-MAX_NUM_FORMS": "1000",
             "ledger-0-id": str(key.ledger.first().pk), "ledger-0-key": str(key.pk)},
            follow=True,
        )
        key.refresh_from_db()
        self.assertEqual(key.balance, 127)
