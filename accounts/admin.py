import hashlib
import secrets

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db.models import Count, IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils.html import format_html

from billing.models import LedgerEntry
from generation.models import GenerationJob
from promos.models import PromoRedemption

from .models import ApiKey, ApiKeyLedger, User

admin.site.site_header = "Naslai admin"
admin.site.site_title = "Naslai admin"
admin.site.index_title = "Boshqaruv paneli"


class LedgerInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0
    can_delete = False
    fields = ("created_at", "type", "amount", "label", "balance_after")
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name_plural = "Token harakatlari"

    def has_add_permission(self, request, obj=None):
        return False


class PromoRedemptionInline(admin.TabularInline):
    model = PromoRedemption
    extra = 0
    can_delete = False
    fields = ("promo", "promo_value", "created_at")
    readonly_fields = fields
    verbose_name_plural = "Ishlatgan promokodlari"

    @admin.display(description="Qiymati")
    def promo_value(self, obj):
        promo = obj.promo
        return f"{promo.value} token" if promo.kind == "tokens" else f"{promo.value}% chegirma"

    def has_add_permission(self, request, obj=None):
        return False


class JobInline(admin.TabularInline):
    model = GenerationJob
    extra = 0
    can_delete = False
    fields = ("open_link", "content_type", "status", "title", "tokens_charged", "created_at")
    readonly_fields = fields
    ordering = ("-created_at",)
    show_change_link = False
    verbose_name_plural = "Generatsiyalari (oxirgi 50)"

    @admin.display(description="Vazifa")
    def open_link(self, obj):
        return format_html('<a href="../../../generation/generationjob/{}/change/">#{}</a>', obj.pk, obj.pk)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        ids = list(queryset.order_by("-created_at").values_list("pk", flat=True)[:50])
        return queryset.filter(pk__in=ids)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = (
        "email", "name", "balance", "jobs_count", "tokens_spent", "promos_used",
        "login_via", "date_joined", "last_login", "is_staff",
    )
    list_filter = ("is_staff", "is_active", "language", "date_joined")
    search_fields = ("email", "name", "support_code", "username")
    ordering = ("-date_joined",)
    list_per_page = 50
    readonly_fields = ("support_code", "date_joined", "last_login")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Naslai", {"fields": ("name", "balance", "language", "support_code", "google_id", "telegram_id")}),
    )
    inlines = (PromoRedemptionInline, LedgerInline, JobInline)

    def get_queryset(self, request):
        # Har biri alohida subquery: bitta so'rovda bir nechta JOIN qilinsa,
        # Sum qatorlar ko'payishidan noto'g'ri (oshirilgan) chiqadi.
        def per_user(model, expression):
            return Coalesce(Subquery(
                model.objects.filter(user=OuterRef("pk")).order_by().values("user")
                .annotate(total=expression).values("total")[:1],
                output_field=IntegerField(),
            ), Value(0))

        return super().get_queryset(request).annotate(
            _jobs=per_user(GenerationJob, Count("pk")),
            _spent=per_user(GenerationJob, Sum("tokens_charged")),
            _promos=per_user(PromoRedemption, Count("pk")),
        )

    @admin.display(description="Generatsiyalar", ordering="_jobs")
    def jobs_count(self, obj):
        return obj._jobs

    @admin.display(description="Sarflagan token", ordering="_spent")
    def tokens_spent(self, obj):
        return obj._spent or 0

    @admin.display(description="Promokodlar", ordering="_promos")
    def promos_used(self, obj):
        return obj._promos

    @admin.display(description="Kirish")
    def login_via(self, obj):
        return ", ".join(obj.providers) or "email"


def _tariff_choices():
    from billing.pricing import TARIFFS

    return [("", "— tarifsiz —")] + [
        (t["id"], f"{t['name']} · {t['price_uzs']:,} so'm · {t['tokens']} token".replace(",", " ")) for t in TARIFFS
    ]


