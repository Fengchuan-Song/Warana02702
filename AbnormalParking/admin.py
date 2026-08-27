from django.contrib import admin

from .models import ParkingBuffer


@admin.register(ParkingBuffer)
class ParkingBufferAdmin(admin.ModelAdmin):
    list_display = ("mmsi", "name", "longitude", "latitude", "speed", "timestamp")
    search_fields = ("mmsi", "name")
    list_filter = ("timestamp",)
