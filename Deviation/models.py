# Deviation/models.py

from django.db import models


class TrajectoryPoint(models.Model):
    """
    存储实时接收的AIS轨迹点，用于积累形成完整的轨迹段进行偏航检测。
    """
    mmsi = models.CharField(max_length=9, db_index=True, verbose_name="MMSI")
    longitude = models.FloatField(verbose_name="经度")
    latitude = models.FloatField(verbose_name="纬度")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="接收时间")

    class Meta:
        verbose_name = "轨迹点"
        verbose_name_plural = "轨迹点列表"
        ordering = ["timestamp"]

    def __str__(self):
        return f"{self.mmsi} @ ({self.longitude:.4f}, {self.latitude:.4f}) at {self.timestamp}"