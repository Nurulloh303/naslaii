"""Matn rejasi: kartada nima yozilishini generatsiyadan oldin ko'rsatish.

Modelga murojaat qilinmaydi — shuning uchun bepul va mgnovenno. Sarlavha va
plashkalar xuddi shu qoidalar bilan hisoblanadi (infographic_rules), faqat
bu yerda ular rasmga emas, tahrirlash mumkin bo'lgan formaga chiqadi.
Odam buni tekshiradi va to'g'rilaydi, keyin generatsiya aynan shu matnni
so'rovga qo'shib yuboradi.
"""

from __future__ import annotations

import uuid

from .infographic_rules import rank_callouts, to_headline
from .slide_types import SLIDE_TYPE_IDS, package_slides, slide_types_for


def _slide_entry(type_id: str, headline: str, subtitle: str, lines: list[str]) -> dict:
    return {"id": uuid.uuid4().hex, "type": type_id, "headline": headline, "subtitle": subtitle, "lines": lines}


def build_plan(brief: dict, settings: dict) -> list[dict]:
    language = settings.get("language") or "uz"
    title = str(brief.get("title") or "").strip()
    subtitle = str(brief.get("subtitle") or "").strip()
    headline = to_headline(title, fallback=title.upper(), language=language)
    lines = rank_callouts(brief.get("benefits") or [])

    content_type = settings.get("contentType") or "card"

    if content_type == "marketplacePackage":
        total = package_slides(settings, 5)
        chosen = slide_types_for(settings)
        # Tanlagani yetarli bo'lmasa — standart ro'yxatdan to'ldiramiz,
        # tanlamagan bo'lsa — standart ro'yxatning o'zi ishlatiladi.
        types = (chosen + SLIDE_TYPE_IDS)[:total] if chosen else SLIDE_TYPE_IDS[:total]
        return [_slide_entry(type_id, headline, subtitle, lines) for type_id in types]

    pages = int(settings.get("pages") or 1)
    if pages > 1:
        return [
            _slide_entry(SLIDE_TYPE_IDS[index % len(SLIDE_TYPE_IDS)], headline, subtitle, lines)
            for index in range(pages)
        ]

    return [_slide_entry("main", headline, subtitle, lines)]
