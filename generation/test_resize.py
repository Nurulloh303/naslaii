"""O'lchamni moslash (/api/resize, content_type="resize")."""

from __future__ import annotations

import base64
import io
import os
import shutil
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User
from billing.models import LedgerEntry
from generation import resize as rz
from generation.models import GenerationJob
from generation.providers import GenerationError


def make_image(size, color=(200, 30, 30), mode="RGB") -> Image.Image:
    return Image.new(mode, size, color)


def to_bytes(image: Image.Image, fmt="PNG", **kw) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kw)
    return buffer.getvalue()


def data_url(image: Image.Image, fmt="PNG", **kw) -> str:
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
    return f"data:{mime};base64,{base64.b64encode(to_bytes(image, fmt, **kw)).decode()}"


def fal_result(image: Image.Image) -> dict:
    return {"image": {"url": data_url(image, "PNG")}}


def no_fal():
    return patch.dict(os.environ, {"FAL_KEY": "", "FAL_MOCK": "0"})


def with_fal():
    return patch.dict(os.environ, {"FAL_KEY": "fal-test", "FAL_MOCK": "0"})


class RenderModesTests(TestCase):
    """Kompozitsiya — bazasiz, xotirada."""

    def test_every_mode_gives_exactly_1080x1440(self):
        for size in ((1600, 900), (900, 1600), (1080, 1440), (3000, 3000), (500, 700)):
            image = make_image(size)
            for mode in ("blur", "color", "crop"):
                with no_fal():
                    out = rz.render(image, mode, color="auto")
                self.assertEqual(out["image"].size, (1080, 1440), (size, mode))

    def test_crop_focus_is_respected(self):
        image = Image.new("RGB", (2000, 1000), (255, 0, 0))
        image.paste((0, 0, 255), (1000, 0, 2000, 1000))  # o'ng yarmi ko'k
        with no_fal():
            left = rz.render(image, "crop", focus_x=0.0)["image"]
            right = rz.render(image, "crop", focus_x=1.0)["image"]
            centre = rz.render(image, "crop")["image"]
        self.assertEqual(left.getpixel((540, 720))[:3], (255, 0, 0))
        self.assertEqual(right.getpixel((540, 720))[:3], (0, 0, 255))
        # Markaz: chap yarmi qizil, o'ng yarmi ko'k.
        self.assertGreater(centre.getpixel((100, 720))[0], 200)
        self.assertGreater(centre.getpixel((980, 720))[2], 200)
        self.assertEqual(rz.crop_box(2000, 1000, 0.0, 0.5), (0, 0, 750, 1000))
        self.assertEqual(rz.crop_box(2000, 1000, 1.0, 0.5), (1250, 0, 2000, 1000))
        self.assertEqual(rz.crop_box(1000, 2000, 0.5, 0.0), (0, 0, 1000, 1333))

    def test_color_auto_uses_dominant_edge_colour(self):
        image = Image.new("RGB", (1600, 900), (20, 160, 60))
        image.paste((250, 250, 250), (300, 200, 1300, 700))
        with no_fal():
            out = rz.render(image, "color", color="auto")["image"]
        r, g, b = out.getpixel((5, 5))
        self.assertTrue(abs(r - 20) < 4 and abs(g - 160) < 4 and abs(b - 60) < 4, (r, g, b))

    def test_color_hex(self):
        with no_fal():
            out = rz.render(make_image((1600, 900)), "color", color="#112233")["image"]
        self.assertEqual(out.getpixel((3, 3)), (0x11, 0x22, 0x33))

    def test_blur_background_and_centered_original(self):
        image = Image.new("RGB", (1600, 900), (240, 200, 40))
        with no_fal():
            out = rz.render(image, "blur")["image"]
        self.assertEqual(out.size, (1080, 1440))
        # Markazda asl rang, fonda — to'qroq (darkened) versiyasi.
        self.assertEqual(out.getpixel((540, 720)), (240, 200, 40))
        top = out.getpixel((540, 20))
        self.assertLess(sum(top), sum((240, 200, 40)))

    def test_transparent_png_keeps_alpha_in_crop_and_flattens_in_color(self):
        image = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
        image.paste((255, 0, 0, 255), (300, 300, 900, 900))
        self.assertTrue(rz.has_transparency(image))
        with no_fal():
            crop = rz.render(image, "crop")["image"]
            color = rz.render(image, "color", color="#00FF00")["image"]
        self.assertEqual(crop.mode, "RGBA")
        self.assertEqual(color.mode, "RGB")
        self.assertEqual(color.getpixel((540, 720)), (255, 0, 0))
        self.assertEqual(color.getpixel((5, 5)), (0, 255, 0))
        data, ext = rz.encode(color, as_png=True)
        self.assertEqual(ext, "png")


