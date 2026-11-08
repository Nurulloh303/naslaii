"""Uzum shablon reyestri va katalogi (kategoriyalar, filtrlar, ma'lumotnomalar).

Shablon reyestri
----------------
Faqat shu yerda ro'yxatga olingan, SHA-256 si tekshirilgan shablonlar bilan
ishlaymiz. Noma'lum shablon rad etiladi (``UnknownTemplate``): uning tuzilmasi
(ustunlar, varaqlar, makros) boshqacha bo'lishi mumkin, ko'r-ko'rona to'ldirish
Uzum importida xatoga olib keladi.

Katalog
-------
Лист2 (kategoriya katalogi) va Лист3 (ma'lumotnomalar) BIR MARTA o'qiladi va
ixcham JSON ga yoziladi: ``uzum/catalog/<sha>.json`` (git'da saqlanadi).
``python manage.py uzum_build_catalog`` uni qayta yasaydi; JSON bo'lmasa —
birinchi so'rovda shablondan yasaladi.

Лист2 tuzilmasi (1-qator sarlavha)::

    A category_id | B category_title | C full_path_ru | D filter_id
    E filter_ru   | F filter_value_ru | G customer_type (aslida filtr turi:
    SINGLE_CHOICE / MULTIPLE_CHOICE)

Har bir kategoriyaning bitta "asosiy" qatori bor (D..G bo'sh) va ixtiyoriy
filtr qatorlari. Filtr qiymatlari bitta katakda ``"; "`` bilan ajratilgan.
Лист2 dagi barcha filtrlar — "majburiy" (manba fayl nomi
``required_filters_by_category.tsv``, Лист1!AE1 izohi: "Сюда подгружаются
обязательные фильтры").

Makros kategoriya tanlanganda nima qiladi (``modFilters.LoadFiltersForCategory``)
-----------------------------------------------------------------------------
Лист2 ni yuqoridan pastga aylanib, ``A = catId`` bo'lgan HAR BIR qatorning
``filter_id`` si bo'yicha lug'at yig'adi (birinchi uchrash tartibida). Asosiy
qatorning ``filter_id`` si bo'sh — shuning uchun lug'atga ``""`` kaliti ham
tushadi ("fantom" ustun): sarlavhasi bo'sh, turi bo'sh → "выбор нескольких",
qiymatlari yo'q, nomi ``FilterList_`` → ``_cache!$A$1:$A$2``. Biz buni AYNAN
takrorlaymiz (``macro_columns``), chunki Uzum shu makros yasagan fayllarni
qabul qiladi. Shablondagi joriy holat (15595: AE2 bo'sh, AE3 "выбор
нескольких", AE4:AE1004 → FilterList_) shuni tasdiqlaydi.
Ochilishda makros Лист2 ni C ustuni bo'yicha saralaydi; bitta kategoriya
qatorlarining C qiymati bir xil, Excel saralashi barqaror — tartib o'zgarmaydi.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from functools import lru_cache
from pathlib import Path

from . import ooxml

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
CATALOG_DIR = APP_DIR / "catalog"

CATALOG_VERSION = 1

# SHA-256 → shablon fayli. Yangi shablon qo'shish: faylni templates/ ga
# qo'ying, shu yerga yozing, ``uzum_build_catalog`` ni ishga tushiring va
# writer/test'larni yangi tuzilmaga qarab tekshiring.
TEMPLATES: dict[str, dict] = {
    "385400a03f18b70e30fae3e8877fab0e02a374c48cdee3f7ef73a6956871e17c": {
        "file": "template_210826.xlsm",
        "label": "Uzum template 21.08.26",
    },
}
DEFAULT_TEMPLATE_SHA = "385400a03f18b70e30fae3e8877fab0e02a374c48cdee3f7ef73a6956871e17c"

SINGLE_CHOICE = "SINGLE_CHOICE"


class UnknownTemplate(LookupError):
    pass


def current_template_sha() -> str:
    sha = os.environ.get("UZUM_TEMPLATE_SHA", "").strip().lower() or DEFAULT_TEMPLATE_SHA
    if sha not in TEMPLATES:
        raise UnknownTemplate(f"Noma'lum Uzum shabloni: {sha}")
    return sha


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@lru_cache(maxsize=4)
def template_bytes(sha: str) -> bytes:
    """Reyestrdagi shablon baytlari. SHA mos kelmasa — rad etiladi."""
    entry = TEMPLATES.get(sha)
    if entry is None:
        raise UnknownTemplate(f"Noma'lum Uzum shabloni: {sha}")
    data = (TEMPLATES_DIR / entry["file"]).read_bytes()
    actual = sha256_bytes(data)
    if actual != sha:
        raise UnknownTemplate(f"Shablon fayli o'zgargan: kutilgan {sha}, topildi {actual}")
    return data


def identify_template(data: bytes) -> str:
    """Tashqaridan kelgan fayl reyestrdagi shablonmi? SHA qaytaradi yoki rad etadi."""
    sha = sha256_bytes(data)
    if sha not in TEMPLATES:
        raise UnknownTemplate("Bu Uzum shabloni tanilmadi (versiyasi ro'yxatda yo'q)")
    return sha


# ------------------------------------------------------------------ yasash

def _clean_filter_values(raw: str) -> list[str]:
    """Makrosdagi tozalash bilan bir xil: CR/LF olib tashlanadi, NBSP/TAB → bo'shliq, ``;`` bo'yicha bo'linadi."""
    raw = raw.replace("\r", "").replace("\n", "").replace(" ", " ").replace("\t", " ")
    return [part.strip(" ") for part in raw.split(";") if part.strip(" ")]


