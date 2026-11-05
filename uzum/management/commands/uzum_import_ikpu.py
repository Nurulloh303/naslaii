"""tasnif.soliq.uz Excel eksportidan IKPU bazasini to'liq yuklash.

Fayllarni O'zbekistondagi kompyuterdan brauzerda yuklab olinadi:
  https://tasnif.soliq.uz/api/cls-api/excel/get/category?lang=ru
  https://tasnif.soliq.uz/api/cls-api/excel/get/category?lang=uz_latn

Foydalanish:
  python manage.py uzum_import_ikpu --ru ikpu_ru.xlsx --uz ikpu_uz.xlsx
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from uzum.ikpu import normalize
from uzum.models import IkpuCode

# Ustun nomlari (katta-kichik harf, bo'shliqlarsiz solishtiriladi).
COLUMNS = {
    "code": ("мхик коди", "mxik kodi", "икпу", "мхик", "mxik", "код мхик", "код икпу", "код"),
    "name": ("мхик номи", "mxik nomi", "название икпу", "наименование мхик", "наименование икпу", "наименование", "название"),
    "group": ("гуруҳ номи", "guruh nomi", "группа", "наименование группы"),
    "class": ("синф номи", "sinf nomi", "класс", "наименование класса"),
    "position": ("позиция номи", "pozitsiya nomi", "позиция", "наименование позиции"),
    "subposition": ("субпозиция номи", "subpozitsiya nomi", "субпозиция", "наименование субпозиции"),
    "brand": ("бренд номи", "brend nomi", "бренд"),
    "attribute": ("aтрибут номи", "атрибут номи", "atribut nomi", "атрибут"),
    "barcode": ("штрих коди", "shtrix kodi", "штрих код", "штрих-код", "штрихкод"),
}


def _key(text) -> str:
    return " ".join(str(text or "").replace("\n", " ").lower().replace("ё", "е").split())


def _code(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        value = int(value)
    digits = "".join(ch for ch in str(value).strip() if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(17) if len(digits) < 17 else digits


def read_workbook(path: str) -> dict[str, dict]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as error:  # noqa: BLE001 — har qanday buzuq fayl
        raise CommandError(f"{path}: Excel fayl ochilmadi ({error})") from error
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    mapping: dict[str, int] = {}
    for _ in range(20):  # sarlavha birinchi 20 qatordan birida
        header = next(rows, None)
        if header is None:
            break
        cells = [_key(c) for c in header]
        found = {}
        for field, names in COLUMNS.items():
            for name in names:  # aniqroq nomlar oldin
                if name in cells and cells.index(name) not in found.values():
                    found[field] = cells.index(name)
                    break
        if "code" in found and "name" in found:
            mapping = found
            break
    if not mapping:
        raise CommandError(f"{path}: MXIK kodi va nomi ustunlari topilmadi")

    def cell(row, field):
        index = mapping.get(field)
        if index is None or index >= len(row) or row[index] is None:
            return ""
        return str(row[index]).strip()

    out: dict[str, dict] = {}
    for row in rows:
        code = _code(row[mapping["code"]] if mapping["code"] < len(row) else None)
        if not code:
            continue
        path = " > ".join(p for p in (cell(row, "group"), cell(row, "class"), cell(row, "position"), cell(row, "subposition")) if p)
        barcode = "".join(ch for ch in cell(row, "barcode").split(".")[0] if ch.isdigit())
        name = cell(row, "name")
        extra = " ".join(p for p in (cell(row, "brand"), cell(row, "attribute")) if p)
        out[code] = {"name": name[:1000], "path": path[:1500], "barcode": barcode[:40], "extra": extra}
    workbook.close()
    return out


class Command(BaseCommand):
    help = "tasnif.soliq.uz Excel eksportidan IKPU (MXIK) bazasini to'liq almashtiradi"

    def add_arguments(self, parser):
        parser.add_argument("--ru", help="Ruscha eksport (lang=ru)")
        parser.add_argument("--uz", help="O'zbekcha lotin eksport (lang=uz_latn) — ixtiyoriy")

    def handle(self, *args, **options):
        if not options.get("ru") and not options.get("uz"):
            raise CommandError("Kamida bitta fayl bering: --ru fayl.xlsx [--uz fayl.xlsx]")
        ru = read_workbook(options["ru"]) if options.get("ru") else {}
        self.stdout.write(f"Ruscha: {len(ru)} ta kod")
        uz = read_workbook(options["uz"]) if options.get("uz") else {}
        self.stdout.write(f"O'zbekcha: {len(uz)} ta kod")

        items = []
        for code in sorted(set(ru) | set(uz)):
            r, u = ru.get(code, {}), uz.get(code, {})
            search = normalize(" ".join((
                r.get("name", ""), u.get("name", ""), r.get("path", ""), u.get("path", ""),
                r.get("extra", "") or u.get("extra", ""),
            )))
            items.append(IkpuCode(
                code=code, name_ru=r.get("name", ""), name_uz=u.get("name", ""),
                path_ru=r.get("path", ""), path_uz=u.get("path", ""),
                barcode=r.get("barcode") or u.get("barcode") or "", search=search,
            ))
        if not items:
            raise CommandError("Fayllarda birorta ham kod topilmadi")
        with transaction.atomic():
            IkpuCode.objects.all().delete()
            IkpuCode.objects.bulk_create(items, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(f"Tayyor: {len(items)} ta IKPU kodi yuklandi"))
