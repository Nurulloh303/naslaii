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


class MagicLayersPipelineTests(TestCase):
    def setUp(self):
        import tempfile

        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)

    def _env(self, **extra):
        import os
        from unittest.mock import patch

        return patch.dict(os.environ, {"FAL_KEY": "fal-test", "FAL_MOCK": "0", **extra})

    def _text_image(self):
        import base64
        import io

        from PIL import Image, ImageDraw

        img = Image.new("RGB", (300, 200), (30, 90, 200))
        ImageDraw.Draw(img).rectangle([40, 40, 200, 70], fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def _blocks(self):
        return [{"id": "text_abc", "text": "SALOM", "x": 45, "y": 45, "width": 140, "height": 22,
                 "fontSize": 18, "fontWeight": 700, "color": "#FFFFFF", "align": "left",
                 "fontFamily": "Inter", "lineHeight": 1.15, "letterSpacing": 0.0, "uppercase": True,
                 "background": None, "rotation": 0, "source": {"x": 45, "y": 45, "width": 140, "height": 22}}]

    def test_default_path_erases_locally_without_inpaint_or_qwen(self):
        import io
        from unittest.mock import patch

        from django.core.files.storage import default_storage
        from django.test import override_settings
        from PIL import Image

        from generation import layers

        source = self._text_image()
        calls = []

        def fake_fal(endpoint, payload, timeout=0):
            calls.append(endpoint)
            if endpoint == layers.FAL_OCR_ENDPOINT:
                return {"results": {"quad_boxes": [{"x": 38, "y": 38, "w": 165, "h": 35, "label": "SALOM"}]}}
            raise AssertionError(f"kutilmagan fal chaqiruvi: {endpoint}")

        with self._env(), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=self._blocks()), \
                patch("generation.layers._fal_run", side_effect=fake_fal):
            out = layers.decompose_layers(source, user_id=1)

            self.assertEqual(calls, [layers.FAL_OCR_ENDPOINT])
            self.assertEqual(out["layersVersion"], 3)
            self.assertEqual(out["layers"], [])
            self.assertEqual(out["baseUrl"], source)
            self.assertEqual((out["width"], out["height"]), (300, 200))
            self.assertTrue(out["textRemoved"])
            [text] = out["textLayers"]
            self.assertEqual((text["x"], text["y"], text["width"], text["height"]), (40, 40, 161, 31))
            self.assertIsNone(text["background"])
            relative = out["cleanUrl"].split("/media/", 1)[1]
            with default_storage.open(relative, "rb") as f:
                clean = Image.open(io.BytesIO(f.read())).convert("RGB")
        self.assertEqual(clean.size, (300, 200))
        self.assertEqual(clean.getpixel((120, 55)), (30, 90, 200))   # matn joyi — fon rangi
        self.assertEqual(clean.getpixel((280, 180)), (30, 90, 200))  # qolgani — asl

    def test_ocr_failure_falls_back_to_gpt_boxes(self):
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers
        from generation.providers import GenerationError

        def fake_fal(endpoint, payload, timeout=0):
            raise GenerationError("ocr down", status=502)

        with self._env(), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=self._blocks()), \
                patch("generation.layers._fal_run", side_effect=fake_fal):
            out = layers.decompose_layers(self._text_image(), user_id=1)

        [text] = out["textLayers"]
        self.assertEqual((text["x"], text["y"], text["width"], text["height"]), (40, 40, 161, 31))
        self.assertTrue(out["textRemoved"])

    def test_no_fal_key_still_edits_text(self):
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers

        with self._env(FAL_KEY=""), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=self._blocks()), \
                patch("generation.layers._fal_run") as fal:
            out = layers.decompose_layers(self._text_image(), user_id=1)
        self.assertFalse(fal.called)
        self.assertEqual(len(out["textLayers"]), 1)
        self.assertTrue(out["cleanUrl"])

    def test_text_detection_failure_returns_empty_text(self):
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers

        with self._env(), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=[]), \
                patch("generation.layers._fal_run", return_value={"results": {"quad_boxes": []}}):
            out = layers.decompose_layers(_png_data_url(), user_id=1)

        self.assertEqual(out["textLayers"], [])
        self.assertIsNone(out["cleanUrl"])
        self.assertFalse(out["textRemoved"])
        self.assertEqual(out["layers"], [])

    def test_eraser_crash_never_500(self):
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers

        with self._env(), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=self._blocks()), \
                patch("generation.layers._fal_run", return_value={}), \
                patch("generation.layers.te.erase_text", side_effect=RuntimeError("boom")):
            out = layers.decompose_layers(self._text_image(), user_id=1)
        self.assertIsNone(out["cleanUrl"])
        self.assertFalse(out["textRemoved"])
        self.assertEqual(len(out["textLayers"]), 1)

    def test_separate_objects_runs_qwen_on_clean_image(self):
        from unittest.mock import patch

        from django.test import override_settings

        from generation import layers

        source = self._text_image()
        layer_png = _png_data_url(color=(0, 0, 0))
        calls = []

        def fake_fal(endpoint, payload, timeout=0):
            calls.append((endpoint, payload))
            if endpoint == layers.FAL_OCR_ENDPOINT:
                return {"results": {"quad_boxes": []}}
            return {"images": [{"url": layer_png}, {"url": layer_png}]}

        with self._env(), override_settings(MEDIA_ROOT=self._media.name), \
                patch("generation.layers.tl.detect_text_blocks", return_value=self._blocks()), \
                patch("generation.layers._fal_run", side_effect=fake_fal):
            out = layers.decompose_layers(source, user_id=1, separate_objects=True)

        qwen = [payload for endpoint, payload in calls if endpoint == layers.FAL_ENDPOINT]
        self.assertEqual(len(qwen), 1)
        self.assertNotEqual(qwen[0]["image_url"], source, "qwen toza rasmda ishlashi kerak")
        self.assertEqual(len(out["layers"]), 2)
        self.assertEqual(out["layers"][0]["name"], "Orqa fon")

    def test_mock_mode(self):
        import os
        from unittest.mock import patch

        from generation import layers

        source = _png_data_url(108, 144)
        with patch.dict(os.environ, {"FAL_MOCK": "1"}):
            out = layers.decompose_layers(source, user_id=1)
        self.assertEqual(out["baseUrl"], source)
        self.assertEqual(out["cleanUrl"], source)
        self.assertEqual((out["width"], out["height"]), (108, 144))
        self.assertEqual(len(out["textLayers"]), 2)
        self.assertIn("source", out["textLayers"][0])
        self.assertEqual(out["layers"], [])
        self.assertEqual(out["layersVersion"], 3)


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
        return {
            "baseUrl": PIXEL_PNG,
            "cleanUrl": "/media/generated/user_1/layers/clean_x.png",
            "width": 1, "height": 1,
            "layers": [],
            "textLayers": [{"id": "text_1", "text": "Yangi"}],
            "textRemoved": True,
            "layersVersion": 3,
            **extra,
        }

    def test_old_v2_cache_is_recomputed(self):
        from unittest.mock import patch

        job = self._job({"layers": [{"id": "old", "url": PIXEL_PNG}], "textLayers": [{"id": "x"}], "layersVersion": 2})
        with patch("generation.views.decompose_layers", return_value=self._decomposed()) as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {}, format="json")

        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(decompose.called)
        self.assertFalse(decompose.call_args.kwargs["separate_objects"])
        body = r.json()
        self.assertEqual(body["textLayers"][0]["text"], "Yangi")
        self.assertTrue(body["textRemoved"])
        self.assertEqual(body["layersVersion"], 3)
        self.assertEqual(body["baseUrl"], PIXEL_PNG)
        self.assertTrue(body["cleanUrl"].endswith("/media/generated/user_1/layers/clean_x.png"))
        self.assertEqual(body["layers"], [])
        self.assertEqual((body["width"], body["height"]), (1, 1))
        job.refresh_from_db()
        stored = job.results[0]
        self.assertEqual(stored["layersVersion"], 3)
        self.assertEqual(stored["cleanUrl"], "/media/generated/user_1/layers/clean_x.png")
        self.assertEqual(stored["baseUrl"], PIXEL_PNG)
        self.assertEqual(stored["layers"], [])
        # Serializer ham yangi maydonlarni beradi
        result = body["job"]["results"][0]
        self.assertEqual(result["baseUrl"], PIXEL_PNG)
        self.assertTrue(result["cleanUrl"].endswith("clean_x.png"))
        self.assertEqual(result["layersVersion"], 3)

    def test_v3_cache_is_returned(self):
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
        self.assertEqual(body["layersVersion"], 3)
        self.assertTrue(body["cleanUrl"].endswith("clean_x.png"))

    def test_cache_without_objects_recomputes_when_objects_requested(self):
        from unittest.mock import patch

        job = self._job(self._decomposed())
        objects = [{"id": "layer_1", "order": 1, "name": "Orqa fon", "url": PIXEL_PNG, "width": 1, "height": 1}]
        with patch("generation.views.decompose_layers", return_value=self._decomposed(layers=objects)) as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {"separateObjects": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(decompose.call_args.kwargs["separate_objects"])
        self.assertEqual(len(r.json()["layers"]), 1)

    def test_force_recomputes_v3_cache(self):
        from unittest.mock import patch

        job = self._job(self._decomposed())
        with patch("generation.views.decompose_layers", return_value=self._decomposed()) as decompose:
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {"force": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(decompose.called)

    def test_no_text_returns_empty_lists(self):
        from unittest.mock import patch

        job = self._job({})
        empty = self._decomposed(cleanUrl=None, textLayers=[], textRemoved=False)
        with patch("generation.views.decompose_layers", return_value=empty):
            r = self.client.post(f"/api/generations/{job.pk}/results/1/layers", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["textLayers"], [])
        self.assertIsNone(body["cleanUrl"])
        self.assertFalse(body["textRemoved"])


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
