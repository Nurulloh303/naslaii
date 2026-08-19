"""Сборка промптов для генерации карточек.

Порт из server/index.mjs. Каждое правило здесь появилось после конкретной
поломки — комментарии объясняют, после какой именно. Не убирайте их,
не проверив на живой генерации.
"""

from __future__ import annotations

import re

from .infographic_rules import first_page_text_rules, rank_callouts, to_headline

INFOGRAPHIC_LANGUAGES = ("uz", "ru", "tg")
LANGUAGE_HINTS = {
    "uz": "Uzbek Latin",
    "ru": "Russian Cyrillic",
    "tg": "Tajik Cyrillic",
}

MARKETPLACE_NAMES = {"uzum": "Uzum", "ozon": "Ozon", "wb": "Wildberries"}

# Запасные заголовки, когда тип товара не распознан.
PRODUCT_TEXT_BY_LANGUAGE = {
    "backpack": {"uz": ("RYUKZAK", "maktab uchun"), "ru": ("РЮКЗАК", "для школы"), "tg": ("БОРХАЛТА", "барои мактаб")},
    "bag": {"uz": ("SUMKA", "har kuni uchun"), "ru": ("СУМКА", "на каждый день"), "tg": ("СУМКА", "барои ҳар рӯз")},
    "dress": {"uz": ("KO‘YLAK", "ayollar uchun"), "ru": ("ПЛАТЬЕ", "женское"), "tg": ("КУРТА", "занона")},
    "shoes": {"uz": ("POYABZAL", "kundalik"), "ru": ("ОБУВЬ", "повседневная"), "tg": ("ПОЙАФЗОЛ", "ҳаррӯза")},
    # У generic НЕТ готового подзаголовка. Одна и та же фраза на любой товар —
    # ровно то, из-за чего автомобильная лампа получала «на каждый день»,
    # как витамины. Не распознали тип — подзаголовок пишет модель по
    # названию и категории, а дежурные фразы мы запрещаем.
    "generic": {"uz": ("MAHSULOT", ""), "ru": ("ТОВАР", ""), "tg": ("МАҲСУЛОТ", "")},
}

# Подзаголовок по категории: работает, когда тип товара не распознан, но
# категория известна. «Пищевая добавка» лучше, чем ничего.
CATEGORY_SUBTITLES = {
    "Salomatlik": {"uz": "ozuqaviy qo‘shimcha", "ru": "пищевая добавка", "tg": "иловаи ғизоӣ"},
    "Sport": {"uz": "mashqlar uchun", "ru": "для тренировок", "tg": "барои машқ"},
    "Bolalar": {"uz": "bolalar uchun", "ru": "для детей", "tg": "барои кӯдакон"},
    "Go‘zallik": {"uz": "parvarish uchun", "ru": "для ухода", "tg": "барои нигоҳубин"},
    "Uy": {"uz": "uy uchun", "ru": "для дома", "tg": "барои хона"},
}

def looks_like_filler_subtitle(value) -> bool:
    """Дежурный подзаголовок, подходящий любому товару.

    Пустой подзаголовок лучше бессмысленного: пользователь дважды приносил
    такое со скриншотов — сначала «marketplace uchun», потом «на каждый
    день» на автомобильной лампе.
    """
    text = str(value or "").strip()
    if not text:
        return True
    latin = re.compile(
        r"^(kundalik(\s+foydalanish)?(\s+uchun)?|har\s+kuni(\s+uchun)?|sifatli\s+mahsulot|marketplace\s+uchun"
        r"|yangi\s+mahsulot|eng\s+yaxshi)$",
        re.IGNORECASE,
    )
    cyrillic = re.compile(
        r"^(на\s+каждый\s+день|качественн[а-яё]*\s+товар|для\s+маркетплейса|нов[а-яё]*\s+товар"
        r"|лучш[а-яё]*\s+выбор|барои\s+ҳар\s+рӯз)$",
        re.IGNORECASE,
    )
    return bool(latin.match(text) or cyrillic.match(text))


# Фразы, подходящие любому товару, и потому не говорящие ничего.
BANNED_SUBTITLES = (
    "The subtitle must fit THIS product and no other. Never write filler that would fit anything: "
    '"на каждый день", "kundalik foydalanish uchun", "har kuni uchun", "барои ҳар рӯз", "качественный товар", '
    '"sifatli mahsulot", "для маркетплейса", "marketplace uchun". If nothing specific and truthful comes to '
    "mind, write no subtitle at all."
)