class ApiKeyAddForm(forms.ModelForm):
    """Yangi kalit: egasi, nomi, izoh va boshlang'ich balans (tarif yoki erkin son)."""

    tariff = forms.ChoiceField(label="Tarif", required=False)
    amount = forms.IntegerField(label="Yoki erkin token soni", required=False, min_value=0, max_value=100_000)

    class Meta:
        model = ApiKey
        fields = ("user", "name", "note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tariff"].choices = _tariff_choices()


class ApiKeyChangeForm(forms.ModelForm):
    """Mavjud kalit: balansni to'ldirish (tarif yoki erkin son). Balansning o'zi faqat o'qiladi."""

    top_up_tariff = forms.ChoiceField(label="Tarif bo'yicha to'ldirish", required=False)
    top_up_amount = forms.IntegerField(label="Yoki erkin token qo'shish", required=False, min_value=1, max_value=100_000)
    top_up_note = forms.CharField(label="Izoh (masalan: to'lov raqami)", required=False, max_length=200)

    class Meta:
        model = ApiKey
        fields = ("name", "note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["top_up_tariff"].choices = _tariff_choices()


class ApiKeyLedgerInline(admin.TabularInline):
    model = ApiKeyLedger
    extra = 0
    can_delete = False
    fields = ("created_at", "type", "amount", "label", "balance_after")
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name_plural = "Balans tarixi"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "prefix", "user", "balance", "spent", "created_at", "last_used_at")
    search_fields = ("user__email", "name", "prefix", "note")
    list_select_related = ("user",)
    autocomplete_fields = ("user",)
    inlines = (ApiKeyLedgerInline,)

    def get_form(self, request, obj=None, **kwargs):
        kwargs["form"] = ApiKeyChangeForm if obj else ApiKeyAddForm
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                ("Kalit", {"fields": ("user", "name", "note")}),
                ("Boshlang'ich balans", {"fields": ("tariff", "amount")}),
            )
        return (
            ("Kalit", {"fields": ("user", "name", "prefix", "note", "balance", "created_at", "last_used_at")}),
            ("Balansni to'ldirish", {"fields": ("top_up_tariff", "top_up_amount", "top_up_note")}),
        )

    def get_readonly_fields(self, request, obj=None):
        return ("user", "prefix", "balance", "created_at", "last_used_at") if obj else ()

    @admin.display(description="Sarflangan")
    def spent(self, obj):
        return sum(item.amount for item in obj.ledger.filter(type=ApiKeyLedger.DEBIT))

    def save_model(self, request, obj, form, change):
        from billing.pricing import tariff_by_id
        from billing.services import key_credit

        if not change:
            # Maxfiy kalit faqat bir marta, yaratilgan zahoti ko'rsatiladi —
            # bazada faqat uning hash'i saqlanadi.
            secret = f"nsk_live_{secrets.token_urlsafe(24)}"
            obj.prefix = secret[:18]
            obj.digest = hashlib.sha256(secret.encode()).hexdigest()
            obj.balance = 0
            if not obj.name:
                obj.name = f"API kalit {ApiKey.objects.filter(user=obj.user).count() + 1}"
            super().save_model(request, obj, form, change)
            tariff = tariff_by_id(form.cleaned_data.get("tariff"))
            amount = tariff["tokens"] if tariff else (form.cleaned_data.get("amount") or 0)
            if amount:
                key_credit(obj, amount, f"Tarif: {tariff['name']}" if tariff else "Boshlang'ich balans")
            messages.warning(
                request,
                format_html(
                    "API kalit yaratildi. <b>Uni hozir nusxalab oling — boshqa ko'rsatilmaydi:</b> "
                    "<code style='font-size:14px;user-select:all'>{}</code>",
                    secret,
                ),
            )
            return

        super().save_model(request, obj, form, change)
        tariff = tariff_by_id(form.cleaned_data.get("top_up_tariff"))
        amount = tariff["tokens"] if tariff else form.cleaned_data.get("top_up_amount")
        if amount:
            note = (form.cleaned_data.get("top_up_note") or "").strip()
            label = f"Tarif: {tariff['name']}" if tariff else "Admin tomonidan qo'shildi"
            key_credit(obj, amount, f"{label} · {note}" if note else label)
            messages.success(request, f"Kalit balansiga {amount} token qo'shildi.")
