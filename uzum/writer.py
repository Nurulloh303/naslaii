"""Uzum XLSM yozuvchisi: asl shablon paketini nusxalab, faqat kerakli qismlarni o'zgartiradi.

Makros (``Лист1.Worksheet_Change`` → ``modCategoryPicker.ApplyCategoryToRows``
→ ``modFilters.LoadFiltersForCategory`` → ``modSaveState.SaveCategoryState``)
foydalanuvchi C1 da kategoriya tanlaganda quyidagilarni qiladi. Biz ularni
serverda, makrosni ISHGA TUSHIRMASDAN, xuddi shunday natija bilan yozamiz:

1. ``C1 = "<full_path_ru> | <category_id>"`` (CategoryList qiymati, Лист2!C & " | " & A).
2. A ustuni to'ldirilgan har bir qatorda (4-qatordan) ``E = category_title``,
   ``F = category_id`` (VBA satrni ``.Value`` ga beradi → Excel uni SON qiladi).
   Eslatma: makros sarlavhani ``Find(..., LookAt:=xlPart)`` bilan qidiradi —
   302 ta kategoriya ID si boshqa ID ichida uchraydi va u yerda makros xato
   sarlavha olishi mumkin. Biz har doim to'g'ri ``category_title`` ni yozamiz
   (Uzum ID bo'yicha ishlaydi; bu farq faqat o'sha holatlarda).
3. ``Лист1!AE2:BI3`` tozalanadi; har bir filtr ustuni uchun (AE dan, Лист2
   tartibida, fantom ``""`` ustun ham) 2-qatorga filtr nomi, 3-qatorga
   ``"выбор одного"`` (SINGLE_CHOICE) yoki ``"выбор нескольких"``.
4. ``_cache`` varag'i tozalanib, har bir filtr ustuni uchun: 1-qator filtr ID,
   2.. qatorlar qiymatlar; ``FilterList_<id>`` nomi shu diapazonga; Лист1 da shu
   ustunning ``4:1004`` qatorlariga ``=FilterList_<id>`` ro'yxat validatsiyasi
   (avvalgi validatsiya shu kataklardan olib tashlanadi).
5. Ko'p tanlovli filtr qiymatlari ``;`` bilan birlashtiriladi (``HandleMultipleChoice``).
6. ``docProps/custom.xml`` dagi ``LastCategoryValue = C1`` (Workbook_Open da
   makros shu qiymatni qayta qo'llaydi — eskisi qolsa, kategoriya "qaytib"
   ketardi).

Boshqa barcha qismlar (vbaProject.bin, Лист2/Лист3, sharedStrings, styles,
connections, queryTables, calcChain, x14 validatsiyalar...) bayt-ma-bayt
o'zgarmaydi; zip ichidagi tartib ham o'sha. Matnlar ``inlineStr`` sifatida
yoziladi — ``=``/``+``/``-``/``@`` bilan boshlangan matn hech qachon formula
bo'lmaydi. Raqamlar (narx, og'irlik, o'lchamlar, F) — son; IKPU, shtrixkod,
sotuvchi ID — matn (boshidagi nol saqlanadi).
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field

from . import ooxml
from .ooxml import encode_cell_text, index_to_col, split_ref, xml_escape

SHEET1 = "xl/worksheets/sheet1.xml"
CACHE_SHEET = "xl/worksheets/sheet6.xml"
WORKBOOK = "xl/workbook.xml"
CUSTOM_PROPS = "docProps/custom.xml"

FIRST_DATA_ROW = 4
FILTER_START_COL = 31  # AE (modInit.FILTER_START_COL)
MAX_ROWS = 1004  # modInit.MAX_ROWS
HEADER_CLEAR_LAST_COL = FILTER_START_COL + 30  # BI: LoadFiltersForCategory tozalaydigan oxirgi ustun
SINGLE_LABEL = "выбор одного"
MULTI_LABEL = "выбор нескольких"

# Лист1 ustunlari (shablon 21.08.26).
COLUMNS = {
    "titleRu": "A", "sellerId": "B", "titleUz": "C", "groupKey": "D",
    "categoryTitle": "E", "categoryId": "F", "brand": "G", "model": "H",
    "country": "I", "descriptionRu": "J", "descriptionUz": "K",
    "shortRu": "L", "shortUz": "M", "compositionRu": "N", "compositionUz": "O",
    "careRu": "P", "careUz": "Q", "sizeChartRu": "R", "sizeChartUz": "S",
    "photos": "T", "barcode": "U", "ikpu": "V", "color": "W", "size": "X",
    "price": "Y", "oldPrice": "Z", "weightG": "AA", "heightMm": "AB",
    "widthMm": "AC", "lengthMm": "AD",
}

# Shablon 2-qatoridagi kutilgan sarlavhalar — boshqa tuzilmaga yozib yubormaslik uchun tekshiriladi.
EXPECTED_HEADERS = {
    "A": "Название товара RU*", "B": "Идентификатор от продавца", "C": "Название товара UZ*",
    "D": "Группировка SKU (до 100 символов)*", "E": "Название категории*", "F": "id категории*",
    "G": "Бренд*", "H": "Модель", "I": "Страна производства*", "J": "Описание товара RU*",
    "K": "Описание товара UZ*", "L": "Краткое описание RU*", "M": "Краткое описание UZ*",
    "N": "Состав RU", "O": "Состав UZ", "P": "Инструкция по уходу и эксплуатации RU",
    "Q": "Инструкция по уходу и эксплуатации UZ", "R": "Размерная сетка RU", "S": "Размерная сетка UZ",
    "T": "Ссылки на фото*", "U": "Штрихкод", "V": "ИКПУ*", "W": "Цвет", "X": "Размер",
    "Y": "Цена продажи (som)*", "Z": "Цена до скидки (som)*", "AA": "Вес (г)*",
    "AB": "Высота (мм)*", "AC": "Ширина (мм)*", "AD": "Длина (мм)*",
}


@dataclass
class Text:
    value: str


@dataclass
class Number:
    value: int


@dataclass
class SheetPlan:
    c1: str
    # (ustun indeksi, nom, belgi) — fantom ustunda nom bo'sh.
    filter_headers: list[tuple[int, str, str]] = field(default_factory=list)
    rows: list[dict[int, Text | Number]] = field(default_factory=list)


# ------------------------------------------------------------ sheet1 XML

_ROW_RE = re.compile(r"<row\b[^>]*?(?:/>|>.*?</row>)", re.S)
_CELL_RE = re.compile(r"<c\b[^>]*?(?:/>|>.*?</c>)", re.S)
_ATTR_RE = re.compile(r'([\w:]+)="([^"]*)"')


def _attrs(tag_open: str) -> dict[str, str]:
    return dict(_ATTR_RE.findall(tag_open))


class _Row:
    __slots__ = ("number", "open_tag", "cells", "original", "dirty")

    def __init__(self, number: int, open_tag: str, cells: dict[int, str], original: str | None):
        self.number = number
        self.open_tag = open_tag  # "<row ...>" (yopilmagan ko'rinishda)
        self.cells = cells  # col → <c .../> XML
        self.original = original
        self.dirty = original is None

    def render(self) -> str:
        if not self.dirty and self.original is not None:
            return self.original
        if self.cells:
            cols = sorted(self.cells)
            spans = f"{cols[0]}:{cols[-1]}"
            tag = self.open_tag
            if 'spans="' in tag:
                tag = re.sub(r'spans="[^"]*"', f'spans="{spans}"', tag)
            else:
                tag = tag.replace(f'<row r="{self.number}"', f'<row r="{self.number}" spans="{spans}"', 1)
            return tag + "".join(self.cells[c] for c in cols) + "</row>"
        return self.open_tag[:-1] + "/>"


class Sheet1Editor:
    def __init__(self, xml: str):
        start = xml.index("<sheetData")
        open_end = xml.index(">", start) + 1
        if xml[open_end - 2] == "/":  # <sheetData/>
            self.head, body, self.tail = xml[:start], "", xml[open_end:]
            self.sheet_data_open = "<sheetData>"
        else:
            end = xml.index("</sheetData>")
            self.head, body, self.tail = xml[:start], xml[open_end:end], xml[end + len("</sheetData>"):]
            self.sheet_data_open = xml[start:open_end]
        self.rows: dict[int, _Row] = {}
        for match in _ROW_RE.finditer(body):
            text = match.group(0)
            open_tag = text[: text.index(">") + 1]
            attrs = _attrs(open_tag)
            number = int(attrs["r"])
            cells: dict[int, str] = {}
            if not open_tag.endswith("/>"):
                for cell in _CELL_RE.finditer(text[len(open_tag):]):
                    col, row = split_ref(_attrs(cell.group(0)[: cell.group(0).index(">") + 1])["r"])
                    if row != number:
                        raise ooxml.TemplateError("Qator va katak raqami mos emas")
                    cells[col] = cell.group(0)
            else:
                open_tag = open_tag[:-2] + ">"
            self.rows[number] = _Row(number, open_tag, cells, text)
        leftover = _ROW_RE.sub("", body).strip()
        if leftover:
            raise ooxml.TemplateError("sheet1.xml sheetData kutilmagan tuzilmada")

    def _row(self, number: int) -> _Row:
        row = self.rows.get(number)
        if row is None:
            row = _Row(number, f'<row r="{number}">', {}, None)
            self.rows[number] = row
        return row

    def _style(self, row: _Row, col: int) -> str:
        existing = row.cells.get(col)
        if existing is None:
            return ""
        s = _attrs(existing[: existing.index(">") + 1]).get("s")
        return f' s="{s}"' if s is not None else ""

    def resolved_text(self, ref: str, shared: list[str]) -> str | None:
        """Katakning matn ko'rinishidagi qiymati (shared/inline/son) yoki None."""
        col, number = split_ref(ref)
        row = self.rows.get(number)
        cell = row.cells.get(col) if row else None
        if cell is None:
            return None
        ctype = _attrs(cell[: cell.index(">") + 1]).get("t", "n")
        if ctype == "inlineStr":
            parts = re.findall(r"<t[^>]*>(.*?)</t>", cell, re.S)
            text = "".join(parts)
            text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
            return ooxml.decode_escapes(text) or None
        raw = self.cell_value(ref)
        if raw is None:
            return None
        if ctype == "s":
            return shared[int(raw)] or None
        return raw

    def cell_value(self, ref: str) -> str | None:
        """Faqat test/tekshiruv uchun: katakdagi xom ``<v>`` yoki shared indeks."""
        col, number = split_ref(ref)
        row = self.rows.get(number)
        cell = row.cells.get(col) if row else None
        if cell is None:
            return None
        match = re.search(r"<v>(.*?)</v>", cell, re.S)
        return match.group(1) if match else None

    def set(self, ref: str, value: Text | Number | None) -> None:
        col, number = split_ref(ref)
        row = self._row(number)
        style = self._style(row, col)
        if value is None or (isinstance(value, Text) and value.value == ""):
            if style:
                row.cells[col] = f'<c r="{ref}"{style}/>'
            else:
                row.cells.pop(col, None)
        elif isinstance(value, Number):
            if isinstance(value.value, bool) or not isinstance(value.value, int):
                raise TypeError("Number faqat butun son")
            row.cells[col] = f'<c r="{ref}"{style}><v>{value.value}</v></c>'
        else:
            text = encode_cell_text(value.value)
            if not text:
                if style:
                    row.cells[col] = f'<c r="{ref}"{style}/>'
                else:
                    row.cells.pop(col, None)
            else:
                row.cells[col] = f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
        row.dirty = True

    def clear(self, ref: str) -> None:
        col, number = split_ref(ref)
        row = self.rows.get(number)
        if row is None or col not in row.cells:
            return
        self.set(ref, None)

    def render(self) -> str:
        numbers = sorted(self.rows)
        max_row = numbers[-1] if numbers else 1
        max_col = max((max(r.cells) for r in self.rows.values() if r.cells), default=1)
        head = self.head
        match = re.search(r'<dimension ref="([^"]*)"/>', head)
        if match:
            old = match.group(1)
            last = old.split(":")[-1]
            old_col, old_row = split_ref(last)
            new_ref = f"A1:{index_to_col(max(max_col, old_col))}{max(max_row, old_row)}"
            if new_ref != old:
                head = head.replace(match.group(0), f'<dimension ref="{new_ref}"/>', 1)
        body = "".join(self.rows[n].render() for n in numbers)
        return head + self.sheet_data_open + body + "</sheetData>" + self.tail


