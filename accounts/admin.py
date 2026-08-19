from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("email", "name", "balance", "is_staff")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Naslai", {"fields": ("name", "balance", "language", "support_code")}),
    )
