from django.contrib import admin

from .models import IllegalBerthingPermit


@admin.register(IllegalBerthingPermit)
class IllegalBerthingPermitAdmin(admin.ModelAdmin):
    list_display = (
        "chinese_mmsi",
        "foreign_mmsi",
        "permit_number",
        "valid_from",
        "valid_until",
        "is_active",
    )
    list_filter = ("is_active",)
    search_fields = (
        "chinese_mmsi",
        "foreign_mmsi",
        "permit_number",
        "notes",
    )
