from django.db import models


class DoubleDraggingPoint(models.Model):
    """
    存储实时接收的轨迹点，用于双拖行为判定
    """
    mmsi = models.CharField(max_length=20, db_index=True, verbose_name="MMSI")
    lat = models.FloatField(verbose_name="纬度")
    lng = models.FloatField(verbose_name="经度")
    sog = models.FloatField(verbose_name="航速(SOG)")
    cog = models.FloatField(verbose_name="航向(COG)")
    timestamp = models.DateTimeField(db_index=True, verbose_name="时间戳")

    # 记录数据入库的时间，方便清理旧数据
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "双拖轨迹点"
        verbose_name_plural = "双拖轨迹点列表"
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["mmsi", "timestamp"],
                name="uniq_double_dragging_mmsi_timestamp",
            )
        ]
        indexes = [
            models.Index(
                fields=["mmsi", "timestamp"],
                name="double_drag_mmsi_ts_idx",
            )
        ]

    def __str__(self):
        return f"{self.mmsi} - {self.timestamp}"
