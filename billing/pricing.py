"""Экономика Naslai.

Цены НЕ забиты константами. Они считаются от себестоимости одной картинки,
поэтому при изменении курса доллара или тарифа OpenAI достаточно поправить
две цифры в окружении — маржа не поплывёт.

Перенесено 1:1 из рабочей Node-версии (server/index.mjs, блок UNIT_ECONOMICS).
Если правите здесь — правьте и там: общего источника правды пока нет.
"""

from __future__ import annotations

import math
import os


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


UNIT_ECONOMICS = {
    # Сколько OpenAI берёт за одну картинку.
    "api_image_cost_usd": _env_float("API_IMAGE_COST_USD", 0.05),
    # Курс ЦБ Узбекистана. Обновляйте перед изменением цен.
    "usd_uzs": _env_float("USD_UZS", 11_935.0),
    # Доля повторных генераций из-за ошибок API — это тоже наш расход.
    "retry_rate": _env_float("RETRY_RATE", 0.10),
    # Комиссия платёжного провайдера.
    "payment_load": _env_float("PAYMENT_LOAD", 0.025),
    # 0.768 — не случайное число. Цена подогнана под уже работающий
    # infografikaai.uz (Standart: 89 000 сум за 23 картинки = 3 870 за штуку).
    # Сделать Naslai вдвое дешевле — значит увести клиентов у своего же
    # старшего сервиса.
    "target_margin": _env_float("TARGET_MARGIN", 0.768),
}

COST_PER_IMAGE_UZS = (
    UNIT_ECONOMICS["api_image_cost_usd"]
    * UNIT_ECONOMICS["usd_uzs"]
    * (1 + UNIT_ECONOMICS["retry_rate"])
)

DEEPEST_DISCOUNT = 0.25

# Считаем от САМОЙ БОЛЬШОЙ скидки: на ней маржа минимальна, и именно она
# должна оставаться не ниже целевой. Остальные пакеты выгоднее автоматически.
TOKEN_PRICE_AT_DEEPEST = (
    COST_PER_IMAGE_UZS
    / (1 - UNIT_ECONOMICS["target_margin"])
    / (1 - UNIT_ECONOMICS["payment_load"])
)
BASE_TOKEN_PRICE_UZS = TOKEN_PRICE_AT_DEEPEST / (1 - DEEPEST_DISCOUNT)


def round_up_to(value: float, step: int = 500) -> int:
    """Округляем вверх до 500 сум — так это выглядит как цена, а не как расчёт."""
    return int(math.ceil(value / step) * step)


PACK_LADDER = [
    {"id": "start", "tokens": 10, "discount": 0.0, "popular": False},
    {"id": "plus", "tokens": 40, "discount": 0.10, "popular": True},
    {"id": "pro", "tokens": 120, "discount": 0.18, "popular": False},
    {"id": "studio", "tokens": 500, "discount": DEEPEST_DISCOUNT, "popular": False},
]


def token_packs() -> list[dict]:
    """Пакеты токенов. Цена за токен строго убывает с размером пакета.

    Скидку считаем от цены САМОГО МАЛЕНЬКОГО пакета, а не от расчётной
    базы. Иначе у стартового пакета выходит «−1%»: цена округляется вверх
    до 500 сум и оказывается чуть выше базы. Так же считает Node-версия.
    """
    priced = []
    for pack in PACK_LADDER:
        price = round_up_to(pack["tokens"] * BASE_TOKEN_PRICE_UZS * (1 - pack["discount"]))
        priced.append((pack, price, price / pack["tokens"]))

    base_per_token = priced[0][2]

    return [
        {
            "id": pack["id"],
            "tokens": pack["tokens"],
            "priceUzs": price,
            "perToken": round(per_token),
            "discount": max(0, round(100 * (1 - per_token / base_per_token))),
            "popular": pack["popular"],
        }
        for pack, price, per_token in priced
    ]