def build_catalog(data: bytes) -> dict:
    sha = sha256_bytes(data)
    archive = ooxml.open_package(data)
    shared = ooxml.read_shared_strings(archive)

    categories: dict[str, dict] = {}
    order: list[str] = []
    # catId → {fid: {"name", "type", "raw": [..]}} — birinchi uchrash tartibida (dict tartibi).
    filter_rows: dict[str, dict[str, dict]] = {}

    rows = ooxml.iter_rows(archive, "xl/worksheets/sheet2.xml", shared)
    header = None
    for row_number, values in rows:
        if row_number == 1:
            header = [values.get(i, "") for i in range(1, 8)]
            continue
        cat_id = (values.get(1) or "").strip(" ")
        if not cat_id:
            continue
        if cat_id not in categories:
            path = (values.get(3) or "").strip()
            categories[cat_id] = {
                "id": int(cat_id),
                "title": (values.get(2) or "").strip(),
                "path": path,
            }
            order.append(cat_id)
        fid = (values.get(4) or "").strip(" ")
        per_cat = filter_rows.setdefault(cat_id, {})
        if fid not in per_cat:
            per_cat[fid] = {
                "name": (values.get(5) or "").strip(" "),
                "type": (values.get(7) or "").strip(" "),
                "raw": [(values.get(6) or "").strip(" ")],
            }
        else:
            per_cat[fid]["raw"].append((values.get(6) or "").strip(" "))

    expected_header = ["category_id", "category_title", "full_path_ru", "filter_id", "filter_ru", "filter_value_ru", "customer_type"]
    if header != expected_header:
        raise ooxml.TemplateError(f"Лист2 sarlavhasi kutilganidek emas: {header}")

    paths = {c["path"] for c in categories.values()}
    prefixes = set()
    for path in paths:
        parts = path.split(" > ")
        for i in range(1, len(parts)):
            prefixes.add(" > ".join(parts[:i]))

    out_categories = []
    for cat_id in order:
        cat = categories[cat_id]
        parts = cat["path"].split(" > ")
        columns = []
        filters = []
        for fid, info in filter_rows.get(cat_id, {}).items():
            values = _clean_filter_values(";".join(info["raw"]))
            multiple = info["type"] != SINGLE_CHOICE
            columns.append(fid)
            if fid:
                filters.append({
                    "id": fid,
                    "name": info["name"],
                    "values": values,
                    "multiple": multiple,
                    "required": True,
                })
        out_categories.append({
            **cat,
            "parentPath": " > ".join(parts[:-1]),
            "leaf": cat["path"] not in prefixes,
            # Makros AE dan boshlab yozadigan ustunlar tartibi ("" — fantom ustun).
            "macroColumns": columns,
            "filters": filters,
        })

    # Лист3: A Размеры ("Тип:Значение"), B Цвета, C Бренды, D Страны.
    sizes: list[str] = []
    colors: list[str] = []
    brands: list[str] = []
    countries: list[str] = []
    dict_header = None
    for row_number, values in ooxml.iter_rows(archive, "xl/worksheets/sheet3.xml", shared):
        if row_number == 1:
            dict_header = [values.get(i, "") for i in range(1, 5)]
            continue
        for col, bucket in ((1, sizes), (2, colors), (3, brands), (4, countries)):
            value = values.get(col)
            if value is not None and value.strip():
                bucket.append(value)
    if dict_header != ["Размеры", "Цвета", "Бренды", "Страны"]:
        raise ooxml.TemplateError(f"Лист3 sarlavhasi kutilganidek emas: {dict_header}")

    def unique(items):
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    return {
        "version": CATALOG_VERSION,
        "sha": sha,
        "file": TEMPLATES.get(sha, {}).get("file", ""),
        "categories": out_categories,
        "dictionaries": {
            "sizes": unique(s for s in sizes if ":" in s),
            "colors": unique(colors),
            "brands": unique(brands),
            "countries": unique(countries),
        },
    }


