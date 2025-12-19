from django.contrib import admin
from .models import MonitorRegion, DetectionRecord


@admin.register(MonitorRegion)
class MonitorRegionAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'min_lon', 'max_lon', 'min_lat', 'max_lat')
    list_editable = ('is_active',)  # 可选：使字段在列表页可编辑
    search_fields = ('name',)  # 可选：添加搜索功能
    list_filter = ('is_active',)  # 可选：添加过滤器


@admin.register(DetectionRecord)
class DetectionRecordAdmin(admin.ModelAdmin):
    list_display = ('target_id', 'timestamp', 'is_abnormal', 'abnormal_count')
    list_filter = ('is_abnormal', 'timestamp')
    search_fields = ('target_id',)  # 可选：添加搜索功能
    date_hierarchy = 'timestamp'  # 可选：添加日期导航

    # 可选：添加自定义字段显示
    def get_abnormal_status(self, obj):
        return "异常" if obj.is_abnormal else "正常"

    get_abnormal_status.short_description = "异常状态"