from generation import slide_types as _slides
from generation.slide_types import package_slides

# Одна картинка — один токен. Одинаково для всех режимов, поэтому маржа
# везде совпадает и её не нужно пересчитывать под каждую операцию.
TOKENS_PER_IMAGE = 1

MARKETPLACE_PACKAGE_VARIANTS = 5
MIN_PACKAGE_SLIDES = _slides.MIN_PACKAGE_SLIDES
MAX_PACKAGE_SLIDES = _slides.MAX_PACKAGE_SLIDES

# Видео пока не подключено. Коэффициенты временные: поставщика нет,
# значит и себестоимость неизвестна.
VIDEO_TOKEN_MULTIPLIER = {5: 3, 10: 5}

FIXED_IMAGE_SIZE = {"width": 1080, "height": 1440}
ALLOWED_VARIANTS = (1, 2, 4)
ALLOWED_PAGES = (1, 2, 3, 4, 5)
CONTENT_TYPES = ("photo", "card", "video", "fashion", "copyStyle", "marketplacePackage")
MARKETPLACE_LABELS = {"uzum": "Uzum", "ozon": "Ozon", "wb": "WB"}

STARTER_TOKENS = _env_int("STARTER_TOKENS", 2)
# Для регистрации по почте — ноль. Одноразовый ящик делается за десять
# секунд, и именно через него накручивали бесплатные генерации.
STARTER_TOKENS_UNVERIFIED = _env_int("STARTER_TOKENS_UNVERIFIED", 0)


def total_renders(settings: dict) -> int:
    """Сколько картинок будет создано: варианты × страницы.

    В пакете картинка — это слайд, и их число выбирает продавец
    (3-8). Старый клиент поля не присылает — остаётся прежняя пятёрка.
    """
    if settings.get("contentType") == "marketplacePackage":
        return package_slides(settings, MARKETPLACE_PACKAGE_VARIANTS)
    variants = int(settings.get("variants") or 1)
    pages = int(settings.get("pages") or 1)
    return variants * pages


def quote_for(settings: dict) -> dict:
    """Стоимость задачи в токенах плюс расшифровка для интерфейса."""
    content_type = settings.get("contentType", "card")

    if content_type == "video":
        raw = (settings.get("video") or {}).get("duration", 5)
        duration = 10 if int(raw) == 10 else 5
        return {
            "tokens": VIDEO_TOKEN_MULTIPLIER[duration],
            "label": f"Video · {duration} soniya",
            "breakdown": "Video hali ulanmagan — narx dastlabki.",
        }

    images = total_renders(settings)
    tokens = images * TOKENS_PER_IMAGE
    platform = MARKETPLACE_LABELS.get(settings.get("marketplace", "uzum"), "Uzum")
    size = f"{FIXED_IMAGE_SIZE['width']}x{FIXED_IMAGE_SIZE['height']}"

    if content_type == "marketplacePackage":
        label = f"{images} slayd · {platform} · {size}"
    else:
        pages = int(settings.get("pages") or 1)
        page_note = f" · {pages} sahifa" if pages > 1 else ""
        label = f"{images} rasm{page_note} · {platform} · {size}"

    return {
        "tokens": tokens,
        "label": label,
        "breakdown": f"1 rasm = {TOKENS_PER_IMAGE} token. Jami {images} ta rasm.",
    }


def public_pricing() -> dict:
    """То, что фронтенд читает из /api/auth/me."""
    packs = token_packs()
    return {
        "perImage": TOKENS_PER_IMAGE,
        "marketplacePackage": {
            "perSlide": TOKENS_PER_IMAGE,
            "min": MIN_PACKAGE_SLIDES,
            "max": MAX_PACKAGE_SLIDES,
        },
        "video": {str(k): v for k, v in VIDEO_TOKEN_MULTIPLIER.items()},
        "tokenPriceUzs": packs[0]["perToken"],
    }