# ------------------------------------------------------ data validation

def _parse_sqref(sqref: str) -> list[tuple[int, int, int, int]]:
    rects = []
    for part in sqref.split():
        a, _, b = part.partition(":")
        c1, r1 = split_ref(a)
        c2, r2 = split_ref(b) if b else (c1, r1)
        rects.append((min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)))
    return rects


def _format_rect(rect: tuple[int, int, int, int]) -> str:
    c1, r1, c2, r2 = rect
    a = f"{index_to_col(c1)}{r1}"
    b = f"{index_to_col(c2)}{r2}"
    return a if a == b else f"{a}:{b}"


def _subtract(rect, cut):
    """rect − cut → to'g'ri to'rtburchaklar ro'yxati."""
    c1, r1, c2, r2 = rect
    k1, s1, k2, s2 = cut
    if k1 > c2 or k2 < c1 or s1 > r2 or s2 < r1:
        return [rect]
    out = []
    if r1 < s1:
        out.append((c1, r1, c2, s1 - 1))
    if r2 > s2:
        out.append((c1, s2 + 1, c2, r2))
    mid_r1, mid_r2 = max(r1, s1), min(r2, s2)
    if c1 < k1:
        out.append((c1, mid_r1, k1 - 1, mid_r2))
    if c2 > k2:
        out.append((k2 + 1, mid_r1, c2, mid_r2))
    return out


