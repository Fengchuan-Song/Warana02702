from django.db import models


class ElectronicFence(models.Model):
    """由一组经纬度顶点组成的多边形电子围栏。"""

    name = models.CharField("名称", max_length=100, unique=True)
    description = models.TextField("说明", blank=True)
    vertices = models.JSONField("顶点坐标")
    crossing_mode = models.CharField(
        "越界规则",
        max_length=8,
        choices=(
            ("enter", "禁止驶入"),
            ("exit", "禁止驶出"),
            ("both", "进出均预警"),
        ),
        default="enter",
    )
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        verbose_name = "电子围栏"
        verbose_name_plural = "电子围栏"

    def __str__(self):
        return self.name