PRESENTATION_RULES = {
    "auto": (
        "Reproduce HOW IMAGE 1 presents the product. If IMAGE 1 shows the item worn or held by a "
        "human model, the final card MUST also show a photorealistic human model wearing the product "
        "from IMAGE 2, in the same pose, crop, scale and framing as IMAGE 1. If IMAGE 1 shows no "
        "person, do not add one."
    ),
    "model": (
        "The final card MUST show a photorealistic human model wearing or holding the product from "
        "IMAGE 2, in the same pose, crop, scale and framing as IMAGE 1."
    ),
    "product": "Show the product alone. Do not add any person.",
}

# Превью карточки покупатель видит шириной около 200 px. Мелкий текст там
# превращается в грязь.
LEGIBILITY_RULE = (
    "Do not reproduce tiny decorative text from the reference (fake navigation menus, small captions, "
    "paragraph blocks, watermarks). The card is first seen about 200 px wide: every word must stay "
    "readable at that size."
)

# Цифры с референса принадлежат ДРУГОМУ товару. Скопировать их — значит
# написать на карточке ложные характеристики, за это Uzum наказывает.
NO_BORROWED_FACTS_RULE = (
    "Never copy numbers, sizes, percentages, material composition, prices or discount badges from "
    "IMAGE 1 — they describe a different product and would be a false claim. Use only the facts given "
    "in this prompt or printed on the product packaging in the product photo."
)

# Модель принимала «сделай карточку для Uzum» за указание нарисовать
# логотип Uzum. Это чужой товарный знак, и покупатель уже внутри Uzum.
NO_MARKETPLACE_BRANDING = (
    "Never draw a marketplace logo, wordmark or icon (Uzum, Wildberries, Ozon) anywhere on the card — "
    "the buyer is already inside that marketplace and the logo is someone else's trademark. Do not add "
    "any brand logo that is not physically printed on the product in the photo."
)

# Надписи на упаковке — самый надёжный источник фактов, они уже есть на
# фото. Раньше модель их игнорировала: карточка мелатонина выходила без
# дозировки и количества, хотя всё было на этикетке.
PACKAGE_TEXT_RULE = (
    "READ the text printed on the product packaging or label in the product photo. Brand, product name, "
    'dosage, quantity, volume and marks like "gluten free" are real facts from the photo — you may keep them '
    "on the card and they are the most trustworthy content. Keep the product name and brand spelled as on the "
    "package. Never invent a number, dosage or claim that is not printed there."
)

BANNED_WORDS = (
    'Never write these words anywhere: BORDO, bordo, burgundy, maroon, mochka, ruxsat qiluvchi, '
    "original, photo, image."
)


def scene_wish_rule(settings: dict) -> str:
    """Пожелание продавца по сцене.

    Отдельное поле, а не общий «промпт»: то, что человек пишет в блоке
    преимуществ, уходит НА карточку текстом. Просьба про машину для
    авто-товара — это про фон и обстановку, а не про плашку.

    Ограничения жёсткие: сцена не меняет товар и не превращается в текст.
    Иначе «добавь скидку 50%» станет ложной надписью, за которую площадка
    наказывает продавца.
    """
    wish = str(settings.get("sceneWish") or "").strip()[:300]
    if not wish:
        return ""
    return (
        f'SELLER\'S SCENE WISH: "{wish}".\n'
        "Apply it to the SCENE only: background, environment, props, mood and staging around the product. "
        "For example, a wish about a car for a car accessory means the card is staged in or around a car.\n"
        "Hard limits on this wish: it must never change the product itself (shape, colour, material, branding); "
        "it must never be written on the card as text; it must never add prices, discounts, percentages, logos, "
        "certificates or claims. If the wish contradicts any rule above, ignore the wish and keep the rule."
    )


CYRILLIC_LANGUAGES = {"ru", "tg"}
CYRILLIC_RE = __import__("re").compile(r"[А-Яа-яЁёӢӯҳҷқғӮҲҶҚҒ]")
LETTERS_RE = __import__("re").compile(r"[A-Za-zА-Яа-яЁё]")


