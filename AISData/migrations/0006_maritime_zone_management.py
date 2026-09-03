from django.db import migrations, models


def seed_revision(apps, schema_editor):
    apps.get_model("AISData", "MaritimeZoneRevision").objects.using(
        schema_editor.connection.alias
    ).get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("AISData", "0005_violation_source_scope")]

    operations = [
        migrations.CreateModel(
            name="MaritimeZoneRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.PositiveBigIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name="MaritimeZoneOverride",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_id", models.BigIntegerField(blank=True, null=True, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("locode", models.CharField(default="CN", max_length=8)),
                ("zone_type", models.CharField(max_length=3)),
                ("vertices", models.JSONField()),
                ("is_active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("pk",)},
        ),
        migrations.RunPython(seed_revision, migrations.RunPython.noop),
    ]
