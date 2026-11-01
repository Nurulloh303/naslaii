"""Ko'p sahifali to'plam uchun har bir sahifaning O'Z matni.

Muammo: ilgari matn rejasi (plan.py) hamma sahifaga bir xil sarlavha,
pastki sarlavha va plashkalarni qo'yardi, model esa har sahifaga yana
o'sha 3 ta afzallikni yozardi — to'plam bitta matnning takrori bo'lib qolardi.

Bu modul har sahifaga vazifasiga mos, TAKRORLANMAYDIGAN matn yozadi:
  - AI kopirayter (OpenAI, JSON) — asosiy yo'l;
  - kalit yo'q yoki xato bo'lsa — afzalliklarni sahifalarga taqsimlaydigan
    deterministik zaxira (hech qachon yiqilmaydi).
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

from .infographic_rules import rank_callouts, to_headline
from .prompts import PAGE_ROLES, page_role, page_text

logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
REQUEST_TIMEOUT = max(5, int(os.environ.get("PAGE_COPY_TIMEOUT_MS", 25_000)) // 1000)

LANGUAGE_NAMES = {
    "uz": "Uzbek (Latin script, with oʻ gʻ apostrophes)",
    "uz_cyrl": "Uzbek (Cyrillic script)",
    "ru": "Russian",
    "en": "English",
    "tg": "Tajik",
}

ROLE_COPY_GUIDE = {
    "cover": "the product name as a 1-2 word headline, a short descriptive subtitle, and the 3 strongest benefits as badges",
    "lifestyle": "the product in real life: a headline about the main everyday benefit, a subtitle about when/where it helps, 1-2 short facts",
    "scenarios": "4 short captions for 4 different usage situations or users (2-4 words each) and a bold headline that ties them together",
    "features": "3-4 concrete features, each 2-5 words, plus a headline about quality/functionality",
    "details": "2-3 detail callouts about materials, build or a key part (2-5 words each), plus a headline about craftsmanship",
    "trust": "a headline about reliability/brand, a short guarantee-free trust statement, and 2 short facts",
}


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", str(text or "").lower()).strip()


def _entry(role: str, headline: str, subtitle: str, lines: list[str]) -> dict:
    import uuid

    return {
        "id": uuid.uuid4().hex,
        "type": role,
        "headline": str(headline or "").strip()[:60],
        "subtitle": str(subtitle or "").strip()[:90],
        "lines": [str(line).strip()[:60] for line in lines if str(line or "").strip()][:4],
    }


def fallback_page_copy(brief: dict, settings: dict, pages: int) -> list[dict]:
    """AI'siz: afzalliklarni sahifalarga TAQSIMLAYMIZ (har biri bir marta)."""
    language = settings.get("language") or "uz"
    title = str(brief.get("title") or "").strip()
    subtitle = str(brief.get("subtitle") or "").strip()
    benefits = rank_callouts(brief.get("benefits") or []) or [b for b in (brief.get("benefits") or []) if b]
    plan = [_entry("cover", to_headline(title, fallback=title.upper(), language=language), subtitle, benefits[:3])]
    rest = benefits[3:]
    for index in range(1, pages):
        role = page_role(pages, index)
        # Muqovadagi afzalliklar keyingi sahifalarda sarlavha bo'ladi — plashka emas.
        head = benefits[(index - 1) % len(benefits)] if benefits else ""
        lines = [rest.pop(0)] if rest else []
        plan.append(_entry(role, head, "", lines))
    return plan


