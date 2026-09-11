"""Bref tarjimasi — rasm chizuvchi modelga yuborishdan oldin.

Muammo: bref (sarlavha, afzalliklar) deyarli har doim bitta tilda yig'iladi
— ko'pincha o'zbekcha lotin, — lekin karta boshqa tilda so'ralishi mumkin:
tilni sozlamalar bosqichida keyinroq o'zgartirsa, bref o'zi qayta
hisoblanmaydi. Bungacha tarjima to'g'ridan-to'g'ri rasm chizuvchi modelga
tashlab qo'yilardi — u matnni "tarjima qil" degan ko'rsatma bilan olib,
ko'pincha MA'NONI emas, HARFLARNI qayta yozib qo'yardi: "ish va o'qish
uchun" -> "иш ва оқиш учун", "для работы и учёбы" o'rniga.

Yechim: kartaga chiqadigan matn (title/subtitle/benefits) generatsiyadan
OLDIN, alohida, arzon matnli so'rov bilan tarjima qilinadi. Rasm chizuvchi
modelga endi tayyor matn beriladi — u endi tarjima qilmaydi, faqat chop
etadi.

Kategoriya (category) TARJIMA QILINMAYDI — bu ko'rinadigan matn emas,
ichki ro'yxatdan (CATEGORIES) tanlangan kod, shablon tanlashda
ishlatiladi. Tarjima uni ro'yxat bilan solishtirishni buzardi.

Xatolik bo'lsa (masalan, kalit yo'q yoki so'rov ishlamasa) — generatsiya
QULAMAYDI, bref o'zgarishsiz qaytariladi va rasm chizuvchi model o'zi
tarjima qilishga harakat qiladi (eski xatti-harakat). Bu xavfsiz fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = max(10, int(os.environ.get("OPENAI_BRIEF_TIMEOUT_MS", 60_000)) // 1000)

LANGUAGE_NAMES = {
    "uz": "o'zbek lotinida",
    "ru": "ruscha (kirill)",
    "tg": "tojikcha (kirill)",
}

_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _script_of(text: str) -> str | None:
    """Matn yozuvi: 'cyrillic', 'latin' yoki aniqlab bo'lmasa None."""
    if _CYRILLIC_RE.search(text):
        return "cyrillic"
    if _LATIN_RE.search(text):
        return "latin"
    return None


def _brief_text(brief: dict) -> str:
    return " ".join(
        [
            str(brief.get("title") or ""),
            str(brief.get("subtitle") or ""),
            " ".join(str(item) for item in (brief.get("benefits") or []) if item),
        ]
    ).strip()


def brief_needs_translation(brief: dict, language: str) -> bool:
    """Bref matni sozlamalardagi til yozuviga mos kelmasa — True.

    Mos kelsa hech qanday qo'shimcha so'rov yuborilmaydi — tezlik yo'qolmaydi.
    """
    if language not in LANGUAGE_NAMES:
        return False
    expected_script = "latin" if language == "uz" else "cyrillic"
    text = _brief_text(brief)
    if not text:
        return False
    script = _script_of(text)
    return script is not None and script != expected_script


def _extract_text(payload: dict) -> str | None:
    text = payload.get("output_text")
    if text:
        return text
    for item in payload.get("output", []):
        for part in item.get("content", []):
            if part.get("text"):
                return part["text"]
    return None


def translate_brief(brief: dict, language: str | None, api_key: str | None) -> dict:
    """Bref matnini kerak bo'lsagina tarjima qiladi. Har doim xavfsiz."""
    if not language or not brief_needs_translation(brief, language):
        return brief
    if not api_key:
        return brief

    lang_name = LANGUAGE_NAMES.get(language, language)
    source = {
        "title": brief.get("title") or "",
        "subtitle": brief.get("subtitle") or "",
        "benefits": [str(item) for item in (brief.get("benefits") or []) if item],
    }
    prompt = (
        f"Quyidagi JSON'dagi title, subtitle va benefits maydonlarini {lang_name}ga TARJIMA "
        "QILING — ma'nosini bering, harflarni bir yozuvdan ikkinchisiga o'girmang (transliteratsiya "
        "qilmang). Bo'sh maydonni bo'sh qoldiring. Faqat JSON qaytaring, boshqa matnsiz, xuddi shu "
        f"kalitlar bilan:\n{json.dumps(source, ensure_ascii=False)}"
    )

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.environ.get("OPENAI_BRIEF_MODEL", "gpt-4o"),
                "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                "max_output_tokens": 500,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            logger.warning("Bref tarjimasi bajarilmadi: %s", response.status_code)
            return brief

        text = _extract_text(response.json())
        if not text:
            return brief

        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        translated = json.loads(cleaned)

        result = dict(brief)
        if translated.get("title"):
            result["title"] = str(translated["title"]).strip()[:60]
        if "subtitle" in translated:
            result["subtitle"] = str(translated.get("subtitle") or "").strip()[:40]
        if translated.get("benefits"):
            result["benefits"] = [str(item).strip()[:80] for item in translated["benefits"] if str(item).strip()][:4]
        return result
    except (requests.RequestException, ValueError, json.JSONDecodeError, KeyError, TypeError) as error:
        logger.warning("Bref tarjimasini o'qib bo'lmadi: %s", error)
        return brief
