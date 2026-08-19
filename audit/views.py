"""Раздел «Плохие продажи?» — разбор карточки продавца.

Две вещи, которые здесь принципиальны:

1. Ссылка на товар нужна, но НЕ обязательна. Страница Uzum рисуется
   скриптом, её внутренний JSON могут закрыть в любой момент. Если данные
   не подтянулись, отчёт всё равно строится по картинкам и ответам.

2. Отчёт всегда показывает, чего мы НЕ проверяли. Падение продаж редко
   объясняется одной карточкой, и умолчать об этом — обмануть продавца.

Порт server/audit.mjs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta

import requests
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from config.throttling import AuditThrottle

from .models import AuditRun
from .uzum_rules import (
    AUDIT_QUESTIONS,
    NOT_CHECKED,
    find_rule,
    UZUM_MANUAL_URL,
    UZUM_RULES,
    UZUM_RULES_REVIEWED_AT,
)

logger = logging.getLogger(__name__)

FREE_PER_DAY = max(0, int(os.environ.get("AUDIT_FREE_PER_DAY", 3)))
# Картинка приходит строкой data:base64 внутри JSON — ничего другого не принимаем.
DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=]+$")
UZUM_API_TIMEOUT = max(1, int(os.environ.get("UZUM_API_TIMEOUT_MS", 6000)) // 1000)
MAX_IMAGES = 4


def parse_uzum_product_id(link: str) -> str | None:
    """Достаём id товара из ссылки. У Uzum он в конце slug."""
    if not link:
        return None
    match = re.match(r"^https?://([^/]+)(/.*)?$", link.strip())
    if not match:
        return None
    host = match.group(1).lower()
    if not re.search(r"(^|\.)uzum\.(uz|com)$", host):
        return None
    numbers = re.findall(r"\d{3,}", match.group(2) or "")
    return numbers[-1] if numbers else None


def fetch_uzum_product(product_id: str | None) -> dict:
    """Никогда не бросает исключение: неудача здесь — обычное дело."""
    if not product_id:
        return {"ok": False, "reason": "NO_ID"}
    try:
        response = requests.get(
            f"https://api.uzum.uz/api/v2/product/{product_id}",
            headers={"accept": "application/json", "accept-language": "ru"},
            timeout=UZUM_API_TIMEOUT,
        )
        if not response.ok:
            return {"ok": False, "reason": f"HTTP_{response.status_code}"}
        body = response.json()
        payload = (body.get("payload") or {}).get("data") or body.get("data") or body
        if not isinstance(payload, dict):
            return {"ok": False, "reason": "SHAPE"}
        return {
            "ok": True,
            "product": {
                "title": payload.get("title") or payload.get("name"),
                "rating": payload.get("rating"),
                "reviewsAmount": payload.get("reviewsAmount") or payload.get("feedbackQuantity"),
                "ordersAmount": payload.get("ordersAmount"),
                "category": (payload.get("category") or {}).get("title"),
                "photoCount": len(payload.get("photos") or []) or None,
            },
        }
    except requests.RequestException as error:
        return {"ok": False, "reason": type(error).__name__}


AUDIT_LANGUAGE_RULES = {
    "uz": "Hisobotning barcha matnlari o‘zbek lotinida bo‘lsin.",
    "ru": "Весь текст отчёта — verdict, title, why, fix — пиши по-русски. Значения ruleId оставь латиницей, как в списке правил.",
    "tg": "Тамоми матни ҳисобот ба забони тоҷикӣ бошад. ruleId-ро бо лотин гузор.",
}


def normalize_audit_language(language) -> str:
    return language if language in AUDIT_LANGUAGE_RULES else "uz"


def with_rule_sources(report: dict) -> dict:
    """К каждой проблеме привязываем источник.

    Модель возвращает только ruleId. Ссылку и признак «официальное
    требование или наша рекомендация» ставит СЕРВЕР: разреши модели писать
    ссылку — она придумает несуществующий адрес.
    """
    problems = report.get("problems") or []
    enriched = []
    for problem in problems:
        rule = find_rule(problem.get("ruleId"))
        enriched.append({
            **problem,
            "source": (rule or {}).get("source", "practice"),
            "ruleTitle": (rule or {}).get("title"),
            "ruleDetail": (rule or {}).get("detail"),
            "ruleUrl": (rule or {}).get("url"),
        })
    return {**report, "problems": enriched}


def sanitize_report(report: dict) -> dict:
    """Категория из отчёта уезжает в генерацию и сверяется там по списку.

    Модель вольна написать «Витамины» — такой категории нет, и в генерации
    она молча стала бы «Boshqa». Приводим сразу.
    """
    categories = {"Aksessuarlar", "Kiyim", "Poyabzal", "Go‘zallik", "Salomatlik", "Sport", "Bolalar", "Uy", "Elektronika", "Boshqa"}
    suggested = report.get("suggested") or {}
    score = report.get("score")
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = 0
    return {
        **report,
        "score": score,
        "suggested": {
            "title": str(suggested.get("title") or "")[:80],
            "category": suggested.get("category") if suggested.get("category") in categories else "",
            "benefits": [b for b in (suggested.get("benefits") or []) if b][:5],
        },
    }


def build_prompt(answers: dict, product: dict | None, link: str, language: str = "uz") -> str:
    rules = "\n".join(f"- [{r['source']}] {r['id']}: {r['title']} — {r['detail']}" for r in UZUM_RULES)
    context = (
        f"Uzum API'dan olingan ma'lumot: {json.dumps(product, ensure_ascii=False)}"
        if product
        else "Uzum API'dan ma'lumot olinmadi — faqat rasm va sotuvchi javoblariga tayaning."
    )
    return f"""{AUDIT_LANGUAGE_RULES[normalize_audit_language(language)]}

