from django.db import models


class IllegalStayingMonitorArea(models.Model):
    """A configurable polygon area where prolonged staying is forbidden."""

    name = models.CharField("区域名称", max_length=100, unique=True)
    reason = models.TextField("禁停原因")
    min_lon = models.FloatField("最小经度")
    min_lat = models.FloatField("最小纬度")
    max_lon = models.FloatField("最大经度")
    max_lat = models.FloatField("最大纬度")
    vertices = models.JSONField("多边形顶点", default=list, blank=True)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        verbose_name = "非法驻留监控区"
        verbose_name_plural = "非法驻留监控区"

    def __str__(self):
        return self.name


class StayingBuffer(models.Model):
    """
    驻留检测专用缓冲区
    """
    mmsi = models.CharField(max_length=50, db_index=True, verbose_name="MMSI")
    name = models.CharField(max_length=100, blank=True, null=True, verbose_name="船名")

    latitude = models.FloatField()
    longitude = models.FloatField()
    speed = models.FloatField(default=0.0, verbose_name="航速")
    heading = models.FloatField(null=True, blank=True, verbose_name="船艏向")
    nav_status = models.SmallIntegerField(
        null=True,
        blank=True,
        verbose_name="AIS航行状态",
    )
    at_dock = models.BooleanField(default=False, verbose_name="是否靠泊")
    matched_port_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="匹配港口",
    )
    in_port_basin = models.BooleanField(
        default=False,
        verbose_name="是否位于港池",
    )
    zone_name = models.CharField(
        max_length=100,
        default="非法驻留监控区",
        verbose_name="禁停区域",
    )
    timestamp = models.DateTimeField(db_index=True, verbose_name="上报时间")

    class Meta:
        ordering = ['timestamp']
        verbose_name = "驻留检测缓冲点"
        constraints = [
            models.UniqueConstraint(
                fields=("mmsi", "timestamp"),
                name="illegal_staying_unique_mmsi_timestamp",
            )
        ]

    def __str__(self):
        return f"{self.mmsi} - {self.timestamp}"
