import csv
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

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
from .views import cached_detection_result
from .normalization import normalise_ais_name
from .management.commands.ais_worker import Command
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
            },
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
        from WanAna02702.routing import websocket_urlpatterns

        routes = {str(route.pattern) for route in websocket_urlpatterns}

        self.assertIn("ws/violations/", routes)
        self.assertEqual(ViolationConsumer.GROUP_NAME, "violation_updates")
        self.assertFalse(hasattr(AisConsumer, "send_detection_update"))

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
        self.assertIsNotNone(cached["computed_at"])

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

        received = wait_for_detection_trigger(
            "detect-CrossingBoundary",
            timeout=1,
        )

        self.assertEqual(received, snapshot)
        connection.delete.assert_not_called()


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

    def test_ship_trajectory_api_excludes_points_at_or_after_warning_time(self):
        self._persist_trajectory_point("2026-08-03T04:00:00Z", 122.4)
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
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["trajectory"][0]["longitude"], 122.4)
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
