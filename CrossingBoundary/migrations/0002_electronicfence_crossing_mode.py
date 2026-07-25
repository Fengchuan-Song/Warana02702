from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("CrossingBoundary", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="electronicfence",
            name="crossing_mode",
            field=models.CharField(
                choices=(
                    ("enter", "禁止驶入"),
                    ("exit", "禁止驶出"),
                    ("both", "进出均预警"),
                ),
                default="enter",
                max_length=8,
                verbose_name="越界规则",
            ),
        ),
    ]
