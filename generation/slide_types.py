"""Типы слайдов пакета для маркетплейса.

Зеркало `server/plan.mjs` в ноде: идентификаторы и формулировки фокуса
обязаны совпадать, иначе один и тот же заказ даст разные картинки на
разных бэкендах.

Раньше пакет был жёсткой пятёркой слайдов. Для футболки слайд
«технические характеристики» оказывался пустым, для дрели «контекст
использования» — самым важным, а поменять состав было нельзя. Теперь
и количество, и темы выбирает продавец.
"""

SLIDE_TYPES = [
    {"id": "main", "focus": "the product itself, big and clear"},
    {"id": "benefits", "focus": "the main advantages as short pills"},
    {"id": "sizes", "focus": "size, volume or quantity with numbers"},
    {"id": "materials", "focus": "materials and composition"},
    {"id": "usage", "focus": "where and how the product is used"},
    {"id": "kit", "focus": "what is included in the box"},
    {"id": "care", "focus": "care and storage"},
    {"id": "beforeAfter", "focus": "the result: before and after"},
]

SLIDE_TYPE_IDS = [item["id"] for item in SLIDE_TYPES]

MIN_PACKAGE_SLIDES = 3
MAX_PACKAGE_SLIDES = 8


def slide_focus(type_id: str) -> str:
    for item in SLIDE_TYPES:
        if item["id"] == type_id:
            return item["focus"]
    return SLIDE_TYPES[0]["focus"]


def package_slides(settings: dict, default: int) -> int:
    """Сколько слайдов в пакете.

    Старый клиент не присылает поле вовсе — тогда остаётся прежняя
    пятёрка, и уже оформленные заказы не меняют цену задним числом.
    """
    try:
        value = int(settings.get("packageSlides") or default)
    except (TypeError, ValueError):
        return default
    return max(MIN_PACKAGE_SLIDES, min(MAX_PACKAGE_SLIDES, value))


def slide_types_for(settings: dict) -> list:
    """Темы слайдов, отфильтрованные от чужих значений."""
    raw = settings.get("slideTypes") or []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw if str(item) in SLIDE_TYPE_IDS]