_DV_BLOCK_RE = re.compile(r'<dataValidations count="\d+">(.*?)</dataValidations>', re.S)
_DV_RE = re.compile(r"<dataValidation\b[^>]*?(?:/>|>.*?</dataValidation>)", re.S)
_X14_SQREF_RE = re.compile(r"<xm:sqref>(.*?)</xm:sqref>", re.S)


def apply_filter_validations(tail: str, columns: list[tuple[int, str]]) -> str:
    """Makrosdagi ``Validation.Delete`` + ``.Add`` ni har bir filtr ustuni uchun takrorlaydi.

    ``columns`` — (ustun indeksi, nom). Agar aynan shu validatsiya allaqachon
    bor bo'lsa (fantom AE4:AE1004 → FilterList_), XML o'zgarmaydi.
    """
    match = _DV_BLOCK_RE.search(tail)
    items = _DV_RE.findall(match.group(1)) if match else []
    changed = False
    for col, name in columns:
        target = (col, FIRST_DATA_ROW, col, MAX_ROWS)
        target_ref = _format_rect(target)
        already = any(
            _attrs(item[: item.index(">") + 1]).get("sqref") == target_ref
            and f"<formula1>{xml_escape(name)}</formula1>" in item
            and 'type="list"' in item
            for item in items
        )
        if already:
            continue
        changed = True
        new_items = []
        for item in items:
            open_tag = item[: item.index(">") + 1]
            attrs = _attrs(open_tag)
            rects = _parse_sqref(attrs.get("sqref", ""))
            remaining = []
            for rect in rects:
                remaining.extend(_subtract(rect, target))
            if not remaining:
                continue
            new_sqref = " ".join(_format_rect(r) for r in remaining)
            if new_sqref != attrs.get("sqref"):
                item = item.replace(f'sqref="{attrs["sqref"]}"', f'sqref="{new_sqref}"', 1)
            new_items.append(item)
        uid = "{" + str(uuid.uuid4()).upper() + "}"
        new_items.append(
            f'<dataValidation type="list" allowBlank="1" showInputMessage="1" sqref="{target_ref}" '
            f'xr:uid="{uid}"><formula1>{xml_escape(name)}</formula1></dataValidation>'
        )
        items = new_items
    # x14 validatsiyalari (G/I/W/X) filtr ustunlari bilan kesishmasligi kerak.
    for sq in _X14_SQREF_RE.findall(tail):
        for rect in _parse_sqref(sq):
            for col, _name in columns:
                if _subtract(rect, (col, FIRST_DATA_ROW, col, MAX_ROWS)) != [rect]:
                    raise ooxml.TemplateError("x14 validatsiya filtr ustuni bilan kesishadi")
    if not changed:
        return tail
    block = f'<dataValidations count="{len(items)}">' + "".join(items) + "</dataValidations>"
    if match:
        return tail[: match.start()] + block + tail[match.end():]
    raise ooxml.TemplateError("sheet1.xml da dataValidations bloki yo'q")


