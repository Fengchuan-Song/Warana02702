from django.db import models


class BlackList(models.Model):
    mmsi = models.CharField(max_length=9, unique=True, blank=False, verbose_name="MMSI")
    shipName = models.CharField(
        max_length=32,
        blank=True,
        default="",
        verbose_name="船舶名称",
    )
    shipType = models.CharField(
        max_length=32,
        blank=True,
        default="",
        verbose_name="船舶类型",
    )
    length = models.FloatField(
        blank=True,
        null=True,
        verbose_name="船长（米）",
    )
    width = models.FloatField(
        blank=True,
        null=True,
        verbose_name="船宽（米）",
    )
    reason = models.TextField(blank=True, default="", verbose_name="列入原因")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    def __str__(self):
        # 返回船舶名称，方便在 Django 管理后台显示
        return self.shipName

    class Meta:
        verbose_name = "黑名单船舶"
        verbose_name_plural = "黑名单船舶"
        ordering = ["mmsi"]
