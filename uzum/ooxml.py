"""Uzum XLSM shabloni uchun past darajadagi OOXML yordamchilari.

Bu modul shablonni HECH QACHON "ochmaydi" (Excel/openpyxl orqali) va makrosni
ishga tushirmaydi: zip ichidagi XML qismlarini oddiy matn sifatida o'qiydi.

Nega openpyxl emas: u saqlashda ``vbaProject.bin`` dan tashqari ``x14``
data-validation kengaytmalarini, ``connections.xml``/``queryTables`` ni va
``calcChain`` ni yo'qotadi — Uzum shabloni esa shularga tayanadi. Shuning uchun
yozuvchi (``writer.py``) asl paketni nusxalab, faqat kerakli XML qismlarini
matn darajasida o'zgartiradi.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS = "{" + NS_MAIN + "}"

# Zip-bomb / path traversal himoyasi. Shablon reyestrdan (SHA tekshirilgan)
# keladi, lekin baribir chegaralaymiz: noma'lum fayl hech qachon ochilmasin.
MAX_MEMBERS = 200
MAX_MEMBER_BYTES = 48 * 1024 * 1024
MAX_TOTAL_BYTES = 96 * 1024 * 1024


class TemplateError(ValueError):
    """Shablon paketi yaroqsiz yoki kutilgan tuzilmada emas."""


def open_package(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise TemplateError("Shablon zip emas") from error
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise TemplateError("Shablonda juda ko'p qism bor")
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in name.split("/") or "\\" in name or ":" in name:
            raise TemplateError(f"Shubhali qism nomi: {name!r}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise TemplateError(f"Qism juda katta: {name}")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise TemplateError("Shablon ochilgandagi hajmi juda katta")
    return archive


def read_part(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    with archive.open(info) as handle:
        data = handle.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise TemplateError(f"Qism juda katta: {name}")
    return data


# ----------------------------------------------------------------- manzillar

def col_to_index(letters: str) -> int:
    """``"A"`` → 1, ``"AE"`` → 31."""
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - 64)
    return value


def index_to_col(index: int) -> str:
    """1 → ``"A"``, 31 → ``"AE"``."""
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


_REF_RE = re.compile(r"^\$?([A-Z]{1,3})\$?(\d+)$")


def split_ref(ref: str) -> tuple[int, int]:
    """``"AE4"`` → (col=31, row=4)."""
    match = _REF_RE.match(ref)
    if not match:
        raise TemplateError(f"Katak manzili noto'g'ri: {ref!r}")
    return col_to_index(match.group(1)), int(match.group(2))


# ---------------------------------------------------------------- o'qish

def _si_text(si: ET.Element) -> str:
    """Shared string matni: ``<t>`` yoki ``<r><t>`` lar, ``<rPh>`` (fonetika) tashlab ketiladi."""
    parts: list[str] = []
    for child in si:
        tag = child.tag
        if tag == _NS + "t":
            parts.append(child.text or "")
        elif tag == _NS + "r":
            for t in child.iter(_NS + "t"):
                parts.append(t.text or "")
    return decode_escapes("".join(parts))


_ESCAPE_RE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def decode_escapes(text: str) -> str:
    """OOXML ``_xHHHH_`` belgilarini ochadi (Excel shunday o'qiydi)."""
    if "_x" not in text:
        return text
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = read_part(archive, "xl/sharedStrings.xml")
    except KeyError:
        return []
    strings: list[str] = []
    for _event, element in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if element.tag == _NS + "si":
            strings.append(_si_text(element))
            element.clear()
    return strings


def iter_rows(archive: zipfile.ZipFile, part: str, shared: list[str]):
    """Varaq qatorlari: ``(row_number, {col_index: value})``.

    Qiymatlar matn ko'rinishida (Excel ``CStr`` qanday ko'rsa). Xato kataklar
    (``t="e"``, masalan ``#NAME?``) tashlab ketiladi — ular ma'lumot emas.
    Formula natijasi bo'lgan kataklar ham tashlanadi: ma'lumotnomada formula
    bo'lishi kerak emas (shablonda bitta shunday buzuq katak bor: Лист3!C757).
    """
    raw = read_part(archive, part)
    for _event, element in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if element.tag != _NS + "row":
            continue
        values: dict[int, str] = {}
        for cell in element.iter(_NS + "c"):
            ctype = cell.get("t", "n")
            if ctype == "e" or cell.find(_NS + "f") is not None:
                continue
            value_el = cell.find(_NS + "v")
            if ctype == "s":
                if value_el is None or value_el.text is None:
                    continue
                value = shared[int(value_el.text)]
            elif ctype == "inlineStr":
                is_el = cell.find(_NS + "is")
                value = _si_text(is_el) if is_el is not None else ""
            elif value_el is not None and value_el.text is not None:
                value = value_el.text
                if ctype == "n" and re.fullmatch(r"-?\d+\.0+", value):
                    value = value.split(".")[0]
            else:
                continue
            col, _row = split_ref(cell.get("r"))
            values[col] = value
        yield int(element.get("r")), values
        element.clear()


# ---------------------------------------------------------------- yozish

# XML 1.0 da ruxsat etilmagan belgilar (fayl buzilmasligi uchun olib tashlanadi).
INVALID_XML_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")


def xml_escape(text: str, attr: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if attr:
        text = text.replace('"', "&quot;")
    return text


def encode_cell_text(text: str) -> str:
    """Katak matnini XML uchun tayyorlaydi.

    * taqiqlangan boshqaruv belgilari olib tashlanadi;
    * ``\\r\\n`` → ``\\n`` (Excel qator uzilishini shunday saqlaydi);
    * matndagi ``_xHHHH_`` literal qolishi uchun ``_`` → ``_x005F_``
      (aks holda Excel uni belgi kodi deb o'qirdi);
    * ``& < >`` escape qilinadi.
    """
    text = INVALID_XML_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _ESCAPE_RE.sub(lambda m: "_x005F_" + m.group(0)[1:], text)
    return xml_escape(text)