# ------------------------------------------------------ workbook / _cache

def _cache_range(cache_col: int, count: int) -> str:
    letter = index_to_col(cache_col)
    last = 1 + count
    first, last = (2, last) if last >= 2 else (1, 2)
    return f"_cache!${letter}${first}:${letter}${last}"


def update_defined_names(workbook_xml: str, names: list[tuple[str, str]]) -> str:
    for name, ref in names:
        escaped_ref = xml_escape(ref)
        pattern = re.compile(r'<definedName name="' + re.escape(name) + r'">(.*?)</definedName>', re.S)
        found = pattern.search(workbook_xml)
        element = f'<definedName name="{name}">{escaped_ref}</definedName>'
        if found:
            if found.group(1) != escaped_ref:
                workbook_xml = workbook_xml[: found.start()] + element + workbook_xml[found.end():]
            continue
        # Excel nomlarni alifbo tartibida saqlaydi (katta-kichik harf farqsiz).
        insert_at = None
        for existing in re.finditer(r'<definedName name="([^"]+)"[^>]*>.*?</definedName>', workbook_xml, re.S):
            if existing.group(1).casefold() > name.casefold():
                insert_at = existing.start()
                break
        if insert_at is None:
            insert_at = workbook_xml.index("</definedNames>")
        workbook_xml = workbook_xml[:insert_at] + element + workbook_xml[insert_at:]
    return workbook_xml


