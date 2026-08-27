from django.db import migrations, models


DEFAULT_BOUNDS = (
    113.6109833,
    22.1700302,
    113.7926567,
    22.2080471,
)


def rectangle_vertices(min_lon, min_lat, max_lon, max_lat):
    return [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
    ]


def populate_monitor_regions(apps, schema_editor):
    MonitorRegion = apps.get_model("AbnormalWandering", "MonitorRegion")
    if not MonitorRegion.objects.exists():
        min_lon, min_lat, max_lon, max_lat = DEFAULT_BOUNDS
        MonitorRegion.objects.create(
            name="异常徘徊监控区",
            min_lon=min_lon,
            min_lat=min_lat,
            max_lon=max_lon,
            max_lat=max_lat,
            vertices=rectangle_vertices(
                min_lon,
                min_lat,
                max_lon,
                max_lat,
            ),
            is_active=True,
        )
        return

    for region in MonitorRegion.objects.filter(vertices=[]):
        region.vertices = rectangle_vertices(
            region.min_lon,
            region.min_lat,
            region.max_lon,
            region.max_lat,
        )
        region.save(update_fields=("vertices",))


class Migration(migrations.Migration):
    dependencies = [
        ("AbnormalWandering", "0002_trajectorybuffer"),
    ]

    operations = [
        migrations.AddField(
            model_name="monitorregion",
            name="vertices",
            field=models.JSONField(blank=True, default=list, verbose_name="多边形顶点"),
        ),
        migrations.AlterModelOptions(
            name="monitorregion",
            options={
                "ordering": ("name", "id"),
                "verbose_name": "异常徘徊监控区",
                "verbose_name_plural": "异常徘徊监控区",
            },
        ),
        migrations.RunPython(populate_monitor_regions, migrations.RunPython.noop),
    ]
