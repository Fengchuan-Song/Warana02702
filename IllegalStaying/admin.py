from django.contrib import admin

from .models import IllegalStayingMonitorArea, StayingBuffer


@admin.register(IllegalStayingMonitorArea)
class IllegalStayingMonitorAreaAdmin(admin.ModelAdmin):
    list_display = ("name", "reason", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "reason")


@admin.register(StayingBuffer)
class StayingBufferAdmin(admin.ModelAdmin):
    list_display = ("mmsi", "name", "zone_name", "speed", "timestamp")
    search_fields = ("mmsi", "name", "zone_name")

# Register your models here.
