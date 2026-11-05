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


from .models import IkpuCode  # noqa: E402


@admin.register(IkpuCode)
class IkpuCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "name_ru", "name_uz", "barcode")
    search_fields = ("code", "name_ru", "name_uz", "barcode")
    readonly_fields = ("code", "name_ru", "name_uz", "path_ru", "path_uz", "barcode", "search")
    list_per_page = 50

    def has_add_permission(self, request):
        return False