class AiModeTests(TestCase):
    def test_ai_outpaint_calls_bria_and_keeps_original_pixels(self):
        original = Image.new("RGB", (1080, 800))
        # Har piksel har xil: nusxa aniq bo'lmasa, test sezadi.
        original.putdata([((x * 7) % 256, (y * 3) % 256, (x + y) % 256) for y in range(800) for x in range(1080)])
        generated = Image.new("RGB", (1080, 1440), (255, 0, 255))
        calls = []

        def fake(endpoint, payload, timeout=0):
            calls.append((endpoint, payload))
            return fal_result(generated)

        with with_fal(), patch("generation.layers._fal_run", side_effect=fake):
            out = rz.render(original, "ai")

        self.assertTrue(out["outpainted"])
        self.assertFalse(out["upscaled"])
        self.assertEqual(len(calls), 1)
        endpoint, payload = calls[0]
        self.assertEqual(endpoint, "fal-ai/bria/expand")
        self.assertEqual(payload["canvas_size"], [1080, 1440])
        self.assertEqual(payload["original_image_size"], [1080, 800])
        self.assertEqual(payload["original_image_location"], [0, 320])
        image = out["image"]
        self.assertEqual(image.size, (1080, 1440))
        self.assertEqual(list(image.crop((0, 320, 1080, 1120)).getdata()), list(original.getdata()))
        self.assertEqual(image.getpixel((10, 10)), (255, 0, 255))
        self.assertEqual(image.getpixel((10, 1430)), (255, 0, 255))

    def test_ai_skips_outpaint_when_already_3_4(self):
        image = make_image((1083, 1440))  # 0.3% farq
        with with_fal(), patch("generation.layers._fal_run") as fal:
            out = rz.render(image, "ai")
        fal.assert_not_called()
        self.assertFalse(out["outpainted"])
        self.assertEqual(out["image"].size, (1080, 1440))

    def test_fal_mock_uses_blur_composition(self):
        image = Image.new("RGB", (1600, 900), (240, 200, 40))
        with patch.dict(os.environ, {"FAL_KEY": "fal-test", "FAL_MOCK": "1"}), \
                patch("generation.layers._fal_run") as fal:
            out = rz.render(image, "ai")
        fal.assert_not_called()
        self.assertFalse(out["outpainted"])
        self.assertEqual(out["image"].size, (1080, 1440))
        self.assertLess(sum(out["image"].getpixel((540, 20))), sum((240, 200, 40)))


class UpscaleTests(TestCase):
    def test_small_image_is_upscaled_with_esrgan(self):
        image = make_image((400, 600), (10, 120, 200))
        calls = []

        def fake(endpoint, payload, timeout=0):
            calls.append((endpoint, payload))
            return fal_result(make_image((1600, 2400), (10, 120, 200)))

        with with_fal(), patch("generation.layers._fal_run", side_effect=fake):
            out = rz.render(image, "color", color="#FFFFFF")
        self.assertTrue(out["upscaled"])
        self.assertEqual(out["upscaler"], "esrgan")
        self.assertEqual(calls[0][0], "fal-ai/esrgan")
        self.assertEqual(calls[0][1]["model"], "RealESRGAN_x4plus")
        self.assertIn(calls[0][1]["scale"], (2.0, 3.0, 4.0))
        self.assertEqual(out["image"].size, (1080, 1440))

    def test_upscale_falls_back_to_lanczos_when_fal_fails(self):
        image = make_image((400, 600))
        with with_fal(), patch("generation.layers._fal_run", side_effect=GenerationError("x", status=502)):
            out = rz.render(image, "crop")
        self.assertTrue(out["upscaled"])
        self.assertEqual(out["upscaler"], "lanczos")
        self.assertEqual(out["image"].size, (1080, 1440))

    def test_large_image_is_not_upscaled(self):
        with with_fal(), patch("generation.layers._fal_run") as fal:
            out = rz.render(make_image((2000, 2000)), "blur")
        fal.assert_not_called()
        self.assertFalse(out["upscaled"])


class DecodeTests(TestCase):
    def test_exif_orientation_is_applied(self):
        image = make_image((400, 300))
        exif = Image.Exif()
        exif[0x0112] = 6  # 90° burilgan
        raw, decoded = rz.decode_upload(data_url(image, "JPEG", exif=exif))
        self.assertEqual(decoded.size, (300, 400))

    def test_limits(self):
        with self.assertRaises(rz.BadImage):
            rz.decode_upload(data_url(make_image((150, 800))))
        with self.assertRaises(rz.BadImage):
            rz.decode_upload("data:image/png;base64," + base64.b64encode(b"not an image").decode())
        with self.assertRaises(rz.BadImage):
            rz.decode_upload("https://example.com/a.png")
        with patch.object(rz, "MAX_INPUT_BYTES", 1000):
            with self.assertRaises(rz.BadImage):
                rz.decode_upload(data_url(make_image((400, 400)), "JPEG"))
        raw, img = rz.decode_upload(data_url(make_image((400, 300)), "WEBP"))
        self.assertEqual(img.format, "WEBP")


