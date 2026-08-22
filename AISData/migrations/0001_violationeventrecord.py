from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ViolationEventRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_key", models.CharField(max_length=64, unique=True, verbose_name="事件键")),
                ("fingerprint", models.CharField(db_index=True, max_length=64, verbose_name="事件指纹")),
                ("feature_id", models.CharField(db_index=True, max_length=64, verbose_name="检测类型标识")),
                ("event_type", models.CharField(max_length=100, verbose_name="违法事件类型")),
                ("target_id", models.CharField(blank=True, db_index=True, max_length=255, verbose_name="目标标识")),
                ("target_name", models.CharField(blank=True, max_length=255, verbose_name="目标名称")),
                ("status", models.CharField(blank=True, max_length=100, verbose_name="识别状态")),
                ("risk_level", models.CharField(blank=True, max_length=50, verbose_name="风险等级")),
                ("longitude", models.FloatField(blank=True, null=True, verbose_name="经度")),
                ("latitude", models.FloatField(blank=True, null=True, verbose_name="纬度")),
                ("event_time", models.DateTimeField(blank=True, db_index=True, null=True, verbose_name="事件时间")),
                ("first_detected_at", models.DateTimeField(db_index=True, verbose_name="首次识别时间")),
                ("last_detected_at", models.DateTimeField(db_index=True, verbose_name="最后识别时间")),
                ("occurrence_count", models.PositiveIntegerField(default=1, verbose_name="识别次数")),
                ("details", models.TextField(blank=True, verbose_name="事件详情")),
                ("raw_data", models.JSONField(default=dict, verbose_name="原始识别结果")),
            ],
            options={
                "verbose_name": "违法事件识别记录",
                "verbose_name_plural": "违法事件识别记录",
                "ordering": ("-last_detected_at", "-id"),
                "indexes": [models.Index(fields=["feature_id", "-last_detected_at"], name="ais_violation_feature_time")],
            },
        ),
        migrations.CreateModel(
            name="ViolationAISTrajectoryPoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("mmsi", models.CharField(db_index=True, max_length=32, verbose_name="MMSI")),
                ("observed_at", models.DateTimeField(db_index=True, verbose_name="AIS时间")),
                ("longitude", models.FloatField(verbose_name="经度")),
                ("latitude", models.FloatField(verbose_name="纬度")),
                ("speed", models.FloatField(blank=True, null=True, verbose_name="航速")),
                ("course", models.FloatField(blank=True, null=True, verbose_name="航向")),
                ("raw_data", models.JSONField(default=dict, verbose_name="原始AIS数据")),
                ("event", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="ais_trajectory", to="AISData.violationeventrecord", verbose_name="违法事件")),
            ],
            options={
                "verbose_name": "违法事件AIS轨迹点",
                "verbose_name_plural": "违法事件AIS轨迹点",
                "ordering": ("observed_at", "id"),
                "constraints": [models.UniqueConstraint(fields=("event", "mmsi", "observed_at"), name="unique_violation_ais_point")],
            },
        ),
    ]
