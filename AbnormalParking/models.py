from django.db import models


class ParkingMonitorArea(models.Model):
    """A configurable rectangular area monitored for abnormal parking."""

    name = models.CharField("区域名称", max_length=100, unique=True)
    min_lon = models.FloatField("最小经度")
    min_lat = models.FloatField("最小纬度")
    max_lon = models.FloatField("最大经度")
    max_lat = models.FloatField("最大纬度")
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        verbose_name = "异常停泊监控区"
        verbose_name_plural = "异常停泊监控区"

    def __str__(self):
        return self.name


class ParkingBuffer(models.Model):
    """
    驻留检测专用缓冲区
    """
    mmsi = models.CharField(max_length=50, db_index=True, verbose_name="MMSI")
    name = models.CharField(max_length=100, blank=True, null=True, verbose_name="船名")

    latitude = models.FloatField()
    longitude = models.FloatField()
    speed = models.FloatField(default=0.0, verbose_name="航速")
    timestamp = models.DateTimeField(db_index=True, verbose_name="上报时间")

    class Meta:
        ordering = ['timestamp']
        verbose_name = "停泊检测缓冲点"

    def __str__(self):
        return f"{self.mmsi} - {self.timestamp}"
