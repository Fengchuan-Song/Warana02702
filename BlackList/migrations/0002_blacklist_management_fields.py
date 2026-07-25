from django.db import migrations, models
from django.utils import timezone


def populate_timestamps(apps, schema_editor):
    blacklist = apps.get_model("BlackList", "BlackList")
    now = timezone.now()
    blacklist.objects.filter(created_at__isnull=True).update(
        created_at=now,
        updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("BlackList", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="blacklist",
            options={
                "ordering": ["mmsi"],
                "verbose_name": "黑名单船舶",
                "verbose_name_plural": "黑名单船舶",
            },
        ),
        migrations.AlterField(
            model_name="blacklist",
            name="length",
            field=models.FloatField(
                blank=True,
                null=True,
                verbose_name="船长（米）",
            ),
        ),
        migrations.AlterField(
            model_name="blacklist",
            name="shipName",
            field=models.CharField(
                blank=True,
                default="",
                max_length=32,
                verbose_name="船舶名称",
            ),
        ),
        migrations.AlterField(
            model_name="blacklist",
            name="shipType",
            field=models.CharField(
                blank=True,
                default="",
                max_length=32,
                verbose_name="船舶类型",
            ),
        ),
        migrations.AlterField(
            model_name="blacklist",
            name="width",
            field=models.FloatField(
                blank=True,
                null=True,
                verbose_name="船宽（米）",
            ),
        ),
        migrations.AddField(
            model_name="blacklist",
            name="created_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="blacklist",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="是否启用"),
        ),
        migrations.AddField(
            model_name="blacklist",
            name="reason",
            field=models.TextField(
                blank=True,
                default="",
                verbose_name="列入原因",
            ),
        ),
        migrations.AddField(
            model_name="blacklist",
            name="updated_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunPython(
            populate_timestamps,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="blacklist",
            name="created_at",
            field=models.DateTimeField(
                auto_now_add=True,
                verbose_name="创建时间",
            ),
        ),
        migrations.AlterField(
            model_name="blacklist",
            name="updated_at",
            field=models.DateTimeField(
                auto_now=True,
                verbose_name="更新时间",
            ),
        ),
    ]
