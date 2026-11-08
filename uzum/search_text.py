"""Uzum kategoriyalarini o'zbekcha/lotincha yozilgan so'rov bilan topish.

Uzum katalogidagi kategoriya nomlari ruscha (kirill). Sotuvchi esa ko'pincha
lotinda yozadi: "sp", "sport", "kiyim", "krossovka". Bu modul so'rovdagi har
bir so'zga muqobillar to'plamini yasaydi:
  - so'zning o'zi;
  - lotin → kirill transliteratsiyasi ("sport" → "спорт", "sp" → "сп");
  - ko'p ishlatiladigan o'zbekcha so'zlarning ruscha o'zagi ("kiyim" → "одежд");
  - (views'da) hech narsa topilmasa — AI tarjimasi, keshlanadi.
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

_DIGRAPHS = (
    ("shch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
    ("ya", "я"), ("yu", "ю"), ("yo", "ё"), ("ye", "е"),
    ("o'", "о"), ("oʻ", "о"), ("o`", "о"), ("g'", "г"), ("gʻ", "г"), ("g`", "г"),
)
_SINGLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
    "i": "и", "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "х",
    "y": "й", "z": "з", "'": "", "ʻ": "", "`": "", "’": "",
}

_LATIN = re.compile(r"[a-z]")


def has_latin(text: str) -> bool:
    return bool(_LATIN.search(str(text or "").lower()))


def latin_to_cyrillic(word: str) -> str:
    word = str(word or "").lower()
    out = []
    i = 0
    while i < len(word):
        for latin, cyr in _DIGRAPHS:
            if word.startswith(latin, i):
                out.append(cyr)
                i += len(latin)
                break
        else:
            ch = word[i]
            out.append(_SINGLE.get(ch, ch))
            i += 1
    return "".join(out)


# O'zbekcha so'z o'zagi → ruscha o'zak(lar). Kalit — so'zning boshi (prefiks),
# shuning uchun "kiyim", "kiyimlar", "kiyimi" hammasi ishlaydi.
UZ_RU = {
    "kiyim": ("одежд",), "poyabzal": ("обув",), "oyoq kiyim": ("обув",),
    "ayol": ("женск",), "xotin": ("женск",), "erkak": ("мужск",), "bola": ("детск", "дет"),
    "chaqaloq": ("младен", "малыш"), "o'yinchoq": ("игрушк",), "oyinchoq": ("игрушк",),
    "sumka": ("сумк",), "ryukzak": ("рюкзак",), "soat": ("час",), "kitob": ("книг",),
    "uy": ("дом",), "oshxona": ("кухн",), "idish": ("посуд",), "mebel": ("мебел",),
    "go'zallik": ("красот",), "gozallik": ("красот",), "kosmetika": ("косметик",),
    "atir": ("парфюм", "аромат"), "parfyum": ("парфюм",), "salomatlik": ("здоров",),
    "sog'liq": ("здоров",), "dori": ("аптек", "здоров"), "vitamin": ("витамин",),
    "mashina": ("авто",), "avto": ("авто",), "ehtiyot qism": ("запчаст",),
    "telefon": ("телефон", "смартфон"), "smartfon": ("смартфон",), "quloqchin": ("наушник",),
    "naushnik": ("наушник",), "kuloqchin": ("наушник",), "kompyuter": ("компьютер",), "noutbuk": ("ноутбук",),
    "zaryad": ("заряд",), "chexol": ("чехл", "чехол"), "maishiy": ("бытов",),
    "texnika": ("техник",), "elektronika": ("электрон",), "televizor": ("телевизор",),
    "futbolka": ("футболк",), "ko'ylak": ("плать", "рубашк"), "koylak": ("плать", "рубашк"),
    "shim": ("брюк", "штан"), "jinsi": ("джинс",), "kurtka": ("куртк",), "palto": ("пальт",),
    "ichki kiyim": ("бель",), "paypoq": ("носк",), "shippak": ("тапк", "шлепан"),
    "krossovka": ("кроссовк",), "tufli": ("туфл",), "etik": ("сапог", "ботин"),
    "sport": ("спорт",), "trenajor": ("тренажер",), "velosiped": ("велосипед",),
    "to'p": ("мяч",), "top": ("мяч",), "baliq": ("рыб",), "ov": ("охот",),
    "sayohat": ("туризм", "путешеств"), "chodir": ("палатк",),
    "oziq": ("продукт", "питани"), "ovqat": ("продукт", "питани"), "shirinlik": ("сладост", "кондитер"),
    "choy": ("чай",), "qahva": ("коф",), "ichimlik": ("напит",),
    "hayvon": ("животн",), "mushuk": ("кош",), "it": ("собак",), "kuchuk": ("собак",),
    "bog'": ("сад",), "bog": ("сад",), "gul": ("цвет",), "qurilish": ("строит",),
    "ta'mir": ("ремонт",), "tamir": ("ремонт",), "asbob": ("инструмент",),
    "kanstovar": ("канцеляр",), "ruchka": ("ручк",), "daftar": ("тетрад",), "maktab": ("школ",),
    "sovg'a": ("подар",), "sovga": ("подар",), "bayram": ("праздн",), "zargarlik": ("ювелир",),
    "taqinchoq": ("бижутер", "украшен"), "uzuk": ("кольц",), "sirg'a": ("серьг",),
    "ko'zoynak": ("очк",), "kozoynak": ("очк",), "hamyon": ("кошел",), "kamar": ("ремн", "ремен"),
    "ro'mol": ("платк",), "romol": ("платк",), "shapka": ("шапк",), "qo'lqop": ("перчат",),
    "yostiq": ("подушк",), "ko'rpa": ("одеял",), "korpa": ("одеял",), "choyshab": ("постельн",),
    "parda": ("штор",), "gilam": ("ковр", "ковер"), "chiroq": ("светил", "ламп"),
    "tozalash": ("уборк", "чистящ"), "sovun": ("мыл",), "shampun": ("шампун",),
    "soch": ("волос",), "yuz": ("лиц",), "tana": ("тел",), "tish": ("зуб",),
    "o'yin": ("игр",), "oyin": ("игр",), "musiqa": ("музык",),
}

_UZ_KEYS = sorted(UZ_RU, key=len, reverse=True)


def _dictionary_hits(token: str) -> set[str]:
    hits: set[str] = set()
    for key in _UZ_KEYS:
        stem = key.strip()
        if " " in stem:
            continue
        # "kiy" → kiyim (foydalanuvchi hali yozib bo'lmagan), "kiyimlar" → kiyim.
        if (len(token) >= 3 and stem.startswith(token)) or (token.startswith(stem) and len(stem) >= 2 and (len(stem) >= 3 or token == stem)):
            hits.update(UZ_RU[key])
    return hits


_CYR_ENDINGS = ("ами", "ями", "ого", "его", "ов", "ев", "ей", "ий", "ый", "ой", "ая", "яя", "ые", "ие", "ам", "ям", "ах", "ях", "а", "я", "ы", "и", "е", "о", "у", "ю", "ь", "й")
_UZ_ENDINGS = ("larining", "laridan", "larini", "lariga", "lari", "lar", "ning", "dagi", "dan", "ga", "si", "ni", "i")


def word_stem(word: str) -> str:
    """Oddiy o'zak: "сумка"/"сумки" → "сумк", "sumkasi"/"sumkalar" → "sumka"."""
    word = word.strip().lower()
    if re.search(r"[а-яё]", word):
        for ending in _CYR_ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                return word[: -len(ending)]
        return word
    for ending in _UZ_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def token_alternatives(token: str) -> set[str]:
    token = token.strip().lower()
    alternatives = {token}
    stem = word_stem(token)
    if stem != token:
        alternatives.add(stem)
    if has_latin(token):
        for word in {token, stem}:
            alternatives.add(word_stem(latin_to_cyrillic(word)))
            alternatives.update(_dictionary_hits(word))
    return {a for a in alternatives if a}


