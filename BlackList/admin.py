from django.contrib import admin

from .models import BlackList


@admin.register(BlackList)
class BlackListAdmin(admin.ModelAdmin):
    list_display = (
        "mmsi",
        "shipName",
        "shipType",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active", "shipType")
    search_fields = ("mmsi", "shipName", "shipType", "reason")
