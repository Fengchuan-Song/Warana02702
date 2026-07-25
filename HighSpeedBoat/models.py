from django.db import models

# Create your models here.
class HighSpeedPoint(models.Model):
    """
    存储实时接收的轨迹点，用于高速行为判定
    """
    mmsi = models.CharField(max_length=20, db_index=True, verbose_name="MMSI")
    speed = models.FloatField(verbose_name="航速(SOG)")
    longitude = models.FloatField("经度", null=True, blank=True)
    latitude = models.FloatField("纬度", null=True, blank=True)
    speed_limit = models.FloatField("适用限速", default=30.0)
    zone_name = models.CharField(
        "限速区域",
        max_length=100,
        default="默认水域",
    )
    timestamp = models.DateTimeField(db_index=True, verbose_name="时间戳")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "高速检测轨迹点"
        verbose_name_plural = "高速检测轨迹点列表"
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["mmsi", "timestamp"],
                name="high_speed_unique_mmsi_timestamp",
            )
        ]

    def __str__(self):
        return f"{self.mmsi} - {self.speed}kn"