def render_cache_sheet(original: str, columns: list[tuple[str, list[str]]]) -> str:
    """``_cache`` varag'i: har ustunda 1-qator — filtr ID, 2.. — qiymatlar."""
    grid: dict[int, dict[int, str]] = {}
    for index, (fid, values) in enumerate(columns, start=1):
        if fid:
            grid.setdefault(1, {})[index] = fid
        for offset, value in enumerate(values, start=2):
            grid.setdefault(offset, {})[index] = value
    start = original.index("<sheetData")
    end_tag = "</sheetData>"
    if original[original.index(">", start) - 1] == "/":
        end = original.index(">", start) + 1
    else:
        end = original.index(end_tag) + len(end_tag)
    if not grid:
        if original[start:end] in ("<sheetData/>", "<sheetData></sheetData>"):
            return original
        body = "<sheetData/>"
    else:
        parts = ["<sheetData>"]
        for r in sorted(grid):
            cols = sorted(grid[r])
            parts.append(f'<row r="{r}" spans="{cols[0]}:{cols[-1]}">')
            for c in cols:
                ref = f"{index_to_col(c)}{r}"
                value = grid[r][c]
                if r == 1 and value.isdigit():
                    parts.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    parts.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{encode_cell_text(value)}</t></is></c>')
            parts.append("</row>")
        parts.append("</sheetData>")
        body = "".join(parts)
    out = original[:start] + body + original[end:]
    max_row = max(grid) if grid else 1
    max_col = max((max(v) for v in grid.values()), default=1)
    dim = "A1" if not grid else f"A1:{index_to_col(max_col)}{max_row}"
    return re.sub(r'<dimension ref="[^"]*"/>', f'<dimension ref="{dim}"/>', out, count=1)


def update_custom_property(custom_xml: str, name: str, value: str) -> str:
    pattern = re.compile(
        r'(<property [^>]*name="' + re.escape(name) + r'"[^>]*><vt:lpwstr>)(.*?)(</vt:lpwstr></property>)', re.S
    )
    escaped = xml_escape(ooxml.INVALID_XML_RE.sub("", value))
    if pattern.search(custom_xml):
        return pattern.sub(lambda m: m.group(1) + escaped + m.group(3), custom_xml, count=1)
    pids = [int(p) for p in re.findall(r'pid="(\d+)"', custom_xml)] or [1]
    element = (
        f'<property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="{max(pids) + 1}" '
        f'name="{xml_escape(name, attr=True)}"><vt:lpwstr>{escaped}</vt:lpwstr></property>'
    )
    return custom_xml.replace("</Properties>", element + "</Properties>", 1)


# ------------------------------------------------------------------ asosiy

