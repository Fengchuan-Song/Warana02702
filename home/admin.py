from django.contrib import admin

from .models import CameraConfiguration


@admin.register(CameraConfiguration)
class CameraConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "camera_key",
        "name",
        "longitude",
        "latitude",
        "is_visible",
        "updated_at",
    )
    list_filter = ("is_visible",)
    search_fields = ("camera_key", "name", "video_url")
    readonly_fields = ("created_at", "updated_at")
