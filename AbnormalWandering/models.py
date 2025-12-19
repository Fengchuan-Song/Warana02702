from django.db import models

class MonitorRegion(models.Model):
    """
    配置监控区域（对应原代码中的 REGION_COORDS）
    """
    name = models.CharField(max_length=100, verbose_name="区域名称")
    min_lon = models.FloatField(verbose_name="最小经度")
    max_lon = models.FloatField(verbose_name="最大经度")
    min_lat = models.FloatField(verbose_name="最小纬度")
    max_lat = models.FloatField(verbose_name="最大纬度")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    def __str__(self):
        return self.name

class DetectionRecord(models.Model):
    """
    保存每次检测的结果
    """
    target_id = models.CharField(max_length=50, verbose_name="目标ID/MMSI")
    timestamp = models.DateTimeField(auto_now_add=True, verbose_name="检测时间")
    is_abnormal = models.BooleanField(default=False, verbose_name="是否存在异常徘徊")
    abnormal_count = models.IntegerField(default=0, verbose_name="异常段数量")
    details = models.TextField(verbose_name="详细结果JSON", blank=True) # 存储具体的异常段信息

    def __str__(self):
        return f"{self.target_id} - {'异常' if self.is_abnormal else '正常'}"


class TrajectoryBuffer(models.Model):
    """
    实时轨迹缓冲区：用于保存通过 API 上传的实时点。
    只保留最近一段时间的数据，旧数据会被清理。
    """
    mmsi = models.CharField(max_length=50, db_index=True, verbose_name="MMSI")
    name = models.CharField(max_length=100, blank=True, null=True, verbose_name="船名")

    # 核心数据
    latitude = models.FloatField()
    longitude = models.FloatField()
    course = models.FloatField(default=0.0, verbose_name="航向")
    speed = models.FloatField(default=0.0, verbose_name="航速")
    timestamp = models.DateTimeField(db_index=True, verbose_name="上报时间")

    class Meta:
        ordering = ['timestamp']  # 默认按时间正序排列
        verbose_name = "实时轨迹点"

    def __str__(self):
        return f"{self.mmsi} @ {self.timestamp}"