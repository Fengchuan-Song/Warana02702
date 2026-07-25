from django.contrib import admin

from .models import TransferOperationPlan


@admin.register(TransferOperationPlan)
class TransferOperationPlanAdmin(admin.ModelAdmin):
    list_display = (
        "operation_name",
        "vessel_a_mmsi",
        "vessel_b_mmsi",
        "approval_number",
        "starts_at",
        "ends_at",
        "max_speed_knots",
        "is_active",
    )
    list_filter = ("is_active",)
    search_fields = (
        "operation_name",
        "vessel_a_mmsi",
        "vessel_b_mmsi",
        "approval_number",
        "notes",
    )
