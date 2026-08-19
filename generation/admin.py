from django.contrib import admin

from .models import GenerationJob


@admin.register(GenerationJob)
class GenerationJobAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "content_type", "status", "tokens_charged", "created_at")
    list_filter = ("content_type", "status")
