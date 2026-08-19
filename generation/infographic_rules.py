"""Правила текста для первой страницы карточки.

Это не выдумка и не «как красиво». Здесь статистика: 104 реальные
инфографики с Wildberries, Ozon и Uzum разобраны вручную, из них
выведены цифры ниже. Меняете правило — сверьтесь с CORPUS_STATS.

Порт файла server/infographicRules.mjs из Node-версии.
"""

from __future__ import annotations

import re

# Результат разбора: 104 файла, август 2026.
CORPUS_STATS = {
    "sample": 104,
    "reviewed_at": "2026-08-12",
    "headline_is_category": 89,      # 86% — заголовок это ТИП товара, не слоган
    "headline_is_benefit": 6,        # 6% — только если показать нечего
    "headline_absent": 3,
    "headline_one_word": 76,         # 73%
    "headline_two_words_or_less": 91,
    "has_subtitle": 89,              # 86%
    "badges_median": 3,
    "badges_mean": 3.7,
    "has_number": 80,                # 77% карточек содержат хотя бы одну цифру
    "human_in_frame": 39,            # 38%
}

# Что выносят в плашки — по частоте. Порядок важен: модели говорим
# «сначала материал, потом размер», потому что в корпусе так.
BADGE_PRIORITY = [
    ("material", 0.50, "material or composition (leather, 100% cotton, stainless steel, EVA)"),
    ("size", 0.46, 'size or size range (35-41, 70x70 cm, 15.6", A4)'),
    ("set", 0.26, "quantity or what is included (2 pcs, 5 compartments, 6 pockets)"),
    ("capacity", 0.18, "volume or weight (20 l, 650 g, 1000 ml)"),
    ("color", 0.18, "colour options"),
    ("scenario", 0.11, "use case (for school, for home, for the office, everyday)"),
    ("water", 0.09, "water resistance, if genuinely true"),
    ("gift", 0.08, "bonus item included"),
    ("warranty", 0.08, "warranty or service life (365 days, 10 years)"),
]

HEADLINE_EXAMPLES = [
    ("СУМКА", "женская"),
    ("РЮКЗАК", "школьный"),
    ("КОВРИК", "для йоги и фитнеса"),
    ("ПЕРЧАТКИ", "для бокса"),
    ("ВЕЛОТРЕНАЖЁР", "для дома"),
]


def first_page_text_rules(language: str) -> str:
    """Блок правил для промпта. На английском — модель точнее следует."""
    priority = "\n".join(
        f"{index}. {hint}" for index, (_, _, hint) in enumerate(BADGE_PRIORITY[:6], start=1)
    )
    return f"""FIRST-PAGE TEXT RULES (derived from {CORPUS_STATS['sample']} real marketplace cards):
- HEADLINE = the product category noun, not a slogan. One word in 73% of real cards, two words maximum. Write it large and in {language}.
- SUBTITLE = a short qualifier under the headline: audience, type, material or use case ("женская", "школьный", "для дома"). One to three words. 86% of real cards have it.
- Do NOT put colour, brand adjectives or marketing slogans in the headline.
- HEADLINE SCALE AND DEPTH: the headline is a display element, not a caption. It may take a third of the card height, wrap onto two or three lines, and the product may overlap and partly cover its letters. That overlap is what makes a card look designed instead of assembled. Keep every letter that stays visible fully readable.
- Use at least three levels of type weight and size on the card: a heavy headline, medium numbers, thin small captions. One weight everywhere reads as a flat banner.
- BADGES: exactly 3 MAIN callouts. Never 4, never more. The median in real cards is 3, and the cards with 5-8 equal-weight badges are the unreadable ones. Each badge is at most 4 words.
- HOW TO SHOW A NUMBER (this is what separates a professional card from a flat one): a small coloured pill with the UNIT ("мАч", "Вт", "мг", "шт"), under it the number very large, and under the number a small caption saying what it is ("ёмкость аккумулятора", "мощность"). Three sizes of type, not one.
- You MAY add, in addition to the 3 main callouts: ONE round accent badge with a single strong number ("45 мин до 50%"), and ONE bottom row of up to 4 small icon+caption items for secondary features. They must be visibly SMALLER than the main callouts — this is a hierarchy, not more noise.
- NO MARKETPLACE BRANDING. Never draw the Uzum, Wildberries or Ozon logo, wordmark or icon on the card. The buyer is already inside that marketplace — the logo only wastes space and is someone else's trademark. Do not add any brand logo that is not physically printed on the product itself.
- ACCENT COLOUR comes from the product photo, not from a fixed palette: take a colour that already exists in the product or a clean contrast to it. Do not force a neon cyan or any pre-set brand colour onto every card.
- At least ONE badge must contain a concrete number — size, quantity, volume, warranty. 77% of real cards do this, and it is what makes a card look professional instead of empty.
- Choose badge content in this order of usefulness:
{priority}
- Never invent a fact that is not visible in the product photo and not given in this prompt. A wrong size or composition on a marketplace card is a violation, not a design choice.
- Vague praise without substance is forbidden as a badge: "качественный", "удобный", "стильный", "лучший выбор" on their own say nothing. Either attach a concrete reason or drop the badge.
- Write every word in {language}."""


