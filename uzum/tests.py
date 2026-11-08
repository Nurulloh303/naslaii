import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User
from generation.models import GenerationJob
from uzum import catalog as catalog_mod
from uzum import writer
from uzum.models import UzumExport

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
TEMPLATE_SHA = "385400a03f18b70e30fae3e8877fab0e02a374c48cdee3f7ef73a6956871e17c"


def png_bytes(size=(1080, 1440), color=(30, 120, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def noisy_png(size):
    import os

    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def data_url(raw, mime="png"):
    import base64

    return f"data:image/{mime};base64,{base64.b64encode(raw).decode()}"


def validations_count(sheet_xml: bytes) -> tuple[int, int]:
    root = ET.fromstring(sheet_xml)
    main = root.find(f"{NS}dataValidations")
    x14 = root.findall(".//{http://schemas.microsoft.com/office/spreadsheetml/2009/9/main}dataValidation")
    return (len(main.findall(f"{NS}dataValidation")) if main is not None else 0), len(x14)


class TempStorageMixin:
    def setUp(self):
        super().setUp()
        cache.clear()  # throttle hisoblagichlari
        self._media = tempfile.mkdtemp()
        self._private = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media, UZUM_EXPORT_ROOT=self._private)
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)
        shutil.rmtree(self._private, ignore_errors=True)
        super().tearDown()


def make_user(email):
    user = User(username=email, email=email, name="Test", balance=0)
    user.set_password("testpass123")
    user.save()
    return user


def make_job(user, results=2, size=(1080, 1440), **extra):
    items = []
    for index in range(results):
        path = default_storage.save(
            f"generated/user_{user.pk}/{index:02d}abcdef0123456789{user.pk}.png", ContentFile(png_bytes(size, (index * 60, 80, 160)))
        )
        items.append({"id": f"r{index}", "url": f"/media/{path}", "width": size[0], "height": size[1]})
    return GenerationJob.objects.create(
        user=user, content_type="card", status="success", title="Futbol to'pi", results=items, **extra
    )


def base_draft(job, **overrides):
    draft = {
        "projectId": job.pk,
        "categoryId": 15598,
        "brand": "Отсутствует бренд",
        "model": "",
        "country": "Узбекистан",
        "titleRu": "Футбольный мяч, размер 5",
        "titleUz": "Futbol to'pi, 5-o'lcham",
        "descriptionRu": "Прочный мяч для игры на улице.\nПодходит для тренировок.",
        "descriptionUz": "Ko'chada o'ynash uchun mustahkam to'p.",
        "shortRu": "Мяч размер 5",
        "shortUz": "To'p 5-o'lcham",
        "ikpu": "0950600100000000",
        "groupKey": "futbol-topi-1",
        "photos": [{"jobId": job.pk, "resultIndex": 0}, {"jobId": job.pk, "resultIndex": 1}],
        "filters": {},
        "skus": [
            {"sellerId": "007", "barcode": "", "color": "Белый", "size": "", "price": 120000, "oldPrice": 150000,
             "weightG": 450, "heightMm": 230, "widthMm": 230, "lengthMm": 230},
            {"sellerId": "008", "color": "Черный", "price": 120000, "oldPrice": None,
             "weightG": 450, "heightMm": 230, "widthMm": 230, "lengthMm": 230},
        ],
    }
    draft.update(overrides)
    return draft


