from django.db import migrations, models
from django.db.models import Count, Min


def deduplicate_points(apps, schema_editor):
    Point = apps.get_model("Deviation", "TrajectoryPoint")
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
        ("Deviation", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_points,
            migrations.RunPython.noop,
        ),
        migrations.AddField(
            model_name="trajectorypoint",
            name="course",
            field=models.FloatField(
                blank=True,
                null=True,
                verbose_name="对地航向",
            ),
        ),
        migrations.AddField(
            model_name="trajectorypoint",
            name="name",
            field=models.CharField(
                blank=True,
                max_length=100,
                null=True,
                verbose_name="船名",
            ),
        ),
        migrations.AddField(
            model_name="trajectorypoint",
            name="speed",
            field=models.FloatField(default=0.0, verbose_name="航速"),
        ),
        migrations.AlterField(
            model_name="trajectorypoint",
            name="timestamp",
            field=models.DateTimeField(
                db_index=True,
                verbose_name="AIS时间",
            ),
        ),
        migrations.AddConstraint(
            model_name="trajectorypoint",
            constraint=models.UniqueConstraint(
                fields=("mmsi", "timestamp"),
                name="deviation_unique_mmsi_timestamp",
            ),
        ),
    ]
