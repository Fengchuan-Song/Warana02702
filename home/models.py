from django.db import models


class CameraConfiguration(models.Model):
    """地图摄像头的位置、视频来源和显示状态。"""

    camera_key = models.SlugField(
        "摄像头标识",
        max_length=64,
        unique=True,
    )
    name = models.CharField("摄像头名称", max_length=100)
    longitude = models.FloatField("经度")
    latitude = models.FloatField("纬度")
    video_url = models.CharField("视频地址", max_length=2000)
    is_visible = models.BooleanField("地图显示", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ("camera_key",)
        verbose_name = "摄像头配置"
        verbose_name_plural = "摄像头配置"

    def __str__(self):
        return f"{self.name} ({self.camera_key})"
