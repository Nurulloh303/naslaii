from django.contrib import admin

from .models import LedgerEntry


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "type", "amount", "label", "balance_after")
    list_filter = ("type", "created_at")
    search_fields = ("user__email", "label")
    list_select_related = ("user",)
    date_hierarchy = "created_at"
    readonly_fields = ("user", "type", "amount", "label", "balance_after", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
