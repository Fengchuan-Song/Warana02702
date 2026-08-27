from django.db import migrations, models


def backfill_rectangle_vertices(apps, schema_editor):
    ParkingMonitorArea = apps.get_model("AbnormalParking", "ParkingMonitorArea")
    for area in ParkingMonitorArea.objects.all().iterator():
        if area.vertices:
            continue
        area.vertices = [
            [area.min_lon, area.min_lat],
            [area.max_lon, area.min_lat],
            [area.max_lon, area.max_lat],
            [area.min_lon, area.max_lat],
        ]
        area.save(update_fields=("vertices",))


class Migration(migrations.Migration):
    dependencies = [
        ("AbnormalParking", "0003_parkingmonitorarea"),
    ]

    operations = [
        migrations.AddField(
            model_name="parkingmonitorarea",
            name="vertices",
            field=models.JSONField(blank=True, default=list, verbose_name="多边形顶点"),
        ),
        migrations.RunPython(
            backfill_rectangle_vertices,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
