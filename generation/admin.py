"""Django admin: har bir generatsiya — kim, nima yukladi, qaysi referens, nima chiqdi.

Rasmlar (mahsulot fotosi va referens) ``job.settings`` ichida data:URL
ko'rinishida saqlanadi, natijalar esa ``job.results[].url`` da. Ro'yxat
sahifasida faqat yengil natija prevyusi ko'rsatiladi (data:URL'lar har biri
yuzlab KB — 30 qatorda sahifa og'irlashib ketardi), to'liq rasmlar esa
vazifaning o'z sahifasida.
"""

from __future__ import annotations

import json

from django.contrib import admin
from django.db.models import Q
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from .models import GenerationJob

STYLE_LABELS = {
    "": "Avto",
    "minimal": "Minimalizm",
    "premium": "Premium",
    "bright": "Yorqin",
    "lifestyle": "Layfstayl",
    "tech": "Texnologik",
    "nature": "Tabiiy",
    "bold": "Kontrast",
}

CONTENT_LABELS = {
    "card": "Karta",
    "photo": "Foto",
    "video": "Video",
    "fashion": "Fashion",
    "copyStyle": "Uslubdan nusxa",
    "marketplacePackage": "Marketplace paketi",
}

# Xom sozlamalarni ko'rsatishda data:URL'larni qisqartiramiz.
_DATA_URL_KEYS = ("assetDataUrl", "designReferenceDataUrl")


def _img(src: str, height: int = 120, link: bool = True) -> str:
    if not src:
        return "—"
    tag = format_html(
        '<img src="{}" style="height:{}px;max-width:{}px;object-fit:contain;border-radius:6px;'
        'border:1px solid #ddd;background:#f6f6f6" loading="lazy">',
        src, height, int(height * 1.2),
    )
    if link and not src.startswith("data:"):
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', src, tag)
    return tag


def _reference_src(settings: dict) -> str:
    """Ro'yxat uchun yengil manzil: shablon URL'i bo'lsa o'sha, data:URL emas."""
    url = str(settings.get("designReferenceUrl") or "")
    return url if url.startswith(("http://", "https://", "/")) else ""


class StyleFilter(admin.SimpleListFilter):
    title = "Karta uslubi"
    parameter_name = "style"

    def lookups(self, request, model_admin):
        return [(key or "auto", label) for key, label in STYLE_LABELS.items()]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        if value == "auto":
            return queryset.exclude(settings__stylePreset__in=[key for key in STYLE_LABELS if key])
        return queryset.filter(settings__stylePreset=value)


class ReferenceFilter(admin.SimpleListFilter):
    title = "Referens"
    parameter_name = "reference"

    def lookups(self, request, model_admin):
        return [("yes", "Referens bilan"), ("no", "Referenssiz")]

    def queryset(self, request, queryset):
        has_reference = Q(settings__has_key="designReferenceDataUrl") & ~Q(settings__designReferenceDataUrl="")
        if self.value() == "yes":
            return queryset.filter(has_reference)
        if self.value() == "no":
            return queryset.exclude(has_reference)
        return queryset


