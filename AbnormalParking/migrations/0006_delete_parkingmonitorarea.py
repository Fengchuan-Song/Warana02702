from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("AbnormalParking", "0005_parkingbuffer_berthing_fields"),
    ]

    operations = [
        migrations.DeleteModel(
            name="ParkingMonitorArea",
        ),
    ]