class CatalogTests(TestCase):
    def setUp(self):
        self.catalog = catalog_mod.get_catalog(TEMPLATE_SHA)

    def test_registry_and_sha(self):
        data = catalog_mod.template_bytes(TEMPLATE_SHA)
        self.assertEqual(catalog_mod.sha256_bytes(data), TEMPLATE_SHA)
        self.assertEqual(catalog_mod.identify_template(data), TEMPLATE_SHA)
        with self.assertRaises(catalog_mod.UnknownTemplate):
            catalog_mod.identify_template(data + b"x")
        with self.assertRaises(catalog_mod.UnknownTemplate):
            catalog_mod.template_bytes("0" * 64)

    def test_committed_json_matches_fresh_build(self):
        built = catalog_mod.build_catalog(catalog_mod.template_bytes(TEMPLATE_SHA))
        self.assertEqual(built, self.catalog.data)

    def test_categories_and_dictionaries(self):
        self.assertEqual(len(self.catalog.categories), 5317)
        ball = self.catalog.categories[15598]
        self.assertEqual(ball["title"], "Футбольные мячи")
        self.assertEqual(ball["path"], "Спорт и отдых > Футбол > Футбольные мячи")
        self.assertEqual(ball["parentPath"], "Спорт и отдых > Футбол")
        self.assertTrue(ball["leaf"])
        self.assertEqual(ball["macroColumns"], [""])
        self.assertEqual(ball["filters"], [])
        self.assertFalse(self.catalog.categories[10002]["leaf"])  # Автотовары

        self.assertIn("Отсутствует бренд", self.catalog.brands)
        self.assertNotIn("+Active", self.catalog.brands)  # Лист3!C757 — buzuq formula
        self.assertIn("Узбекистан", self.catalog.countries)
        self.assertIn("Белый", self.catalog.colors)
        self.assertTrue(all(":" in s for s in self.catalog.sizes))
        self.assertEqual(self.catalog.canonical("brand", "  отсутствует БРЕНД "), "Отсутствует бренд")
        self.assertIsNone(self.catalog.canonical("brand", "Definitely Not A Brand 123"))

    def test_filters_of_known_category_follow_macro_order(self):
        orthoses = self.catalog.categories[15983]
        # Asosiy (filtrsiz) qator birinchi → makros AE ni bo'sh "fantom" ustun qiladi.
        self.assertEqual(orthoses["macroColumns"], ["", "4114", "4115", "4116"])
        names = [f["name"] for f in orthoses["filters"]]
        self.assertEqual(names, ["Тип ортеза", "Назначение ортеза", "Степень жесткости ортеза"])
        first = orthoses["filters"][0]
        self.assertEqual(first["values"], ["Для верхних конечностей", "Для нижних конечностей", "Для позвоночника", "Для пальцев"])
        self.assertFalse(first["multiple"])
        self.assertTrue(first["required"])

        bandages = self.catalog.categories[13683]
        self.assertEqual(bandages["macroColumns"], ["221", "503", ""])  # fantom oxirida
        animal = bandages["filters"][1]
        self.assertTrue(animal["multiple"])
        self.assertIn("Для собак", animal["values"])

    def test_search(self):
        ids = [c["id"] for c in self.catalog.search_categories("футбольные мячи")]
        self.assertEqual(ids[0], 15598)
        self.assertEqual([c["id"] for c in self.catalog.search_categories("15598")], [15598])
        self.assertTrue(all(c["leaf"] for c in self.catalog.search_categories("мяч")))
        popular = self.catalog.search_categories("")
        self.assertTrue(0 < len(popular) <= 50)
        self.assertTrue(all(c["leaf"] for c in popular))


class GeneratedFileRegexTests(TempStorageMixin, TestCase):
    def test_uzum_photo_is_public_jpeg_and_other_paths_are_not(self):
        path = default_storage.save(f"generated/user_5/uzum/{'a' * 32}/01.jpg", ContentFile(b"\xff\xd8\xffjpeg"))
        client = APIClient()
        r = client.get(f"/api/files/{path}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/jpeg")
        self.assertIn("public", r["Cache-Control"])
        self.assertIn("immutable", r["Cache-Control"])
        self.assertEqual(len(r.cookies), 0)
        self.assertEqual(client.head(f"/api/files/{path}").status_code, 200)
        for bad in (
            f"generated/user_5/uzum/{'a' * 31}/01.jpg",
            f"generated/user_5/uzum/{'A' * 32}/01.jpg",
            f"generated/user_5/uzum/{'a' * 32}/001.jpg",
            f"generated/user_5/uzum/{'a' * 32}/01.png",
            f"generated/user_5/uzum/{'a' * 32}/../01.jpg",
            "uzum_exports/user_5/x.xlsm",
        ):
            self.assertEqual(client.get(f"/api/files/{bad}").status_code, 404, bad)


