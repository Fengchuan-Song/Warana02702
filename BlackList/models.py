from django.db import models

# Create your models here.
class BlackList(models.Model):
    mmsi = models.CharField(max_length=9, unique=True, blank=False, verbose_name="MMSI")
    shipName = models.CharField(max_length=32, verbose_name="船舶名称")
    shipType = models.CharField(max_length=32, verbose_name="船舶类型")
    length = models.FloatField(verbose_name="船长（米）")
    width = models.FloatField(verbose_name="船宽（米）")

    def __str__(self):
        # 返回船舶名称，方便在 Django 管理后台显示
        return self.shipName

    class Meta:
        # 元数据，用于定义模型的一些额外信息
        verbose_name = "船舶"
        verbose_name_plural = "船舶列表"
        ordering = ["mmsi"]  # 默认按船舶名称排序
