from django.core.exceptions import ValidationError
from django.db import models


class SmugglingZone(models.Model):
    HONG_KONG_ORIGIN = "hong_kong_origin"
    ZONE_TYPE_CHOICES = [
        (HONG_KONG_ORIGIN, "香港地区空间范围"),
    ]

    name = models.CharField(max_length=100, unique=True, verbose_name="区域名称")
    zone_type = models.CharField(
        max_length=32,
        choices=ZONE_TYPE_CHOICES,
        db_index=True,
        verbose_name="区域类型",
    )
    vertices = models.JSONField(verbose_name="多边形顶点")
    buffer_metres = models.PositiveIntegerField(
        default=0,
        verbose_name="外围预警距离（米）",
    )
    legal_reference = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="依据/批准文件",
    )
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    notes = models.TextField(blank=True, default="", verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "走私研判区域"
        verbose_name_plural = "走私研判区域"
        ordering = ["zone_type", "name"]
        indexes = [
            models.Index(
                fields=["zone_type", "is_active"],
                name="smuggle_zone_type_idx",
            )
        ]

    def __str__(self):
        return f"{self.get_zone_type_display()}：{self.name}"

    def clean(self):
        if not isinstance(self.vertices, list) or len(self.vertices) < 3:
            raise ValidationError(
                {"vertices": "区域至少需要3个多边形顶点"}
            )


class SmugglingVoyagePermit(models.Model):
    mmsi = models.CharField(
        max_length=9,
        db_index=True,
        verbose_name="船舶 MMSI",
    )
    permit_number = models.CharField(
        max_length=64,
        verbose_name="许可/报告编号",
    )
    origin_zone = models.ForeignKey(
        SmugglingZone,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="origin_permits",
        limit_choices_to={"zone_type": SmugglingZone.HONG_KONG_ORIGIN},
        verbose_name="许可起航区",
    )
    destination_port_id = models.PositiveBigIntegerField(
        blank=True,
        null=True,
        db_index=True,
        verbose_name="许可目的广东港口数据ID",
    )
    valid_from = models.DateTimeField(verbose_name="有效期开始")
    valid_until = models.DateTimeField(verbose_name="有效期结束")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    notes = models.TextField(blank=True, default="", verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "走私研判航次许可"
        verbose_name_plural = "走私研判航次许可"
        ordering = ["mmsi", "-valid_from"]
        indexes = [
            models.Index(
                fields=["mmsi", "is_active", "valid_from", "valid_until"],
                name="smuggle_permit_time_idx",
            )
        ]

    def __str__(self):
        return f"{self.mmsi} / {self.permit_number}"

    def clean(self):
        errors = {}
        mmsi = str(self.mmsi or "").strip()
        if len(mmsi) != 9 or not mmsi.isdigit():
            errors["mmsi"] = "MMSI必须是9位数字"
        if self.valid_until and self.valid_from:
            if self.valid_until <= self.valid_from:
                errors["valid_until"] = "有效期结束必须晚于开始时间"
        if (
            self.origin_zone
            and self.origin_zone.zone_type
            != SmugglingZone.HONG_KONG_ORIGIN
        ):
            errors["origin_zone"] = "起航区必须是香港起航区"
        if errors:
            raise ValidationError(errors)
