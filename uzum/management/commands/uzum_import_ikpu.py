"""tasnif.soliq.uz Excel eksportidan IKPU bazasini to'liq yuklash.

Fayllarni O'zbekistondagi kompyuterdan brauzerda yuklab olinadi:
  https://tasnif.soliq.uz/api/cls-api/excel/get/category?lang=ru
  https://tasnif.soliq.uz/api/cls-api/excel/get/category?lang=uz_latn

Foydalanish:
  python manage.py uzum_import_ikpu                 # repodagi tayyor baza (uzum/data/ikpu.tsv.xz)
  python manage.py uzum_import_ikpu --ru ikpu_ru.xlsx --uz ikpu_uz.xlsx   # yangi eksportdan
  python manage.py uzum_import_ikpu --ru ... --uz ... --dump              # + repodagi faylni yangilash
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

import lzma
from pathlib import Path

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


_CODE_PREFIX = re.compile(r"^\s*\d+\s*-\s*")


def _clean(text: str) -> str:
    """"00101-Tirik otlar" → "Tirik otlar"; "00101001001000 ---" → ""."""
    text = _CODE_PREFIX.sub("", str(text or "").strip())
    text = re.sub(r"^\d+\s*", "", text) if re.match(r"^\d+\s+-{2,}", text) else text
    return "" if set(text.strip()) <= {"-", " "} else text.strip()


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
        return _clean(row[index])

    out: dict[str, dict] = {}
    for row in rows:
        code = _code(row[mapping["code"]] if mapping["code"] < len(row) else None)
        if not code:
            continue
        # Ko'rsatish uchun qisqa yo'l (pozitsiya > subpozitsiya); qidiruv uchun to'liq.
        path = " > ".join(p for p in (cell(row, "position"), cell(row, "subposition")) if p)
        full_path = " ".join(p for p in (cell(row, "class"), cell(row, "position"), cell(row, "subposition")) if p)
        barcode = "".join(ch for ch in cell(row, "barcode").split(".")[0] if ch.isdigit())
        name = cell(row, "name")
        extra = " ".join(p for p in (cell(row, "brand"), cell(row, "attribute")) if p)
        out[code] = {"name": name[:1000], "path": path[:1500], "full": full_path, "barcode": barcode[:40], "extra": extra}
    workbook.close()
    return out


BUNDLED = Path(__file__).resolve().parents[2] / "data" / "ikpu.tsv.xz"


def read_bundled(path: Path = BUNDLED) -> list[IkpuCode]:
    if not path.is_file():
        raise CommandError(f"{path} topilmadi — --ru/--uz bilan Excel fayllarni bering")
    items = []
    with lzma.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6:
                continue
            code, name_ru, name_uz, path_ru, barcode, search = parts[:6]
            generic = code[11:14] == "000"
            items.append(IkpuCode(
                code=code, name_ru=name_ru, name_uz=name_uz, path_ru=path_ru, path_uz="", barcode=barcode,
                generic=generic, search=search if generic and search else normalize(f"{name_ru} {name_uz}"),
            ))
    return items


def write_bundled(items: list[IkpuCode], path: Path = BUNDLED) -> None:
    clean = lambda text: str(text or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")  # noqa: E731
    path.parent.mkdir(parents=True, exist_ok=True)
    with lzma.open(path, "wt", encoding="utf-8", preset=9) as handle:
        handle.write("# tasnif.soliq.uz MXIK eksporti (ru + uz_latn). Ustunlar: code name_ru name_uz path_ru barcode search(faqat umumiy kodlar)\n")
        for item in sorted(items, key=lambda i: i.code):
            row = (item.code, item.name_ru, item.name_uz, item.path_ru, item.barcode, item.search if item.generic else "")
            handle.write("\t".join(map(clean, row)) + "\n")


class Command(BaseCommand):
    help = "tasnif.soliq.uz Excel eksportidan IKPU (MXIK) bazasini to'liq almashtiradi"

    def add_arguments(self, parser):
        parser.add_argument("--ru", help="Ruscha eksport (lang=ru)")
        parser.add_argument("--uz", help="O'zbekcha lotin eksport (lang=uz_latn) — ixtiyoriy")
        parser.add_argument("--dump", action="store_true", help="Natijani uzum/data/ikpu.tsv.xz ga ham yozish")

    def handle(self, *args, **options):
        if not options.get("ru") and not options.get("uz"):
            items = read_bundled()
            self.stdout.write(f"Repodagi baza: {len(items)} ta kod")
        else:
            items = self._from_excel(options)
        if options.get("dump"):
            write_bundled(items)
            self.stdout.write(f"Yozildi: {BUNDLED}")
        if not items:
            raise CommandError("Fayllarda birorta ham kod topilmadi")
        with transaction.atomic():
            IkpuCode.objects.all().delete()
            IkpuCode.objects.bulk_create(items, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(f"Tayyor: {len(items)} ta IKPU kodi yuklandi"))

    def _from_excel(self, options) -> list[IkpuCode]:
        ru = read_workbook(options["ru"]) if options.get("ru") else {}
        self.stdout.write(f"Ruscha: {len(ru)} ta kod")
        uz = read_workbook(options["uz"]) if options.get("uz") else {}
        self.stdout.write(f"O'zbekcha: {len(uz)} ta kod")
        items = []
        for code in sorted(set(ru) | set(uz)):
            r, u = ru.get(code, {}), uz.get(code, {})
            generic = code[11:14] == "000"
            # Brendli kodlar nomida umumiy nom bor — yo'lni faqat umumiy kodlarga qo'shamiz (baza 3 barobar kichik).
            parts = [r.get("name", ""), u.get("name", ""), r.get("extra", "") or u.get("extra", "")]
            if generic:
                parts += [r.get("full", ""), u.get("full", "")]
            items.append(IkpuCode(
                code=code, name_ru=r.get("name", ""), name_uz=u.get("name", ""),
                path_ru=r.get("path", "") or u.get("path", ""), path_uz="",
                barcode=r.get("barcode") or u.get("barcode") or "", generic=generic, search=normalize(" ".join(parts)),
            ))
        return items
