import csv
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.management import call_command
from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .detection import (
    DETECTORS,
    detection_cache_key,
    get_detection_result,
    run_detector,
)
from .detection_queue import (
    detection_pending_key,
    detection_queue_key,
    enqueue_all_detections,
    enqueue_detection,
    wait_for_detection_trigger,
)
from .detection_source import (
    ACTIVE_DETECTION_SOURCE_CACHE_KEY,
    internal_detection_cache_key,
)
from .views import cached_detection_result
from .normalization import normalise_ais_name
from .normalization import (
    normalise_dynamic_ais_record,
    normalise_static_ais_record,
)
from .ais_state import (
    load_file_checkpoints,
    merge_ais_state,
    save_file_checkpoint,
)
from .management.commands.ais_worker import (
    Command,
    ais_checkpoint_key,
    discover_ais_csv_files,
)
from .maritime_zones import (
    ENCRYPTED_DATASET_PATH,
    ENCRYPTED_FILE_MAGIC,
    MaritimeZoneDataError,
    get_maritime_zones,
    load_maritime_zones,
    maritime_zone_geojson,
)
from .consumers import (
    AisConsumer,
    PredictAisConsumer,
    ViolationConsumer,
    load_initial_ais_state,
    load_initial_detection_state,
)
from .models import (
    DetectionModelConfiguration,
    ViolationAISTrajectoryPoint,
    ViolationEventRecord,
)
from .trajectory_history import append_ais_history, get_ais_history
from .violation_records import persist_detection_payload


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "backend-detection-tests",
    }
}