def headline_matches_language(headline: str, language: str) -> bool:
    """Совпадает ли письменность заголовка с языком карточки.

    БАГ, который это лечит: продавец выбирает русский, а заголовок выходит
    латиницей — "RYUKZAK" вместо "РЮКЗАК". Название приходит из брифа на
    узбекском, а в промпте стояло «используй дословно».
    """
    value = str(headline or "")
    if not LETTERS_RE.search(value):
        return True
    return (language in CYRILLIC_LANGUAGES) == bool(CYRILLIC_RE.search(value))


def subtitle_rule(brief: dict, settings: dict) -> str:
    """Подзаголовок: известный тип — дословно, неизвестный — по товару.

    Иначе модель ставит одну и ту же дежурную фразу на всё подряд.
    """
    text = visible_text_plan(brief, settings)
    hint = language_hint(settings)
    if text["subtitle"]:
        return f"- Subtitle: {text_for_card(settings, text['subtitle'])} — one short line under the headline, in {hint}.\n- {BANNED_SUBTITLES}"
    return (
        f"- Write the subtitle yourself: one short qualifier in {hint} taken from THIS product's name, type or "
        f'category — what kind it is, who it is for, what it is made of, or where it is used ("для автомобиля", '
        f'"светодиодная", "школьный", "пищевая добавка"). Two or three words maximum.\n- {BANNED_SUBTITLES}'
    )


