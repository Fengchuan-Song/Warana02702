from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("AISData", "0004_violation_trajectory_mmsi_time_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="violationeventrecord",
            name="source_namespace",
            field=models.CharField(
                db_index=True,
                default="operational",
                max_length=32,
                verbose_name="AIS来源命名空间",
            ),
        ),
        migrations.AddField(
            model_name="violationeventrecord",
            name="simulation_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=64,
                verbose_name="模拟回放标识",
            ),
        ),
        migrations.AddIndex(
            model_name="violationeventrecord",
            index=models.Index(
                fields=[
                    "source_namespace",
                    "simulation_id",
                    "-last_detected_at",
                ],
                name="ais_violation_source_time",
            ),
        ),
    ]
