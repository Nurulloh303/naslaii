import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from openpyxl import Workbook
from rest_framework.test import APIClient

from accounts.models import User
from uzum import ikpu
from uzum.catalog import get_catalog
from uzum.models import IkpuCode


def make_xlsx(path, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["Классификатор"])  # sarlavhadan oldingi qator
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


RU_HEADER = ["Группа", "Класс", "Позиция", "Субпозиция", "Бренд", "Атрибут", "МХИК коди", "МХИК номи", "Штрих коди"]
UZ_HEADER = ["GURUH NOMI", "SINF NOMI", "POZITSIYA NOMI", "SUBPOZITSIYA NOMI", "BREND NOMI", "ATRIBUT NOMI", "MXIK KODI", "MXIK NOMI", "SHTRIX KODI"]


class IkpuImportTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.ru = base / "ru.xlsx"
        self.uz = base / "uz.xlsx"
        make_xlsx(self.ru, RU_HEADER, [
            ["Спорт", "Мячи", "Мячи футбольные", "", "", "", 6950400001000000, "Мяч футбольный", "4600000000017"],
            ["Спорт", "Мячи", "Мячи баскетбольные", "", "", "", "06950400002000000", "Мяч баскетбольный", ""],
            ["Одежда", "Футболки", "Футболки мужские", "", "", "", 6109100001000000, "Футболка мужская хлопковая", ""],
        ])
        make_xlsx(self.uz, UZ_HEADER, [
            ["Sport", "To'plar", "Futbol to'plari", "", "", "", 6950400001000000, "Futbol to'pi", ""],
            ["Kiyim", "Futbolkalar", "Erkaklar futbolkalari", "", "", "", 6109100001000000, "Erkaklar paxta futbolkasi", ""],
        ])
        call_command("uzum_import_ikpu", ru=str(self.ru), uz=str(self.uz), stdout=open("/dev/null", "w"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_merges_languages_and_pads_codes(self):
        self.assertEqual(IkpuCode.objects.count(), 3)
        ball = IkpuCode.objects.get(code="06950400001000000")
        self.assertEqual(len(ball.code), 17)
        self.assertEqual(ball.name_uz, "Futbol to'pi")
        self.assertEqual(ball.barcode, "4600000000017")
        self.assertIn("Спорт > Мячи", ball.path_ru)

    def test_search_ru_uz_latin(self):
        self.assertEqual(ikpu.search("мяч футбольный")[0]["code"], "06950400001000000")
        self.assertEqual(ikpu.search("futbolka")[0]["code"], "06109100001000000")
        self.assertEqual(ikpu.search("futbol to'p")[0]["code"], "06950400001000000")
        self.assertEqual(ikpu.search("мячи")[0]["name"][:3], "Мяч")

    def test_barcode_and_suggest_without_ai(self):
        self.assertEqual(ikpu.by_barcode("4600000000017")[0]["code"], "06950400001000000")
        found = ikpu.suggest("Erkaklar futbolkasi", "Одежда > Мужская одежда > Футболки", use_ai=False)
        self.assertEqual(found[0]["code"], "06109100001000000")

    def test_api(self):
        user = User.objects.create_user(username="u@x.uz", email="u@x.uz", password="x")
        client = APIClient()
        client.force_authenticate(user)
        res = client.get("/api/uzum/ikpu", {"q": "мяч"}).json()
        self.assertTrue(res["ready"])
        self.assertEqual(len(res["results"]), 2)
        res = client.get("/api/uzum/ikpu", {"code": "06950400002000000"}).json()
        self.assertEqual(res["results"][0]["name"], "Мяч баскетбольный")
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            res = client.get("/api/uzum/ikpu", {"title": "Futbol to'pi", "category": "Спорт и отдых > Футбол > Футбольные мячи"}).json()
        self.assertEqual(res["results"][0]["code"], "06950400001000000")

    def test_api_not_loaded(self):
        IkpuCode.objects.all().delete()
        user = User.objects.create_user(username="v@x.uz", email="v@x.uz", password="x")
        client = APIClient()
        client.force_authenticate(user)
        self.assertEqual(client.get("/api/uzum/ikpu", {"q": "мяч"}).json(), {"ready": False, "results": []})


class CategorySearchTests(TestCase):
    def test_latin_and_uzbek_queries(self):
        catalog = get_catalog()
        self.assertTrue(any("Спорт" in c["path"] for c in catalog.search_categories("sp")))
        self.assertTrue(all("порт" in c["path"].lower() for c in catalog.search_categories("sport")[:10]))
        self.assertIn("Одежда", catalog.search_categories("kiyim")[0]["path"])
        self.assertIn("Кроссовки", catalog.search_categories("krossovka")[0]["title"])
        self.assertIn("Кофе", catalog.search_categories("qahva")[0]["path"])


class UzbekCategoryNamesTests(TestCase):
    def test_uzbek_names_and_commission(self):
        catalog = get_catalog()
        found = catalog.search_categories("futbol topi")
        self.assertEqual(found[0]["id"], 15598)
        self.assertEqual(found[0]["titleUz"], "Futbol to'plari")
        self.assertEqual(found[0]["commission"], 25)
        self.assertTrue(any(c["titleUz"].startswith("Oʻyinchoq") for c in catalog.search_categories("o'yinchoq")[:5]))
        self.assertTrue(any(c["titleUz"] == "Dazmollar" for c in catalog.search_categories("dazmol")[:3]))
        # Asl katalog ma'lumoti o'zgarmaydi (shablondan qayta yasash testi uchun muhim).
        self.assertNotIn("titleUz", catalog.data["categories"][0])

