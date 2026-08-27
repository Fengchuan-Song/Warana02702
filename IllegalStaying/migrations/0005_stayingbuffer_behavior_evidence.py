from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("IllegalStaying", "0004_illegalstayingmonitorarea_vertices"),
    ]

    operations = [
        migrations.AddField(
            model_name="stayingbuffer",
            name="heading",
            field=models.FloatField(blank=True, null=True, verbose_name="船艏向"),
        ),
        migrations.AddField(
            model_name="stayingbuffer",
            name="nav_status",
            field=models.SmallIntegerField(blank=True, null=True, verbose_name="AIS航行状态"),
        ),
        migrations.AddField(
            model_name="stayingbuffer",
            name="at_dock",
            field=models.BooleanField(default=False, verbose_name="是否靠泊"),
        ),
        migrations.AddField(
            model_name="stayingbuffer",
            name="matched_port_name",
            field=models.CharField(blank=True, default="", max_length=100, verbose_name="匹配港口"),
        ),
        migrations.AddField(
            model_name="stayingbuffer",
            name="in_port_basin",
            field=models.BooleanField(default=False, verbose_name="是否位于港池"),
        ),
    ]
