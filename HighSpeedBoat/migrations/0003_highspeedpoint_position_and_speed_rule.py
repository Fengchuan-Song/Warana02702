from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("HighSpeedBoat", "0002_deduplicate_and_constrain_points"),
    ]

    operations = [
        migrations.AddField(
            model_name="highspeedpoint",
            name="latitude",
            field=models.FloatField(blank=True, null=True, verbose_name="纬度"),
        ),
        migrations.AddField(
            model_name="highspeedpoint",
            name="longitude",
            field=models.FloatField(blank=True, null=True, verbose_name="经度"),
        ),
        migrations.AddField(
            model_name="highspeedpoint",
            name="speed_limit",
            field=models.FloatField(default=30.0, verbose_name="适用限速"),
        ),
        migrations.AddField(
            model_name="highspeedpoint",
            name="zone_name",
            field=models.CharField(
                default="默认水域",
                max_length=100,
                verbose_name="限速区域",
            ),
        ),
    ]
