from django.db import models


class LowSpeedPoint(models.Model):
    """
    存储实时接收的轨迹点，用于低速行为判定
    """
    mmsi = models.CharField(max_length=20, db_index=True, verbose_name="MMSI")
    name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="船名",
    )
    longitude = models.FloatField(null=True, blank=True, verbose_name="经度")
    latitude = models.FloatField(null=True, blank=True, verbose_name="纬度")
    speed = models.FloatField(verbose_name="航速(SOG)")
    speed_limit = models.FloatField(default=1.5, verbose_name="最低航速")
    zone_name = models.CharField(
        max_length=100,
        default="默认水域",
        verbose_name="监控区域",
    )
    timestamp = models.DateTimeField(db_index=True, verbose_name="时间戳")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "低速检测轨迹点"
        verbose_name_plural = "低速检测轨迹点列表"
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=("mmsi", "timestamp"),
                name="low_speed_unique_mmsi_timestamp",
            )
        ]

    def __str__(self):
        return f"{self.mmsi} - {self.speed}kn"


from django.db import models

# Create your models here.