def catalog_path(sha: str) -> Path:
    return CATALOG_DIR / f"{sha}.json"


def write_catalog(catalog: dict) -> Path:
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    path = catalog_path(catalog["sha"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ o'qish

_WORD = re.compile(r"\w+")


def _norm(text: str) -> str:
    return " ".join(str(text).replace("ё", "е").replace("Ё", "Е").split()).casefold()


_APOSTROPHES = str.maketrans("", "", "'ʻʼ`’‘")


def _snorm(text: str) -> str:
    """Qidiruv uchun: o'zbekcha tutuq belgilari olib tashlanadi ("Oʻyinchoq" = "o'yinchoq" = "oyinchoq")."""
    return _norm(str(text or "").translate(_APOSTROPHES))


def names_path(sha: str) -> Path:
    """Kategoriyalarning o'zbekcha nomlari va komissiyasi (Uzum komissiya jadvallaridan)."""
    return CATALOG_DIR / f"names_{sha}.json"


class Catalog:
    """Xotiradagi katalog va qidiruv indekslari."""

    def __init__(self, data: dict):
        self.data = data
        self.sha = data["sha"]
        names = {}
        try:
            names = json.loads(names_path(self.sha).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        # Asl ``data`` o'zgarmaydi — o'zbekcha nom va komissiya nusxaga qo'shiladi.
        cats = []
        for cat in data["categories"]:
            extra = names.get(str(cat["id"])) or {}
            cats.append({**cat, "titleUz": extra.get("titleUz", ""), "pathUz": extra.get("pathUz", ""), "commission": extra.get("commission")})
        self.categories = {c["id"]: c for c in cats}
        self.leaves = [c for c in cats if c["leaf"]]
        # (kategoriya, [sarlavhalar ru/uz], [yo'llar ru/uz])
        self._search = [
            (c, [t for t in (_snorm(c["title"]), _snorm(c["titleUz"])) if t], [p for p in (_snorm(c["path"]), _snorm(c["pathUz"])) if p])
            for c in self.leaves
        ]
        d = data["dictionaries"]
        self.brands = d["brands"]
        self.colors = d["colors"]
        self.countries = d["countries"]
        self.sizes = d["sizes"]
        self._lookup = {}
        for kind, values in (("brand", self.brands), ("color", self.colors), ("country", self.countries), ("size", self.sizes)):
            exact = {}
            folded = {}
            for value in values:
                exact.setdefault(value.strip(), value)
                folded.setdefault(_norm(value), value)
            self._lookup[kind] = (exact, folded, [(v, _norm(v)) for v in values])

    def dictionary(self, kind: str) -> list[str]:
        return {"brand": self.brands, "color": self.colors, "country": self.countries, "size": self.sizes}[kind]

    def canonical(self, kind: str, value: str) -> str | None:
        """Foydalanuvchi qiymatini ma'lumotnomadagi aniq yozuvga keltiradi (yo'q bo'lsa None)."""
        if not isinstance(value, str):
            return None
        exact, folded, _ = self._lookup[kind]
        stripped = value.strip()
        if stripped in exact:
            return exact[stripped]
        return folded.get(_norm(stripped))

    def search_dictionary(self, kind: str, query: str, limit: int = 50, size_types: list[str] | None = None) -> list[str]:
        _, _, items = self._lookup[kind]
        if kind == "size" and size_types is not None:
            allowed = set(size_types)
            items = [(v, n) for v, n in items if size_type(v) in allowed]
        q = _norm(query or "")
        if not q:
            return [v for v, _ in items[:limit]]
        starts, contains = [], []
        for value, norm in items:
            if norm.startswith(q):
                starts.append(value)
                if len(starts) >= limit:
                    break
            elif q in norm and len(contains) < limit:
                contains.append(value)
        return (starts + contains)[:limit]

    def search_categories(self, query: str, limit: int = 50, extra: str = "") -> list[dict]:
        """Ruscha, lotincha (transliteratsiya) va o'zbekcha so'zlar bilan qidiruv.

        Har bir so'rov so'zi uchun muqobillar to'plami olinadi (search_text.py):
        so'z kategoriya yo'lida bo'lsa mos keladi; so'z boshi bilan mos kelish
        (``sp`` → ``Спорт``) so'z ichidagi mos kelishdan yuqori turadi.
        """
        from .search_text import phrase_alternatives

        q = _norm(query or "")
        if not q:
            return self.popular(limit)
        if q.isdigit():
            cat = self.categories.get(int(q))
            return [cat] if cat and cat["leaf"] else []
        groups = [{_snorm(a) for a in alts} for alts in phrase_alternatives(q)]
        if extra:
            groups = [{_snorm(a) for a in alts} for alts in phrase_alternatives(extra)] or groups
        groups = [{a for a in g if a} for g in groups]
        groups = [g for g in groups if g]
        if len(groups) > 1:
            # Ko'p so'zli so'rovda 3 harfli transliteratsiya ("qol" → "кол") shovqin beradi.
            groups = [{a for a in g if len(a) >= 4 or not re.search(r"[а-яё]", a)} or g for g in groups]
        if not groups:
            return []
        qs = _snorm(q)
        # Ko'p so'zli so'rovda bitta so'z topilmasa ham (masalan "qo'l sumkasi" — "qo'l"
        # kategoriya nomida yo'q) yarmidan ko'pi mos kelganlar keyinroq ko'rsatiladi.
        need = max(1, (len(groups) + 1) // 2)
        ranked = []
        for cat, titles, paths in self._search:
            title_words = [w for t in titles for w in _WORD.findall(t)]
            path_words = [w for p in paths for w in _WORD.findall(p)]
            score = 0
            missed = 0
            last_missed = 0
            title_hit = False
            for index, alts in enumerate(groups):
                # So'z ichidan qidirish faqat 4+ harfli muqobil uchun ("кол" → "Коллаген" bo'lmasin).
                inner = [a for a in alts if len(a) >= 4]
                if any(w.startswith(a) for a in alts for w in title_words):
                    title_hit = True
                    score -= 1
                elif any(a in t for a in inner for t in titles):
                    score += 2
                    title_hit = True
                elif any(w.startswith(a) for a in alts for w in path_words):
                    score += 3
                elif any(a in p for a in inner for p in paths):
                    score += 5
                else:
                    missed += 1
                    # O'zbekcha ("qo'l sumkasi") va ruscha ("женская сумка") iborada asosiy so'z — oxirgisi.
                    last_missed = int(index == len(groups) - 1)
            if len(groups) - missed < need or (missed and not title_hit):
                continue
            if qs in titles:
                score -= 2
            ranked.append(((missed, last_missed), score, len(cat["path"]), cat["path"], cat))
        ranked.sort(key=lambda item: item[:4])
        return [item[4] for item in ranked[:limit]]

    # Bo'sh qidiruvda ko'rsatiladigan ommabop barg kategoriyalar (sarlavha bo'yicha).
    POPULAR_TITLES = (
        "Футболки", "Платья", "Кроссовки", "Джинсы", "Худи", "Чехлы",
        "Наушники", "Смартфоны", "Сумки", "Рюкзаки", "Футбольные мячи",
        "Кружки", "Постельное белье", "Игрушки", "Часы", "Кремы для лица",
        "Парфюмерная вода", "Зарядные устройства",
    )

    def popular(self, limit: int = 50) -> list[dict]:
        by_title: dict[str, dict] = {}
        for cat in self.leaves:
            by_title.setdefault(_norm(cat["title"]), cat)
        picked = []
        for title in self.POPULAR_TITLES:
            cat = by_title.get(_norm(title))
            if cat and cat not in picked:
                picked.append(cat)
        if len(picked) < limit:
            for cat in self.leaves:
                if len(picked) >= limit:
                    break
                if cat not in picked and cat["path"].count(" > ") <= 1:
                    picked.append(cat)
        return picked[:limit]


# ------------------------------------------------------------------ o'lcham turlari

# Uzum o'lcham ("предустановленная характеристика") qo'shishga faqat mos
# kategoriyalarda ruxsat beradi — masalan "Мыши" ga o'lcham qo'yilsa import
# "toifasi uchun oldindan belgilangan tavsiflarni yaratib bo'lmaydi" xatosini beradi.
# Shablonda kategoriya → o'lcham turi bog'lanishi yo'q, shuning uchun qoidalar
# yuqori bo'lim bo'yicha: (o'lcham turi nomidagi so'z, ruxsat etilgan bo'limlar).
_CLOTHES = r"одежд|форма\b|бель|носк|гольф|колгот|чулк|костюм|купальн|плавк|кимоно|леггинс|лосин|футболк|майк|брюк|штан|шорт|юбк|плать|куртк|комбинезон|худи|свитш|толстовк|джемпер|кофт|рубаш|блуз|жилет|пижам|халат|бюстгальтер|трус|боди\b|ползун|перчат|варежк"
_SHOES = r"обув|кроссов|кед|бутс|ботин|сапог|туфл|сандал|тапк|шлепан|сланц|мокасин|лофер|балетк"
# (o'lcham turi nomidagi so'z, mos kategoriya yo'li (regex), har doim ruxsat bo'limlari)
SIZE_RULES = (
    ("шин", r"шин", ()),
    ("обуви", _SHOES, ("Обувь",)),
    ("подгузник", r"подгуз|пеленк|трусик", ()),
    ("колец", r"кольц", ()),
    ("ремня", r"ремн|ремен|пояс", ()),
    ("головных уборов", r"головн|шапк|кепк|бейсболк|панам|шляп|берет|балаклав|тюбетей|чалм", ()),
    ("носков", r"носк|гольф|следк|подследник", ()),
    ("колгот", r"колгот|чулк", ()),
    ("бюстгальтер", r"бюстгальтер|бель|купальн", ()),
    ("джинс", r"джинс|брюк|штан", ("Одежда",)),
    ("рубашек", r"рубаш|сорочк", ("Одежда",)),
    ("одежд", _CLOTHES, ("Одежда",)),
    ("двойные", _CLOTHES, ("Одежда",)),
)


def size_type(value: str) -> str:
    return str(value or "").split(":", 1)[0].strip()


# Nomida kiyim/poyabzal so'zi bor, lekin o'zi kiyim emas (poyabzal kremi, shina qopqog'i...).
_NOT_WEARABLE = r"для обув|за обув|для шин|шин и дисков|за шинами|давления шин|устройств|двигател|безопасност|багаж|для техники|для швейных|шиномонтаж|для тетрад|баскетбольн|объектив|для выпечк|под елку|крепеж|для стирки|для чистки|для пельмен|для наращивания|поясные сумки"


def _size_type_allowed(kind: str, path: str) -> bool:
    lower = kind.lower()
    top = path.split(" > ")[0]
    leaf = path.split(" > ")[-1].lower()
    for word, pattern, tops in SIZE_RULES:
        if word in lower:
            if top in tops:
                return True
            if re.search(_NOT_WEARABLE, path.lower()):
                return False
            # Faqat oxirgi kategoriya nomi va so'z boshidan: "шин" → "Шины", "Машины" emas.
            return bool(re.search(rf"\b(?:{pattern})", leaf))
    return False


def allowed_size_types(catalog: "Catalog", category: dict | None) -> list[str]:
    """Shu kategoriyaga qo'yish mumkin bo'lgan o'lcham turlari (bo'sh — o'lcham qo'yilmaydi)."""
    if not category:
        return []
    path = category["path"]
    kinds = []
    for value in catalog.sizes:
        kind = size_type(value)
        if kind not in kinds and _size_type_allowed(kind, path):
            kinds.append(kind)
    return kinds


_lock = threading.Lock()
_loaded: dict[str, Catalog] = {}


def get_catalog(sha: str | None = None) -> Catalog:
    """Katalog (xotirada bir marta). JSON bo'lmasa — shablondan yasab yozadi."""
    sha = sha or current_template_sha()
    cached = _loaded.get(sha)
    if cached is not None:
        return cached
    with _lock:
        cached = _loaded.get(sha)
        if cached is not None:
            return cached
        path = catalog_path(sha)
        data = None
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("version") != CATALOG_VERSION or data.get("sha") != sha:
                    data = None
            except (OSError, ValueError):
                data = None
        if data is None:
            logger.info("Uzum katalogi shablondan yasalmoqda: %s", sha)
            data = build_catalog(template_bytes(sha))
            try:
                write_catalog(data)
            except OSError as error:  # faqat o'qiladigan diskda ham ishlayveramiz
                logger.warning("Uzum katalogini yozib bo'lmadi: %s", error)
        catalog = Catalog(data)
        _loaded[sha] = catalog
        return catalog