def text_for_card(settings: dict, value) -> str:
    """Строка из брифа с учётом языка карточки.

    Бриф пишется на языке, который был выбран во время анализа. Если потом
    язык карточки поменяли, указание «печатай дословно» оказывается
    сильнее общего правила о переводе — и узбекская строка уезжает на
    русскую карточку как есть. Это уже случалось на живой генерации.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    language = normalize_language(settings.get("language"))
    hint = language_hint(settings)
    if headline_matches_language(text, language):
        return f'"{text}"'
    return f'the {hint} translation of "{text}" (translate the meaning, never print the original spelling)'


def headline_rule(brief: dict, settings: dict, prefix: str = "Main headline") -> str:
    text = visible_text_plan(brief, settings)
    language = normalize_language(settings.get("language"))
    hint = language_hint(settings)
    if headline_matches_language(text["headline"], language):
        return f'{prefix} exactly: "{text["headline"]}" — one product-type noun, large.'
    return (
        f'{prefix}: the product name written in {hint}. The word "{text["headline"]}" is given in '
        f"another script — write it in {hint} and print ONLY that version, never the original spelling. "
        f"If it is a brand or an international product name (Melatonin, Omega, Nike), transliterate it into "
        f"the {hint} alphabet instead of inventing a translation. One or two words, large."
    )


def normalize_language(language: str | None) -> str:
    return language if language in INFOGRAPHIC_LANGUAGES else "uz"


def language_hint(settings: dict) -> str:
    return LANGUAGE_HINTS[normalize_language(settings.get("language"))]


def marketplace_name(settings: dict) -> str:
    return MARKETPLACE_NAMES.get(settings.get("marketplace", "uzum"), "Wildberries")


def presentation_rule(settings: dict) -> str:
    mode = (settings.get("copyStyle") or {}).get("presentation")
    return PRESENTATION_RULES.get(mode, PRESENTATION_RULES["auto"])


# Здесь была тонкая, но дорогая ошибка. Мы просили «Russian Cyrillic», и
# модель понимала это как АЛФАВИТ: узбекские слова записывались русскими
# буквами. На реальной карточке вышло «УЙҚУНИ ҚЎЛЛАБ-ҚУВВАТЛАЙДИ» и
# «90 ТАБЛЕТКА МАВЖУД» — покупатель-россиянин такого не прочитает.
LANGUAGE_WORD_RULES = {
    "ru": (
        "The words themselves must be RUSSIAN, not Uzbek written in Cyrillic letters. Cyrillic alphabet alone "
        'is NOT enough. Wrong: "УЙҚУНИ ҚЎЛЛАБ-ҚУВВАТЛАЙДИ", "90 ТАБЛЕТКА МАВЖУД", "ГЛЮТЕН ФРИ". '
        'Right: "ПОДДЕРЖИВАЕТ СОН", "90 ТАБЛЕТОК", "БЕЗ ГЛЮТЕНА". Never use the letters ў, қ, ғ, ҳ, ҷ — they do '
        'not exist in Russian. Numbers and nouns must agree grammatically: "90 ТАБЛЕТОК", not "90 ТАБЛЕТКА".'
    ),
    "tg": (
        "The words themselves must be TAJIK, not Uzbek written in Cyrillic letters. Cyrillic alphabet alone is "
        "NOT enough: translate the meaning."
    ),
    "uz": "Write real Uzbek Latin words. Do not transliterate Russian words into Latin letters.",
}

# Модель перерисовала этикетку флакона на русский. Покупатель получит
# бутылку с английской этикеткой — это уже не тот товар, что на картинке.
KEEP_PACKAGE_LABEL_RULE = (
    "NEVER repaint, translate or re-typeset the text printed on the product itself. The label, brand, dosage "
    "and wording on the package must stay EXACTLY as they appear in the product photo, in the original "
    "language and typography. Your own text goes only on the card background, never onto the product."
)


def language_word_rule(settings: dict) -> str:
    return LANGUAGE_WORD_RULES[normalize_language(settings.get("language"))]


def translate_note(settings: dict) -> str:
    """AI-бриф всегда пишется на узбекском, а карточка может быть русской.

    Раньше узбекские плашки уходили в промпт дословно рядом с указанием
    «весь текст на русском» — два противоречащих требования, и на карточке
    получался смешанный язык.
    """
    if normalize_language(settings.get("language")) == "uz":
        return language_word_rule(settings)
    hint = language_hint(settings)
    return (
        f"TRANSLATION: the product name and the verified facts given below are written in Uzbek. TRANSLATE "
        f"their MEANING into {hint} — do not transliterate, do not respell Uzbek words with Cyrillic letters. "
        f"Print only the translation, never the Uzbek original. If a line is already correct {hint}, keep it. "
        f"Keep every number, size and unit exactly as given.\n{language_word_rule(settings)}"
    )


def visible_text_plan(brief: dict, settings: dict) -> dict:
    """Что именно будет написано на карточке."""
    language = normalize_language(settings.get("language"))
    fallback = PRODUCT_TEXT_BY_LANGUAGE["generic"][language]

    title = str(brief.get("title") or "").strip()
    headline = to_headline(title, fallback[0], language)

    custom = str(settings.get("cardPrompt") or "").strip()
    custom_lines = [line.strip() for line in custom.replace("\n", ";").split(";") if line.strip()]
    brief_lines = [line for line in (brief.get("benefits") or []) if line]
    callouts = rank_callouts(custom_lines or brief_lines)

    # Порядок: подзаголовок из брифа (его написал AI, который видел фото,
    # или сам продавец) -> точный тип товара -> категория -> ничего.
    category_subtitle = CATEGORY_SUBTITLES.get(brief.get("category"), {}).get(language, "")
    brief_subtitle = "" if looks_like_filler_subtitle(brief.get("subtitle")) else str(brief.get("subtitle")).strip()
    return {
        "language": language,
        "headline": headline,
        "subtitle": brief_subtitle or fallback[1] or category_subtitle,
        "callouts": "; ".join(callouts),
        "custom": custom,
    }


def style_copy_prompt(brief: dict, settings: dict, variant: int) -> str:
    """Режим «копия стиля»: IMAGE 1 — образец, IMAGE 2 — товар."""
    platform = marketplace_name(settings)
    text = visible_text_plan(brief, settings)
    hint = language_hint(settings)
    strictness = (
        "Follow the reference style closely, while allowing small composition adjustments for the new product."
        if settings.get("referenceMode") == "inspire"
        # «Копировать» раньше означало «скопируй систему», и модель делала
        # свою композицию «в похожем духе». Продавцу нужен тот же макет со
        # своим товаром и своими словами — поэтому копия описана поэлементно.
        else (
            "STRUCTURAL COPY — this is the main requirement. Reproduce IMAGE 1 as a template, not as inspiration. "
            "The result must look like the SAME design file where only the product and the words were swapped:\n"
            "- Keep the SAME number of text blocks and badges, in the SAME positions, with the same alignment, "
            "order and relative size as in IMAGE 1.\n"
            "- Keep the SAME background, gradients, decorative shapes, lines, icons, panels and their colours.\n"
            "- Keep the SAME typography: weights, letter case, line breaks and the size relation between "
            "headline, subtitle and badges.\n"
            "- Keep the product in the SAME place, at the same scale, angle and crop as the product in IMAGE 1.\n"
            "- Change ONLY two things: the product itself (take it from IMAGE 2) and the words inside the text blocks.\n"
            "- If your text is longer than the reference text, make the font smaller inside the same block. Never "
            "move, resize or delete a block, and never add a new one.\n"
            '- Do not invent a new layout, do not re-arrange, do not "improve" the composition.\n'
            "\nTYPOGRAPHY COPY — the words must be SET the same way, not just placed in the same spot:\n"
            "- The headline keeps the SAME relative size as in IMAGE 1. If it fills a third of the card height "
            "there, it fills a third here. Do not shrink it to a polite caption.\n"
            "- The headline keeps the SAME number of lines and the SAME way of breaking words. If the reference "
            "splits the word across lines, split it too.\n"
            "- If the headline in IMAGE 1 runs BEHIND the product or is partly covered by it, do exactly the same: "
            "the product overlaps the letters. This depth is the main reason the reference looks designed.\n"
            "- Keep the SAME weight contrast: heavy display face for the headline, thin small caps for secondary "
            "lines. Do not set everything in one weight.\n"
            "- Keep the SAME letter case per block, the SAME bold accents inside captions, and the SAME small "
            'marks ("+", "x30", "%", leader lines from a badge to a point on the product).\n'
            "- If your word is longer or shorter than the reference word, change the font size so the block keeps "
            "the same visual mass and the same footprint."
        )
    )
    callouts_line = (
        f"Add only these verified short callout badges, written in {hint}: {text['callouts']}. "
        f"If a line is in another language, translate its meaning — never print it in the original language."
        if text["callouts"]
        else "Do not add benefit callout badges. No unverified advantages."
    )

    return f"""Create one vertical 1080x1440 {platform} marketplace infographic card.

