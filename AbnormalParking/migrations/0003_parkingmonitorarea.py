from django.db import migrations, models


def create_default_monitor_area(apps, schema_editor):
    ParkingMonitorArea = apps.get_model("AbnormalParking", "ParkingMonitorArea")
    ParkingMonitorArea.objects.get_or_create(
        name="异常停泊监控区",
        defaults={
            "min_lon": 113.6279434,
            "min_lat": 22.1376273,
            "max_lon": 113.7935190,
            "max_lat": 22.2008193,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("AbnormalParking", "0002_alter_parkingbuffer_options"),
    ]

    operations = [
        migrations.CreateModel(
            name="ParkingMonitorArea",
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
                ("name", models.CharField(max_length=100, unique=True, verbose_name="区域名称")),
                ("min_lon", models.FloatField(verbose_name="最小经度")),
                ("min_lat", models.FloatField(verbose_name="最小纬度")),
                ("max_lon", models.FloatField(verbose_name="最大经度")),
                ("max_lat", models.FloatField(verbose_name="最大纬度")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "异常停泊监控区",
                "verbose_name_plural": "异常停泊监控区",
                "ordering": ("-updated_at", "-id"),
            },
        ),
        migrations.RunPython(
            create_default_monitor_area,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
