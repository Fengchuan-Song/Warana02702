from django.db import models


class StayingBuffer(models.Model):
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
        verbose_name = "驻留检测缓冲点"

    def __str__(self):
        return f"{self.mmsi} - {self.timestamp}"