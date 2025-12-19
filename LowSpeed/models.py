from django.db import models


class LowSpeedPoint(models.Model):
    """
    存储实时接收的轨迹点，用于低速行为判定
    """
    mmsi = models.CharField(max_length=20, db_index=True, verbose_name="MMSI")
    speed = models.FloatField(verbose_name="航速(SOG)")
    timestamp = models.DateTimeField(db_index=True, verbose_name="时间戳")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "低速检测轨迹点"
        verbose_name_plural = "低速检测轨迹点列表"
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.mmsi} - {self.speed}kn"


from django.db import models

# Create your models here.
