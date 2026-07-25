from django.contrib import admin

from .models import SmugglingVoyagePermit, SmugglingZone


@admin.register(SmugglingZone)
class SmugglingZoneAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "zone_type",
        "buffer_metres",
        "is_active",
        "updated_at",
    )
    list_filter = ("zone_type", "is_active")
    search_fields = ("name", "legal_reference", "notes")


@admin.register(SmugglingVoyagePermit)
class SmugglingVoyagePermitAdmin(admin.ModelAdmin):
    list_display = (
        "mmsi",
        "permit_number",
        "origin_zone",
        "destination_zone",
        "valid_from",
        "valid_until",
        "is_active",
    )
    list_filter = ("is_active", "origin_zone", "destination_zone")
    search_fields = ("mmsi", "permit_number", "notes")
