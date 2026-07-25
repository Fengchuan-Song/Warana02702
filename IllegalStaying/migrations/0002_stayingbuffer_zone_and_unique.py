from django.db import migrations, models
from django.db.models import Count, Min


def deduplicate_points(apps, schema_editor):
    Point = apps.get_model("IllegalStaying", "StayingBuffer")
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
        ("IllegalStaying", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_points,
            migrations.RunPython.noop,
        ),
        migrations.AddField(
            model_name="stayingbuffer",
            name="zone_name",
            field=models.CharField(
                default="非法驻留监控区",
                max_length=100,
                verbose_name="禁停区域",
            ),
        ),
        migrations.AddConstraint(
            model_name="stayingbuffer",
            constraint=models.UniqueConstraint(
                fields=("mmsi", "timestamp"),
                name="illegal_staying_unique_mmsi_timestamp",
            ),
        ),
    ]
