from django.contrib import admin

from .models import Promo, PromoRedemption


@admin.register(Promo)
class PromoAdmin(admin.ModelAdmin):
    list_display = ("code", "kind", "value", "used", "max_uses", "active", "owner", "expires_at")
    list_filter = ("kind", "active")
    search_fields = ("code", "owner", "note")


@admin.register(PromoRedemption)
class PromoRedemptionAdmin(admin.ModelAdmin):
    list_display = ("promo", "user", "created_at")
    search_fields = ("promo__code", "user__email")
