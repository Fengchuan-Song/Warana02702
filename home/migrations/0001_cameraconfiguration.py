from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="CameraConfiguration",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "camera_key",
                    models.SlugField(
                        max_length=64,
                        unique=True,
                        verbose_name="摄像头标识",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=100,
                        verbose_name="摄像头名称",
                    ),
                ),
                ("longitude", models.FloatField(verbose_name="经度")),
                ("latitude", models.FloatField(verbose_name="纬度")),
                (
                    "video_url",
                    models.CharField(
                        max_length=2000,
                        verbose_name="视频地址",
                    ),
                ),
                (
                    "is_visible",
                    models.BooleanField(
                        default=True,
                        verbose_name="地图显示",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        verbose_name="创建时间",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True,
                        verbose_name="更新时间",
                    ),
                ),
            ],
            options={
                "verbose_name": "摄像头配置",
                "verbose_name_plural": "摄像头配置",
                "ordering": ("camera_key",),
            },
        ),
    ]
