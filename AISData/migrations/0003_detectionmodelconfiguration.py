from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("AISData", "0002_backfill_detection_times"),
    ]

    operations = [
        migrations.CreateModel(
            name="DetectionModelConfiguration",
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
                    "feature_id",
                    models.CharField(
                        max_length=64,
                        unique=True,
                        verbose_name="检测类型标识",
                    ),
                ),
                (
                    "parameters",
                    models.JSONField(default=dict, verbose_name="参数覆盖"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="更新时间"),
                ),
            ],
            options={
                "verbose_name": "预警模型参数配置",
                "verbose_name_plural": "预警模型参数配置",
                "ordering": ("feature_id",),
            },
        ),
    ]