Images are in this exact order:
IMAGE 1 = design reference / template.
IMAGE 2 = product photo.

IMAGE 1 is the TEMPLATE: layout, background, colours, typography and staging come from it unchanged. IMAGE 2 is the SOURCE OF THE PRODUCT ONLY: the product shown in IMAGE 1 must be replaced by the product from IMAGE 2, standing in the same spot.
{strictness}
{presentation_rule(settings)}
Never keep mannequins, dress forms, hangers, clothing racks, holding hands or the original room and background from IMAGE 2 — those are shooting props, not the product itself.

TEXT LANGUAGE — STRICT: every visible word on the card must be written in {hint}. IMAGE 1 may contain words in a different language: never copy, keep or transliterate any word from IMAGE 1.
{translate_note(settings)}
{LEGIBILITY_RULE}
{NO_BORROWED_FACTS_RULE}
{NO_MARKETPLACE_BRANDING}
{PACKAGE_TEXT_RULE}
{KEEP_PACKAGE_LABEL_RULE}

Text plan:
- {headline_rule(brief, settings)}
{subtitle_rule(brief, settings)}
- {callouts_line}
- Write everything in {hint}.
{f"- User notes: {text['custom']}" if text["custom"] else ""}

{scene_wish_rule(settings)}
{BANNED_WORDS}
Do not put color words in the headline. Keep all text large, clean and readable.
Preserve the real product shape, color, proportions, material and visible details from IMAGE 2.
Premium commercial quality. Variant {variant}."""


def generation_prompt(brief: dict, settings: dict, variant: int) -> str:
    """Обычная карточка или фото — без образца стиля."""
    platform = marketplace_name(settings)
    text = visible_text_plan(brief, settings)
    hint = language_hint(settings)
    is_photo = settings.get("contentType") == "photo"
    category = brief.get("category") or "Marketplace kategoriyasi"
    benefits = "; ".join([b for b in (brief.get("benefits") or []) if b][:4])
    facts = text["callouts"] or benefits

    mode = "clean commercial product photo" if is_photo else "marketplace infographic product card"
    style = (
        ("natural home lifestyle scene" if settings.get("photoStyle") == "home" else "premium commercial studio scene")
        if is_photo
        else ("creative but clean marketplace layout" if settings.get("style") == "creative" else "commercial high-conversion marketplace layout")
    )
    user_wish = settings.get("photoPrompt") if is_photo else settings.get("cardPrompt")

    text_block = ""
    if not is_photo:
        text_block = f"""{first_page_text_rules(hint)}
{translate_note(settings)}
- {headline_rule(brief, settings)}
{subtitle_rule(brief, settings)}
"""

    facts_line = (
        f"Verified facts you may put in badges (pick the 3 most useful, best ones first): {facts}\n"
        f"Write these badges in {hint}. If a line is in another language, translate its meaning — never print it "
        f"in the original language and never respell it with another alphabet."
        if facts
        else f"No verified benefits were supplied. Do not invent callouts or advantages; use only the headline and subtitle in {hint}."
    )

    return f"""Edit the first input image into a finished {mode} for {platform}.
