"""Чек-лист требований Uzum Market.

Как это вести — важно:

1. source="uzum" — ОФИЦИАЛЬНОЕ требование из руководства продавца.
   Нарушение может привести к статусу «Замечания» или блокировке.
   Источник: https://seller.uzum.uz/manual/

2. source="practice" — это НЕ правило Uzum, а рекомендация по практике
   маркетплейсов. В отчёте они помечаются отдельно: говорить «вы нарушили
   правило» там, где правила нет, — вводить продавца в заблуждение.

Страницы руководства рисуются JavaScript и не выкачиваются автоматически
(проверено: возвращается пустая страница). Поэтому список ведётся РУКАМИ.
Uzum обновил требования — обновите файл и дату ниже.

Порт server/uzumRules.mjs.
"""

UZUM_RULES_REVIEWED_AT = "2026-08-12"
UZUM_MANUAL_URL = "https://seller.uzum.uz/manual/5.product-creation/"

UZUM_RULES = [
    {
        "id": "photo_required",
        "source": "uzum",
        "title": "Har bir SKU uchun kamida bitta rasm",
        "detail": "Mahsulotning old tomoni ko‘ringan rasm majburiy va u nom hamda tavsifga mos bo‘lishi kerak.",
    },
    {
        "id": "no_foreign_marks",
        "source": "uzum",
        "title": "Boshqa do‘kon reklamasi, kontakt va havolalar taqiqlanadi",
        "detail": "Rasmda telefon raqami, ijtimoiy tarmoq havolasi, boshqa do‘kon logotipi yoki reklamasi bo‘lmasligi kerak — kartochka bloklanadi.",
    },
    {
        "id": "product_visible",
        "source": "uzum",
        "title": "Mahsulot aniq ko‘rinishi kerak",
        "detail": "Rasmda ortiqcha elementlar bo‘lmasligi, mahsulotning o‘zi aniq va to‘liq ko‘rinishi shart.",
    },
    {
        "id": "infographic_readable",
        "source": "uzum",
        "title": "Infografika o‘qiladigan bo‘lsin",
        "detail": "Mayda, past kontrastli yoki bir-birining ustiga tushgan matn talabga javob bermaydi.",
    },
    {
        "id": "repack_box_photo",
        "source": "uzum",
        "title": "Nostandart qadoqda — quti rasmi majburiy",
        "detail": "Qurilma boshqa qadoqqa solingan bo‘lsa, kartochkada qutining rasmi bo‘lishi kerak.",
    },
    # ── Рекомендации по практике (не правила Uzum) ────────────────────
    {
        "id": "thumbnail_legibility",
        "source": "practice",
        "title": "Matn kichik prevyuda o‘qilmayapti",
        "detail": "Xaridor avval kartochkani taxminan 200 px kenglikda ko‘radi. Asosiy yozuv rasm balandligining kamida 6–8% bo‘lsin.",
    },
    {
        "id": "first_frame_overload",
        "source": "practice",
        "title": "Birinchi kadr ortiqcha yuklangan",
        "detail": "Birinchi rasmda 3 tadan ortiq plashka diqqatni bo‘lib yuboradi.",
    },
    {
        "id": "contrast",
        "source": "practice",
        "title": "Matn foni bilan yetarli kontrastda emas",
        "detail": "Och matn och fonda yo‘qoladi. Matn ostiga to‘q plashka yoki soya qo‘ying.",
    },
    {
        "id": "product_share",
        "source": "practice",
        "title": "Infografika mahsulotni bosib qo‘ygan",
        "detail": "Birinchi kadrda mahsulot maydonning kamida yarmini egallashi kerak.",
    },
    {
        "id": "benefit_specific",
        "source": "practice",
        "title": "Afzalliklar umumiy so‘zlar bilan yozilgan",
        "detail": "«Sifatli», «qulay» hech narsa demaydi. «3 ta bo‘lim», «suv o‘tkazmaydi», «1.2 kg» ishlaydi.",
    },
    {
        "id": "language_match",
        "source": "practice",
        "title": "Matn tili auditoriyaga mos emas",
        "detail": "O‘zbek bozorida o‘zbekcha yoki ruscha matn kutiladi.",
    },
]

# Ссылки. Официальное требование -> руководство продавца. Отдельной
# «глубокой» ссылки на каждое правило НЕ существует и придумывать её нельзя:
# руководство — SPA, оно отвечает 200 на любой адрес, поэтому проверить
# несуществующий раздел невозможно. Нерабочая ссылка хуже отсутствующей.
for _rule in UZUM_RULES:
    _rule["url"] = UZUM_MANUAL_URL if _rule["source"] == "uzum" else None

UZUM_RULES_BY_ID = {rule["id"]: rule for rule in UZUM_RULES}


def find_rule(rule_id) -> dict | None:
    """Правило по id. Незнакомый id — не ошибка: модель могла придумать."""
    return UZUM_RULES_BY_ID.get(str(rule_id or "").strip())


UZUM_OFFICIAL_RULES = [rule for rule in UZUM_RULES if rule["source"] == "uzum"]

# Что мы проверить НЕ можем. Показываем это в отчёте открыто: падение
# продаж чаще объясняется не карточкой, и скрывать это — обманывать.
# Ключи, а не текст: отчёт показывается на языке сайта, перевод живёт
# во фронтенде. Раньше здесь были узбекские строки, и в русском
# интерфейсе они выходили по-узбекски.
NOT_CHECKED = ["price", "reviews", "search", "delivery", "season"]

AUDIT_QUESTIONS = [
    {"id": "price", "type": "text", "required": True},
    {"id": "competitorPrice", "type": "text", "required": True},
    {"id": "reviews", "type": "choice", "options": ["none", "few", "many"], "required": True},
    {"id": "impressions", "type": "choice", "options": ["noImpressions", "impressionsNoOrders", "unknown"], "required": True},
    {"id": "audience", "type": "text", "required": False},
    {"id": "fbo", "type": "choice", "options": ["fbo", "fbs", "unknown"], "required": True},
    {"id": "promo", "type": "choice", "options": ["inPromo", "notInPromo", "unknown"], "required": True},
    {"id": "boost", "type": "choice", "options": ["boostOn", "boostOff", "unknown"], "required": True},
]