@override_settings(
    CACHES=TEST_CACHES,
    HIGH_SPEED_DETECTION={
        "default_speed_limit_knots": 30,
        "minimum_duration_seconds": 300,
        "maximum_gap_seconds": 90,
        "analysis_window_minutes": 30,
        "high_risk_excess_knots": 10,
    },
)
class DetectionModelParameterTests(TestCase):
    feature_id = "detect-highSpeedBoat"
    models_without_configurable_parameters = {
        "detect-ais-off",
        "detect-blackList",
        "detect-illegalFarming",
        "detect-illegalFishing",
        "detect-illegalPollutionDis",
        "detect-illegalSandMining",
        "detect-overload",
        "detect-spoofing",
    }

    def test_collection_exposes_parameter_schema_and_defaults(self):
        response = self.client.get(
            reverse("detection_model_parameter_collection")
        )

        self.assertEqual(response.status_code, 200)
        configurations = {
            item["feature_id"]: item
            for item in response.json()["results"]
        }
        configuration = configurations[self.feature_id]
        self.assertEqual(
            configuration["parameters"]["default_speed_limit_knots"],
            30,
        )
        self.assertFalse(configuration["is_customized"])

    def test_collection_includes_models_without_configurable_parameters(self):
        response = self.client.get(
            reverse("detection_model_parameter_collection")
        )

        configurations = {
            item["feature_id"]: item
            for item in response.json()["results"]
        }
        self.assertTrue(
            self.models_without_configurable_parameters.issubset(configurations)
        )
        for feature_id in self.models_without_configurable_parameters:
            configuration = configurations[feature_id]
            self.assertEqual(configuration["fields"], [])
            self.assertEqual(configuration["parameters"], {})
            self.assertFalse(configuration["is_configurable"])
            self.assertFalse(configuration["is_customized"])

    def test_model_without_parameters_rejects_update(self):
        feature_id = "detect-overload"
        response = self.client.put(
            reverse(
                "detection_model_parameter_detail",
                args=[feature_id],
            ),
            data=json.dumps({"parameters": {}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "该模型暂无可配置参数")
        self.assertFalse(
            DetectionModelConfiguration.objects.filter(
                feature_id=feature_id
            ).exists()
        )

    def test_collection_exposes_illegal_anchored_parameters(self):
        response = self.client.get(
            reverse("detection_model_parameter_collection")
        )

        configurations = {
            item["feature_id"]: item
            for item in response.json()["results"]
        }
        configuration = configurations["detect-illegalAnchored"]
        self.assertEqual(
            configuration["parameters"],
            {
                "max_speed_knots": 0.5,
                "min_duration_seconds": 300,
                "min_observations": 3,
                "max_drift_metres": 250,
                "history_window_seconds": 1800,
                "max_gap_seconds": 300,
                "min_heading_observations": 2,
                "anchor_swing_heading_degrees": 45,
                "min_anchor_status_ratio": 0.6,
            },
        )

    def test_collection_exposes_low_speed_stationary_parameters(self):
        response = self.client.get(
            reverse("detection_model_parameter_collection")
        )

        configurations = {
            item["feature_id"]: item
            for item in response.json()["results"]
        }
        configuration = configurations["detect-lowSpeedBoat"]
        field_keys = {field["key"] for field in configuration["fields"]}

        self.assertTrue(
            {
                "stationary_max_speed_knots",
                "stationary_max_drift_metres",
                "stationary_minimum_duration_seconds",
            }.issubset(field_keys)
        )
        self.assertEqual(
            configuration["parameters"]["stationary_max_speed_knots"],
            0.5,
        )
        self.assertEqual(
            configuration["parameters"]["stationary_max_drift_metres"],
            50.0,
        )
        self.assertEqual(
            configuration["parameters"]
            ["stationary_minimum_duration_seconds"],
            300,
        )

    def test_collection_exposes_wandering_quality_parameters(self):
        response = self.client.get(
            reverse("detection_model_parameter_collection")
        )
        configurations = {
            item["feature_id"]: item
            for item in response.json()["results"]
        }
        configuration = configurations["detect-abnormalWandering"]
        fields = {field["key"]: field for field in configuration["fields"]}

        self.assertTrue(
            {
                "revisit_enabled",
                "min_leg_distance_metres",
                "max_valid_speed_knots",
                "max_jump_speed_knots",
                "min_jump_distance_metres",
                "max_gap_minutes",
                "min_turn_interval_seconds",
                "monitored_only",
            }.issubset(fields)
        )
        self.assertEqual(fields["revisit_enabled"]["type"], "boolean")
        self.assertIs(configuration["parameters"]["revisit_enabled"], True)
        self.assertEqual(fields["monitored_only"]["type"], "boolean")
        self.assertIs(configuration["parameters"]["monitored_only"], True)
        for key in (
            "max_range_metres",
            "min_path_distance_metres",
            "min_leg_distance_metres",
            "grid_size_metres",
            "min_jump_distance_metres",
        ):
            self.assertEqual(fields[key]["step"], 1)
        self.assertEqual(
            configuration["parameters"]["min_path_distance_metres"],
            300,
        )

    def test_collection_exposes_abnormal_berthing_parameters(self):
        response = self.client.get(
            reverse("detection_model_parameter_collection")
        )
        configurations = {
            item["feature_id"]: item
            for item in response.json()["results"]
        }
        configuration = configurations["detect-abnormalStaying"]
        field_keys = {field["key"] for field in configuration["fields"]}

        self.assertTrue(
            {
                "max_speed_knots",
                "exit_speed_knots",
                "distance_threshold_metres",
                "position_exit_radius_metres",
                "min_duration_minutes",
                "max_gap_minutes",
                "near_shore_distance_metres",
                "max_heading_change_degrees",
                "min_heading_observations",
                "anchor_swing_heading_degrees",
                "legal_max_duration_minutes",
            }.issubset(field_keys)
        )

    def test_abnormal_berthing_parameters_apply_at_runtime(self):
        from AbnormalParking.utils import get_parking_config
        from AISData.model_parameters import apply_runtime_parameter_override

        parameters = {
            "max_speed_knots": 0.4,
            "exit_speed_knots": 0.9,
            "distance_threshold_metres": 40.0,
            "position_exit_radius_metres": 90.0,
            "max_gap_minutes": 12.0,
            "near_shore_distance_metres": 80.0,
            "max_heading_change_degrees": 20.0,
            "min_heading_observations": 3,
            "anchor_swing_heading_degrees": 50.0,
            "legal_max_duration_minutes": 120.0,
        }
        response = self.client.put(
            reverse(
                "detection_model_parameter_detail",
                args=["detect-abnormalStaying"],
            ),
            data=json.dumps({"parameters": parameters}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        with apply_runtime_parameter_override("detect-abnormalStaying"):
            config = get_parking_config()
        for key, value in parameters.items():
            self.assertEqual(config[key], value)

    def test_abnormal_berthing_exit_thresholds_are_validated(self):
        url = reverse(
            "detection_model_parameter_detail",
            args=["detect-abnormalStaying"],
        )
        response = self.client.put(
            url,
            data=json.dumps(
                {
                    "parameters": {
                        "max_speed_knots": 0.5,
                        "exit_speed_knots": 0.4,
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("退出航速", response.json()["message"])

        response = self.client.put(
            url,
            data=json.dumps(
                {
                    "parameters": {
                        "distance_threshold_metres": 50,
                        "position_exit_radius_metres": 40,
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("退出半径", response.json()["message"])

    def test_wandering_quality_parameters_apply_at_runtime(self):
        from AbnormalWandering.utils import get_wandering_config
        from AISData.model_parameters import apply_runtime_parameter_override

        parameters = {
            "revisit_enabled": False,
            "min_leg_distance_metres": 35.0,
            "max_valid_speed_knots": 90.0,
            "max_jump_speed_knots": 70.0,
            "min_jump_distance_metres": 650.0,
            "max_gap_minutes": 8.0,
            "min_turn_interval_seconds": 45.0,
            "monitored_only": False,
        }
        response = self.client.put(
            reverse(
                "detection_model_parameter_detail",
                args=["detect-abnormalWandering"],
            ),
            data=json.dumps({"parameters": parameters}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        with apply_runtime_parameter_override("detect-abnormalWandering"):
            config = get_wandering_config()
        for key, value in parameters.items():
            self.assertEqual(config[key], value)

    def test_wandering_revisit_switch_requires_boolean(self):
        response = self.client.put(
            reverse(
                "detection_model_parameter_detail",
                args=["detect-abnormalWandering"],
            ),
            data=json.dumps({"parameters": {"revisit_enabled": 0}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("必须是布尔值", response.json()["message"])

    def test_model_parameter_page_renders_boolean_control_support(self):
        response = self.client.get(reverse("model_parameter_page"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "field.type === 'boolean'")
        self.assertContains(response, "new Option('启用', 'true')")

    def test_low_speed_stationary_parameters_apply_at_runtime(self):
        from LowSpeed.utils import get_low_speed_config
        from AISData.model_parameters import apply_runtime_parameter_override

        parameters = {
            "stationary_max_speed_knots": 0.4,
            "stationary_max_drift_metres": 75.0,
            "stationary_minimum_duration_seconds": 420,
        }
        response = self.client.put(
            reverse(
                "detection_model_parameter_detail",
                args=["detect-lowSpeedBoat"],
            ),
            data=json.dumps({"parameters": parameters}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            DetectionModelConfiguration.objects.get(
                feature_id="detect-lowSpeedBoat"
            ).parameters,
            parameters,
        )
        with apply_runtime_parameter_override("detect-lowSpeedBoat"):
            config = get_low_speed_config()
        self.assertEqual(config["stationary_max_speed_knots"], 0.4)
        self.assertEqual(config["stationary_max_drift_metres"], 75.0)
        self.assertEqual(
            config["stationary_minimum_duration_seconds"],
            420,
        )

    def test_illegal_anchored_history_window_covers_duration(self):
        response = self.client.put(
            reverse(
                "detection_model_parameter_detail",
                args=["detect-illegalAnchored"],
            ),
            data=json.dumps(
                {
                    "parameters": {
                        "min_duration_seconds": 600,
                        "history_window_seconds": 300,
                    }
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("历史时间窗口不能短于", response.json()["message"])

    def test_update_and_reset_model_parameters(self):
        url = reverse(
            "detection_model_parameter_detail",
            args=[self.feature_id],
        )
        parameters = {
            "default_speed_limit_knots": 36.5,
            "minimum_duration_seconds": 180,
            "maximum_gap_seconds": 60,
            "analysis_window_minutes": 20,
            "high_risk_excess_knots": 8,
        }

        response = self.client.put(
            url,
            data=json.dumps({"parameters": parameters}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            DetectionModelConfiguration.objects.get(
                feature_id=self.feature_id
            ).parameters,
            parameters,
        )
        self.assertTrue(response.json()["configuration"]["is_customized"])

        response = self.client.delete(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DetectionModelConfiguration.objects.filter(
                feature_id=self.feature_id
            ).exists()
        )
        self.assertEqual(
            response.json()["configuration"]["parameters"]
            ["default_speed_limit_knots"],
            30,
        )

    def test_invalid_parameter_value_is_rejected(self):
        response = self.client.put(
            reverse(
                "detection_model_parameter_detail",
                args=[self.feature_id],
            ),
            data=json.dumps(
                {"parameters": {"default_speed_limit_knots": 200}}
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("不能大于", response.json()["message"])

    def test_saved_parameters_are_applied_for_one_detection_run(self):
        from django.conf import settings

        DetectionModelConfiguration.objects.create(
            feature_id=self.feature_id,
            parameters={"default_speed_limit_knots": 36.5},
        )
        observed_limits = []

        def detector(request):
            observed_limits.append(
                settings.HIGH_SPEED_DETECTION["default_speed_limit_knots"]
            )
            return JsonResponse({"success": True, "count": 0, "results": []})

        with patch.dict(
            DETECTORS,
            {self.feature_id: detector},
            clear=True,
        ):
            result = run_detector(self.feature_id, ship_list=[])

        self.assertTrue(result["success"])
        self.assertEqual(observed_limits, [36.5])
        self.assertEqual(
            settings.HIGH_SPEED_DETECTION["default_speed_limit_knots"],
            30,
        )


class MaritimeZoneDatasetTests(SimpleTestCase):
    def test_dataset_is_stored_only_as_authenticated_ciphertext(self):
        plaintext_path = ENCRYPTED_DATASET_PATH.with_suffix("")

        self.assertFalse(plaintext_path.exists())
        encrypted_payload = ENCRYPTED_DATASET_PATH.read_bytes()
        self.assertTrue(encrypted_payload.startswith(ENCRYPTED_FILE_MAGIC))
        self.assertNotIn(b"AOI_Name", encrypted_payload)

    def test_bundled_dataset_has_expected_regions_and_types(self):
        zones = load_maritime_zones()

        self.assertEqual(len(zones), 137)
        self.assertEqual(len({zone.source_id for zone in zones}), 137)
        self.assertEqual(
            sum(zone.region == "广东" for zone in zones),
            115,
        )
        self.assertEqual(
            sum(zone.region == "香港" for zone in zones),
            20,
        )
        self.assertEqual(
            sum(zone.region == "澳门" for zone in zones),
            2,
        )
        self.assertEqual(len(get_maritime_zones({"PRT"})), 52)
        self.assertEqual(len(get_maritime_zones({"ANC"})), 43)

    def test_geojson_api_exposes_and_filters_zones(self):
        response = self.client.get(
            reverse("maritime_zone_collection"),
            {"type": "PRT,ANC", "locode": "HKHKG,MOMFM"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(payload["count"], 19)
        self.assertEqual(payload["region_counts"], {"澳门": 2, "香港": 17})
        self.assertTrue(
            all(
                feature["geometry"]["type"] == "Polygon"
                for feature in payload["features"]
            )
        )

    def test_geojson_summary_and_unknown_type_validation(self):
        payload = maritime_zone_geojson(zone_types={"ANC"})

        self.assertEqual(payload["count"], 43)
        self.assertEqual(payload["type_counts"], {"ANC": 43})
        response = self.client.get(
            reverse("maritime_zone_collection"),
            {"type": "UNKNOWN"},
        )
        self.assertEqual(response.status_code, 400)

    @patch(
        "AISData.views.maritime_zone_geojson",
        side_effect=MaritimeZoneDataError("sensitive internal detail"),
    )
    def test_geojson_api_hides_encryption_failures(self, _mock_geojson):
        response = self.client.get(reverse("maritime_zone_collection"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                "success": False,
                "message": "海事区域加密数据暂时不可用",
            },
        )

    def test_home_page_exposes_maritime_zone_layers(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="info-maritime-ports"')
        self.assertContains(response, 'id="info-maritime-anchorages"')
        self.assertContains(response, reverse("maritime_zone_collection"))


@override_settings(CACHES=TEST_CACHES)
class RealtimeConsumerSeparationTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_dedicated_websockets_replay_their_own_cached_state(self):
        from django.core.cache import cache

        ais_snapshot = [{"mmsi": "123456789"}]
        detection_payload = {
            "success": True,
            "feature_id": "detect-highSpeedBoat",
            "count": 0,
            "results": [],
        }
        cache.set("latest_ais_data_raw", ais_snapshot, timeout=300)
        cache.set(
            detection_cache_key("detect-highSpeedBoat"),
            detection_payload,
            timeout=300,
        )

        replayed_ais = load_initial_ais_state()
        replayed_results = load_initial_detection_state()

        self.assertEqual(
            replayed_ais,
            [{"mmsi": "123456789", "name": "未知目标"}],
        )
        self.assertEqual(
            replayed_results["detect-highSpeedBoat"],
            detection_payload,
        )

    def test_violation_updates_use_a_dedicated_route_and_group(self):
        from WanAna02702.routing import (
            default_ais_consumer,
            websocket_urlpatterns,
        )

        routes = {str(route.pattern) for route in websocket_urlpatterns}

        self.assertIn("ws/violations/", routes)
        self.assertIn("ws/ais-predict/", routes)
        self.assertIs(default_ais_consumer(), AisConsumer)
        with self.settings(AIS_DEFAULT_SOURCE="predict"):
            self.assertIs(default_ais_consumer(), PredictAisConsumer)
        self.assertEqual(PredictAisConsumer.AIS_NAMESPACE, "predict")
        self.assertEqual(ViolationConsumer.GROUP_NAME, "violation_updates")
        self.assertFalse(hasattr(AisConsumer, "send_detection_update"))

    @override_settings(
        CHANNEL_LAYERS={
            "default": {
                "BACKEND": "channels.layers.InMemoryChannelLayer",
            }
        }
    )
    async def test_ais_socket_replays_snapshot_then_accepts_delta(self):
        from asgiref.sync import sync_to_async
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator
        from django.core.cache import cache

        await sync_to_async(cache.set)(
            "latest_ais_data_raw",
            [{"mmsi": "123456789", "name": "测试船"}],
            timeout=300,
        )
        socket = WebsocketCommunicator(AisConsumer.as_asgi(), "/ws/ais/")
        try:
            self.assertTrue((await socket.connect())[0])
            self.assertEqual(
                await socket.receive_json_from(),
                {
                    "type": "ais_snapshot",
                    "data": [{"mmsi": "123456789", "name": "测试船"}],
                    "version": 0,
                },
            )
            await get_channel_layer().group_send(
                AisConsumer.AIS_GROUP_NAME,
                {
                    "type": "send_ais_delta",
                    "data": {
                        "upserts": [{"mmsi": "987654321"}],
                        "removes": ["123456789"],
                        "server_time": "2026-08-22T00:00:00+00:00",
                        "version": 1,
                    },
                },
            )
            self.assertEqual(
                await socket.receive_json_from(),
                {
                    "type": "ais_delta",
                    "data": {
                        "upserts": [
                            {"mmsi": "987654321", "name": "未知目标"}
                        ],
                        "removes": ["123456789"],
                        "server_time": "2026-08-22T00:00:00+00:00",
                        "version": 1,
                    },
                },
            )
            await get_channel_layer().group_send(
                AisConsumer.AIS_GROUP_NAME,
                {
                    "type": "send_ais_snapshot",
                    "data": [],
                    "version": 2,
                },
            )
            self.assertEqual(
                await socket.receive_json_from(),
                {
                    "type": "ais_snapshot",
                    "data": [],
                    "version": 2,
                },
            )
            await socket.send_json_to({"type": "ais_resync"})
            self.assertEqual(
                await socket.receive_json_from(),
                {
                    "type": "ais_snapshot",
                    "data": [{"mmsi": "123456789", "name": "测试船"}],
                    "version": 0,
                },
            )
        finally:
            await socket.disconnect()

    @override_settings(
        CHANNEL_LAYERS={
            "default": {
                "BACKEND": "channels.layers.InMemoryChannelLayer",
            }
        }
    )
    async def test_violation_broadcast_only_reaches_violation_socket(self):
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator

        ais_socket = WebsocketCommunicator(
            AisConsumer.as_asgi(),
            "/ws/ais/",
        )
        violation_socket = WebsocketCommunicator(
            ViolationConsumer.as_asgi(),
            "/ws/violations/",
        )
        try:
            self.assertTrue((await ais_socket.connect())[0])
            self.assertTrue((await violation_socket.connect())[0])

            payload = {
                "success": True,
                "feature_id": "detect-highSpeedBoat",
                "count": 1,
                "results": [{"mmsi": "123456789"}],
            }
            await get_channel_layer().group_send(
                ViolationConsumer.GROUP_NAME,
                {
                    "type": "send_detection_update",
                    "results": {"detect-highSpeedBoat": payload},
                },
            )

            self.assertEqual(
                await violation_socket.receive_json_from(),
                {
                    "type": "detection_update",
                    "data": {"detect-highSpeedBoat": payload},
                },
            )
            self.assertTrue(await ais_socket.receive_nothing(timeout=0.05))
        finally:
            await ais_socket.disconnect()
            await violation_socket.disconnect()

    @override_settings(
        CHANNEL_LAYERS={
            "default": {
                "BACKEND": "channels.layers.InMemoryChannelLayer",
            }
        }
    )
    async def test_violation_socket_filters_empty_detection_results(self):
        from asgiref.sync import sync_to_async
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator
        from django.core.cache import cache

        empty_payload = {
            "success": True,
            "feature_id": "detect-overload",
            "count": 0,
            "results": [],
        }
        await sync_to_async(cache.set, thread_sensitive=True)(
            detection_cache_key("detect-overload"),
            empty_payload,
            timeout=300,
        )

        violation_socket = WebsocketCommunicator(
            ViolationConsumer.as_asgi(),
            "/ws/violations/",
        )
        try:
            self.assertTrue((await violation_socket.connect())[0])
            self.assertTrue(await violation_socket.receive_nothing(timeout=0.05))

            await get_channel_layer().group_send(
                ViolationConsumer.GROUP_NAME,
                {
                    "type": "send_detection_update",
                    "results": {"detect-overload": empty_payload},
                },
            )
            self.assertTrue(await violation_socket.receive_nothing(timeout=0.05))

            violation_payload = {
                "success": True,
                "feature_id": "detect-highSpeedBoat",
                "count": 1,
                "results": [{"mmsi": "123456789"}],
            }
            await get_channel_layer().group_send(
                ViolationConsumer.GROUP_NAME,
                {
                    "type": "send_detection_update",
                    "results": {
                        "detect-overload": empty_payload,
                        "detect-highSpeedBoat": violation_payload,
                    },
                },
            )
            self.assertEqual(
                await violation_socket.receive_json_from(),
                {
                    "type": "detection_update",
                    "data": {"detect-highSpeedBoat": violation_payload},
                },
            )
        finally:
            await violation_socket.disconnect()


@override_settings(CACHES=TEST_CACHES, CACHE_TTL=300)
class BackendDetectionPipelineTests(TestCase):
    def setUp(self):
        cache.clear()
        self.request = RequestFactory().get(
            "/AISData/detection-results/detect-test/"
        )

    @staticmethod
    def successful_detector(request):
        return JsonResponse(
            {
                "success": True,
                "timestamp": "2020-01-01T00:00:00Z",
                "count": 1,
                "results": [{"mmsi": "123456789"}],
                "message": "检测成功",
            }
        )

    def test_backend_runner_executes_and_caches_detector(self):
        with patch.dict(
            DETECTORS,
            {"detect-test": self.successful_detector},
            clear=True,
        ):
            result = run_detector("detect-test")
            cached = get_detection_result("detect-test")

        self.assertEqual(result["count"], 1)
        self.assertEqual(cached["results"][0]["mmsi"], "123456789")
        self.assertIs(cached["results"][0]["is_new"], True)
        self.assertEqual(
            len(cached["results"][0]["prediction_id"]), 32
        )
        self.assertNotIn("event_id", cached["results"][0])
        self.assertIsNotNone(cached["computed_at"])

    def test_backend_runner_marks_continuing_and_reappearing_events(self):
        payloads = [
            self.successful_detector(None),
            self.successful_detector(None),
            JsonResponse({"success": True, "count": 0, "results": []}),
            self.successful_detector(None),
        ]

        with patch.dict(
            DETECTORS,
            {"detect-test": lambda request: payloads.pop(0)},
            clear=True,
        ), patch("AISData.violation_records.persist_detection_payload"):
            first = run_detector("detect-test")
            continuing = run_detector("detect-test")
            run_detector("detect-test")
            reappearing = run_detector("detect-test")

        self.assertIs(first["results"][0]["is_new"], True)
        self.assertIs(continuing["results"][0]["is_new"], False)
        self.assertIs(reappearing["results"][0]["is_new"], True)
        self.assertEqual(
            first["results"][0]["prediction_id"],
            continuing["results"][0]["prediction_id"],
        )
        self.assertNotEqual(
            continuing["results"][0]["prediction_id"],
            reappearing["results"][0]["prediction_id"],
        )

    def test_backend_runner_preserves_detector_event_state(self):
        calls = 0

        def stateful_detector(request):
            nonlocal calls
            calls += 1
            return JsonResponse(
                {
                    "success": True,
                    "count": 1,
                    "results": [
                        {
                            "mmsi": "123456789",
                            "event_id": "detector-event-1",
                            "is_new": calls == 1,
                        }
                    ],
                }
            )

        with patch.dict(
            DETECTORS,
            {"detect-test": stateful_detector},
            clear=True,
        ), patch("AISData.violation_records.persist_detection_payload"):
            first = run_detector("detect-test")
            result = run_detector("detect-test")

        self.assertIs(first["results"][0]["is_new"], True)
        self.assertIs(result["results"][0]["is_new"], False)
        self.assertEqual(
            first["results"][0]["prediction_id"],
            result["results"][0]["prediction_id"],
        )
        self.assertNotEqual(
            result["results"][0]["prediction_id"],
            result["results"][0]["event_id"],
        )

    def test_backend_runner_passes_one_frozen_snapshot(self):
        snapshot = [{"mmsi": "123456789", "timestamp": "snapshot-1"}]

        def snapshot_detector(request):
            self.assertIsNot(request.ais_ship_list, snapshot)
            self.assertEqual(
                request.ais_ship_list[0]["name"],
                "未知目标",
            )
            return JsonResponse(
                {
                    "success": True,
                    "count": 0,
                    "results": [],
                }
            )

        with patch.dict(
            DETECTORS,
            {"detect-snapshot": snapshot_detector},
            clear=True,
        ):
            result = run_detector(
                "detect-snapshot",
                ship_list=snapshot,
            )

        self.assertTrue(result["success"])

    def test_independent_detector_invokes_result_callback(self):
        callbacks = []

        with patch.dict(
            DETECTORS,
            {"detect-test": self.successful_detector},
            clear=True,
        ):
            result = run_detector(
                "detect-test",
                ship_list=[{"mmsi": "123456789"}],
                on_result=lambda feature_id, payload: callbacks.append(
                    feature_id
                ),
            )

        self.assertTrue(result["success"])
        self.assertEqual(callbacks, ["detect-test"])

    def test_predict_context_is_available_without_changing_result_payload(self):
        observed_context = []

        def context_detector(request):
            observed_context.append(request.ais_context)
            return self.successful_detector(request)

        cache.set(
            ACTIVE_DETECTION_SOURCE_CACHE_KEY,
            "predict:simulation-1",
            timeout=None,
        )
        with patch.dict(
            DETECTORS,
            {"detect-test": context_detector},
            clear=True,
        ):
            result = run_detector(
                "detect-test",
                ship_list=[{"mmsi": "123456789"}],
                detection_context={
                    "namespace": "predict",
                    "simulation_id": "simulation-1",
                },
            )

        self.assertEqual(
            observed_context,
            [
                {
                    "namespace": "predict",
                    "simulation_id": "simulation-1",
                }
            ],
        )
        self.assertNotIn("namespace", result)
        self.assertNotIn("simulation_id", result)
        self.assertEqual(get_detection_result("detect-test"), result)

    def test_runner_passes_predict_namespace_to_trajectory_persistence(self):
        cache.set(
            ACTIVE_DETECTION_SOURCE_CACHE_KEY,
            "predict:simulation-1",
            timeout=None,
        )
        with patch.dict(
            DETECTORS,
            {"detect-test": self.successful_detector},
            clear=True,
        ):
            with patch(
                "AISData.violation_records.persist_detection_payload"
            ) as persist:
                run_detector(
                    "detect-test",
                    ship_list=[{"mmsi": "123456789"}],
                    detection_context={
                        "namespace": "predict",
                        "simulation_id": "simulation-1",
                    },
                )

        self.assertEqual(
            persist.call_args.kwargs["trajectory_namespace"],
            "predict",
        )

    def test_runner_keeps_operational_trajectory_namespace_unprefixed(self):
        with patch.dict(
            DETECTORS,
            {"detect-test": self.successful_detector},
            clear=True,
        ):
            with patch(
                "AISData.violation_records.persist_detection_payload"
            ) as persist:
                run_detector(
                    "detect-test",
                    ship_list=[{"mmsi": "123456789"}],
                )

        self.assertIsNone(
            persist.call_args.kwargs["trajectory_namespace"]
        )

    def test_inactive_source_cannot_publish_through_compatibility_cache(self):
        callbacks = []
        cache.set(
            ACTIVE_DETECTION_SOURCE_CACHE_KEY,
            "operational",
            timeout=None,
        )
        with patch.dict(
            DETECTORS,
            {"detect-test": self.successful_detector},
            clear=True,
        ):
            result = run_detector(
                "detect-test",
                ship_list=[{"mmsi": "123456789"}],
                on_result=lambda feature_id, payload: callbacks.append(
                    feature_id
                ),
                detection_context={
                    "namespace": "predict",
                    "simulation_id": "simulation-2",
                },
            )

        self.assertEqual(callbacks, [])
        self.assertIsNone(cache.get(detection_cache_key("detect-test")))
        self.assertEqual(
            cache.get(
                internal_detection_cache_key(
                    "detect-test",
                    "predict",
                    "simulation-2",
                )
            ),
            result,
        )

    def test_detector_failure_is_cached_as_its_own_result(self):
        def failing_detector(request):
            raise RuntimeError("test failure")

        with patch.dict(
            DETECTORS,
            {"detect-failing": failing_detector},
            clear=True,
        ):
            result = run_detector("detect-failing")
            cached = get_detection_result("detect-failing")

        self.assertFalse(result["success"])
        self.assertFalse(cached["success"])

    def test_public_endpoint_only_reads_cached_result(self):
        detector = Mock()
        cached_payload = {
            "success": True,
            "feature_id": "detect-test",
            "count": 2,
            "results": [],
            "message": "cached",
        }

        with patch.dict(DETECTORS, {"detect-test": detector}, clear=True):
            from django.core.cache import cache

            cache.set(
                detection_cache_key("detect-test"),
                cached_payload,
                timeout=300,
            )
            response = cached_detection_result(
                self.request,
                feature_id="detect-test",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["count"], 2)
        detector.assert_not_called()

    def test_legacy_high_speed_url_is_now_cache_only(self):
        match = resolve("/HighSpeedBoat/detectHighSpeedBoat/")

        self.assertIs(match.func, cached_detection_result)
        self.assertEqual(
            match.kwargs["feature_id"],
            "detect-highSpeedBoat",
        )

    def test_worker_normalises_naive_csv_timestamp(self):
        converted = Command().convert_row(
            {
                "timestamp": "2020-12-27 08:00:37",
                "MMSI": "123456789",
                "Name": "测试船",
                "longitude": "113.7",
                "latitude": "22.4",
                "course": "90",
                "speed": "12.5",
                "status": "1.0",
                "at_dock": "False",
                "matchedPortName": "深圳港",
            }
        )

        timestamp = parse_datetime(converted["timestamp"])
        self.assertIsNotNone(timestamp)
        self.assertTrue(timezone.is_aware(timestamp))
        self.assertEqual(converted["nav_status"], 1)
        self.assertFalse(converted["at_dock"])
        self.assertEqual(converted["matched_port_name"], "深圳港")
        self.assertEqual(converted["name"], "测试船")
        self.assertEqual(converted["mmsi"], "123456789")
        self.assertIn("ship_type", converted)
        self.assertIn("draught", converted)
        self.assertIn("length", converted)
        self.assertIn("width", converted)

    def test_worker_discovers_all_nested_csv_files_with_distinct_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            first = root / "same.csv"
            second = nested / "same.csv"
            ignored = nested / "notes.txt"
            first.write_text("header\n", encoding="utf-8")
            second.write_text("header\n", encoding="utf-8")
            ignored.write_text("not AIS", encoding="utf-8")

            discovered = discover_ais_csv_files(root)

        self.assertEqual(discovered, [second, first])
        self.assertEqual(ais_checkpoint_key(first, root), "same.csv")
        self.assertEqual(ais_checkpoint_key(second, root), "nested/same.csv")

    def test_missing_ais_names_use_one_system_wide_label(self):
        for value in (
            None,
            "",
            "  ",
            "NaN",
            "none",
            "null",
            "UNKNOWN",
            "未知船舶",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalise_ais_name(value), "未知目标")

        self.assertEqual(normalise_ais_name("  测试船  "), "测试船")

    def test_worker_normalises_missing_csv_ship_name(self):
        converted = Command().convert_row(
            {
                "timestamp": "2020-12-27 08:00:37",
                "MMSI": "123456789",
                "Name": "NaN",
                "longitude": "113.7",
                "latitude": "22.4",
                "course": "90",
                "speed": "12.5",
            }
        )

        self.assertEqual(converted["name"], "未知目标")

    def test_worker_keeps_every_valid_row_for_trajectory_history(self):
        base = {
            "Name": "测试船",
            "course": "90",
            "speed": "12.5",
        }
        snapshot, history = Command().convert_rows(
            [
                {
                    **base,
                    "timestamp": "2020-12-27 08:00:10",
                    "MMSI": "123456789",
                    "longitude": "113.7000",
                    "latitude": "22.4000",
                },
                {
                    **base,
                    "timestamp": "2020-12-27 08:00:20",
                    "MMSI": "123456789",
                    "longitude": "113.7010",
                    "latitude": "22.4010",
                },
                {
                    **base,
                    "timestamp": "2020-12-27 08:00:15",
                    "MMSI": "987654321",
                    "longitude": "113.8000",
                    "latitude": "22.5000",
                },
            ]
        )

        self.assertEqual(len(snapshot), 2)
        self.assertEqual(len(history), 3)
        latest = next(
            point for point in snapshot if point["mmsi"] == "123456789"
        )
        self.assertEqual(latest["lon"], 113.701)

    def test_trajectory_history_retains_all_rows_inside_source_time_window(self):
        append_ais_history(
            [
                {
                    "mmsi": "123456789",
                    "timestamp": "2026-08-03T03:29:59Z",
                    "lon": 122.30,
                    "lat": 29.70,
                },
                {
                    "mmsi": "123456789",
                    "timestamp": "2026-08-03T03:30:00Z",
                    "lon": 122.31,
                    "lat": 29.70,
                },
                {
                    "mmsi": "123456789",
                    "timestamp": "2026-08-03T04:00:00Z",
                    "lon": 122.40,
                    "lat": 29.70,
                },
            ]
        )

        history = get_ais_history(
            ["123456789"],
            end_at="2026-08-03T04:00:00Z",
        )

        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["lon"], 122.31)
        self.assertEqual(history[1]["lon"], 122.40)

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_detection_trigger_is_enqueued_once(self, get_connection):
        connection = get_connection.return_value
        connection.set.return_value = True

        with patch.dict(DETECTORS, {"detect-test": "unused"}, clear=True):
            queued = enqueue_detection("detect-test")

        self.assertTrue(queued)
        connection.set.assert_called_once()
        connection.rpush.assert_called_once_with(
            detection_queue_key("detect-test"),
            "latest",
        )

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_detection_trigger_is_coalesced_while_pending(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        connection.set.return_value = False

        with patch.dict(DETECTORS, {"detect-test": "unused"}, clear=True):
            queued = enqueue_detection("detect-test")

        self.assertFalse(queued)
        connection.rpush.assert_not_called()

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_all_models_receive_independent_queue_signals(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        connection.set.side_effect = [True, False]

        with patch.dict(
            DETECTORS,
            {
                "detect-fast": "unused",
                "detect-slow": "unused",
            },
            clear=True,
        ):
            statuses = enqueue_all_detections()

        self.assertEqual(
            statuses,
            {
                "detect-fast": True,
                "detect-slow": False,
            },
        )
        connection.rpush.assert_called_once_with(
            detection_queue_key("detect-fast"),
            "latest",
        )

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_consuming_trigger_allows_one_follow_up(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        connection.blpop.return_value = (
            detection_queue_key("detect-test").encode(),
            b"latest",
        )

        with patch.dict(DETECTORS, {"detect-test": "unused"}, clear=True):
            received = wait_for_detection_trigger(
                "detect-test",
                timeout=1,
            )

        self.assertTrue(received)
        connection.delete.assert_called_once_with(
            detection_pending_key("detect-test")
        )

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_crossing_boundary_queues_each_snapshot_payload(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        snapshot = [
            {
                "mmsi": "123456789",
                "timestamp": "2026-07-24T08:00:00+00:00",
            }
        ]

        queued = enqueue_detection(
            "detect-CrossingBoundary",
            ship_list=snapshot,
        )

        self.assertTrue(queued)
        queued_payload = connection.rpush.call_args.args[1]
        self.assertEqual(json.loads(queued_payload), snapshot)
        connection.ltrim.assert_called_once()
        connection.set.assert_not_called()

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_crossing_boundary_prefers_incremental_points_over_snapshot(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        snapshot = [{"mmsi": "111111111", "timestamp": "snapshot"}]
        incremental = [
            {"mmsi": "123456789", "timestamp": "incremental"}
        ]

        queued = enqueue_detection(
            "detect-CrossingBoundary",
            ship_list=snapshot,
            incremental_ship_list=incremental,
        )

        self.assertTrue(queued)
        queued_payload = connection.rpush.call_args.args[1]
        self.assertEqual(json.loads(queued_payload), incremental)

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_trajectory_models_prefer_incremental_points_over_snapshot(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        snapshot = [{"mmsi": "111111111", "timestamp": "snapshot"}]
        incremental = [
            {"mmsi": "123456789", "timestamp": "incremental"}
        ]

        for feature_id in (
            "detect-smuggling",
            "detect-abnormalWandering",
            "detect-deviation",
            "detect-highSpeedBoat",
            "detect-lowSpeedBoat",
        ):
            with self.subTest(feature_id=feature_id):
                connection.reset_mock()
                queued = enqueue_detection(
                    feature_id,
                    ship_list=snapshot,
                    incremental_ship_list=incremental,
                )
                self.assertTrue(queued)
                queued_payload = connection.rpush.call_args.args[1]
                self.assertEqual(json.loads(queued_payload), incremental)

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_crossing_boundary_worker_receives_queued_snapshot(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        snapshot = [
            {
                "mmsi": "123456789",
                "timestamp": "2026-07-24T08:00:00+00:00",
            }
        ]
        connection.blpop.return_value = (
            detection_queue_key("detect-CrossingBoundary").encode(),
            json.dumps(snapshot).encode(),
        )
        connection.pipeline.return_value.execute.return_value = ([], True)

        received = wait_for_detection_trigger(
            "detect-CrossingBoundary",
            timeout=1,
        )

        self.assertEqual(received, snapshot)
        connection.delete.assert_not_called()

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_crossing_boundary_worker_batches_incremental_queue_items(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        first = [{"mmsi": "123456789", "timestamp": "first"}]
        second = [{"mmsi": "123456789", "timestamp": "second"}]
        third = [{"mmsi": "987654321", "timestamp": "third"}]
        connection.blpop.return_value = (
            detection_queue_key("detect-CrossingBoundary").encode(),
            json.dumps(first).encode(),
        )
        connection.pipeline.return_value.execute.return_value = (
            [json.dumps(second).encode(), json.dumps(third).encode()],
            True,
        )

        received = wait_for_detection_trigger(
            "detect-CrossingBoundary",
            timeout=1,
        )

        self.assertEqual(received, first + second + third)
        pipeline = connection.pipeline.return_value
        pipeline.lrange.assert_called_once_with(
            detection_queue_key("detect-CrossingBoundary"),
            0,
            98,
        )
        pipeline.ltrim.assert_called_once_with(
            detection_queue_key("detect-CrossingBoundary"),
            99,
            -1,
        )

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_predict_source_queues_every_model_snapshot(self, get_connection):
        connection = get_connection.return_value
        snapshot = [
            {
                "mmsi": "123456789",
                "timestamp": "2026-07-24T08:00:00+00:00",
            }
        ]

        with patch.dict(DETECTORS, {"detect-test": "unused"}, clear=True):
            queued = enqueue_detection(
                "detect-test",
                ship_list=snapshot,
                namespace="predict",
                simulation_id="simulation-1",
            )

        self.assertTrue(queued)
        connection.rpush.assert_called_once_with(
            detection_queue_key(
                "detect-test",
                "predict",
                "simulation-1",
            ),
            json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        connection.ltrim.assert_called_once()
        connection.set.assert_not_called()

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_predict_blacklist_coalesces_to_latest_snapshot(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        connection.set.return_value = True

        queued = enqueue_detection(
            "detect-blackList",
            ship_list=[{"mmsi": "123456789"}],
            namespace="predict",
            simulation_id="simulation-1",
        )

        self.assertTrue(queued)
        connection.rpush.assert_called_once_with(
            detection_queue_key(
                "detect-blackList",
                "predict",
                "simulation-1",
            ),
            "latest",
        )
        connection.ltrim.assert_not_called()

    @patch("AISData.detection_queue.get_detection_queue_connection")
    def test_blacklist_worker_discards_legacy_replay_backlog(
        self,
        get_connection,
    ):
        connection = get_connection.return_value
        oldest = [{"mmsi": "123456789", "timestamp": "oldest"}]
        newest = [{"mmsi": "123456789", "timestamp": "newest"}]
        queue_key = detection_queue_key(
            "detect-blackList",
            "predict",
            "simulation-1",
        )
        connection.blpop.return_value = (
            queue_key.encode(),
            json.dumps(oldest).encode(),
        )
        pipeline = connection.pipeline.return_value
        pipeline.execute.return_value = (
            [json.dumps(newest).encode()],
            1,
            0,
        )

        received = wait_for_detection_trigger(
            "detect-blackList",
            timeout=1,
            namespace="predict",
            simulation_id="simulation-1",
        )

        self.assertEqual(received, newest)
        pipeline.lrange.assert_called_once_with(queue_key, -1, -1)
        pipeline.delete.assert_any_call(queue_key)
        pipeline.delete.assert_any_call(
            detection_pending_key(
                "detect-blackList",
                "predict",
                "simulation-1",
            )
        )


@override_settings(
    CACHES=TEST_CACHES,
    AIS_LATEST_STATE_RETENTION_SECONDS=300,
    AIS_STATIC_STATE_RETENTION_SECONDS=24 * 60 * 60,
)
class AISOperationalStateTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    @staticmethod
    def dynamic(timestamp, mmsi="123456789", **overrides):
        source = {
            "timestamp": timestamp,
            "MMSI": mmsi,
            "msg_type": "1",
            "longitude": "113.7",
            "latitude": "22.4",
            "speed": "12.5",
            "course": "90",
            "heading": "91",
            "rot": "0",
        }
        source.update(overrides)
        return normalise_dynamic_ais_record(source)

    def test_dynamic_normalisation_maps_ais_sentinels_to_none(self):
        item = self.dynamic(
            "2026-08-22T00:00:00Z",
            speed="102.3",
            course="360",
            heading="511",
            rot="-128",
        )

        self.assertIsNone(item["speed"])
        self.assertIsNone(item["course"])
        self.assertIsNone(item["heading"])
        self.assertIsNone(item["rot"])
        self.assertEqual(
            set(item["quality_flags"]),
            {
                "speed_unavailable",
                "course_unavailable",
                "heading_unavailable",
                "rot_unavailable",
            },
        )

    def test_static_update_enriches_position_and_older_position_is_ignored(self):
        current = self.dynamic("2026-08-22T00:10:00Z")
        first = merge_ais_state([current])
        self.assertEqual(first.accepted, 1)

        static = normalise_static_ais_record(
            {
                "timestamp": "2026-08-22T00:10:10Z",
                "MMSI": "123456789",
                "msg_type": "5",
                "Name": "海试一号",
                "IMO": "9876543",
                "length": "88",
            }
        )
        enriched = merge_ais_state([], [static])
        self.assertEqual(enriched.upserts[0]["name"], "海试一号")
        self.assertEqual(enriched.upserts[0]["imo"], "9876543")
        self.assertEqual(enriched.upserts[0]["length"], 88.0)

        older = self.dynamic(
            "2026-08-22T00:09:59Z",
            longitude="114.9",
        )
        ignored = merge_ais_state([older])
        self.assertEqual(ignored.out_of_order, 1)
        self.assertEqual(ignored.accepted_dynamic, [])
        self.assertEqual(ignored.upserts, [])
        self.assertEqual(ignored.snapshot[0]["lon"], 113.7)

    def test_state_update_returns_every_accepted_dynamic_point_in_order(self):
        first = self.dynamic("2026-08-22T00:00:00Z", longitude="113.7")
        second = self.dynamic("2026-08-22T00:00:10Z", longitude="113.8")
        # Source files can contain interleaved or reverse-ordered rows. The
        # incremental stream must still be event-time ordered within a batch.
        update = merge_ais_state([second, first])

        self.assertEqual(
            [item["timestamp"] for item in update.accepted_dynamic],
            [first["timestamp"], second["timestamp"]],
        )
        self.assertEqual(
            [item["lon"] for item in update.accepted_dynamic],
            [113.7, 113.8],
        )

        older = self.dynamic("2026-08-21T23:59:59Z", longitude="113.6")
        duplicate = self.dynamic("2026-08-22T00:00:10Z", longitude="113.8")
        ignored = merge_ais_state([older, duplicate])

        self.assertEqual(ignored.accepted_dynamic, [])
        self.assertEqual(ignored.out_of_order, 1)
        self.assertEqual(ignored.duplicate, 1)

    def test_delta_removes_targets_outside_event_time_retention(self):
        merge_ais_state(
            [self.dynamic("2026-08-22T00:00:00Z", "123456789")]
        )
        update = merge_ais_state(
            [self.dynamic("2026-08-22T00:10:00Z", "987654321")]
        )

        self.assertEqual(update.removes, ["123456789"])
        self.assertEqual(
            [item["mmsi"] for item in update.snapshot],
            ["987654321"],
        )

    def test_duplicate_position_does_not_advance_delta_version(self):
        item = self.dynamic("2026-08-22T00:00:00Z")
        first = merge_ais_state([item])
        duplicate = merge_ais_state(
            [self.dynamic("2026-08-22T00:00:00Z")]
        )

        self.assertEqual(duplicate.duplicate, 1)
        self.assertEqual(duplicate.upserts, [])
        self.assertEqual(duplicate.version, first.version)

    def test_worker_separates_static_and_dynamic_records(self):
        dynamic, history, static = Command().convert_batch(
            [
                {
                    "timestamp": "2026-08-22T00:00:00Z",
                    "MMSI": "123456789",
                    "msg_type": "1",
                    "longitude": "113.7",
                    "latitude": "22.4",
                    "speed": "10",
                    "course": "90",
                },
                {
                    "timestamp": "2026-08-22T00:00:01Z",
                    "MMSI": "123456789",
                    "msg_type": "24",
                    "Name": "海试一号",
                    "ship_type": "70",
                },
            ]
        )

        self.assertEqual(len(dynamic), 1)
        self.assertEqual(len(history), 1)
        self.assertEqual(len(static), 1)
        self.assertEqual(static[0]["name"], "海试一号")

    def test_worker_history_can_feed_every_point_to_incremental_detectors(self):
        latest, history, static = Command().convert_batch(
            [
                {
                    "timestamp": "2026-08-22T00:00:00Z",
                    "MMSI": "123456789",
                    "msg_type": "1",
                    "longitude": "113.7",
                    "latitude": "22.4",
                    "speed": "10",
                    "course": "90",
                },
                {
                    "timestamp": "2026-08-22T00:00:10Z",
                    "MMSI": "123456789",
                    "msg_type": "1",
                    "longitude": "113.8",
                    "latitude": "22.4",
                    "speed": "11",
                    "course": "91",
                },
            ]
        )

        update = merge_ais_state(history, static)

        self.assertEqual(len(latest), 1)
        self.assertEqual(len(update.accepted_dynamic), 2)
        self.assertEqual(
            [point["lon"] for point in update.accepted_dynamic],
            [113.7, 113.8],
        )
        self.assertEqual(update.snapshot[0]["lon"], 113.8)

    def test_file_checkpoint_is_shared_through_cache(self):
        self.assertEqual(load_file_checkpoints(), {})
        save_file_checkpoint("frame.csv", "100:200")
        self.assertEqual(
            load_file_checkpoints(),
            {"frame.csv": "100:200"},
        )


@override_settings(
    CACHES=TEST_CACHES,
    CACHE_TTL=300,
    AIS_TRAJECTORY_HISTORY_WINDOW_SECONDS=30 * 60,
    AIS_TRAJECTORY_MIN_DISTANCE_METERS=3.0,
    AIS_TRAJECTORY_MAX_INTERVAL_SECONDS=3 * 60,
)
class ViolationRecordTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _payload(self, timestamp="2026-08-03T04:00:00Z"):
        return {
            "success": True,
            "feature_id": "detect-illegalStaying",
            "timestamp": timestamp,
            "count": 1,
            "results": [
                {
                    "event_id": "illegal-staying:413123456:test-zone",
                    "mmsi": "413123456",
                    "name": "测试船",
                    "status": "疑似非法驻留",
                    "risk": "高风险",
                    "details": "测试违法事件",
                    "timestamp": timestamp,
                    "lon": 122.4,
                    "lat": 29.7,
                }
            ],
        }

    def _persist_trajectory_point(self, timestamp, longitude):
        payload = self._payload(timestamp)
        payload["results"][0]["event_id"] = f"event:{timestamp}"
        payload["results"][0]["lon"] = longitude
        persist_detection_payload(
            "detect-illegalStaying",
            payload,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "timestamp": timestamp,
                    "lon": longitude,
                    "lat": 29.7,
                }
            ],
        )

    def test_repeated_event_is_deduplicated_and_saves_ais_trajectory(self):
        first_payload = self._payload()
        second_payload = self._payload("2026-08-03T04:00:01Z")

        persist_detection_payload(
            "detect-illegalStaying",
            first_payload,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T04:00:00Z",
                    "lon": 122.4,
                    "lat": 29.7,
                    "speed": 0.2,
                    "course": 90,
                }
            ],
        )
        persist_detection_payload(
            "detect-illegalStaying",
            second_payload,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T04:00:01Z",
                    "lon": 122.4001,
                    "lat": 29.7001,
                    "speed": 0.3,
                    "course": 91,
                }
            ],
        )

        self.assertEqual(ViolationEventRecord.objects.count(), 1)
        record = ViolationEventRecord.objects.get()
        self.assertEqual(record.occurrence_count, 2)
        self.assertEqual(record.target_id, "413123456")
        self.assertEqual(
            record.first_detected_at,
            parse_datetime("2026-08-03T04:00:00Z"),
        )
        self.assertEqual(
            record.last_detected_at,
            parse_datetime("2026-08-03T04:00:01Z"),
        )
        self.assertEqual(ViolationAISTrajectoryPoint.objects.count(), 2)

    def test_stationary_trajectory_is_sampled_every_three_minutes(self):
        for timestamp in (
            "2026-08-03T04:00:00Z",
            "2026-08-03T04:01:00Z",
            "2026-08-03T04:03:00Z",
        ):
            persist_detection_payload(
                "detect-illegalStaying",
                self._payload(timestamp),
                ais_snapshot=[
                    {
                        "mmsi": "413123456",
                        "timestamp": timestamp,
                        "lon": 122.4,
                        "lat": 29.7,
                        "speed": 0.0,
                        "course": 90,
                    }
                ],
            )

        points = list(ViolationAISTrajectoryPoint.objects.all())
        self.assertEqual(
            [point.observed_at for point in points],
            [
                parse_datetime("2026-08-03T04:00:00Z"),
                parse_datetime("2026-08-03T04:03:00Z"),
            ],
        )

    def test_adjacent_event_with_changed_id_uses_continuity_fallback(self):
        first_payload = self._payload("2026-08-03T04:00:00Z")
        second_payload = self._payload("2026-08-03T04:03:00Z")
        second_payload["results"][0]["event_id"] = (
            "illegal-anchored:413123456:test-zone:stable-episode-start"
        )
        second_payload["results"][0]["episode_started_at"] = (
            "2026-08-03T04:00:00Z"
        )

        persist_detection_payload(
            "detect-illegalAnchored",
            first_payload,
            ais_snapshot=[],
        )
        persist_detection_payload(
            "detect-illegalAnchored",
            second_payload,
            ais_snapshot=[],
        )

        self.assertEqual(ViolationEventRecord.objects.count(), 1)
        record = ViolationEventRecord.objects.get()
        self.assertEqual(record.occurrence_count, 2)
        self.assertEqual(
            record.last_detected_at,
            parse_datetime("2026-08-03T04:03:00Z"),
        )

    def test_changed_id_after_continuity_gap_starts_new_event(self):
        first_payload = self._payload("2026-08-03T04:00:00Z")
        second_payload = self._payload("2026-08-03T04:06:00Z")
        second_payload["results"][0]["event_id"] = (
            "illegal-anchored:413123456:test-zone:new-episode"
        )

        persist_detection_payload(
            "detect-illegalAnchored",
            first_payload,
            ais_snapshot=[],
        )
        persist_detection_payload(
            "detect-illegalAnchored",
            second_payload,
            ais_snapshot=[],
        )

        self.assertEqual(ViolationEventRecord.objects.count(), 2)

    def test_distinct_anchor_episode_starts_do_not_use_fallback(self):
        first_payload = self._payload("2026-08-03T04:00:00Z")
        first_payload["results"][0].update(
            {
                "event_id": "illegal-anchored:413123456:test-zone:first",
                "episode_started_at": "2026-08-03T03:55:00Z",
            }
        )
        second_payload = self._payload("2026-08-03T04:03:00Z")
        second_payload["results"][0].update(
            {
                "event_id": "illegal-anchored:413123456:test-zone:second",
                "episode_started_at": "2026-08-03T04:02:00Z",
            }
        )

        persist_detection_payload(
            "detect-illegalAnchored",
            first_payload,
            ais_snapshot=[],
        )
        persist_detection_payload(
            "detect-illegalAnchored",
            second_payload,
            ais_snapshot=[],
        )

        self.assertEqual(ViolationEventRecord.objects.count(), 2)

    def test_older_replay_does_not_overwrite_latest_event_details(self):
        latest = self._payload("2026-08-03T04:00:00Z")
        latest["results"][0]["speed"] = 0.1
        older = self._payload("2026-08-03T03:00:00Z")
        older["results"][0]["speed"] = 3.5

        persist_detection_payload(
            "detect-illegalStaying",
            latest,
            ais_snapshot=[],
        )
        persist_detection_payload(
            "detect-illegalStaying",
            older,
            ais_snapshot=[],
        )

        record = ViolationEventRecord.objects.get()
        self.assertEqual(
            record.event_time,
            parse_datetime("2026-08-03T04:00:00Z"),
        )
        self.assertEqual(record.last_detected_at, record.event_time)
        self.assertEqual(record.raw_data["speed"], 0.1)
        self.assertEqual(
            record.first_detected_at,
            parse_datetime("2026-08-03T03:00:00Z"),
        )

    def test_event_includes_pre_detection_history_and_drops_near_duplicates(self):
        append_ais_history(
            [
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T03:45:00Z",
                    "lon": 122.35,
                    "lat": 29.70,
                },
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T03:50:00Z",
                    "lon": 122.36,
                    "lat": 29.70,
                },
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T03:59:59Z",
                    "lon": 122.39999,
                    "lat": 29.70,
                },
            ]
        )

        persist_detection_payload(
            "detect-illegalStaying",
            self._payload(),
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T04:00:00Z",
                    "lon": 122.4,
                    "lat": 29.7,
                }
            ],
        )

        points = list(ViolationAISTrajectoryPoint.objects.all())
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0].observed_at, parse_datetime("2026-08-03T03:45:00Z"))
        self.assertAlmostEqual(points[-1].longitude, 122.39999)

    def test_predict_event_uses_only_predict_trajectory_history(self):
        append_ais_history(
            [
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T03:50:00Z",
                    "lon": 121.50,
                    "lat": 28.50,
                }
            ]
        )
        append_ais_history(
            [
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T03:50:00Z",
                    "lon": 122.35,
                    "lat": 29.70,
                }
            ],
            namespace="predict",
        )

        persist_detection_payload(
            "detect-illegalStaying",
            self._payload(),
            ais_snapshot=[],
            trajectory_namespace="predict",
        )

        points = list(ViolationAISTrajectoryPoint.objects.all())
        self.assertEqual(len(points), 1)
        self.assertIn(122.35, [point.longitude for point in points])
        self.assertNotIn(121.50, [point.longitude for point in points])

    def test_detector_result_location_is_not_an_ais_point_when_snapshot_exists(self):
        payload = self._payload()
        result = payload["results"][0]
        result.pop("lon")
        result.pop("lat")
        result["location"] = [120.0, 20.0]

        persist_detection_payload(
            "detect-illegalStaying",
            payload,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T04:00:00Z",
                    "lon": 122.4,
                    "lat": 29.7,
                }
            ],
        )

        points = list(ViolationAISTrajectoryPoint.objects.all())
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0].longitude, 122.4)
        self.assertAlmostEqual(points[0].latitude, 29.7)

    def test_retained_crossing_event_appends_real_ais_without_duplicate(self):
        payload = self._payload()
        result = payload["results"][0]
        result.update(
            {
                "event": "CrossingBoundary",
                "event_id": "crossing-boundary:4:413123456:enter:test",
                "fence_id": 4,
                "crossing_direction": "enter",
                "is_new": True,
                "first_detected_at": "2026-08-03T04:00:00Z",
            }
        )
        persist_detection_payload(
            "detect-CrossingBoundary",
            payload,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "longitude": 122.40,
                    "latitude": 29.70,
                    "timestamp": "2026-08-03T04:00:00Z",
                }
            ],
        )

        retained = self._payload("2026-08-03T04:01:00Z")
        retained["results"][0].update(result)
        retained["results"][0]["is_new"] = False
        cache.set(
            "crossing_boundary:state:v1",
            {"4:413123456": {"stable_inside": True}},
        )
        persist_detection_payload(
            "detect-CrossingBoundary",
            retained,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "longitude": 122.41,
                    "latitude": 29.71,
                    "timestamp": "2026-08-03T04:01:00Z",
                }
            ],
        )

        record = ViolationEventRecord.objects.get()
        self.assertEqual(record.occurrence_count, 1)
        points = list(record.ais_trajectory.order_by("observed_at"))
        self.assertEqual(len(points), 2)
        self.assertEqual(
            [(point.longitude, point.latitude) for point in points],
            [(122.40, 29.70), (122.41, 29.71)],
        )

    def test_retained_crossing_enter_stops_appending_after_exit(self):
        payload = self._payload()
        result = payload["results"][0]
        result.update(
            {
                "event": "CrossingBoundary",
                "event_id": "crossing-boundary:4:413123456:enter:closed",
                "fence_id": 4,
                "crossing_direction": "enter",
                "is_new": True,
                "first_detected_at": "2026-08-03T04:00:00Z",
            }
        )
        persist_detection_payload(
            "detect-CrossingBoundary",
            payload,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "longitude": 122.40,
                    "latitude": 29.70,
                    "timestamp": "2026-08-03T04:00:00Z",
                }
            ],
        )

        retained = self._payload("2026-08-03T04:01:00Z")
        retained["results"][0].update(result)
        retained["results"][0]["is_new"] = False
        cache.set(
            "crossing_boundary:state:v1",
            {"4:413123456": {"stable_inside": False}},
        )
        persist_detection_payload(
            "detect-CrossingBoundary",
            retained,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "longitude": 122.50,
                    "latitude": 29.80,
                    "timestamp": "2026-08-03T04:01:00Z",
                }
            ],
        )

        record = ViolationEventRecord.objects.get()
        self.assertEqual(record.occurrence_count, 1)
        self.assertEqual(record.ais_trajectory.count(), 1)
        self.assertAlmostEqual(
            record.ais_trajectory.get().longitude,
            122.40,
        )

    def test_retained_crossing_exit_never_appends_post_exit_ais(self):
        payload = self._payload()
        result = payload["results"][0]
        result.update(
            {
                "event": "CrossingBoundary",
                "event_id": "crossing-boundary:4:413123456:exit:test",
                "fence_id": 4,
                "crossing_direction": "exit",
                "is_new": True,
                "first_detected_at": "2026-08-03T04:00:00Z",
            }
        )
        persist_detection_payload(
            "detect-CrossingBoundary",
            payload,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "longitude": 122.40,
                    "latitude": 29.70,
                    "timestamp": "2026-08-03T04:00:00Z",
                }
            ],
        )

        retained = self._payload("2026-08-03T04:01:00Z")
        retained["results"][0].update(result)
        retained["results"][0]["is_new"] = False
        persist_detection_payload(
            "detect-CrossingBoundary",
            retained,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "longitude": 122.50,
                    "latitude": 29.80,
                    "timestamp": "2026-08-03T04:01:00Z",
                }
            ],
        )

        record = ViolationEventRecord.objects.get()
        self.assertEqual(record.occurrence_count, 1)
        self.assertEqual(record.ais_trajectory.count(), 1)

    def test_crossing_trajectory_artifact_command_is_dry_run_by_default(self):
        payload = self._payload()
        result = payload["results"][0]
        result.pop("lon")
        result.pop("lat")
        result.pop("timestamp")
        result.update(
            {
                "location": [122.4, 29.7],
                "event": "CrossingBoundary",
                "event_id": "crossing-boundary:4:413123456:enter:cleanup",
                "is_new": True,
            }
        )
        persist_detection_payload(
            "detect-CrossingBoundary",
            payload,
            ais_snapshot=[],
        )
        record = ViolationEventRecord.objects.get()
        ViolationAISTrajectoryPoint.objects.create(
            event=record,
            mmsi="413123456",
            observed_at=parse_datetime("2026-08-03T04:01:00Z"),
            longitude=122.41,
            latitude=29.71,
            raw_data={
                "mmsi": "413123456",
                "timestamp": "2026-08-03T04:01:00Z",
            },
        )

        output = io.StringIO()
        call_command("clean_crossing_trajectory_artifacts", stdout=output)
        self.assertIn("matched=1", output.getvalue())
        self.assertEqual(ViolationAISTrajectoryPoint.objects.count(), 2)

        with tempfile.TemporaryDirectory() as backup_directory:
            with self.settings(
                CROSSING_TRAJECTORY_CLEANUP_BACKUP_DIR=backup_directory
            ):
                applied_output = io.StringIO()
                call_command(
                    "clean_crossing_trajectory_artifacts",
                    apply=True,
                    stdout=applied_output,
                )
                self.assertIn("Backup created:", applied_output.getvalue())
                self.assertEqual(
                    len(list(Path(backup_directory).glob("*.jsonl"))),
                    1,
                )
        self.assertEqual(ViolationAISTrajectoryPoint.objects.count(), 1)
        self.assertTrue(
            ViolationAISTrajectoryPoint.objects.filter(
                raw_data__timestamp="2026-08-03T04:01:00Z"
            ).exists()
        )

    def test_crossing_post_exit_trim_is_dry_run_and_backed_up(self):
        entered_at = parse_datetime("2026-08-03T04:00:00Z")
        exited_at = parse_datetime("2026-08-03T04:10:00Z")
        common = {
            "fingerprint": "f" * 64,
            "feature_id": "detect-CrossingBoundary",
            "event_type": "海上围栏越界船舶检测",
            "target_id": "413123456",
            "status": "CrossingBoundary",
            "first_detected_at": entered_at,
            "last_detected_at": entered_at,
        }
        enter = ViolationEventRecord.objects.create(
            **common,
            event_key="a" * 64,
            event_time=entered_at,
            raw_data={
                "mmsi": "413123456",
                "fence_id": 4,
                "crossing_direction": "enter",
            },
        )
        exit_record = ViolationEventRecord.objects.create(
            **{
                **common,
                "fingerprint": "e" * 64,
                "first_detected_at": exited_at,
                "last_detected_at": exited_at,
            },
            event_key="b" * 64,
            event_time=exited_at,
            raw_data={
                "mmsi": "413123456",
                "fence_id": 4,
                "crossing_direction": "exit",
            },
        )
        for event, timestamps in (
            (
                enter,
                (
                    "2026-08-03T04:00:00Z",
                    "2026-08-03T04:10:00Z",
                    "2026-08-03T04:11:00Z",
                ),
            ),
            (
                exit_record,
                (
                    "2026-08-03T04:10:00Z",
                    "2026-08-03T04:11:00Z",
                ),
            ),
        ):
            for index, timestamp in enumerate(timestamps):
                ViolationAISTrajectoryPoint.objects.create(
                    event=event,
                    mmsi="413123456",
                    observed_at=parse_datetime(timestamp),
                    longitude=122.4 + index * 0.01,
                    latitude=29.7,
                    raw_data={"timestamp": timestamp},
                )

        output = io.StringIO()
        call_command(
            "trim_crossing_event_trajectories",
            mmsi=["413123456"],
            stdout=output,
        )
        self.assertIn("matched_points=2", output.getvalue())
        self.assertEqual(ViolationAISTrajectoryPoint.objects.count(), 5)

        with tempfile.TemporaryDirectory() as backup_directory:
            with self.settings(
                CROSSING_TRAJECTORY_CLEANUP_BACKUP_DIR=backup_directory
            ):
                applied_output = io.StringIO()
                call_command(
                    "trim_crossing_event_trajectories",
                    mmsi=["413123456"],
                    apply=True,
                    stdout=applied_output,
                )
            self.assertIn("Deleted post-exit points: 2", applied_output.getvalue())
            self.assertEqual(
                len(list(Path(backup_directory).glob("*.jsonl"))),
                1,
            )
        self.assertEqual(ViolationAISTrajectoryPoint.objects.count(), 3)

    def test_historical_records_span_sources_while_map_trajectory_is_isolated(self):
        payload = self._payload()
        sources = (
            ("operational", None, None, 122.1),
            ("predict", "simulation-1", "predict:simulation-1", 122.2),
            ("predict", "simulation-2", "predict:simulation-2", 122.3),
        )
        for namespace, simulation_id, scope, longitude in sources:
            persist_detection_payload(
                "detect-illegalStaying",
                payload,
                ais_snapshot=[
                    {
                        "mmsi": "413123456",
                        "timestamp": "2026-08-03T04:00:00Z",
                        "lon": longitude,
                        "lat": 29.7,
                    }
                ],
                source_namespace=namespace,
                simulation_id=simulation_id,
                persistence_scope=scope,
            )

        self.assertEqual(ViolationEventRecord.objects.count(), 3)
        cache.set(
            ACTIVE_DETECTION_SOURCE_CACHE_KEY,
            "predict:simulation-1",
            timeout=None,
        )
        list_response = self.client.get(reverse("violation_record_list"))
        previous_batch_record = ViolationEventRecord.objects.get(
            source_namespace="predict",
            simulation_id="simulation-2",
        )
        detail_response = self.client.get(
            reverse(
                "violation_record_detail",
                args=(previous_batch_record.id,),
            )
        )
        trajectory_response = self.client.get(
            reverse("ship_trajectory"),
            {
                "mmsi": "413123456",
                "end": "2026-08-03T05:00:00Z",
            },
        )

        self.assertEqual(list_response.json()["count"], 3)
        self.assertEqual(list_response.json()["statistics"]["total"], 3)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(
            detail_response.json()["record"]["id"],
            previous_batch_record.id,
        )
        self.assertEqual(trajectory_response.json()["count"], 1)
        self.assertEqual(
            trajectory_response.json()["trajectory"][0]["longitude"],
            122.2,
        )
        self.assertTrue(
            all(
                "source_namespace" not in item
                for item in list_response.json()["results"]
            )
        )

    def test_event_trajectory_start_excludes_motion_before_episode(self):
        append_ais_history(
            [
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T03:50:00Z",
                    "lon": 122.30,
                    "lat": 29.70,
                    "speed": 3.5,
                },
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T03:56:00Z",
                    "lon": 122.36,
                    "lat": 29.70,
                    "speed": 0.1,
                },
            ]
        )
        payload = self._payload()
        payload["results"][0]["trajectory_started_at"] = (
            "2026-08-03T03:55:00Z"
        )

        persist_detection_payload(
            "detect-illegalStaying",
            payload,
            ais_snapshot=[],
        )

        points = list(ViolationAISTrajectoryPoint.objects.all())
        self.assertTrue(points)
        self.assertTrue(
            all(
                point.observed_at
                >= parse_datetime("2026-08-03T03:55:00Z")
                for point in points
            )
        )
        self.assertNotIn(3.5, [point.speed for point in points])

    def test_management_command_backfills_old_record_without_rerunning_detector(self):
        payload = self._payload()
        payload["results"][0]["trajectory_started_at"] = (
            "2026-08-03T03:45:00Z"
        )
        persist_detection_payload(
            "detect-illegalStaying",
            payload,
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T04:00:00Z",
                    "lon": 122.4,
                    "lat": 29.7,
                }
            ],
        )
        record = ViolationEventRecord.objects.get()

        with tempfile.TemporaryDirectory() as directory:
            rows = (
                ("2026-08-03 03-30-00.csv", "03:30:10", "8.0"),
                ("2026-08-03 03-45-00.csv", "03:45:10", "0.1"),
            )
            for filename, observed_time, speed in rows:
                path = Path(directory) / filename
                with path.open(
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as destination:
                    writer = csv.writer(destination)
                    writer.writerow(
                        [
                            "timestamp",
                            "MMSI",
                            "latitude",
                            "longitude",
                            "speed",
                            "course",
                        ]
                    )
                    writer.writerow(
                        [
                            f"2026-08-03 {observed_time}",
                            "413123456",
                            "29.7",
                            "122.35",
                            speed,
                            "90.0",
                        ]
                    )

            for _ in range(2):
                call_command(
                    "backfill_violation_trajectories",
                    data_dir=directory,
                    record_ids=[record.id],
                    verbosity=0,
                )

        points = list(record.ais_trajectory.all())
        self.assertEqual(len(points), 2)
        self.assertTrue(points[0].raw_data["backfilled"])
        self.assertEqual(points[0].speed, 0.1)

    def test_radar_only_event_has_no_ais_trajectory(self):
        persist_detection_payload(
            "detect-ais-off",
            {
                "success": True,
                "count": 1,
                "timestamp": "2026-08-03T04:00:00Z",
                "results": [
                    {
                        "radar_id": "8-1",
                        "name": "雷达目标 8-1",
                        "status": "疑似关闭AIS",
                        "lon": 122.4,
                        "lat": 29.7,
                    }
                ],
            },
        )

        record = ViolationEventRecord.objects.get()
        self.assertEqual(record.target_id, "8-1")
        self.assertEqual(record.ais_trajectory.count(), 0)

    def test_record_list_and_detail_api_return_saved_trajectory(self):
        persist_detection_payload(
            "detect-illegalStaying",
            self._payload(),
            ais_snapshot=[
                {
                    "mmsi": "413123456",
                    "timestamp": "2026-08-03T04:00:00Z",
                    "lon": 122.4,
                    "lat": 29.7,
                }
            ],
        )
        record = ViolationEventRecord.objects.get()

        list_response = self.client.get(
            reverse("violation_record_list"),
            {"feature_id": "detect-illegalStaying", "target": "413123456"},
        )
        detail_response = self.client.get(
            reverse("violation_record_detail", args=(record.id,))
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["count"], 1)
        self.assertEqual(list_response.json()["statistics"]["total"], 1)
        self.assertEqual(
            list_response.json()["statistics"]["items"][0]["percentage"],
            100.0,
        )
        self.assertEqual(list_response.json()["results"][0]["trajectory_count"], 1)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(len(detail_response.json()["record"]["ais_trajectory"]), 1)

    def test_ship_trajectory_api_queries_by_mmsi_and_deduplicates_events(self):
        first_payload = self._payload("2026-08-03T04:00:00Z")
        second_payload = self._payload("2026-08-03T04:00:00Z")
        second_payload["results"][0]["event_id"] = "another-event"
        snapshot = [
            {
                "mmsi": "413123456",
                "timestamp": "2026-08-03T04:00:00Z",
                "lon": 122.4,
                "lat": 29.7,
                "speed": 0.2,
                "course": 90,
            }
        ]
        persist_detection_payload(
            "detect-illegalStaying",
            first_payload,
            ais_snapshot=snapshot,
        )
        persist_detection_payload(
            "detect-illegalStaying",
            second_payload,
            ais_snapshot=snapshot,
        )

        response = self.client.get(
            reverse("ship_trajectory"),
            {
                "mmsi": "413123456",
                "end": "2026-08-03T05:00:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["mmsi"], "413123456")
        self.assertEqual(payload["source"], "violation_event_trajectory")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["trajectory"]), 1)
        self.assertEqual(payload["trajectory"][0]["longitude"], 122.4)

    def test_ship_trajectory_api_filters_points_by_warning_model(self):
        observed_at = parse_datetime("2026-08-03T04:00:00Z")
        low_speed = ViolationEventRecord.objects.create(
            event_key="low-speed-trajectory-filter",
            fingerprint="low-speed-trajectory-filter",
            feature_id="detect-lowSpeedBoat",
            event_type="低速航行预警",
            target_id="413123456",
            first_detected_at=observed_at,
            last_detected_at=observed_at,
            raw_data={"event_id": "low-speed-event"},
        )
        high_speed = ViolationEventRecord.objects.create(
            event_key="high-speed-trajectory-filter",
            fingerprint="high-speed-trajectory-filter",
            feature_id="detect-highSpeedBoat",
            event_type="高速船舶预警",
            target_id="413123456",
            first_detected_at=observed_at,
            last_detected_at=observed_at,
            raw_data={"event_id": "high-speed-event"},
        )
        ViolationAISTrajectoryPoint.objects.bulk_create(
            [
                ViolationAISTrajectoryPoint(
                    event=low_speed,
                    mmsi="413123456",
                    observed_at=observed_at,
                    longitude=122.4,
                    latitude=29.7,
                ),
                ViolationAISTrajectoryPoint(
                    event=high_speed,
                    mmsi="413123456",
                    observed_at=observed_at + timedelta(minutes=1),
                    longitude=123.4,
                    latitude=30.7,
                ),
            ]
        )

        response = self.client.get(
            reverse("ship_trajectory"),
            {
                "mmsi": "413123456",
                "end": "2026-08-03T05:00:00Z",
                "feature_id": "detect-lowSpeedBoat",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["feature_id"], "detect-lowSpeedBoat")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["trajectory"][0]["longitude"], 122.4)

    def test_ship_trajectory_api_filters_one_event_without_advancing_end(self):
        detected_at = parse_datetime("2026-08-03T04:00:00Z")
        crossing = ViolationEventRecord.objects.create(
            event_key="crossing-event-trajectory-filter",
            fingerprint="crossing-event-trajectory-filter",
            feature_id="detect-CrossingBoundary",
            event_type="海上围栏越界",
            target_id="413123456",
            first_detected_at=detected_at,
            last_detected_at=detected_at + timedelta(minutes=10),
            raw_data={"prediction_id": "prediction-crossing-test"},
        )
        other_crossing = ViolationEventRecord.objects.create(
            event_key="other-crossing-event-trajectory-filter",
            fingerprint="other-crossing-event-trajectory-filter",
            feature_id="detect-CrossingBoundary",
            event_type="海上围栏越界",
            target_id="413123456",
            first_detected_at=detected_at,
            last_detected_at=detected_at,
            raw_data={"prediction_id": "prediction-other-crossing-test"},
        )
        ViolationAISTrajectoryPoint.objects.bulk_create(
            [
                ViolationAISTrajectoryPoint(
                    event=crossing,
                    mmsi="413123456",
                    observed_at=detected_at,
                    longitude=122.4,
                    latitude=29.7,
                ),
                ViolationAISTrajectoryPoint(
                    event=crossing,
                    mmsi="413123456",
                    observed_at=detected_at + timedelta(minutes=10),
                    longitude=122.5,
                    latitude=29.8,
                ),
                ViolationAISTrajectoryPoint(
                    event=other_crossing,
                    mmsi="413123456",
                    observed_at=detected_at + timedelta(minutes=5),
                    longitude=123.5,
                    latitude=30.8,
                ),
            ]
        )

        prediction_id = "prediction-crossing-test"
        response = self.client.get(
            reverse("ship_trajectory"),
            {
                "mmsi": "413123456",
                # A retained alert keeps this original event time. Event-scoped
                # lookup still returns the trajectory accumulated while active.
                "end": "2026-08-03T04:00:00Z",
                "feature_id": "detect-CrossingBoundary",
                "prediction_id": prediction_id,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["query_scope"], "event")
        self.assertEqual(payload["prediction_id"], prediction_id)
        self.assertIsNone(payload["end"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [point["longitude"] for point in payload["trajectory"]],
            [122.4, 122.5],
        )

    def test_ship_trajectory_api_filters_time_and_supports_path_mmsi(self):
        for timestamp, longitude in (
            ("2026-08-03T04:00:00Z", 122.4),
            ("2026-08-03T05:00:00Z", 122.5),
        ):
            self._persist_trajectory_point(timestamp, longitude)

        response = self.client.get(
            reverse("ship_trajectory_by_mmsi", args=("413123456",)),
            {
                "start": "2026-08-03T04:30:00Z",
                "end": "2026-08-03T05:30:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["trajectory"][0]["longitude"], 122.5)
        self.assertEqual(
            payload["trajectory"][0]["timestamp"],
            "2026-08-03T05:00:00+00:00",
        )

    def test_ship_trajectory_api_includes_warning_time_and_excludes_after(self):
        self._persist_trajectory_point("2026-08-03T04:00:00Z", 122.4)
        self._persist_trajectory_point("2026-08-03T04:30:00Z", 122.45)
        self._persist_trajectory_point("2026-08-03T05:00:00Z", 122.5)

        response = self.client.get(
            reverse("ship_trajectory"),
            {
                "mmsi": "413123456",
                "end": "2026-08-03T04:30:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [point["longitude"] for point in payload["trajectory"]],
            [122.4, 122.45],
        )
        self.assertEqual(payload["start"], "2026-08-02T23:30:00+00:00")
        self.assertEqual(payload["end"], "2026-08-03T04:30:00+00:00")

    def test_ship_trajectory_api_defaults_end_to_server_current_time(self):
        self._persist_trajectory_point("2026-08-03T04:00:00Z", 122.4)
        self._persist_trajectory_point("2026-08-03T05:00:00Z", 122.5)
        current_time = parse_datetime("2026-08-03T04:30:00Z")

        with patch("AISData.views.timezone.now", return_value=current_time):
            response = self.client.get(
                reverse("ship_trajectory"),
                {"mmsi": "413123456"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["trajectory"][0]["longitude"], 122.4)
        self.assertEqual(payload["start"], "2026-08-02T23:30:00+00:00")
        self.assertEqual(payload["end"], "2026-08-03T04:30:00+00:00")

    def test_ship_trajectory_api_returns_at_most_latest_50_points(self):
        base_time = parse_datetime("2026-08-03T00:00:00Z")
        record = ViolationEventRecord.objects.create(
            event_key="trajectory-limit-test",
            fingerprint="trajectory-limit-test",
            feature_id="detect-illegalStaying",
            event_type="非法驻留预警",
            target_id="413123456",
            first_detected_at=base_time,
            last_detected_at=base_time,
        )
        ViolationAISTrajectoryPoint.objects.bulk_create(
            [
                ViolationAISTrajectoryPoint(
                    event=record,
                    mmsi="413123456",
                    observed_at=base_time + timedelta(minutes=index),
                    longitude=120 + index / 1000,
                    latitude=30,
                )
                for index in range(105)
            ]
        )

        response = self.client.get(
            reverse("ship_trajectory"),
            {
                "mmsi": "413123456",
                "end": "2026-08-03T02:00:00Z",
                "page_size": 5000,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 105)
        self.assertEqual(payload["page_size"], 50)
        self.assertEqual(len(payload["trajectory"]), 50)
        self.assertEqual(
            payload["trajectory"][0]["timestamp"],
            "2026-08-03T00:55:00+00:00",
        )
        self.assertEqual(
            payload["trajectory"][-1]["timestamp"],
            "2026-08-03T01:44:00+00:00",
        )

    def test_ship_trajectory_api_validates_mmsi_and_time_range(self):
        missing = self.client.get(reverse("ship_trajectory"))
        invalid = self.client.get(
            reverse("ship_trajectory"),
            {"mmsi": "ship-1"},
        )
        reversed_range = self.client.get(
            reverse("ship_trajectory"),
            {
                "mmsi": "413123456",
                "start": "2026-08-04T00:00:00Z",
                "end": "2026-08-03T00:00:00Z",
            },
        )
        oversized_range = self.client.get(
            reverse("ship_trajectory"),
            {
                "mmsi": "413123456",
                "start": "2026-08-02T18:59:59Z",
                "end": "2026-08-03T00:00:00Z",
            },
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(reversed_range.status_code, 400)
        self.assertEqual(oversized_range.status_code, 400)
        self.assertIn("mmsi", missing.json()["message"])
        self.assertIn("9位数字", invalid.json()["message"])
        self.assertIn("start", reversed_range.json()["message"])
        self.assertIn("不能超过5小时", oversized_range.json()["message"])


class IllegalAnchoredHistoryMergeTests(TestCase):
    fingerprint = "same-illegal-anchor-event"

    def _record(
        self,
        event_key,
        timestamp,
        *,
        occurrence_count=1,
        episode_started_at=None,
    ):
        raw_data = {"event_id": event_key, "marker": timestamp}
        if episode_started_at:
            raw_data["episode_started_at"] = episode_started_at
        observed_at = parse_datetime(timestamp)
        return ViolationEventRecord.objects.create(
            event_key=event_key,
            fingerprint=self.fingerprint,
            feature_id="detect-illegalAnchored",
            event_type="非法抛锚船舶检测",
            target_id="477996310",
            target_name="测试船",
            status="疑似非法抛锚",
            risk_level="待核查",
            longitude=113.93,
            latitude=22.33,
            event_time=observed_at,
            first_detected_at=observed_at,
            last_detected_at=observed_at,
            occurrence_count=occurrence_count,
            details=timestamp,
            raw_data=raw_data,
        )

    def test_dry_run_does_not_change_records(self):
        self._record("anchor-a", "2020-12-28T09:01:59Z")
        self._record("anchor-b", "2020-12-28T09:04:59Z")
        output = io.StringIO()

        call_command(
            "merge_illegal_anchored_events",
            target_id="477996310",
            stdout=output,
        )

        self.assertEqual(ViolationEventRecord.objects.count(), 2)
        self.assertIn("duplicate_records=1", output.getvalue())
        self.assertIn("Dry-run only", output.getvalue())

    def test_apply_merges_fields_and_deduplicates_trajectory(self):
        first = self._record(
            "anchor-a",
            "2020-12-28T09:01:59Z",
            occurrence_count=2,
        )
        second = self._record("anchor-b", "2020-12-28T09:04:59Z")
        for event in (first, second):
            ViolationAISTrajectoryPoint.objects.create(
                event=event,
                mmsi="477996310",
                observed_at=parse_datetime("2020-12-28T09:00:00Z"),
                longitude=113.93,
                latitude=22.33,
                raw_data={"source_event": event.id},
            )

        with tempfile.TemporaryDirectory() as directory:
            backup_path = Path(directory) / "backup.json"
            call_command(
                "merge_illegal_anchored_events",
                apply=True,
                target_id="477996310",
                backup_path=str(backup_path),
                verbosity=0,
            )
            backup = json.loads(backup_path.read_text(encoding="utf-8"))

        self.assertEqual(ViolationEventRecord.objects.count(), 1)
        record = ViolationEventRecord.objects.get()
        self.assertEqual(record.id, first.id)
        self.assertEqual(record.occurrence_count, 3)
        self.assertEqual(
            record.last_detected_at,
            parse_datetime("2020-12-28T09:04:59Z"),
        )
        self.assertEqual(record.raw_data["marker"], "2020-12-28T09:04:59Z")
        self.assertEqual(record.ais_trajectory.count(), 1)
        self.assertEqual(len(backup["groups"]), 1)
        self.assertEqual(len(backup["groups"][0]["events"]), 2)

    def test_gap_and_distinct_fixed_episode_starts_remain_separate(self):
        self._record(
            "anchor-a",
            "2020-12-28T09:01:59Z",
            episode_started_at="2020-12-28T09:00:59Z",
        )
        self._record(
            "anchor-b",
            "2020-12-28T09:07:59Z",
            episode_started_at="2020-12-28T09:06:59Z",
        )
        self._record(
            "anchor-c",
            "2020-12-28T09:10:59Z",
            episode_started_at="2020-12-28T09:09:59Z",
        )
        self._record(
            "anchor-d",
            "2020-12-28T09:13:59Z",
            episode_started_at="2020-12-28T09:12:59Z",
        )

        output = io.StringIO()
        call_command(
            "merge_illegal_anchored_events",
            target_id="477996310",
            stdout=output,
        )

        self.assertIn("groups=0", output.getvalue())