Output must be a vertical 1080x1440 marketplace-ready image.
Preserve the product identity, real shape, color, proportions, material appearance, and visible brand details from the uploaded product photo.
Product: {text['headline']}
Category: {category}

{text_block}
{facts_line}
Visual style: {style}. Pick the accent colour from the product photo itself (a colour already present in the product, or a clean contrast to it). Do not use a fixed neon cyan.
{NO_MARKETPLACE_BRANDING}
{PACKAGE_TEXT_RULE}
{KEEP_PACKAGE_LABEL_RULE}
{f"User wishes: {user_wish}" if user_wish else ""}
{scene_wish_rule(settings)}
Variant {variant}: make composition meaningfully different while keeping the same product and facts.
Do not invent discounts, prices, medical claims, composition/material claims, certification badges, or guarantees not visible in the product photo.
{BANNED_WORDS}"""


FASHION_POSE_HINTS = {
    "front": "front facing standing pose",
    "side": "side profile walking pose",
    "dynamic": "dynamic fashion action pose",
    "sitting": "relaxed sitting pose",
}
FASHION_STYLE_HINTS = {
    "studio": "high-end studio lighting, soft shadows",
    "street": "urban street photography, natural daylight",
    "lookbook": "clean catalog lookbook aesthetic",
    "cinematic": "cinematic dramatic lighting, movie atmosphere",
}
FASHION_ENV_HINTS = {
    "minimal": "plain minimalist background",
    "urban": "modern city street background",
    "interior": "luxury modern apartment interior",
    "nature": "outdoor natural park background",
}


def fashion_prompt(settings: dict, variant: int) -> str:
    fashion = settings.get("fashion") or {}
    gender = fashion.get("gender", "female")
    if gender == "none":
        model_line = "Flatlay product photography, no model, garment arranged neatly."
    else:
        who = "male" if gender == "male" else "female"
        pose = FASHION_POSE_HINTS.get(fashion.get("pose"), FASHION_POSE_HINTS["front"])
        model_line = f"Photorealistic {who} fashion model wearing the uploaded garment, {pose}."

    notes = fashion.get("customPrompt")
    return f"""Create a vertical 1080x1440 fashion/lookbook marketplace card from the uploaded clothing or footwear photo.
{model_line}
Style: {FASHION_STYLE_HINTS.get(fashion.get('style'), FASHION_STYLE_HINTS['studio'])}.
Environment: {FASHION_ENV_HINTS.get(fashion.get('env'), FASHION_ENV_HINTS['minimal'])}.
{f"User notes: {notes}" if notes else ""}
Keep the real garment shape, color, pattern, material and visible details unchanged — do not redesign the product.
Premium commercial fashion photography quality, sharp focus, natural proportions. Variant {variant}.
Do not add any text, logos or badges onto the image."""


MARKETPLACE_PACKAGE_SLIDES = [
    "Hero — main presentation of the product",
    "Benefits — 3 concrete features",
    "Usage context — where and how it is used",
    "Technical characteristics",
    "Trust — warranty or quality mark",
]


def marketplace_package_prompt(settings: dict, slide_index: int) -> str:
    package = settings.get("marketplacePackage") or {}
    platform = marketplace_name(settings)
    hint = language_hint(settings)
    focus = MARKETPLACE_PACKAGE_SLIDES[slide_index % len(MARKETPLACE_PACKAGE_SLIDES)]
    description = package.get("productDescription") or "(mahsulot tavsifi berilmagan, fotosuratdan aniq ko‘rinadigan narsalarga tayanib qoling)"
    style_notes = package.get("styleNotes")

    return f"""Create one slide ({slide_index + 1} of 5) of a vertical 1080x1440 {platform} marketplace infographic package, all 5 slides sharing one consistent visual style.
