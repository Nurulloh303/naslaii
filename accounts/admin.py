from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db.models import Count, IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils.html import format_html

from billing.models import LedgerEntry
from generation.models import GenerationJob
from promos.models import PromoRedemption

from .models import ApiKey, User

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


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "prefix", "user", "balance", "created_at", "last_used_at")
    readonly_fields = ("prefix", "digest", "created_at", "last_used_at")
    search_fields = ("user__email",)
