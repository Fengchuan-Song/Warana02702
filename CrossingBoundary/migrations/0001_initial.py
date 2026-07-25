from django.db import migrations, models


def create_default_fences(apps, schema_editor):
    ElectronicFence = apps.get_model("CrossingBoundary", "ElectronicFence")
    ElectronicFence.objects.bulk_create([
        ElectronicFence(
            name="生态保护区",
            description="由系统原有固定区域迁移而来",
            vertices=[
                [113.6950, 22.4050],
                [113.7050, 22.4050],
                [113.7050, 22.4150],
                [113.6950, 22.4150],
            ],
        ),
        ElectronicFence(
            name="港池禁入区",
            description="由系统原有固定区域迁移而来",
            vertices=[
                [113.6800, 22.3950],
                [113.6900, 22.3950],
                [113.6900, 22.4050],
                [113.6800, 22.4050],
            ],
        ),
    ])


def remove_default_fences(apps, schema_editor):
    ElectronicFence = apps.get_model("CrossingBoundary", "ElectronicFence")
    ElectronicFence.objects.filter(
        name__in=["生态保护区", "港池禁入区"],
        description="由系统原有固定区域迁移而来",
    ).delete()


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ElectronicFence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True, verbose_name="名称")),
                ("description", models.TextField(blank=True, verbose_name="说明")),
                ("vertices", models.JSONField(verbose_name="顶点坐标")),
                ("is_active", models.BooleanField(default=True, verbose_name="启用")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "电子围栏",
                "verbose_name_plural": "电子围栏",
                "ordering": ("-updated_at", "-id"),
            },
        ),
        migrations.RunPython(create_default_fences, remove_default_fences),
    ]