def phrase_alternatives(query: str) -> list[set[str]]:
    """Ko'p so'zli iboralar ("oyoq kiyim") bitta so'z sifatida tarjima qilinadi."""
    text = " ".join(str(query or "").lower().split())
    extra: list[set[str]] = []
    for key in _UZ_KEYS:
        if " " in key.strip() and key.strip() in text:
            extra.append(set(UZ_RU[key]))
            text = text.replace(key.strip(), " ")
    return [token_alternatives(t) for t in text.split()] + extra


# ------------------------------------------------------------------ AI zaxira

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


def ai_translate_query(query: str) -> str:
    """Lug'atda yo'q so'z uchun: o'zbekcha so'rovni Uzum'ning ruscha kategoriya so'zlariga."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return ""
    model = os.environ.get("OPENAI_BRIEF_MODEL") or "gpt-4o-mini"
    prompt = (
        "Translate this marketplace product-category search query (Uzbek, Russian or English, may be "
        "a partial word) into 1-3 short Russian words as used in marketplace category names. "
        'Return ONLY JSON {"ru":"..."}. Query: ' + json.dumps(query, ensure_ascii=False)
    )
    try:
        response = requests.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": 40,
            },
            timeout=8,
        )
        if not response.ok:
            return ""
        content = response.json()["choices"][0]["message"]["content"]
        return str(json.loads(content).get("ru") or "").strip()[:60]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
        logger.info("Kategoriya tarjimasi bo'lmadi: %s", error)
        return ""
