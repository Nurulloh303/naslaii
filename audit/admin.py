from django.contrib import admin

from .models import AuditRun


@admin.register(AuditRun)
class AuditRunAdmin(admin.ModelAdmin):
    list_display = ("user", "link", "created_at")
    search_fields = ("user__email", "link")
