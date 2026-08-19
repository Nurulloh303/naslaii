"""Разбор фотографии товара: название, категория и преимущества.

Фронтенд зовёт это первым шагом (`POST /api/analyze`) и подставляет ответ
в форму брифа. Человек его ПРОВЕРЯЕТ и правит — поэтому здесь не страшно
ошибиться в формулировке, но страшно выдумать характеристику: «водостойкий»
на обычной сумке уедет прямо на карточку и станет обманом покупателя.

Ключ OpenAI живёт только на сервере. Браузер его не видит никогда —
именно ради этого запрос идёт через нас, а не напрямую из страницы.
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

# Тот же список, что проверяет разбор карточки. Категорию дальше читает
# генерация и сверяет по списку: незнакомая молча превращается в «Boshqa».
CATEGORIES = [
    "Aksessuarlar", "Kiyim", "Poyabzal", "Go‘zallik", "Salomatlik",
    "Sport", "Bolalar", "Uy", "Elektronika", "Boshqa",
]

LANGUAGE_RULES = {
    "uz": "Barcha matnni o‘zbek lotinida yozing.",
    "ru": "Весь текст пиши по-русски.",
    "tg": "Тамоми матнро ба забони тоҷикӣ нависед.",
}

MOCK_NOTICE = {
    "uz": "AI tahlili o‘chirilgan (mock rejim). Mahsulot nomi, kategoriyasi va afzalliklarini qo‘lda kiriting.",
    "ru": "Разбор фото выключен (режим mock). Заполните название, категорию и преимущества вручную.",
    "tg": "Таҳлили акс хомӯш аст (реҷаи mock). Ном, категория ва бартариҳоро дастӣ нависед.",
}

CHECK_NOTICE = {
    "uz": "Bu matnni AI foto asosida yozdi. Generatsiyadan oldin nomi, kategoriyasi va aniq afzalliklarini tekshiring.",
    "ru": "Этот текст AI написал по фотографии. Перед генерацией проверьте название, категорию и преимущества.",
    "tg": "Ин матнро AI аз рӯи акс навишт. Пеш аз тавлид ном, категория ва бартариҳоро санҷед.",
}

MAX_BENEFITS = 4
REQUEST_TIMEOUT = max(10, int(os.environ.get("OPENAI_BRIEF_TIMEOUT_MS", 60_000)) // 1000)


def normalize_language(value) -> str:
    return value if value in LANGUAGE_RULES else "uz"


def build_prompt(marketplace: str, language: str) -> str:
    return f"""{LANGUAGE_RULES[language]}

Siz marketplace kartochkalari uchun mahsulot brifini tayyorlaysiz. Platforma: {marketplace}.

FAQAT RASMDA KO‘RINADIGAN narsalarga tayaning. Ko‘rmagan xususiyatni yozmang:
suv o‘tkazmasligi, kafolat muddati, material tarkibi kabi narsalarni rasmdan
bilib bo‘lmaydi — ularni yozmang.

Kategoriya faqat shu ro‘yxatdan: {", ".join(CATEGORIES)}.

JAVOB FORMATI — faqat JSON, boshqa matnsiz:
{{"title": "mahsulot nomi, 40 belgidan oshmasin",
  "subtitle": "qisqa aniqlovchi, 40 belgidan oshmasin yoki bo‘sh satr",
  "category": "ro‘yxatdagi kategoriya",
  "benefits": ["aniq afzallik", "aniq afzallik", "aniq afzallik"]}}"""


def _clean(payload: dict, language: str) -> dict:
    """Приводим ответ модели к тому, что ждёт форма брифа."""
    title = str(payload.get("title") or "").strip()[:60]
    subtitle = str(payload.get("subtitle") or "").strip()[:40]
    category = payload.get("category")
    benefits = [str(item).strip()[:80] for item in (payload.get("benefits") or []) if str(item).strip()]

    return {
        "title": title,
        "subtitle": subtitle,
        # Незнакомая категория ломает подбор правил в генерации.
        "category": category if category in CATEGORIES else "",
        "benefits": benefits[:MAX_BENEFITS],
        "aiNotice": CHECK_NOTICE[language],
    }


def mock_brief(language: str) -> dict:
    """Пустой бриф с честной пометкой.

    Придумывать название за модель нельзя: человек не станет проверять
    правдоподобный текст, и выдумка уедет на карточку.
    """
    return {
        "title": "",
        "subtitle": "",
        "category": "",
        "benefits": ["", "", ""],
        "aiNotice": MOCK_NOTICE[language],
    }


def analyze_image(image_data_url: str, marketplace: str, language: str) -> dict:
    language = normalize_language(language)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    provider = os.environ.get("GENERATION_PROVIDER", "mock").lower()

    if provider == "mock" or not api_key:
        return mock_brief(language)

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
                            {"type": "input_text", "text": build_prompt(marketplace, language)},
                            {"type": "input_image", "image_url": image_data_url, "detail": "high"},
                        ],
                    }
                ],
                "max_output_tokens": 600,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            logger.warning("Brief so‘rovi bajarilmadi: %s", response.status_code)
            return {**mock_brief(language), "aiNotice": CHECK_NOTICE[language]}

        payload = response.json()
        text = payload.get("output_text")
        if not text:
            for item in payload.get("output", []):
                for part in item.get("content", []):
                    if part.get("text"):
                        text = part["text"]
                        break
        if not text:
            return mock_brief(language)

        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return _clean(json.loads(cleaned), language)
    except (requests.RequestException, ValueError, json.JSONDecodeError, KeyError, TypeError) as error:
        # Отказ анализа не должен ломать поток: человек заполнит бриф сам.
        logger.warning("Brief javobini o‘qib bo‘lmadi: %s", error)
        return mock_brief(language)
