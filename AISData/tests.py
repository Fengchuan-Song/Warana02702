import json
from unittest.mock import Mock, patch

from django.http import JsonResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve
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
from .consumers import load_initial_realtime_state


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "backend-detection-tests",
    }
}


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

    def test_initial_websocket_state_replays_cached_results(self):
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

        replayed_ais, replayed_results = load_initial_realtime_state()

        self.assertEqual(
            replayed_ais,
            [{"mmsi": "123456789", "name": "未知目标"}],
        )
        self.assertEqual(
            replayed_results["detect-highSpeedBoat"],
            detection_payload,
        )

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
