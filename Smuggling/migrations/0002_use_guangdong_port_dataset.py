from django.db import migrations, models


def remove_deprecated_smuggling_zones(apps, schema_editor):
    SmugglingZone = apps.get_model("Smuggling", "SmugglingZone")
    SmugglingZone.objects.exclude(zone_type="hong_kong_origin").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("Smuggling", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="smugglingvoyagepermit",
            name="destination_port_id",
            field=models.PositiveBigIntegerField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="许可目的广东港口数据ID",
            ),
        ),
        migrations.RemoveField(
            model_name="smugglingvoyagepermit",
            name="destination_zone",
        ),
        migrations.RunPython(
            remove_deprecated_smuggling_zones,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="smugglingzone",
            name="zone_type",
            field=models.CharField(
                choices=[("hong_kong_origin", "香港地区空间范围")],
                db_index=True,
                max_length=32,
                verbose_name="区域类型",
            ),
        ),
    ]
