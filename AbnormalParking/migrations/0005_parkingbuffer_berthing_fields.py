from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("AbnormalParking", "0004_parkingmonitorarea_vertices"),
    ]

    operations = [
        migrations.AddField(
            model_name="parkingbuffer",
            name="heading",
            field=models.FloatField(blank=True, null=True, verbose_name="船艏向"),
        ),
        migrations.AddField(
            model_name="parkingbuffer",
            name="nav_status",
            field=models.SmallIntegerField(
                blank=True,
                null=True,
                verbose_name="AIS航行状态",
            ),
        ),
        migrations.AddField(
            model_name="parkingbuffer",
            name="at_dock",
            field=models.BooleanField(default=False, verbose_name="是否靠泊"),
        ),
        migrations.AddField(
            model_name="parkingbuffer",
            name="matched_port_name",
            field=models.CharField(
                blank=True,
                default="",
                max_length=100,
                verbose_name="匹配港口",
            ),
        ),
    ]