Siz marketplace kartochkalari bo‘yicha auditorsiz. Uzum Market uchun yuklangan kartochka rasmlarini tekshiring.

QOIDALAR RO‘YXATI:
{rules}

MAHSULOT: {link or "havola berilmagan"}
{context}
SOTUVCHI JAVOBLARI: {json.dumps(answers, ensure_ascii=False)}

VAZIFA. Faqat RASMDA ko‘rinadigan narsalar bo‘yicha xulosa qiling. Ko‘rmagan narsangizni o‘ylab topmang.
Agar sotuvchi javoblaridan ko‘rinib tursaki, sabab kartochkada emas (narx ancha yuqori, izohlar yo‘q, ko‘rsatishlar umuman yo‘q) — buni ochiq ayting.

JAVOB FORMATI — faqat JSON:
{{"score": 0-100, "verdict": "bir jumlada xulosa",
 "problems": [{{"ruleId":"...", "severity":"critical|major|minor", "title":"...", "why":"...", "fix":"..."}}],
 "likelyNotTheCard": ["kartochkadan tashqari sabablar"],
 "suggested": {{"title":"...", "category":"...", "benefits":["...","..."]}}}}"""


MOCK_TEXT = {
    "uz": {
        "verdict": "Kartochka o‘qilishi bo‘yicha zaif, lekin sotuv tushishining yagona sababi bo‘lmasligi mumkin.",
        "problems": [
            ("Matn kichik prevyuda o‘qilmaydi", "Xaridor kartochkani avval ~200 px kenglikda ko‘radi.", "Asosiy yozuvni rasm balandligining kamida 7% qiling."),
            ("Birinchi kadrda juda ko‘p plashka", "Diqqat bo‘linadi, asosiy afzallik ajralib turmaydi.", "Birinchi kadrda 3 ta plashka qoldiring."),
        ],
        "external": {
            "reviews": "Izohlar yo‘q — bu konversiyaga kartochkadan kuchliroq ta'sir qiladi.",
            "impressions": "Ko‘rsatishlar yo‘q — muammo qidiruvdagi ko‘rinishda, kartochkada emas.",
            "boost": "Bust yoqilmagan — yangi kartochka o‘zi trafik olmasligi mumkin.",
        },
        "benefits": ["Aniq afzallikni yozing", "Ikkinchi aniq afzallik"],
    },
    "ru": {
        "verdict": "Карточка слабая по читаемости, но это может быть не единственная причина падения продаж.",
        "problems": [
            ("Текст не читается в маленьком превью", "Покупатель сначала видит карточку шириной около 200 px.", "Сделайте главную надпись не меньше 7% высоты картинки."),
            ("В первом кадре слишком много плашек", "Внимание рассеивается, главное преимущество не выделяется.", "Оставьте в первом кадре 3 плашки."),
        ],
        "external": {
            "reviews": "Отзывов нет — на конверсию это влияет сильнее, чем карточка.",
            "impressions": "Показов нет — проблема в видимости в поиске, а не в карточке.",
            "boost": "Буст не включён — новая карточка может не получать трафик сама.",
        },
        "benefits": ["Напишите конкретное преимущество", "Второе конкретное преимущество"],
    },
    "tg": {
        "verdict": "Корт аз ҷиҳати хонишоӣ заиф аст, вале ин ягона сабаби паст шудани фурӯш нест.",
        "problems": [
            ("Матн дар пешнамоиши хурд хонда намешавад", "Харидор кортро аввал бо бари тақрибан 200 px мебинад.", "Навиштаҷоти асосиро камтар аз 7% баландии расм накунед."),
            ("Дар кадри аввал плашкаҳо зиёданд", "Диққат пароканда мешавад.", "Дар кадри аввал 3 плашка монед."),
        ],
        "external": {
            "reviews": "Тақризҳо нестанд — ин ба конверсия аз корт қавитар таъсир мекунад.",
            "impressions": "Намоишҳо нестанд — мушкил дар ҷустуҷӯ аст.",
            "boost": "Буст фаъол нест — корти нав худаш трафик нагирад.",
        },
        "benefits": ["Бартарии мушаххас нависед", "Бартарии дуюми мушаххас"],
    },
}


def mock_report(answers: dict, product: dict | None, language: str = "uz") -> dict:
    """Отчёт-заглушка. Структура ТА ЖЕ, что у настоящего: интерфейс
    не должен вести себя по-разному в зависимости от провайдера."""
    text = MOCK_TEXT[normalize_audit_language(language)]
    likely = [text["external"][key] for key, value in (("reviews", "none"), ("impressions", "noImpressions"), ("boost", "boostOff")) if answers.get(key) == value]
    severities = ["critical", "major"]
    rule_ids = ["thumbnail_legibility", "first_frame_overload"]
    return {
        "score": 58,
        "verdict": text["verdict"],
        "problems": [
            {"ruleId": rule_ids[index], "severity": severities[index], "title": title, "why": why, "fix": fix}
            for index, (title, why, fix) in enumerate(text["problems"])
        ],
        "likelyNotTheCard": likely,
        "suggested": {
            "title": (product or {}).get("title") or "",
            "category": (product or {}).get("category") or "",
            "benefits": text["benefits"],
        },
        "mock": True,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def questions(request):
    used = AuditRun.objects.filter(user=request.user, created_at__gte=timezone.now() - timedelta(days=1)).count()
    # Бесплатная проверка одна в сутки: потерять отчёт из-за обновления
    # страницы обиднее всего, поэтому последний возвращаем.
    last = AuditRun.objects.filter(user=request.user).exclude(report={}).first()
    return Response({
        "questions": AUDIT_QUESTIONS,
        "remainingToday": max(0, FREE_PER_DAY - used),
        "lastReport": last.report if last else None,
        "lastReportAt": last.created_at.isoformat() if last else None,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([AuditThrottle])
def run_audit(request):
    used = AuditRun.objects.filter(user=request.user, created_at__gte=timezone.now() - timedelta(days=1)).count()
    if used >= FREE_PER_DAY:
        return Response(
            {
                "error": "AUDIT_LIMIT",
                "message": f"Bepul tahlil kuniga {FREE_PER_DAY} marta. Ertaga yana urinib ko‘ring.",
                "remainingToday": 0,
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    images = request.data.get("images") or []
    if not isinstance(images, list) or not images:
        return Response({"error": "NO_IMAGE", "message": "Kamida bitta rasm yuklang"}, status=status.HTTP_400_BAD_REQUEST)

    # Только картинки, вложенные прямо в запрос. Разреши здесь обычный
    # адрес — и наш ключ OpenAI станет чужим загрузчиком страниц: модель
    # сходит по любой ссылке, включая внутренние адреса сервера.
    images = [item for item in images if isinstance(item, str) and DATA_IMAGE_RE.match(item)]
    if not images:
        return Response(
            {"error": "INVALID_IMAGE", "message": "Rasmlar noto‘g‘ri formatda. Faylni qaytadan yuklang."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    link = str(request.data.get("link") or "").strip()
    answers = request.data.get("answers") or {}

    product_id = parse_uzum_product_id(link)
    fetched = fetch_uzum_product(product_id)
    product = fetched.get("product") if fetched.get("ok") else None

    meta = {
        "link": link or None,
        # Фронтенд читает productId; без него в отчёте была пустая строка.
        "productId": product_id,
        "productFetched": fetched.get("ok", False),
        "productFetchReason": None if fetched.get("ok") else fetched.get("reason"),
        "notChecked": NOT_CHECKED,
        "rulesSource": UZUM_MANUAL_URL,
        "rulesReviewedAt": UZUM_RULES_REVIEWED_AT,
    }

    provider = os.environ.get("AUDIT_PROVIDER", "mock")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    language = normalize_audit_language(request.data.get("language"))

    if provider == "mock" or not api_key:
        report = mock_report(answers, product, language)
    else:
        report = _ask_openai(answers, product, link, images, api_key, language) or mock_report(answers, product, language)

    report = with_rule_sources(sanitize_report(report))
    report["meta"] = meta
    AuditRun.objects.create(user=request.user, link=link[:300], report=report)
    used += 1

    return Response({"report": report, "remainingToday": max(0, FREE_PER_DAY - used)})


def _ask_openai(answers: dict, product: dict | None, link: str, images: list, api_key: str, language: str = "uz") -> dict | None:
    """Используем /v1/responses — тот же путь, что и в Node-версии.

    Второй, непроверенный способ обращения к модели заводить не стоит.
    """
    content = [{"type": "input_text", "text": build_prompt(answers, product, link, language)}]
    for image in images[:MAX_IMAGES]:
        content.append({"type": "input_image", "image_url": image, "detail": "high"})

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.environ.get("OPENAI_BRIEF_MODEL", "gpt-4o"),
                "input": [{"role": "user", "content": content}],
                "max_output_tokens": 1200,
            },
            timeout=90,
        )
        if not response.ok:
            logger.warning("Audit so‘rovi bajarilmadi: %s", response.status_code)
            return None
        payload = response.json()
        text = payload.get("output_text")
        if not text:
            for item in payload.get("output", []):
                for part in item.get("content", []):
                    if part.get("text"):
                        text = part["text"]
                        break
        if not text:
            return None
        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(cleaned)
    except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
        logger.warning("Audit javobini o‘qib bo‘lmadi: %s", error)
        return None