def check_template_layout(editor: Sheet1Editor, shared: list[str]) -> None:
    """2-qator sarlavhalari kutilganidek ekanini tekshiradi (boshqa shablonga yozib yubormaslik uchun)."""
    for letter, expected in EXPECTED_HEADERS.items():
        raw = editor.cell_value(f"{letter}2")
        actual = shared[int(raw)] if raw is not None and raw.isdigit() else raw
        if actual != expected:
            raise ooxml.TemplateError(f"Лист1!{letter}2 kutilgan '{expected}', topildi '{actual}'")


def build_xlsm(template: bytes, category: dict, plan: SheetPlan) -> bytes:
    archive = ooxml.open_package(template)
    shared = ooxml.read_shared_strings(archive)
    names = archive.namelist()
    for required in (SHEET1, CACHE_SHEET, WORKBOOK, CUSTOM_PROPS, "xl/vbaProject.bin"):
        if required not in names:
            raise ooxml.TemplateError(f"Shablonda {required} yo'q")

    editor = Sheet1Editor(ooxml.read_part(archive, SHEET1).decode("utf-8"))
    check_template_layout(editor, shared)

    # 1. C1
    editor.set("C1", Text(plan.c1))
    # 3. Filtr sarlavhalari: AE2:BI3 tozalanadi, so'ng yoziladi.
    # Natija bir xil bo'lsa katakka tegmaymiz (masalan, shablondagi AE3 "выбор нескольких").
    desired: dict[str, str] = {}
    for col, name, label in plan.filter_headers:
        letter = index_to_col(col)
        if name:
            desired[f"{letter}2"] = name
        desired[f"{letter}3"] = label
    for col in range(FILTER_START_COL, max(HEADER_CLEAR_LAST_COL, *(c for c, _, _ in plan.filter_headers), 0) + 1):
        for r in (2, 3):
            ref = f"{index_to_col(col)}{r}"
            want = desired.get(ref)
            if editor.resolved_text(ref, shared) != want:
                editor.set(ref, Text(want) if want else None)
    # 2/5. Ma'lumot qatorlari.
    if len(plan.rows) > MAX_ROWS - FIRST_DATA_ROW + 1:
        raise ValueError("Juda ko'p qator")
    for offset, row in enumerate(plan.rows):
        number = FIRST_DATA_ROW + offset
        for col, value in row.items():
            editor.set(f"{index_to_col(col)}{number}", value)

    # 4. Validatsiyalar, _cache, nomlar.
    macro_columns = category.get("macroColumns") or []
    filters = {f["id"]: f for f in category.get("filters") or []}
    validation_cols = []
    cache_cols = []
    defined = []
    for index, fid in enumerate(macro_columns):
        values = filters[fid]["values"] if fid else []
        name = f"FilterList_{fid}"
        validation_cols.append((FILTER_START_COL + index, name))
        cache_cols.append((fid, values))
        defined.append((name, _cache_range(index + 1, len(values))))
    editor.tail = apply_filter_validations(editor.tail, validation_cols)
    sheet1_xml = editor.render()

    workbook_xml = ooxml.read_part(archive, WORKBOOK).decode("utf-8")
    new_workbook = update_defined_names(workbook_xml, defined)
    cache_xml = ooxml.read_part(archive, CACHE_SHEET).decode("utf-8")
    new_cache = render_cache_sheet(cache_xml, cache_cols)
    custom_xml = ooxml.read_part(archive, CUSTOM_PROPS).decode("utf-8")
    new_custom = update_custom_property(custom_xml, "LastCategoryValue", plan.c1)

    replacements = {SHEET1: sheet1_xml.encode("utf-8")}
    if new_workbook != workbook_xml:
        replacements[WORKBOOK] = new_workbook.encode("utf-8")
    if new_cache != cache_xml:
        replacements[CACHE_SHEET] = new_cache.encode("utf-8")
    if new_custom != custom_xml:
        replacements[CUSTOM_PROPS] = new_custom.encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in archive.infolist():
            data = replacements.get(info.filename)
            if data is None:
                data = ooxml.read_part(archive, info.filename)
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.compress_type = info.compress_type
            clone.external_attr = info.external_attr
            clone.create_system = info.create_system
            target.writestr(clone, data)
    return out.getvalue()
