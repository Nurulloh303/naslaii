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




def token_packs() -> list[dict]:
    """Sotuvdagi paketlar = tariflar (Start, Standart, Profi, Maksimum).

    Ilgari bu yerda formuladan hisoblangan 10/40/120/500 token paketlari
    turardi — sotuvda esa boshqa tariflar edi, profil sahifasida eski narxlar
    ko'rinib qolgandi. Endi narx va token soni bitta joydan — TARIFFS dan.

    Chegirma eng kichik tarifning bir token narxiga nisbatan hisoblanadi.
    Eng ommabopi — ikkinchi tarif (Standart).
    """
    tariffs = [t for t in TARIFFS if t["price_uzs"] > 0 and t["tokens"] > 0]
    if not tariffs:
        return []
    base_per_token = tariffs[0]["price_uzs"] / tariffs[0]["tokens"]
    popular_id = tariffs[1]["id"] if len(tariffs) > 1 else tariffs[0]["id"]
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "tokens": t["tokens"],
            "images": t["images"],
            "priceUzs": t["price_uzs"],
            "perToken": round(t["price_uzs"] / t["tokens"]),
            "discount": max(0, round(100 * (1 - (t["price_uzs"] / t["tokens"]) / base_per_token))),
            "popular": t["id"] == popular_id,
        }
        for t in tariffs
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


# ================================================================ Тарифы
# Готовые наборы токенов, которые админ выдаёт по оплаченному тарифу
# (промокодом или прямым начислением). Свободное «любое имя, любая сумма»
# остаётся — тариф лишь второй способ.
# Можно переопределить через NASLAI_TARIFFS_JSON:
#   [{"id":"start","name":"Start","price_uzs":89000,"images":21}, ...]

DEFAULT_TARIFFS = [
    {"id": "start", "name": "Start", "price_uzs": 89_000, "images": 21},
    {"id": "standart", "name": "Standart", "price_uzs": 199_000, "images": 52},
    {"id": "profi", "name": "Profi", "price_uzs": 399_000, "images": 117},
    {"id": "maksimum", "name": "Maksimum", "price_uzs": 599_000, "images": 195},
]


def _load_tariffs() -> list[dict]:
    import json

    raw = os.environ.get("NASLAI_TARIFFS_JSON", "").strip()
    source = DEFAULT_TARIFFS
    if raw:
        try:
            parsed = json.loads(raw)
            cleaned = []
            for item in parsed:
                tariff_id = str(item["id"]).strip().lower()
                images = int(item["images"])
                if not tariff_id or tariff_id == "custom" or images <= 0:
                    raise ValueError(tariff_id)
                cleaned.append({
                    "id": tariff_id,
                    "name": str(item.get("name") or tariff_id),
                    "price_uzs": int(item.get("price_uzs") or 0),
                    "images": images,
                })
            if cleaned:
                source = cleaned
        except (ValueError, TypeError, KeyError):
            # Битый JSON в окружении не должен ронять сервер — берём встроенные.
            source = DEFAULT_TARIFFS
    return [{**item, "tokens": item["images"] * TOKENS_PER_IMAGE} for item in source]


TARIFFS = _load_tariffs()


def tariff_by_id(tariff_id) -> dict | None:
    key = str(tariff_id or "").strip().lower()
    return next((t for t in TARIFFS if t["id"] == key), None)


def public_tariffs() -> list[dict]:
    return [
        {"id": t["id"], "name": t["name"], "priceUzs": t["price_uzs"], "images": t["images"], "tokens": t["tokens"]}
        for t in TARIFFS
    ]
