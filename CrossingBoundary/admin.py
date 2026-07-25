from django.contrib import admin

from .models import ElectronicFence


@admin.register(ElectronicFence)
class ElectronicFenceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "crossing_mode",
        "is_active",
        "updated_at",
    )
    list_filter = ("crossing_mode", "is_active")
    search_fields = ("name", "description")
