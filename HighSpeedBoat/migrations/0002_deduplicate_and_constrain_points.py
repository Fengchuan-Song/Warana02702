from django.db import migrations, models
from django.db.models import Count, Min


def deduplicate_points(apps, schema_editor):
    HighSpeedPoint = apps.get_model("HighSpeedBoat", "HighSpeedPoint")
    duplicates = (
        HighSpeedPoint.objects.values("mmsi", "timestamp")
        .annotate(keep_id=Min("id"), row_count=Count("id"))
        .filter(row_count__gt=1)
    )

    for duplicate in duplicates.iterator():
        (
            HighSpeedPoint.objects.filter(
                mmsi=duplicate["mmsi"],
                timestamp=duplicate["timestamp"],
            )
            .exclude(id=duplicate["keep_id"])
            .delete()
        )


class Migration(migrations.Migration):
    dependencies = [
        ("HighSpeedBoat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_points,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterModelOptions(
            name="highspeedpoint",
            options={
                "ordering": ["-timestamp"],
                "verbose_name": "高速检测轨迹点",
                "verbose_name_plural": "高速检测轨迹点列表",
            },
        ),
        migrations.AddConstraint(
            model_name="highspeedpoint",
            constraint=models.UniqueConstraint(
                fields=("mmsi", "timestamp"),
                name="high_speed_unique_mmsi_timestamp",
            ),
        ),
    ]
