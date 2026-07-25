import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="SmugglingZone",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=100,
                        unique=True,
                        verbose_name="区域名称",
                    ),
                ),
                (
                    "zone_type",
                    models.CharField(
                        choices=[
                            ("hong_kong_origin", "香港起航区"),
                            ("customs_port", "合法口岸/海关监管区"),
                            ("non_customs_landing", "广东非设关靠泊区"),
                        ],
                        db_index=True,
                        max_length=32,
                        verbose_name="区域类型",
                    ),
                ),
                (
                    "vertices",
                    models.JSONField(verbose_name="多边形顶点"),
                ),
                (
                    "buffer_metres",
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name="外围预警距离（米）",
                    ),
                ),
                (
                    "legal_reference",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=255,
                        verbose_name="依据/批准文件",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        verbose_name="是否启用",
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True,
                        default="",
                        verbose_name="备注",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        verbose_name="创建时间",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True,
                        verbose_name="更新时间",
                    ),
                ),
            ],
            options={
                "verbose_name": "走私研判区域",
                "verbose_name_plural": "走私研判区域",
                "ordering": ["zone_type", "name"],
            },
        ),
        migrations.CreateModel(
            name="SmugglingVoyagePermit",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "mmsi",
                    models.CharField(
                        db_index=True,
                        max_length=9,
                        verbose_name="船舶 MMSI",
                    ),
                ),
                (
                    "permit_number",
                    models.CharField(
                        max_length=64,
                        verbose_name="许可/报告编号",
                    ),
                ),
                (
                    "valid_from",
                    models.DateTimeField(verbose_name="有效期开始"),
                ),
                (
                    "valid_until",
                    models.DateTimeField(verbose_name="有效期结束"),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        verbose_name="是否启用",
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True,
                        default="",
                        verbose_name="备注",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        verbose_name="创建时间",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True,
                        verbose_name="更新时间",
                    ),
                ),
                (
                    "destination_zone",
                    models.ForeignKey(
                        blank=True,
                        limit_choices_to={
                            "zone_type": "non_customs_landing"
                        },
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="destination_permits",
                        to="Smuggling.smugglingzone",
                        verbose_name="许可目的区域",
                    ),
                ),
                (
                    "origin_zone",
                    models.ForeignKey(
                        blank=True,
                        limit_choices_to={
                            "zone_type": "hong_kong_origin"
                        },
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="origin_permits",
                        to="Smuggling.smugglingzone",
                        verbose_name="许可起航区",
                    ),
                ),
            ],
            options={
                "verbose_name": "走私研判航次许可",
                "verbose_name_plural": "走私研判航次许可",
                "ordering": ["mmsi", "-valid_from"],
            },
        ),
        migrations.AddIndex(
            model_name="smugglingzone",
            index=models.Index(
                fields=["zone_type", "is_active"],
                name="smuggle_zone_type_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="smugglingvoyagepermit",
            index=models.Index(
                fields=["mmsi", "is_active", "valid_from", "valid_until"],
                name="smuggle_permit_time_idx",
            ),
        ),
    ]
