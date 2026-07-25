from django.db import migrations, models
from django.db.models import Count, Min


def deduplicate_points(apps, schema_editor):
    Point = apps.get_model("LowSpeed", "LowSpeedPoint")
    duplicates = (
        Point.objects.values("mmsi", "timestamp")
        .annotate(keep_id=Min("id"), row_count=Count("id"))
        .filter(row_count__gt=1)
    )
    for duplicate in duplicates.iterator():
        (
            Point.objects.filter(
                mmsi=duplicate["mmsi"],
                timestamp=duplicate["timestamp"],
            )
            .exclude(id=duplicate["keep_id"])
            .delete()
        )


class Migration(migrations.Migration):
    dependencies = [
        ("LowSpeed", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_points,
            migrations.RunPython.noop,
        ),
        migrations.AddField(
            model_name="lowspeedpoint",
            name="latitude",
            field=models.FloatField(blank=True, null=True, verbose_name="纬度"),
        ),
        migrations.AddField(
            model_name="lowspeedpoint",
            name="longitude",
            field=models.FloatField(blank=True, null=True, verbose_name="经度"),
        ),
        migrations.AddField(
            model_name="lowspeedpoint",
            name="name",
            field=models.CharField(
                blank=True,
                max_length=100,
                null=True,
                verbose_name="船名",
            ),
        ),
        migrations.AddField(
            model_name="lowspeedpoint",
            name="speed_limit",
            field=models.FloatField(default=1.5, verbose_name="最低航速"),
        ),
        migrations.AddField(
            model_name="lowspeedpoint",
            name="zone_name",
            field=models.CharField(
                default="默认水域",
                max_length=100,
                verbose_name="监控区域",
            ),
        ),
        migrations.AddConstraint(
            model_name="lowspeedpoint",
            constraint=models.UniqueConstraint(
                fields=("mmsi", "timestamp"),
                name="low_speed_unique_mmsi_timestamp",
            ),
        ),
    ]
