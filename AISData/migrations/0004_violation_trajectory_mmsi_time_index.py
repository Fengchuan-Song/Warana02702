from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("AISData", "0003_detectionmodelconfiguration"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="violationaistrajectorypoint",
            index=models.Index(
                fields=["mmsi", "observed_at"],
                name="ais_traj_mmsi_time",
            ),
        ),
    ]
