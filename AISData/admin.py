from django.contrib import admin

from .models import (
    DetectionModelConfiguration,
    ViolationAISTrajectoryPoint,
    ViolationEventRecord,
)


@admin.register(DetectionModelConfiguration)
class DetectionModelConfigurationAdmin(admin.ModelAdmin):
    list_display = ("feature_id", "updated_at")
    search_fields = ("feature_id",)
    readonly_fields = ("updated_at",)


class ViolationAISTrajectoryPointInline(admin.TabularInline):
    model = ViolationAISTrajectoryPoint
    extra = 0
    fields = ("mmsi", "observed_at", "longitude", "latitude", "speed", "course")
    readonly_fields = fields


@admin.register(ViolationEventRecord)
class ViolationEventRecordAdmin(admin.ModelAdmin):
    inlines = (ViolationAISTrajectoryPointInline,)
    list_display = (
        "event_type",
        "target_id",
        "risk_level",
        "last_detected_at",
        "occurrence_count",
    )
    list_filter = ("feature_id", "risk_level")
    search_fields = ("target_id", "target_name", "details")
    readonly_fields = (
        "event_key",
        "fingerprint",
        "first_detected_at",
        "last_detected_at",
        "occurrence_count",
        "raw_data",
    )
