from django.contrib import admin

from .models import ParkingBuffer, ParkingMonitorArea


@admin.register(ParkingMonitorArea)
class ParkingMonitorAreaAdmin(admin.ModelAdmin):
    list_display = ("name", "min_lon", "min_lat", "max_lon", "max_lat", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(ParkingBuffer)
class ParkingBufferAdmin(admin.ModelAdmin):
    list_display = ("mmsi", "name", "longitude", "latitude", "speed", "timestamp")
    search_fields = ("mmsi", "name")
    list_filter = ("timestamp",)
