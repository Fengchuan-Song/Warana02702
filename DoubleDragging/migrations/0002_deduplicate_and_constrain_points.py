from django.db import migrations, models
from django.db.models import Count, Max


def deduplicate_points(apps, schema_editor):
    Point = apps.get_model("DoubleDragging", "DoubleDraggingPoint")
    duplicates = (
        Point.objects.values("mmsi", "timestamp")
        .annotate(keep_id=Max("id"), duplicate_count=Count("id"))
        .filter(duplicate_count__gt=1)
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
        ("DoubleDragging", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_points,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="doubledraggingpoint",
            constraint=models.UniqueConstraint(
                fields=("mmsi", "timestamp"),
                name="uniq_double_dragging_mmsi_timestamp",
            ),
        ),
        migrations.AddIndex(
            model_name="doubledraggingpoint",
            index=models.Index(
                fields=["mmsi", "timestamp"],
                name="double_drag_mmsi_ts_idx",
            ),
        ),
    ]
