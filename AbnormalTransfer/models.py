import math

from django.core.exceptions import ValidationError
from django.db import models


def _valid_vessel_mmsi(value):
    mmsi = str(value or "").strip()
    return (
        len(mmsi) == 9
        and mmsi.isdigit()
        and mmsi[0] in "234567"
    )


class TransferOperationPlan(models.Model):
    """Approved time and speed constraints for one vessel pair."""

    vessel_a_mmsi = models.CharField(
        max_length=9,
        db_index=True,
        verbose_name="船舶 A MMSI",
    )
    vessel_b_mmsi = models.CharField(
        max_length=9,
        db_index=True,
        verbose_name="船舶 B MMSI",
    )
    operation_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="作业名称",
    )
    approval_number = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="批准/通告编号",
    )
    starts_at = models.DateTimeField(
        db_index=True,
        verbose_name="批准开始时间",
    )
    ends_at = models.DateTimeField(
        db_index=True,
        verbose_name="批准结束时间",
    )
    max_speed_knots = models.FloatField(
        verbose_name="最大规定航速（节）"
    )
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    notes = models.TextField(blank=True, default="", verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "接驳作业计划"
        verbose_name_plural = "接驳作业计划"
        ordering = ["-starts_at", "vessel_a_mmsi", "vessel_b_mmsi"]
        indexes = [
            models.Index(
                fields=[
                    "vessel_a_mmsi",
                    "vessel_b_mmsi",
                    "is_active",
                ],
                name="transfer_plan_pair_idx",
            )
        ]

    def clean(self):
        errors = {}
        if not _valid_vessel_mmsi(self.vessel_a_mmsi):
            errors["vessel_a_mmsi"] = "必须是有效的9位船舶 MMSI"
        if not _valid_vessel_mmsi(self.vessel_b_mmsi):
            errors["vessel_b_mmsi"] = "必须是有效的9位船舶 MMSI"
        if (
            self.vessel_a_mmsi
            and self.vessel_a_mmsi == self.vessel_b_mmsi
        ):
            errors["vessel_b_mmsi"] = "接驳双方不能是同一艘船"
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            errors["ends_at"] = "批准结束时间必须晚于开始时间"
        try:
            speed = float(self.max_speed_knots)
        except (TypeError, ValueError):
            speed = None
        if speed is None or not math.isfinite(speed) or speed < 0:
            errors["max_speed_knots"] = "最大规定航速必须是非负有限数字"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        first = str(self.vessel_a_mmsi or "").strip()
        second = str(self.vessel_b_mmsi or "").strip()
        if first and second and second < first:
            first, second = second, first
        self.vessel_a_mmsi = first
        self.vessel_b_mmsi = second
        super().save(*args, **kwargs)

    @property
    def pair_key(self):
        return f"{self.vessel_a_mmsi}:{self.vessel_b_mmsi}"

    def __str__(self):
        return (
            self.operation_name
            or f"{self.vessel_a_mmsi} / {self.vessel_b_mmsi}"
        )
