from django.db import models


class MaritimeZoneRevision(models.Model):
    """A singleton lock and version for atomic edits/imports."""

    version = models.PositiveBigIntegerField(default=0)


class MaritimeZoneOverride(models.Model):
    """Persist operator edits without overwriting the bundled source files."""

    source_id = models.BigIntegerField(unique=True, null=True, blank=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    locode = models.CharField(max_length=8, default="CN")
    zone_type = models.CharField(max_length=3)
    vertices = models.JSONField()
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("pk",)


class DetectionModelConfiguration(models.Model):
    """Operator-provided runtime overrides for one detection model."""

    feature_id = models.CharField("检测类型标识", max_length=64, unique=True)
    parameters = models.JSONField("参数覆盖", default=dict)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        ordering = ("feature_id",)
        verbose_name = "预警模型参数配置"
        verbose_name_plural = "预警模型参数配置"

    def __str__(self):
        return self.feature_id


class ViolationEventRecord(models.Model):
    """A deduplicated illegal-event detection produced by any model."""

    source_namespace = models.CharField(
        "AIS来源命名空间",
        max_length=32,
        default="operational",
        db_index=True,
    )
    simulation_id = models.CharField(
        "模拟回放标识",
        max_length=64,
        blank=True,
        db_index=True,
    )
    event_key = models.CharField("事件键", max_length=64, unique=True)
    fingerprint = models.CharField("事件指纹", max_length=64, db_index=True)
    feature_id = models.CharField("检测类型标识", max_length=64, db_index=True)
    event_type = models.CharField("违法事件类型", max_length=100)
    target_id = models.CharField("目标标识", max_length=255, blank=True, db_index=True)
    target_name = models.CharField("目标名称", max_length=255, blank=True)
    status = models.CharField("识别状态", max_length=100, blank=True)
    risk_level = models.CharField("风险等级", max_length=50, blank=True)
    longitude = models.FloatField("经度", null=True, blank=True)
    latitude = models.FloatField("纬度", null=True, blank=True)
    event_time = models.DateTimeField("事件时间", null=True, blank=True, db_index=True)
    first_detected_at = models.DateTimeField("首次识别时间", db_index=True)
    last_detected_at = models.DateTimeField("最后识别时间", db_index=True)
    occurrence_count = models.PositiveIntegerField("识别次数", default=1)
    details = models.TextField("事件详情", blank=True)
    raw_data = models.JSONField("原始识别结果", default=dict)

    class Meta:
        ordering = ("-last_detected_at", "-id")
        indexes = [
            models.Index(
                fields=("source_namespace", "simulation_id", "-last_detected_at"),
                name="ais_violation_source_time",
            ),
            models.Index(
                fields=("feature_id", "-last_detected_at"),
                name="ais_violation_feature_time",
            ),
        ]
        verbose_name = "违法事件识别记录"
        verbose_name_plural = "违法事件识别记录"

    def __str__(self):
        target = self.target_id or self.target_name or "未知目标"
        return f"{self.event_type} - {target}"


class ViolationAISTrajectoryPoint(models.Model):
    """An AIS position associated with a persisted violation event."""

    event = models.ForeignKey(
        ViolationEventRecord,
        on_delete=models.CASCADE,
        related_name="ais_trajectory",
        verbose_name="违法事件",
    )
    mmsi = models.CharField("MMSI", max_length=32, db_index=True)
    observed_at = models.DateTimeField("AIS时间", db_index=True)
    longitude = models.FloatField("经度")
    latitude = models.FloatField("纬度")
    speed = models.FloatField("航速", null=True, blank=True)
    course = models.FloatField("航向", null=True, blank=True)
    raw_data = models.JSONField("原始AIS数据", default=dict)

    class Meta:
        ordering = ("observed_at", "id")
        indexes = [
            models.Index(
                fields=("mmsi", "observed_at"),
                name="ais_traj_mmsi_time",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("event", "mmsi", "observed_at"),
                name="unique_violation_ais_point",
            ),
        ]
        verbose_name = "违法事件AIS轨迹点"
        verbose_name_plural = "违法事件AIS轨迹点"

    def __str__(self):
        return f"{self.mmsi} @ {self.observed_at.isoformat()}"