@admin.register(GenerationJob)
class GenerationJobAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user_email", "result_preview", "content_label", "style_label", "reference_preview",
        "title", "status", "tokens_charged", "created_at",
    )
    list_display_links = ("id", "result_preview")
    list_filter = ("status", "content_type", StyleFilter, ReferenceFilter, "refunded", "created_at")
    search_fields = ("user__email", "user__name", "title", "category", "id")
    date_hierarchy = "created_at"
    list_per_page = 30
    list_select_related = ("user",)
    ordering = ("-created_at",)

    readonly_fields = (
        "user_link", "status", "progress", "message", "error", "content_label", "style_label",
        "created_at", "updated_at", "tokens_charged", "refunded", "cancelled",
        "brief_block", "input_images", "reference_block", "results_block", "settings_block",
    )
    fieldsets = (
        ("Vazifa", {"fields": (
            "user_link", "status", "progress", "message", "error", "content_label", "style_label",
            "tokens_charged", "refunded", "cancelled", "created_at", "updated_at",
        )}),
        ("Kiritilgan ma'lumot", {"fields": ("brief_block", "input_images")}),
        ("Tanlangan referens", {"fields": ("reference_block",)}),
        ("Natija", {"fields": ("results_block",)}),
        ("Barcha sozlamalar (texnik)", {"classes": ("collapse",), "fields": ("settings_block",)}),
    )

    def has_add_permission(self, request):
        return False

    # --- ro'yxat ustunlari ---------------------------------------------

    @admin.display(description="Foydalanuvchi", ordering="user__email")
    def user_email(self, obj):
        return obj.user.email

    @admin.display(description="Natija")
    def result_preview(self, obj):
        results = obj.results or []
        url = str((results[0] or {}).get("url") or "") if results else ""
        return mark_safe(_img(url, 70, link=False)) if url else "—"

    @admin.display(description="Turi", ordering="content_type")
    def content_label(self, obj):
        return CONTENT_LABELS.get(obj.content_type, obj.content_type)

    @admin.display(description="Uslub")
    def style_label(self, obj):
        preset = str((obj.settings or {}).get("stylePreset") or "")
        return STYLE_LABELS.get(preset, preset)

    @admin.display(description="Referens")
    def reference_preview(self, obj):
        settings = obj.settings or {}
        if not settings.get("designReferenceDataUrl"):
            return "—"
        src = _reference_src(settings)
        if src:
            return mark_safe(_img(src, 56, link=False))
        return settings.get("designReferenceName") or "o'z rasmi"

    # --- vazifa sahifasi -----------------------------------------------

    @admin.display(description="Foydalanuvchi")
    def user_link(self, obj):
        return format_html(
            '<a href="../../../accounts/user/{}/change/">{}</a> · balans: {} token',
            obj.user_id, obj.user.email, obj.user.balance,
        )

    @admin.display(description="Brif")
    def brief_block(self, obj):
        settings = obj.settings or {}
        benefits = [b for b in (obj.benefits or []) if b]
        rows = [
            ("Nomi", obj.title or "—"),
            ("Qo'shimcha", obj.subtitle or "—"),
            ("Kategoriya", obj.category or "—"),
            ("Marketplace", settings.get("marketplace") or "—"),
            ("Til", settings.get("language") or "—"),
            ("Matnni kim yozadi", "O'zimiz (brauzer)" if settings.get("textMode") == "own" else "Model"),
            ("Variantlar / sahifalar", f"{settings.get('variants') or 1} / {settings.get('pages') or 1}"),
            ("Foydalanuvchi istagi", settings.get("cardPrompt") or settings.get("photoPrompt") or "—"),
        ]
        table = format_html_join("", "<tr><th style='text-align:left;padding:2px 12px 2px 0'>{}</th><td>{}</td></tr>", rows)
        items = format_html_join("", "<li>{}</li>", ((b,) for b in benefits)) if benefits else mark_safe("<li>—</li>")
        return format_html("<table>{}</table><b>Afzalliklar:</b><ul style='margin:4px 0 0 18px'>{}</ul>", table, items)

    @admin.display(description="Yuklangan mahsulot rasmi")
    def input_images(self, obj):
        settings = obj.settings or {}
        src = str(settings.get("assetDataUrl") or "")
        return mark_safe(_img(src, 260)) if src else "—"

    @admin.display(description="Referens")
    def reference_block(self, obj):
        settings = obj.settings or {}
        data_url = str(settings.get("designReferenceDataUrl") or "")
        if not data_url:
            return format_html("Referens tanlanmagan · uslub: <b>{}</b>", self.style_label(obj))
        src = _reference_src(settings) or data_url
        name = settings.get("designReferenceName") or "o'zi yuklagan rasm"
        used = [r.get("referenceUsed") for r in (obj.results or [])]
        if any(u is False for u in used):
            note = "⚠ AI referensni ishlata olmadi — oddiy shablonga o'tilgan"
        elif any(u for u in used):
            note = "✓ Referens ishlatilgan"
        else:
            note = ""
        return format_html("{}<div style='margin-top:6px'><b>{}</b><br>{}</div>", mark_safe(_img(src, 260)), name, note)

    @admin.display(description="Natija rasmlari")
    def results_block(self, obj):
        results = obj.results or []
        if not results:
            return obj.error or "Natija yo'q"
        cards = []
        for index, result in enumerate(results, start=1):
            url = str((result or {}).get("url") or "")
            meta = []
            if result.get("promptTier"):
                meta.append(f"urinish: {result['promptTier']}")
            if result.get("referenceUsed") is False:
                meta.append("referenssiz")
            if result.get("layers") or result.get("elements"):
                meta.append("Magic Layers ochilgan")
            cards.append(format_html(
                "<div style='display:inline-block;margin:0 14px 14px 0;vertical-align:top;text-align:center'>"
                "{}<div>{}-variant</div><small>{}</small><br>"
                "<a href='{}' target='_blank' rel='noopener'>to'liq o'lcham</a></div>",
                mark_safe(_img(url, 300)), index, " · ".join(meta), url,
            ))
        return mark_safe("".join(cards))

    @admin.display(description="Sozlamalar (JSON)")
    def settings_block(self, obj):
        settings = dict(obj.settings or {})
        for key in _DATA_URL_KEYS:
            if settings.get(key):
                settings[key] = f"<data:URL, {len(str(settings[key])) // 1024} KB>"
        return format_html(
            "<pre style='white-space:pre-wrap;max-height:420px;overflow:auto'>{}</pre>",
            json.dumps(settings, ensure_ascii=False, indent=2),
        )