class ApiTests(TempStorageMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("owner@test.uz")
        self.other = make_user("other@test.uz")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.job = make_job(self.user)

    # --- katalog endpointlari
    def test_requires_auth(self):
        anon = APIClient()
        for url in ("/api/uzum/categories", "/api/uzum/categories/15598", "/api/uzum/dictionary?type=brand",
                    f"/api/uzum/prefill?projectId={self.job.pk}", "/api/uzum/exports"):
            self.assertIn(anon.get(url).status_code, (401, 403), url)
        self.assertIn(anon.post("/api/uzum/validate", {"draft": {}}, format="json").status_code, (401, 403))

    def test_categories_endpoints(self):
        body = self.client.get("/api/uzum/categories", {"q": "футбольные мячи"}).json()
        first = body["categories"][0]
        self.assertEqual({k: first[k] for k in ("id", "title", "path")}, {"id": 15598, "title": "Футбольные мячи", "path": "Спорт и отдых > Футбол > Футбольные мячи"})
        self.assertLessEqual(len(self.client.get("/api/uzum/categories").json()["categories"]), 50)
        detail = self.client.get("/api/uzum/categories/15983").json()
        self.assertEqual([f["id"] for f in detail["filters"]], ["4114", "4115", "4116"])
        self.assertEqual(set(detail["filters"][0]), {"id", "name", "values", "multiple", "required"})
        self.assertEqual(self.client.get("/api/uzum/categories/10002").status_code, 404)  # barg emas
        self.assertEqual(self.client.get("/api/uzum/categories/99999999").status_code, 404)

    def test_dictionary_endpoint(self):
        brands = self.client.get("/api/uzum/dictionary", {"type": "brand", "q": "отсутствует"}).json()["values"]
        self.assertIn("Отсутствует бренд", brands)
        sizes = self.client.get("/api/uzum/dictionary", {"type": "size", "q": "обуви"}).json()["values"]
        self.assertTrue(sizes and all(":" in s for s in sizes))
        self.assertLessEqual(len(self.client.get("/api/uzum/dictionary", {"type": "brand"}).json()["values"]), 50)
        self.assertEqual(self.client.get("/api/uzum/dictionary", {"type": "x"}).status_code, 400)

    def test_prefill_uses_listing_and_own_images(self):
        self.job.settings = {"listing": {
            "title": {"ru": "Мяч", "uz": "To'p"}, "fullDescription": {"ru": "Описание", "uz": "Tavsif"},
            "shortDescription": {"ru": "Кратко", "uz": "Qisqa"},
        }}
        self.job.save()
        body = self.client.get("/api/uzum/prefill", {"projectId": self.job.pk}).json()
        draft = body["draft"]
        self.assertEqual((draft["titleRu"], draft["titleUz"]), ("Мяч", "To'p"))
        self.assertEqual((draft["descriptionRu"], draft["shortUz"]), ("Описание", "Qisqa"))
        self.assertEqual([(i["jobId"], i["resultIndex"]) for i in draft["images"]], [(self.job.pk, 0), (self.job.pk, 1)])
        self.assertTrue(draft["groupKey"].startswith("top-"))
        self.assertLessEqual(len(draft["groupKey"]), 100)
        other_job = make_job(self.other, results=1)
        self.assertEqual(self.client.get("/api/uzum/prefill", {"projectId": other_job.pk}).status_code, 404)

    def test_prefill_generates_listing_when_missing(self):
        # mock provayderda listing bo'sh — sarlavha loyiha nomidan olinadi.
        draft = self.client.get("/api/uzum/prefill", {"projectId": self.job.pk}).json()["draft"]
        self.assertEqual(draft["titleUz"], "Futbol to'pi")

    # --- validatsiya
    def errors_for(self, draft):
        body = self.client.post("/api/uzum/validate", {"draft": draft}, format="json").json()
        return body["ok"], {(e["field"], e.get("sku")) for e in body["errors"]}, body["errors"]

    def test_valid_draft(self):
        ok, fields, errors = self.errors_for(base_draft(self.job))
        self.assertTrue(ok, errors)
        ok, fields, errors = self.errors_for(base_draft(self.job, ikpu="09506620001000000"))  # 17 xonali MXIK
        self.assertTrue(ok, errors)
        ok, fields, _ = self.errors_for(base_draft(self.job, ikpu="095066200010000"))
        self.assertIn(("ikpu", None), fields)

    def test_validation_errors(self):
        draft = base_draft(self.job, ikpu="12345", brand="No Such Brand 999", country="", titleUz="", groupKey="x" * 101)
        draft["skus"][0]["oldPrice"] = 100  # narxdan kam
        draft["skus"][1]["price"] = "12.5"
        draft["skus"][1]["weightG"] = 0
        ok, fields, errors = self.errors_for(draft)
        self.assertFalse(ok)
        for expected in [("ikpu", None), ("brand", None), ("country", None), ("titleUz", None), ("groupKey", None),
                         ("oldPrice", 0), ("price", 1), ("weightG", 1)]:
            self.assertIn(expected, fields)
        self.assertTrue(all(e["message"] for e in errors))

    def test_seller_id_auto_and_latin_only(self):
        from uzum.validation import validate_draft

        draft = base_draft(self.job)
        draft["skus"][0]["sellerId"] = ""
        draft["skus"][1]["sellerId"] = ""
        errors, out = validate_draft(draft, self.user, catalog_mod.get_catalog(TEMPLATE_SHA), check_images=False)
        ids = [sku["sellerId"] for sku in out["skus"]]
        self.assertTrue(all(re.fullmatch(r"NS[A-Z0-9]{6}", i) for i in ids), ids)
        self.assertEqual(len(set(ids)), 2)
        draft["skus"][0]["sellerId"] = "Сумка 1"
        ok, fields, _ = self.errors_for(draft)
        self.assertIn(("sellerId", 0), fields)

    def test_price_must_be_multiple_of_1000(self):
        draft = base_draft(self.job)
        draft["skus"][0]["price"] = 125500
        draft["skus"][0]["oldPrice"] = 150500
        ok, fields, errors = self.errors_for(draft)
        self.assertFalse(ok)
        self.assertIn(("price", 0), fields)
        self.assertIn(("oldPrice", 0), fields)
        self.assertTrue(any("126 000" in e["message"] for e in errors), errors)

    def test_size_not_allowed_for_category(self):
        draft = base_draft(self.job, categoryId=13187)  # Мыши — o'lcham qo'yilmaydi
        draft["skus"][0]["size"] = "Размер детских колготок:134/9–10"
        ok, fields, errors = self.errors_for(draft)
        self.assertIn(("size", 0), fields)
        detail = self.client.get("/api/uzum/categories/13187").json()
        self.assertEqual(detail["sizeTypes"], [])
        self.assertEqual(self.client.get("/api/uzum/categories/15598").json()["sizeTypes"], [])  # futbol to'pi
        values = self.client.get("/api/uzum/dictionary", {"type": "size", "q": "", "categoryId": 13187}).json()["values"]
        self.assertEqual(values, [])

    def test_duplicate_variants_and_missing_photos(self):
        draft = base_draft(self.job, photos=[])
        draft["skus"][1]["color"] = "Белый"
        ok, fields, _ = self.errors_for(draft)
        self.assertIn(("photos", None), fields)
        self.assertIn(("color", 1), fields)

    def test_dictionary_values_for_sku(self):
        draft = base_draft(self.job)
        draft["skus"][0]["color"] = "Зелёный-не-цвет"
        draft["skus"][1]["size"] = "41"
        draft["skus"][1]["barcode"] = "4601234567891"  # noto'g'ri nazorat raqami
        ok, fields, _ = self.errors_for(draft)
        self.assertIn(("color", 0), fields)
        self.assertIn(("size", 1), fields)
        self.assertIn(("barcode", 1), fields)

    def test_other_users_job_photo_is_rejected(self):
        other_job = make_job(self.other, results=1)
        ok, fields, _ = self.errors_for(base_draft(self.job, photos=[{"jobId": other_job.pk, "resultIndex": 0}]))
        self.assertIn(("photos.0", None), fields)

    def test_required_category_filters(self):
        draft = base_draft(self.job, categoryId=15983, filters={"4114": "Для пальцев", "9999": "x"})
        ok, fields, _ = self.errors_for(draft)
        self.assertIn(("filters.4115", None), fields)
        self.assertIn(("filters.4116", None), fields)
        self.assertIn(("filters.9999", None), fields)
        self.assertNotIn(("filters.4114", None), fields)
        draft["filters"] = {"4114": ["Для пальцев", "Для позвоночника"], "4115": "Профилактика травм", "4116": "Не бывает"}
        ok, fields, _ = self.errors_for(draft)
        self.assertIn(("filters.4114", None), fields)  # bitta tanlov
        self.assertIn(("filters.4116", None), fields)  # ro'yxatda yo'q

    # --- eksport
    def create(self, draft, key=None):
        payload = {"draft": draft}
        if key:
            payload["idempotencyKey"] = key
        return self.client.post("/api/uzum/exports", payload, format="json")

    def download(self, export_id, client=None):
        r = (client or self.client).get(f"/api/uzum/exports/{export_id}/download")
        return r, (b"".join(r.streaming_content) if r.status_code == 200 else b"")

    def test_export_roundtrip(self):
        draft = base_draft(self.job, titleRu="=HYPERLINK(\"http://evil\")", model="@SUM(1)", careRu="+1-2 _x000D_ literal")
        r = self.create(draft)
        self.assertEqual(r.status_code, 201, r.content)
        export = r.json()["export"]
        self.assertEqual(export["status"], "ready")
        self.assertEqual((export["categoryId"], export["skuCount"]), (15598, 2))
        self.assertEqual(export["categoryPath"], "Спорт и отдых > Футбол > Футбольные мячи")
        self.assertEqual(export["fileName"], f"naslai-uzum-{export['id']}.xlsm")
        self.assertEqual(len(export["photoUrls"]), 2)
        for key in ("createdAt", "expiresAt"):
            self.assertTrue(export[key])

        # Rasmlar: anonim GET/HEAD, image/jpeg, aniq 1080x1440, ≤ 5 MB.
        anon = APIClient()
        for url in export["photoUrls"]:
            self.assertTrue(url.startswith("https://"), url)
            self.assertRegex(url, r"/api/files/generated/user_\d+/uzum/[0-9a-f]{32}/0[12]\.jpg$")
            path = url.split("/api/files/", 1)[1]
            resp = anon.get(f"/api/files/{path}")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp["Content-Type"], "image/jpeg")
            self.assertEqual(len(resp.cookies), 0)
            raw = b"".join(resp.streaming_content)
            self.assertLessEqual(len(raw), 5 * 1000 * 1000)
            img = Image.open(io.BytesIO(raw))
            self.assertEqual((img.format, img.size, img.mode), ("JPEG", (1080, 1440), "RGB"))
            self.assertEqual(anon.head(f"/api/files/{path}").status_code, 200)

        # Fayl
        resp, data = self.download(export["id"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/vnd.ms-excel.sheet.macroEnabled.12")
        self.assertIn(f'naslai-uzum-{export["id"]}.xlsm', resp["Content-Disposition"])
        self.assertIn("attachment", resp["Content-Disposition"])

        template = zipfile.ZipFile(io.BytesIO(catalog_mod.template_bytes(TEMPLATE_SHA)))
        produced = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(produced.testzip())
        self.assertEqual(produced.namelist(), template.namelist())
        changed = [n for n in template.namelist() if template.read(n) != produced.read(n)]
        self.assertEqual(changed, ["xl/worksheets/sheet1.xml", "docProps/custom.xml"])
        self.assertEqual(produced.read("xl/vbaProject.bin"), template.read("xl/vbaProject.bin"))
        self.assertEqual(validations_count(produced.read("xl/worksheets/sheet1.xml")),
                         validations_count(template.read("xl/worksheets/sheet1.xml")))
        ET.fromstring(produced.read("xl/worksheets/sheet1.xml"))  # yaxshi shakllangan XML
        self.assertIn("Спорт и отдых &gt; Футбол &gt; Футбольные мячи | 15598", produced.read("docProps/custom.xml").decode())
        sheet_xml = produced.read("xl/worksheets/sheet1.xml").decode()
        self.assertNotIn("<f>", sheet_xml.split("<sheetData>")[1].split("</sheetData>")[0])

        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        self.assertEqual(wb.sheetnames, ["Лист1", "Лист2", "Лист3", "Инструкция_RU", "Ko'rsatmalar_UZ", "_cache"])
        ws = wb["Лист1"]
        cells = {c.coordinate: c for row in ws.iter_rows(min_row=1, max_row=6, max_col=40) for c in row if c.value is not None}
        v = lambda ref: cells[ref].value if ref in cells else None  # noqa: E731
        self.assertEqual(v("C1"), "Спорт и отдых > Футбол > Футбольные мячи | 15598")
        self.assertEqual(v("A2"), "Название товара RU*")
        self.assertIsNone(v("AE2"))
        self.assertEqual(v("AE3"), "выбор нескольких")
        for r in (4, 5):
            self.assertEqual(v(f"E{r}"), "Футбольные мячи")
            self.assertEqual(v(f"F{r}"), 15598)
            self.assertEqual(cells[f"F{r}"].data_type, "n")
            self.assertEqual(v(f"D{r}"), "futbol-topi-1")
            self.assertEqual(v(f"G{r}"), "Отсутствует бренд")
            self.assertEqual(v(f"I{r}"), "Узбекистан")
            self.assertEqual(v(f"V{r}"), "0950600100000000")
            self.assertEqual(cells[f"V{r}"].data_type, "s")
            self.assertEqual(v(f"T{r}"), ";".join(export["photoUrls"]))
            for col, expected in (("Y", 120000), ("AA", 450), ("AB", 230), ("AC", 230), ("AD", 230)):
                self.assertEqual(v(f"{col}{r}"), expected)
                self.assertIsInstance(v(f"{col}{r}"), int)
        self.assertEqual(v("A4"), '=HYPERLINK("http://evil")')
        self.assertEqual(cells["A4"].data_type, "s")
        self.assertEqual(v("H4"), "@SUM(1)")
        self.assertEqual(v("B4"), "007")
        self.assertEqual((v("W4"), v("W5")), ("Белый", "Черный"))
        self.assertEqual((v("Z4"), v("Z5")), (150000, 120000))  # chegirmasiz: eski narx = narx
        self.assertIsNone(v("A6"))
        self.assertIn("_x000D_ literal", sheet_xml.replace("_x005F_x000D_", "_x000D_"))
        self.assertIn("_x005F_x000D_", sheet_xml)
        wb.close()

        # Ro'yxat va bitta eksport
        self.assertEqual([e["id"] for e in self.client.get("/api/uzum/exports").json()["exports"]], [export["id"]])
        self.assertEqual(self.client.get(f"/api/uzum/exports/{export['id']}").json()["export"]["id"], export["id"])

        # Egasi bo'lmagan foydalanuvchi
        intruder = APIClient()
        intruder.force_authenticate(self.other)
        self.assertEqual(intruder.get(f"/api/uzum/exports/{export['id']}").status_code, 404)
        self.assertEqual(self.download(export["id"], intruder)[0].status_code, 404)
        self.assertEqual(intruder.get("/api/uzum/exports").json()["exports"], [])
        self.assertEqual(APIClient().get(f"/api/uzum/exports/{export['id']}/download").status_code in (401, 403), True)

        # Xlsm shaxsiy papkada, /media/ ostida emas.
        record = UzumExport.objects.get(pk=export["id"])
        self.assertTrue((Path(self._private) / record.file_path).is_file())
        self.assertFalse(list(Path(self._media).rglob("*.xlsm")))
        self.assertNotIn("dataUrl", str(record.draft.get("photos")))

    def test_export_validation_error_is_400(self):
        r = self.create(base_draft(self.job, ikpu="abc"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("ikpu", {e["field"] for e in r.json()["errors"]})
        self.assertEqual(UzumExport.objects.count(), 0)

    def test_idempotency(self):
        draft = base_draft(self.job)
        first = self.create(draft, key="key-123")
        second = self.create(draft, key="key-123")
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.json()["export"]["id"], second.json()["export"]["id"])
        self.assertEqual(UzumExport.objects.count(), 1)
        changed = base_draft(self.job, titleRu="Другой")
        self.assertEqual(self.create(changed, key="key-123").status_code, 409)
        self.assertEqual(self.create(draft).status_code, 201)  # kalitsiz — yangi eksport
        self.assertEqual(UzumExport.objects.count(), 2)

    def test_data_url_photo_is_fitted_to_3_4(self):
        draft = base_draft(self.job, photos=[{"jobId": self.job.pk, "resultIndex": 0}, {"dataUrl": data_url(png_bytes((800, 800)))}])
        r = self.create(draft)
        self.assertEqual(r.status_code, 201, r.content)
        path = r.json()["export"]["photoUrls"][1].split("/api/files/", 1)[1]
        with default_storage.open(path, "rb") as handle:
            self.assertEqual(Image.open(handle).size, (1080, 1440))

    def test_large_noisy_photo_stays_under_5mb(self):
        from uzum import photos

        data = photos.encode_jpeg(photos.to_export_image(Image.open(io.BytesIO(noisy_png((1600, 2000))))))
        self.assertLessEqual(len(data), photos.MAX_BYTES)
        self.assertEqual(Image.open(io.BytesIO(data)).size, (1080, 1440))

    def test_filter_category_export_matches_macro(self):
        draft = base_draft(self.job, categoryId=15983, filters={
            "4114": "Для пальцев", "4115": ["Профилактика травм"], "4116": "Мягкий",
        })
        r = self.create(draft)
        self.assertEqual(r.status_code, 201, r.content)
        _, data = self.download(r.json()["export"]["id"])
        template = zipfile.ZipFile(io.BytesIO(catalog_mod.template_bytes(TEMPLATE_SHA)))
        produced = zipfile.ZipFile(io.BytesIO(data))
        changed = {n for n in template.namelist() if template.read(n) != produced.read(n)}
        self.assertEqual(changed, {"xl/worksheets/sheet1.xml", "docProps/custom.xml", "xl/workbook.xml", "xl/worksheets/sheet6.xml"})

        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        ws = wb["Лист1"]
        grid = {c.coordinate: c.value for row in ws.iter_rows(min_row=2, max_row=5, min_col=31, max_col=40) for c in row if c.value is not None}
        self.assertEqual(grid.get("AF2"), "Тип ортеза")
        self.assertEqual(grid.get("AG2"), "Назначение ортеза")
        self.assertEqual(grid.get("AH2"), "Степень жесткости ортеза")
        self.assertNotIn("AE2", grid)
        self.assertEqual([grid.get(f"{c}3") for c in ("AE", "AF", "AG", "AH")], ["выбор нескольких", "выбор одного", "выбор одного", "выбор одного"])
        self.assertEqual([grid.get(f"{c}4") for c in ("AF", "AG", "AH")], ["Для пальцев", "Профилактика травм", "Мягкий"])
        cache_ws = wb["_cache"]
        cache_grid = {c.coordinate: c.value for row in cache_ws.iter_rows() for c in row if c.value is not None}
        self.assertEqual(cache_grid["B1"], 4114)
        self.assertEqual(cache_grid["B5"], "Для пальцев")
        self.assertNotIn("A1", cache_grid)
        wb.close()

        workbook_xml = produced.read("xl/workbook.xml").decode()
        self.assertIn('<definedName name="FilterList_4114">_cache!$B$2:$B$5</definedName>', workbook_xml)
        self.assertIn('<definedName name="FilterList_4116">_cache!$D$2:$D$4</definedName>', workbook_xml)
        self.assertIn('<definedName name="FilterList_">_cache!$A$1:$A$2</definedName>', workbook_xml)
        names = re.findall(r'<definedName name="(FilterList_[^"]*)"', workbook_xml)
        self.assertEqual(names, sorted(names, key=str.casefold))

        root = ET.fromstring(produced.read("xl/worksheets/sheet1.xml"))
        dvs = {dv.get("sqref"): dv.findtext(f"{NS}formula1") for dv in root.find(f"{NS}dataValidations")}
        self.assertEqual(dvs["AE4:AE1004"], "FilterList_")
        self.assertEqual(dvs["AF4:AF1004"], "FilterList_4114")
        self.assertEqual(dvs["AG4:AG1004"], "FilterList_4115")
        self.assertEqual(dvs["AH4:AH1004"], "FilterList_4116")
        self.assertEqual(dvs["AI4:AI1000"], "FilterList_")  # makros keyingi ustunlarga tegmaydi
        self.assertEqual(dvs["C1"], "CategoryList")
        self.assertNotIn("AF4:AF1001", dvs)
        self.assertEqual(int(root.find(f"{NS}dataValidations").get("count")), len(dvs))

    def test_libreoffice_opens_file(self):
        soffice = shutil.which("soffice")
        if not soffice:
            self.skipTest("soffice yo'q")
        r = self.create(base_draft(self.job))
        _, data = self.download(r.json()["export"]["id"])
        workdir = Path(tempfile.mkdtemp())
        try:
            src = workdir / "export.xlsm"
            src.write_bytes(data)
            proc = subprocess.run(
                [soffice, "--headless", f"-env:UserInstallation=file://{workdir}/profile", "--convert-to", "csv:Text - txt - csv (StarCalc):44,34,76,1",
                 "--outdir", str(workdir), str(src)],
                capture_output=True, timeout=240,
            )
            csv_path = workdir / "export.csv"
            self.assertTrue(csv_path.is_file(), proc.stderr.decode(errors="replace"))
            text = csv_path.read_text(encoding="utf-8")
            self.assertIn("Спорт и отдых > Футбол > Футбольные мячи | 15598", text)
            self.assertIn("0950600100000000", text)
            self.assertIn('"Футбольные мячи",15598,"Отсутствует бренд"', text)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class WriterUnitTests(TestCase):
    def test_subtract_and_sqref(self):
        self.assertEqual(writer._subtract((32, 4, 32, 1001), (32, 4, 32, 1004)), [])
        self.assertEqual(writer._subtract((32, 1, 32, 2000), (32, 4, 32, 1004)), [(32, 1, 32, 3), (32, 1005, 32, 2000)])
        self.assertEqual(writer._subtract((1, 4, 2, 789), (32, 4, 32, 1004)), [(1, 4, 2, 789)])
        self.assertEqual(writer._format_rect((31, 4, 31, 1004)), "AE4:AE1004")

    def test_encode_cell_text(self):
        from uzum.ooxml import decode_escapes, encode_cell_text

        raw = "a & <b> _x0041_ \x01\r\nz"
        encoded = encode_cell_text(raw)
        self.assertEqual(encoded, "a &amp; &lt;b&gt; _x005F_x0041_ \nz")
        self.assertEqual(decode_escapes(encoded.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")), "a & <b> _x0041_ \nz")