def _prompt(brief: dict, settings: dict, pages: int) -> str:
    language = LANGUAGE_NAMES.get(str(settings.get("language") or "uz"), "Uzbek (Latin script)")
    roles = [page_role(pages, index) for index in range(pages)]
    focus = settings.get("pageFocus") or []
    page_lines = []
    for index, role in enumerate(roles):
        custom = str(focus[index]).strip() if index < len(focus) and focus[index] else ""
        guide = ROLE_COPY_GUIDE.get(role, "")
        page_lines.append(
            f"Page {index + 1} — role \"{role}\": {guide}."
            + (f" Seller's wish for this page: {custom}." if custom else "")
        )
    benefits = "; ".join(b for b in (brief.get("benefits") or []) if b) or "(none supplied)"
    return f"""You are a senior marketplace copywriter. Write the visible text for a {pages}-page product image set for Uzum / Wildberries / Ozon.

Product name: {brief.get("title") or ""}
Subtitle from the seller: {brief.get("subtitle") or ""}
Category: {brief.get("category") or ""}
Verified benefits from the seller: {benefits}

Pages:
{chr(10).join(page_lines)}

RULES:
- Language: {language}. Every word in that language and script. Natural, correct, grammatical.
- EVERY PAGE MUST HAVE DIFFERENT TEXT. Never repeat a headline, subtitle or badge from another page, not even reworded. Each page sells a different angle.
- Headline: 1-4 words, punchy. Only page 1's headline may be the product name; other pages must NOT use the product name as the headline.
- Subtitle: max 8 words, may be empty.
- Lines (badges/captions/feature labels): 2-5 words each; use the count the page role needs (cover 3, scenarios 4, others 2-3).
- Be specific to THIS product. You may describe what is obvious from the product type and the given benefits (usage situations, who it is for, what it helps with).
- Do NOT invent numbers, sizes, specifications, certifications, warranties, prices, discounts or brand claims that are not in the benefits.
- No emojis, no exclamation-mark spam, no marketplace names.

Return ONLY JSON: {{"pages":[{{"headline":"...","subtitle":"...","lines":["...","..."]}}, ...]}} with exactly {pages} items in page order."""


def ai_page_copy(brief: dict, settings: dict, pages: int) -> list[dict] | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.environ.get("OPENAI_COPY_MODEL") or os.environ.get("OPENAI_BRIEF_MODEL") or "gpt-4o-mini"
    try:
        response = requests.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": _prompt(brief, settings, pages)}],
                "response_format": {"type": "json_object"},
                "temperature": 0.7,
                "max_tokens": 900,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            logger.warning("Sahifa matni: OpenAI xato %s %s", response.status_code, response.text[:200])
            return None
        content = response.json()["choices"][0]["message"]["content"]
        items = json.loads(content).get("pages") or []
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning("Sahifa matni: OpenAI javobini o'qib bo'lmadi: %s", error)
        return None
    if len(items) < pages:
        return None
    plan = [
        _entry(page_role(pages, index), item.get("headline", ""), item.get("subtitle", ""), item.get("lines") or [])
        for index, item in enumerate(items[:pages])
        if isinstance(item, dict)
    ]
    if len(plan) < pages or any(not entry["headline"] for entry in plan):
        return None
    return dedupe_plan(plan)


def dedupe_plan(plan: list[dict]) -> list[dict]:
    """Model baribir takrorlasa — ikkinchi marta uchragan plashkani olib tashlaymiz."""
    seen: set[str] = set()
    for entry in plan:
        seen.add(_norm(entry["headline"]))
    for entry in plan:
        kept = []
        for line in entry["lines"]:
            key = _norm(line)
            if key and key not in seen:
                seen.add(key)
                kept.append(line)
        entry["lines"] = kept
    return plan


def plan_is_repetitive(plan: list) -> bool:
    """Eski rejalar: hamma sahifada bir xil sarlavha/plashkalar."""
    if not isinstance(plan, list) or len(plan) < 2:
        return False
    heads = {_norm((item or {}).get("headline", "")) for item in plan}
    lines = [tuple(_norm(x) for x in ((item or {}).get("lines") or [])) for item in plan]
    return len(heads) < len(plan) or len(set(lines)) < len(lines)


def build_page_copy(brief: dict, settings: dict, pages: int, use_ai: bool = True) -> list[dict]:
    plan = ai_page_copy(brief, settings, pages) if use_ai else None
    return plan or fallback_page_copy(brief, settings, pages)


__all__ = ["PAGE_ROLES", "build_page_copy", "page_text", "plan_is_repetitive", "fallback_page_copy"]
