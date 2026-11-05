"""IKPU (MXIK) kodlarini mahalliy bazadan topish.

tasnif.soliq.uz chet eldagi serverlarga javob bermaydi, shuning uchun
klassifikator Excel eksporti bir marta yuklanadi (``uzum_import_ikpu``) va
qidiruv o'z bazamizda ishlaydi. Sayt kodni hech qachon o'zi qo'ymaydi —
sotuvchiga variantlar beriladi, tanlov uniki.
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests
from django.db.models import Q

from .models import IkpuCode
from .search_text import phrase_alternatives

logger = logging.getLogger(__name__)

_WORD = re.compile(r"\w+")
# Juda umumiy so'zlar — qidiruvni buzadi.
STOP_WORDS = {
    "и", "для", "с", "из", "на", "в", "по", "от", "без", "bilan", "uchun", "va", "the", "for", "with",
    "набор", "товар", "mahsulot", "tovar", "новый", "yangi", "original", "оригинал",
}
CANDIDATE_LIMIT = 3000


def normalize(text: str) -> str:
    text = str(text or "").lower().replace("ё", "е").replace("ʻ", "'").replace("`", "'").replace("’", "'")
    return " ".join(_WORD.findall(text.replace("'", "")))


_CYR = re.compile(r"[а-я]")
_UZ_SUFFIXES = ("larning", "larini", "lari", "lar", "ning", "ni", "si", "i")


def _stem(word: str) -> str:
    """Ruscha qo'shimchalar: "мячи"/"мячей" → "мяч"; o'zbekcha: "to'plar" → "top"."""
    if _CYR.search(word):
        return word[:-2] if len(word) >= 7 else word[:-1] if len(word) >= 5 else word
    for suffix in _UZ_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def query_groups(text: str) -> list[tuple[set[str], set[str]]]:
    """Har bir so'z uchun (to'liq muqobillar, o'zaklar) juftligi."""
    groups = []
    for alts in phrase_alternatives(normalize(text)):
        full = {normalize(a) for a in alts}
        full = {a for a in full if a and a not in STOP_WORDS and len(a) >= 2 and not a.isdigit()}
        if full:
            groups.append((full, {_stem(a) for a in full}))
    return groups


def _row(item: IkpuCode, score: float = 0) -> dict:
    return {
        "code": item.code,
        "name": item.name_ru or item.name_uz,
        "nameUz": item.name_uz,
        "path": item.path_ru or item.path_uz,
        "score": round(score, 3),
    }


def _score(item: IkpuCode, groups) -> float:
    name = normalize(f"{item.name_ru} {item.name_uz}")
    name_words = name.split()
    full_text = item.search
    score = 0.0
    for full, stems in groups:
        if any(w.startswith(a) for a in full for w in name_words):
            score += 4
        elif any(w.startswith(a) for a in stems for w in name_words):
            score += 3
        elif any(a in name for a in stems):
            score += 2
        elif any(a in full_text for a in stems):
            score += 1
    # Qisqa, aniq nomlar uzun ro'yxat nomlaridan yaxshiroq.
    return score - min(len(name_words), 30) * 0.02


def search(text: str, limit: int = 10, require_all: bool = True) -> list[dict]:
    groups = query_groups(text)
    if not groups:
        return []
    query = Q()
    for _, stems in groups:
        part = Q()
        for alt in stems:
            part |= Q(search__contains=alt)
        query = (query & part) if require_all else (query | part)
    candidates = list(IkpuCode.objects.filter(query)[:CANDIDATE_LIMIT])
    if not candidates and require_all and len(groups) > 1:
        return search(text, limit, require_all=False)
    ranked = sorted(((_score(item, groups), item) for item in candidates), key=lambda pair: (-pair[0], pair[1].code))
    return [_row(item, score) for score, item in ranked[:limit]]


def by_barcode(barcode: str) -> list[dict]:
    barcode = re.sub(r"\D", "", str(barcode or ""))
    if len(barcode) < 8:
        return []
    return [_row(item, 99) for item in IkpuCode.objects.filter(barcode=barcode)[:5]]


def by_code(code: str) -> dict | None:
    item = IkpuCode.objects.filter(code=re.sub(r"\D", "", str(code or ""))).first()
    return _row(item) if item else None


def is_loaded() -> bool:
    return IkpuCode.objects.exists()


# ------------------------------------------------------------------ AI yordamchi

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


def _openai(prompt: str, max_tokens: int = 200) -> dict | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        response = requests.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.environ.get("OPENAI_BRIEF_MODEL") or "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": max_tokens,
            },
            timeout=12,
        )
        if not response.ok:
            logger.info("IKPU AI: %s %s", response.status_code, response.text[:200])
            return None
        return json.loads(response.json()["choices"][0]["message"]["content"])
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
        logger.info("IKPU AI javobi o'qilmadi: %s", error)
        return None


def ai_keywords(title: str, category: str) -> list[str]:
    data = _openai(
        "A seller in Uzbekistan lists this product. Give 3 short Russian search phrases (1-3 words each, "
        "generic product type nouns, no brands, no colors, no sizes) that would match its name in the "
        "Uzbek tax product classifier (MXIK / ИКПУ). Most specific first.\n"
        f"Product title: {title}\nMarketplace category: {category}\n"
        'Return ONLY JSON {"phrases":["...","...","..."]}',
        120,
    ) or {}
    phrases = data.get("phrases") if isinstance(data.get("phrases"), list) else []
    return [str(p).strip()[:60] for p in phrases if str(p or "").strip()][:3]


def ai_rerank(title: str, category: str, candidates: list[dict], limit: int) -> list[dict]:
    if len(candidates) <= 1:
        return candidates[:limit]
    listing = "\n".join(f"{i}. {c['name']} — {c['path']}" for i, c in enumerate(candidates[:40]))
    data = _openai(
        "Pick the MXIK (ИКПУ) classifier entries that best describe this product, best first. "
        "Prefer the exact product type; ignore entries for different products.\n"
        f"Product title: {title}\nMarketplace category: {category}\n\nEntries:\n{listing}\n\n"
        f'Return ONLY JSON {{"best":[index, ...]}} with up to {limit} indexes.',
        80,
    ) or {}
    picked = []
    for index in data.get("best") or []:
        if isinstance(index, int) and 0 <= index < min(40, len(candidates)) and candidates[index] not in picked:
            picked.append(candidates[index])
    if not picked:
        return candidates[:limit]
    rest = [c for c in candidates if c not in picked]
    return (picked + rest)[:limit]


def suggest(title: str, category: str, barcode: str = "", limit: int = 5, use_ai: bool = True) -> list[dict]:
    """Mahsulot nomi + Uzum kategoriyasi bo'yicha eng mos IKPU variantlari."""
    found: list[dict] = []
    seen: set[str] = set()

    def add(items):
        for item in items:
            if item["code"] not in seen:
                seen.add(item["code"])
                found.append(item)

    add(by_barcode(barcode))
    leaf = (category or "").split(">")[-1].strip()
    phrases = ai_keywords(title, category) if use_ai else []
    for phrase in phrases:
        add(search(phrase, 15))
    add(search(f"{leaf} {title}", 15))
    add(search(leaf, 15))
    add(search(title, 15))
    if use_ai and len(found) > limit:
        exact = [c for c in found if c.get("score") == 99]
        rest = [c for c in found if c.get("score") != 99]
        return (exact + ai_rerank(title, category, rest, limit))[:limit]
    return found[:limit]