_NUMBER_RE = re.compile(r"\d")
_TOPIC_WEIGHTS = [
    (re.compile(r"\b(kg|г|гр|л|мл|ml|litr|gramm)\b", re.I), 3),
    (re.compile(r"(sm|см|mm|мм|размер|o‘lcham|olcham|size|\d+\s*[x×]\s*\d+)", re.I), 4),
    (re.compile(r"(материал|charm|кожа|хлопок|paxta|len|лен|сталь|po‘lat|polat|silikon|силикон)", re.I), 5),
    (re.compile(r"(комплект|to‘plam|toplam|шт|dona|отделен|карман|cho‘ntak|chontak)", re.I), 4),
    (re.compile(r"(гарант|kafolat|срок службы)", re.I), 2),
    (re.compile(r"(подарок|sovg‘a|sovga|в комплекте)", re.I), 2),
]


def rank_callouts(lines: list[str]) -> list[str]:
    """Сортируем плашки по полезности: с цифрами и конкретикой — вперёд.

    На карточку идут только 3 штуки. Отдавать модели больше нельзя:
    она попытается «пристроить остальные».
    """
    seen: list[str] = []
    for raw in lines or []:
        line = str(raw or "").strip()
        if line and line not in seen:
            seen.append(line)

    def score(line: str) -> int:
        value = 6 if _NUMBER_RE.search(line) else 0
        for pattern, weight in _TOPIC_WEIGHTS:
            if pattern.search(line):
                value += weight
        if len(line.split()) <= 4:
            value += 2
        return value

    return sorted(seen, key=score, reverse=True)[:3]


# Дозировки и единицы. Название с упаковки часто выглядит как
# "Melatonin 3 mg", и правило «в узбекском главное слово последнее»
# выдавало заголовок "MG".
MEASURE_TOKEN = re.compile(
    r"^(\d+([.,]\d+)?|mg|mkg|g|gr|kg|ml|l|mm|sm|cm|m|dona|ta|sht|tab|tabletka|tablets?|kaps|kapsula|"
    r"caps|capsules?|pcs|шт|мг|мкг|г|кг|мл|л|мм|см|таб|табл|капс)$",
    re.IGNORECASE,
)


def to_headline(text: str, fallback: str = "", language: str = "uz") -> str:
    """Заголовок — одно слово (73% корпуса), максимум два.

    Три тонкости, все всплыли на реальных карточках:
      1. Апостроф убирать нельзя: «Ko‘ylak» превращается в «KO YLAK».
      2. В длинном названии главное слово стоит по-разному: в узбекском
         в конце («maktab uchun ryukzak» → RYUKZAK), в русском в начале
         («сумка для ноутбука» → СУМКА).
      3. Дозировка — не заголовок: «Melatonin 3 mg» давало «MG».
    """
    cleaned = re.sub(r"[«»\".,]", " ", str(text or "")).strip()
    words = [w for w in cleaned.split() if w]
    if not words:
        return fallback
    meaningful = [w for w in words if not MEASURE_TOKEN.match(w)]
    if not meaningful:
        return fallback
    # Одно-два значимых слова — берём оба: «Vitamin C» уже заголовок.
    if len(meaningful) <= 2:
        return " ".join(meaningful).upper()
    return (meaningful[-1] if language == "uz" else meaningful[0]).upper()
