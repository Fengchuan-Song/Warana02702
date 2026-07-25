from django.core.exceptions import ValidationError
from django.db import models

from .mmsi import classify_vessel_mmsi


class IllegalBerthingPermit(models.Model):
    """Permit for one Chinese vessel to berth alongside one foreign vessel."""

    chinese_mmsi = models.CharField(
        max_length=9,
        db_index=True,
        verbose_name="中国籍船舶 MMSI",
    )
    foreign_mmsi = models.CharField(
        max_length=9,
        db_index=True,
        verbose_name="外籍船舶 MMSI",
    )
    permit_number = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="许可编号",
    )
    valid_from = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="有效期开始",
    )
    valid_until = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="有效期结束",
    )
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    notes = models.TextField(blank=True, default="", verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "船舶搭靠许可"
        verbose_name_plural = "船舶搭靠许可"
        ordering = ["chinese_mmsi", "foreign_mmsi", "-valid_from"]
        indexes = [
            models.Index(
                fields=[
                    "chinese_mmsi",
                    "foreign_mmsi",
                    "is_active",
                ],
                name="berth_permit_pair_idx",
            )
        ]

    def __str__(self):
        return f"{self.chinese_mmsi} / {self.foreign_mmsi}"

    def clean(self):
        errors = {}
        if classify_vessel_mmsi(self.chinese_mmsi) != "chinese":
            errors["chinese_mmsi"] = (
                "必须使用中国 MID（412/413/414）的有效船舶 MMSI"
            )
        if classify_vessel_mmsi(self.foreign_mmsi) != "foreign":
            errors["foreign_mmsi"] = (
                "必须使用非中国 MID 的有效船舶 MMSI"
            )
        if (
            self.valid_from
            and self.valid_until
            and self.valid_until <= self.valid_from
        ):
            errors["valid_until"] = "有效期结束必须晚于有效期开始"
        if errors:
            raise ValidationError(errors)