class ResizeApiTests(TestCase):
    """Pullik rejim (RESIZE_TOKENS=1) — token yechish/qaytarish yo'li ishlashini tekshiradi."""

    def setUp(self):
        import os
        from unittest import mock

        self._env = mock.patch.dict(os.environ, {"RESIZE_TOKENS": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        cache.clear()
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user(username="r@n.uz", email="r@n.uz", password="parol12345")
        self.user.balance = 5
        self.user.save(update_fields=["balance"])
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    def _post(self, **body):
        body.setdefault("image", data_url(make_image((1600, 900), (240, 200, 40))))
        return self.client.post("/api/resize", body, format="json")

    def _balance(self):
        self.user.refresh_from_db()
        return self.user.balance

    def test_blur_job_success_payload_and_one_token(self):
        with no_fal():
            r = self._post(mode="blur")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["balance"], 4)
        job = body["job"]
        self.assertEqual(job["status"], "success")
        self.assertEqual(job["progress"], 100)
        self.assertEqual(job["tokensCharged"], 1)
        self.assertEqual(job["context"]["contentType"], "resize")
        self.assertEqual(job["quote"]["tokens"], 1)
        result = job["results"][0]
        self.assertEqual((result["width"], result["height"]), (1080, 1440))
        self.assertEqual(result["mode"], "blur")
        self.assertFalse(result["upscaled"])
        self.assertEqual((result["originalWidth"], result["originalHeight"]), (1600, 900))
        self.assertTrue(result["url"].endswith(".jpg"))

        stored = GenerationJob.objects.get(pk=job["id"])
        self.assertEqual(stored.content_type, "resize")
        self.assertEqual(stored.title, "O'lcham: 1080×1440")
        self.assertNotIn("data:", str(stored.settings))
        self.assertRegex(stored.settings["sourcePath"], r"^generated/user_\d+/src_[0-9a-f]{32}\.png$")
        self.assertTrue(default_storage.exists(stored.settings["sourcePath"]))
        self.assertEqual(LedgerEntry.objects.filter(user=self.user, type=LedgerEntry.DEBIT).count(), 1)

        # Yuklab olish endpointi faylni beradi.
        path = result["url"].split("/media/", 1)[1]
        download = self.client.get(f"/api/files/{path}?download=1")
        self.assertEqual(download.status_code, 200)
        content = b"".join(download.streaming_content)
        self.assertEqual(Image.open(io.BytesIO(content)).size, (1080, 1440))
        src = self.client.get(f"/api/files/{stored.settings['sourcePath']}")
        self.assertEqual(src.status_code, 200)

        # Loyihalar ro'yxatida ko'rinadi.
        projects = self.client.get("/api/projects").json()["projects"]
        self.assertEqual(projects[0]["id"], job["id"])
        detail = self.client.get(f"/api/projects/{job['id']}").json()["project"]
        self.assertIn("/media/generated/", detail["sourceUrl"])

        # Polling endpointi ham shu maydonlarni beradi.
        polled = self.client.get(f"/api/generations/{job['id']}").json()["job"]["results"][0]
        self.assertEqual(polled["mode"], "blur")

    def test_every_mode_through_api(self):
        for mode, extra in (("color", {"color": "auto"}), ("color", {"color": "#FF0000"}),
                            ("crop", {"focusX": 0.2, "focusY": 0.9}), ("ai", {})):
            with patch.dict(os.environ, {"FAL_KEY": "", "FAL_MOCK": "1"}):
                r = self._post(mode=mode, title="Krossovka", **extra)
            self.assertEqual(r.status_code, 201, (mode, r.content))
            self.assertEqual(r.json()["job"]["results"][0]["width"], 1080)
        self.assertEqual(self._balance(), 1)

    def test_small_image_reports_upscaled(self):
        with no_fal():
            r = self._post(mode="color", color="#FFFFFF", image=data_url(make_image((400, 600))))
        self.assertTrue(r.json()["job"]["results"][0]["upscaled"])

    def test_transparent_png_output_is_png(self):
        image = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
        image.paste((255, 0, 0, 255), (300, 300, 900, 900))
        with no_fal():
            r = self._post(mode="color", color="#FFFFFF", image=data_url(image))
        self.assertTrue(r.json()["job"]["results"][0]["url"].endswith(".png"))

    def test_failure_refunds_token(self):
        with with_fal(), patch("generation.layers._fal_run", side_effect=GenerationError("fal down", status=502)):
            r = self._post(mode="ai")
        self.assertEqual(r.status_code, 201, r.content)
        job = r.json()["job"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self._balance(), 5)
        self.assertTrue(GenerationJob.objects.get(pk=job["id"]).refunded)

    def test_bad_image_and_mode(self):
        r = self._post(mode="stretch")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "BAD_MODE"))
        r = self._post(mode="blur", image="data:image/png;base64,AAAA")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "BAD_IMAGE"))
        r = self._post(mode="blur", image=data_url(make_image((100, 100))))
        self.assertEqual((r.status_code, r.json()["error"]), (400, "BAD_IMAGE"))
        self.assertIn("200", r.json()["message"])
        r = self._post(mode="color", color="red")
        self.assertEqual((r.status_code, r.json()["error"]), (400, "BAD_MODE"))
        r = self._post(mode="crop", focusX="abc")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._balance(), 5)
        self.assertFalse(GenerationJob.objects.exists())

    def test_insufficient_tokens(self):
        self.user.balance = 0
        self.user.save(update_fields=["balance"])
        r = self._post(mode="blur")
        self.assertEqual((r.status_code, r.json()["error"]), (402, "INSUFFICIENT_TOKENS"))

    def test_requires_auth(self):
        self.client.force_authenticate(None)
        self.assertIn(self._post(mode="blur").status_code, (401, 403))

    def test_queued_and_cancel_refunds(self):
        with patch("generation.views.enqueue"):
            r = self._post(mode="blur")
        job = r.json()["job"]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(r.json()["balance"], 4)
        cancel = self.client.post(f"/api/generations/{job['id']}/cancel")
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.json()["refunded"], 1)
        self.assertEqual(cancel.json()["job"]["status"], "failed")
        self.assertEqual(self._balance(), 5)

    def test_cancel_during_render_discards_result(self):
        from generation.tasks import run_job

        with patch("generation.views.enqueue"):
            job_id = self._post(mode="blur").json()["job"]["id"]
        job = GenerationJob.objects.get(pk=job_id)
        original = rz.ResizeProvider.generate

        def cancel_then_generate(provider, j, index, previous=None):
            self.client.post(f"/api/generations/{job_id}/cancel")
            return original(provider, j, index, previous)

        with no_fal(), patch.object(rz.ResizeProvider, "generate", cancel_then_generate):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.results, [])
        self.assertEqual(self._balance(), 5)

    def test_idempotency_key(self):
        key = "resize-idem-000000000000001"
        with no_fal():
            first = self._post(mode="blur", idempotencyKey=key)
            second = self._post(mode="blur", idempotencyKey=key)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["job"]["id"], second.json()["job"]["id"])
        self.assertEqual(self._balance(), 4)

    def test_quote(self):
        r = self.client.post("/api/quote", {"settings": {"contentType": "resize"}}, format="json")
        self.assertEqual(r.json()["tokens"], 1)

    def test_admin_page_shows_resize_job(self):
        from django.conf import settings

        with no_fal():
            job_id = self._post(mode="blur").json()["job"]["id"]
        admin = User.objects.create_superuser(username="boss", email="boss@example.com", password="x-strong-pass-123")
        client = APIClient()
        client.force_login(admin)
        base = f"/{settings.DJANGO_ADMIN_PATH}"
        self.assertEqual(client.get(f"{base}/generation/generationjob/").status_code, 200)
        page = client.get(f"{base}/generation/generationjob/{job_id}/change/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn("O&#x27;lcham moslash", html)
        self.assertIn("/media/generated/user_", html)
        self.assertIn("src_", html)


class ResizeFreeTests(ResizeApiTests):
    """Default: o'lchamni moslash bepul — balans o'zgarmaydi, 0 token bo'lsa ham ishlaydi."""

    def setUp(self):
        super().setUp()
        self._env.stop()
        import os
        from unittest import mock

        self._env = mock.patch.dict(os.environ, {"RESIZE_TOKENS": "0"})
        self._env.start()

    # Pullik rejim testlari bu klassda qayta ishlamasin.
    test_blur_job_success_payload_and_one_token = None
    test_every_mode_through_api = None
    test_failure_refunds_token = None
    test_idempotency_key = None
    test_queued_and_cancel_refunds = None
    test_quote = None
    test_insufficient_tokens = None

    def test_free_with_zero_balance(self):
        self.user.balance = 0
        self.user.save(update_fields=["balance"])
        response = self.client.post("/api/resize", {"image": data_url(make_image((800, 800))), "mode": "blur"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 0)
        job = GenerationJob.objects.get(pk=response.data["job"]["id"])
        self.assertEqual(job.tokens_charged, 0)

    def test_quote_is_free(self):
        response = self.client.post("/api/quote", {"settings": {"contentType": "resize"}}, format="json")
        self.assertEqual(response.json()["tokens"], 0)

