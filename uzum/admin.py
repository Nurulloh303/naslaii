from django.contrib import admin

from .models import UzumExport


@admin.register(UzumExport)
class UzumExportAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "category_id", "category_path", "sku_count", "status", "created_at", "expires_at")
    list_filter = ("status", "template_sha")
    search_fields = ("user__email", "category_path", "=category_id", "=id")
    raw_id_fields = ("user", "job")
    readonly_fields = (
        "user", "job", "category_id", "category_path", "template_sha", "draft", "sku_count", "file_path",
        "photo_paths", "status", "errors", "idempotency_key", "draft_hash", "created_at",
    )