Slide focus: {focus}
Product description and benefits: {description}
{f"Style notes: {style_notes}" if style_notes else ""}
Keep the real product shape, color, proportions and visible details from the uploaded photo.
Use short, large, readable {hint} text only, at most 3 callouts on this slide.
The description above may be written in another language: translate everything into {hint} and never print the original wording.
{NO_MARKETPLACE_BRANDING}
{BANNED_WORDS}
Do not invent discounts, prices, medical claims, certifications or guarantees not visible in the photo.
Premium commercial quality, consistent accent color and typography across the whole 5-slide set."""


def compact_prompt(brief: dict, settings: dict, variant: int, has_reference: bool) -> str:
    """Короткий вариант на случай, когда полный промпт отклонён API."""
    platform = marketplace_name(settings)
    text = visible_text_plan(brief, settings)
    hint = language_hint(settings)
    facts = text["callouts"]

    if has_reference:
        return f"""Vertical 1080x1440 {platform} marketplace card.
IMAGE 1 is style reference. IMAGE 2 is product. Use product from IMAGE 2 only.
Match IMAGE 1 mood, composition, colors and large readable typography.
{presentation_rule(settings)}
No mannequins, hangers or original background from IMAGE 2.
{headline_rule(brief, settings, "Headline")}
Write every word in {hint}; never copy words, numbers or percentages from IMAGE 1.
{translate_note(settings)}
{NO_MARKETPLACE_BRANDING}
{f"If adding callouts, use only: {facts}." if facts else "Do not add callout badges or advantages."}
Variant {variant}."""

    return f"""Create a vertical 1080x1440 {platform} marketplace product card from the uploaded product photo.
Keep the exact product identity, shape, color and visible details.
Use {hint} text only.
{translate_note(settings)}
{headline_rule(brief, settings, "Headline")} It is the product category noun.
{subtitle_rule(brief, settings)}
Exactly 3 short badges, at least one containing a concrete number.
{f"Use only these verified callouts if needed: {facts}." if facts else "Do not invent product benefits or callouts."}
Clean commercial design, readable large text, accent colour taken from the product itself.
{NO_MARKETPLACE_BRANDING}
Variant {variant}."""


def safe_minimal_prompt(brief: dict, settings: dict, variant: int) -> str:
    """Последняя попытка: минимум требований, лишь бы получить картинку."""
    platform = marketplace_name(settings)
    text = visible_text_plan(brief, settings)
    callouts = "; ".join([b for b in (brief.get("benefits") or []) if b][:3])
    return f"""Create a vertical 1080x1440 {platform} marketplace card from the uploaded product image.
Use the uploaded product as the main subject.
Large readable text only.
{translate_note(settings)}
{headline_rule(brief, settings, "Headline")}
{f"Callouts: {callouts}" if callouts else ""}
Clean commercial layout. Accent colour taken from the product itself.
{NO_MARKETPLACE_BRANDING}
Variant {variant}."""


def build_prompt(brief: dict, settings: dict, variant: int, has_reference: bool) -> str:
    """Главная точка входа: какой промпт нужен для этой задачи."""
    content_type = settings.get("contentType", "card")
    if has_reference and content_type in ("card", "copyStyle"):
        prompt = style_copy_prompt(brief, settings, variant)
    elif content_type == "fashion":
        prompt = fashion_prompt(settings, variant)
    elif content_type == "marketplacePackage":
        prompt = marketplace_package_prompt(settings, variant - 1)
    else:
        prompt = generation_prompt(brief, settings, variant)

    pages = int(settings.get("pages") or 1)
    if pages <= 1 or content_type == "marketplacePackage":
        return prompt

    variants = int(settings.get("variants") or 1)
    page_index = (variant - 1) // max(1, variants)
    focus = (settings.get("pageFocus") or [""] * 5)[page_index] if page_index < 5 else ""

    # С образцом стиля НЕЛЬЗЯ просить менять композицию между страницами:
    # это прямо противоречит указанию «повтори образец» и стиль разваливается.
    if has_reference:
        extra = (
            f"\n\nThis is page {page_index + 1} of {pages}. Keep EXACTLY the same reference style, layout "
            f"system, colors and typography on every page."
        )
        if focus:
            extra += f" Change only the emphasised benefit: {focus}"
    else:
        extra = f"\n\nThis is page {page_index + 1} of {pages} in a set that must share ONE consistent visual style."
        if focus:
            extra += f" This page must emphasise: {focus}"
        extra += " Vary the composition between pages."

    return prompt + extra
