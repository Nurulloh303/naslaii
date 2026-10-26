from django.contrib import admin
from django.db.models import Count

from .models import Promo, PromoRedemption


def _value_label(promo: Promo) -> str:
    return f"{promo.value} token" if promo.kind == "tokens" else f"{promo.value}% chegirma"


class RedemptionInline(admin.TabularInline):
    model = PromoRedemption
    extra = 0
    can_delete = False
    fields = ("user", "user_name", "user_balance", "created_at")
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name_plural = "Kim ishlatgan"

    @admin.display(description="Ismi")
    def user_name(self, obj):
        return obj.user.name or "—"

    @admin.display(description="Hozirgi balansi")
    def user_balance(self, obj):
        return obj.user.balance

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Promo)
class PromoAdmin(admin.ModelAdmin):
    list_display = (
        "code", "value_label", "tariff_label", "used_label", "total_given", "active", "owner",
        "expires_at", "created_at",
    )
    list_filter = ("kind", "active", "tariff", "created_at")
    search_fields = ("code", "owner", "note", "redemptions__user__email")
    readonly_fields = ("used", "created_at", "total_given")
    inlines = (RedemptionInline,)
    ordering = ("-created_at",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_redeemed=Count("redemptions"))

    @admin.display(description="Qiymati", ordering="value")
    def value_label(self, obj):
        return _value_label(obj)

    @admin.display(description="Tarif", ordering="tariff")
    def tariff_label(self, obj):
        from billing.pricing import tariff_by_id

        tariff = tariff_by_id(obj.tariff) if obj.tariff else None
        return f"{tariff['name']} · {tariff['price_uzs']:,} so'm".replace(",", " ") if tariff else "Erkin"

    @admin.display(description="Ishlatilgan", ordering="_redeemed")
    def used_label(self, obj):
        limit = obj.max_uses or "∞"
        return f"{obj._redeemed} / {limit}"

    @admin.display(description="Jami berilgan")
    def total_given(self, obj):
        count = getattr(obj, "_redeemed", None)
        if count is None:
            count = obj.redemptions.count()
        return f"{obj.value * count} token" if obj.kind == "tokens" else f"{count} ta chegirma"


@admin.register(PromoRedemption)
class PromoRedemptionAdmin(admin.ModelAdmin):
    list_display = ("promo", "promo_value", "user", "user_name", "created_at")
    list_filter = ("promo__kind", "promo__tariff", "created_at")
    search_fields = ("promo__code", "user__email", "user__name")
    list_select_related = ("promo", "user")
    date_hierarchy = "created_at"
    readonly_fields = ("promo", "user", "created_at")

    @admin.display(description="Qiymati")
    def promo_value(self, obj):
        return _value_label(obj.promo)

    @admin.display(description="Ismi")
    def user_name(self, obj):
        return obj.user.name or "—"

    def has_add_permission(self, request):
        return False
