"""Uzum uchun bepul mahsulot tavsifi: sarlavha, UTP, tavsif, xususiyat va
kalit so'zlar — ikkala tilda birdaniga.

Xuddi `analyze.py` kabi: faqat rasmda ko'ringan narsalarga tayanamiz.
Ko'rmagan xususiyatni o'ylab topish — sotuvchini aldov ustida qo'lga
tushirish demakdir, buni qilmaymiz.

Bepul: token yechilmaydi, chunki bu — mahsulotga kirish nuqtasi, odam
matn sifatiga qarab rasmga ham ishonadimi-yo'qmi hal qiladi.
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = max(10, int(os.environ.get("OPENAI_BRIEF_TIMEOUT_MS", 60_000)) // 1000)

MOCK_NOTICE = "AI tahlili o'chirilgan (mock rejim). Matnni qo'lda to'ldiring."
CHECK_NOTICE = "Bu matnni AI foto asosida yozdi. E'lon qilishdan oldin tekshiring."


def _empty_bilingual() -> dict:
    return {"uz": "", "ru": ""}


def mock_listing() -> dict:
    return {
        "title": _empty_bilingual(),
        "utp": _empty_bilingual(),
        "shortDescription": _empty_bilingual(),
        "fullDescription": _empty_bilingual(),
        "characteristics": [],
        "keywords": {"uz": [], "ru": []},
        "notice": MOCK_NOTICE,
    }


PROMPT_TEMPLATE = """Siz Uzum uchun mahsulot kartochkasi matnini yozasiz. Rasmga qarab, FAQAT
ko'ringan narsalarga tayaning — ko'rmagan xususiyatni (suv o'tkazmasligi,
kafolat muddati, material tarkibi) o'ylab topmang.
{hint_line}
Matnni IKKALA tilda bering: o'zbek lotinida VA ruscha.

JAVOB FORMATI — faqat JSON, boshqa matnsiz:
{{"title": {{"uz": "mahsulot nomi", "ru": "..."}},
  "utp": {{"uz": "bitta kuchli jumla, nega aynan shu mahsulot", "ru": "..."}},
  "shortDescription": {{"uz": "2-3 gapli qisqa tavsif", "ru": "..."}},
  "fullDescription": {{"uz": "to'liq tavsif, 4-8 gap", "ru": "..."}},
  "characteristics": [{{"name": {{"uz": "xususiyat nomi", "ru": "..."}}, "value": {{"uz": "qiymati", "ru": "..."}}}}],
  "keywords": {{"uz": ["kalit so'z", "..."], "ru": ["...", "..."]}}}}"""


def _extract_text(payload: dict) -> str | None:
    text = payload.get("output_text")
    if text:
        return text
    for item in payload.get("output", []):
        for part in item.get("content", []):
            if part.get("text"):
                return part["text"]
    return None


def generate_listing(image_data_url: str, hint: str = "") -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    provider = os.environ.get("GENERATION_PROVIDER", "mock").lower()

    if provider == "mock" or not api_key:
        return mock_listing()

    hint_line = f"\nSotuvchi izohi (e'tiborga oling): {hint}\n" if hint else ""
    prompt = PROMPT_TEMPLATE.format(hint_line=hint_line)

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.environ.get("OPENAI_BRIEF_MODEL", "gpt-4o"),
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": image_data_url, "detail": "high"},
                        ],
                    }
                ],
                "max_output_tokens": 1200,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            logger.warning("Tavsif so'rovi bajarilmadi: %s", response.status_code)
            return {**mock_listing(), "notice": CHECK_NOTICE}

        text = _extract_text(response.json())
        if not text:
            return mock_listing()

        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        data = json.loads(cleaned)
        return {
            "title": data.get("title") or _empty_bilingual(),
            "utp": data.get("utp") or _empty_bilingual(),
            "shortDescription": data.get("shortDescription") or _empty_bilingual(),
            "fullDescription": data.get("fullDescription") or _empty_bilingual(),
            "characteristics": data.get("characteristics") or [],
            "keywords": data.get("keywords") or {"uz": [], "ru": []},
            "notice": CHECK_NOTICE,
        }
    except (requests.RequestException, ValueError, json.JSONDecodeError, KeyError, TypeError) as error:
        logger.warning("Tavsif javobini o'qib bo'lmadi: %s", error)
        return mock_listing()
