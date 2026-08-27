import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
from django.core.cache import cache
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import resolve

from AISData.detection import DETECTORS
from AISData.views import cached_detection_result

from .models import TrajectoryPoint
from .route_index import load_route_index
from .views import DEVIATION_STATE_CACHE_KEY, detect_deviation


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "deviation-tests",
    }
}

TEST_CONFIG = {
    "minimum_speed_knots": 2,
    "minimum_duration_seconds": 120,
    "minimum_observations": 5,
    "maximum_gap_seconds": 40,
    "minimum_displacement_metres": 100,
    "route_entry_distance_metres": 500,
    "deviation_distance_metres": 1000,
    "confirmation_observations": 3,
    "confirmation_duration_seconds": 60,
    "direction_tolerance_degrees": 45,
    "minimum_direction_coherence": 0.5,
    "coverage_margin_metres": 10_000,
    "analysis_window_minutes": 10,
    "retention_window_minutes": 20,
    "max_position_age_seconds": 60,
    "future_tolerance_seconds": 60,
    "max_valid_speed_knots": 102.2,
    "state_retention_minutes": 10,
    "eligible_nav_statuses": [0, 15],
    "allow_missing_nav_status": True,
}


class FakeRouteIndex:
    metadata = {
        "source": "test",
        "sample_count": 10,
    }

    def contains(self, lon, lat, margin_metres=0):
        return 9 <= lon <= 11 and -1 <= lat <= 1

    def query(self, points):
        return {
            "distance_metres": np.asarray(
                [abs(lat) * 111_000 for lon, lat in points],
                dtype=float,
            ),
            "axis_degrees": np.full(len(points), 90.0),
            "coherence": np.ones(len(points)),
        }


class DeviationRegistrationTests(SimpleTestCase):
    def test_detector_registration_and_cache_only_url(self):
        self.assertEqual(
            DETECTORS["detect-deviation"],
            "Deviation.views.detect_deviation",
        )
        match = resolve("/Deviation/detectDeviation/")
        self.assertIs(match.func, cached_detection_result)
        self.assertEqual(match.kwargs["feature_id"], "detect-deviation")

    def test_compact_route_index_loads(self):
        route_index = load_route_index()
        result = route_index.query(
            [(110.52223333333332, 20.380416666666665)]
        )

        self.assertGreater(len(route_index.latitudes), 20_000)
        self.assertLess(
            float(result["distance_metres"][0]),
            100,
        )
        self.assertEqual(
            route_index.metadata["source_rows"],
            11_343_362,
        )


@override_settings(
    CACHES=TEST_CACHES,
    DEVIATION_DETECTION=TEST_CONFIG,
)
class DeviationDetectorTests(TestCase):
    base_time = datetime(2020, 12, 29, tzinfo=timezone.utc)

    def setUp(self):
        cache.delete(DEVIATION_STATE_CACHE_KEY)
        self.factory = RequestFactory()
        self.route_index = FakeRouteIndex()
        self.route_patch = patch(
            "Deviation.views.get_route_index",
            return_value=self.route_index,
        )
        self.route_patch.start()

    def tearDown(self):
        self.route_patch.stop()

    def ship(
        self,
        seconds,
        lon=10,
        lat=0,
        speed=10,
        course=90,
        mmsi="123456789",
        nav_status=0,
        **extra,
    ):
        ship = {
            "timestamp": (
                self.base_time + timedelta(seconds=seconds)
            ).isoformat(),
            "mmsi": mmsi,
            "name": None,
            "lon": lon,
            "lat": lat,
            "speed": speed,
            "course": course,
            "nav_status": nav_status,
        }
        ship.update(extra)
        return ship

    def call_view(self, ship_list, method="get"):
        request = getattr(self.factory, method)("/internal/detection/")
        request.ais_ship_list = ship_list
        response = detect_deviation(request)
        payload = None
        if response.headers.get("Content-Type", "").startswith(
            "application/json"
        ):
            payload = json.loads(response.content)
        return response, payload

    def feed(self, points, **overrides):
        payload = None
        latest_ship = None
        for seconds, lon, lat in points:
            latest_ship = self.ship(
                seconds,
                lon=lon,
                lat=lat,
                **overrides,
            )
            _, payload = self.call_view([latest_ship])
        return payload, latest_ship

    def on_route_points(self):
        return [
            (0, 10.000, 0),
            (30, 10.001, 0),
            (60, 10.002, 0),
            (90, 10.003, 0),
        ]

    def deviating_points(self):
        return [
            (120, 10.004, 0.02),
            (150, 10.005, 0.02),
            (180, 10.006, 0.02),
        ]

    def test_empty_snapshot_and_post_are_safe(self):
        response, payload = self.call_view([])
        post_response, _ = self.call_view([], method="post")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(post_response.status_code, 405)

    def test_missing_route_index_returns_explicit_failure(self):
        with patch(
            "Deviation.views.get_route_index",
            return_value=None,
        ):
            response, payload = self.call_view([self.ship(0)])

        self.assertEqual(response.status_code, 503)
        self.assertFalse(payload["success"])
        self.assertIn("未执行", payload["message"])

    def test_invalid_records_are_skipped(self):
        _, payload = self.call_view(
            [
                self.ship(0, speed=None, mmsi="1"),
                self.ship(0, speed=102.3, mmsi="2"),
                self.ship(0, lon=999, mmsi="3"),
                {"mmsi": "4", "speed": 10},
            ]
        )

        self.assertEqual(payload["skipped_count"], 4)
        self.assertEqual(TrajectoryPoint.objects.count(), 0)

    def test_outside_knowledge_coverage_is_not_evaluated(self):
        _, payload = self.call_view([self.ship(0, lon=20)])

        self.assertEqual(payload["unmonitored_count"], 1)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(TrajectoryPoint.objects.count(), 0)

    def test_ship_must_first_associate_with_route(self):
        payload, _ = self.feed(
            [
                (0, 10.000, 0.02),
                (30, 10.001, 0.02),
                (60, 10.002, 0.02),
                (90, 10.003, 0.02),
                (120, 10.004, 0.02),
                (150, 10.005, 0.02),
                (180, 10.006, 0.02),
            ]
        )

        self.assertEqual(payload["count"], 0)

    def test_continuous_departure_from_route_alerts(self):
        payload, _ = self.feed(
            self.on_route_points() + self.deviating_points()
        )

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["event"], "Deviation")
        self.assertEqual(result["name"], "未知目标")
        self.assertEqual(result["duration_seconds"], 60)
        self.assertEqual(result["observation_count"], 3)
        self.assertGreater(result["route_distance_metres"], 2000)
        self.assertTrue(result["is_new"])

    def test_one_incremental_batch_preserves_route_departure_points(self):
        ships = [
            self.ship(seconds, lon=lon, lat=lat)
            for seconds, lon, lat in (
                self.on_route_points() + self.deviating_points()
            )
        ]

        _, payload = self.call_view(ships)

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["duration_seconds"], 60)
        self.assertEqual(TrajectoryPoint.objects.count(), len(ships))

    def test_unmodified_active_vessel_is_retained_until_incremental_gap(self):
        self.feed(self.on_route_points() + self.deviating_points())

        _, retained = self.call_view(
            [self.ship(210, mmsi="987654321")]
        )
        _, expired = self.call_view(
            [self.ship(240, mmsi="987654321")]
        )

        self.assertEqual(retained["count"], 1)
        self.assertEqual(retained["results"][0]["mmsi"], "123456789")
        self.assertFalse(retained["results"][0]["is_new"])
        self.assertEqual(expired["count"], 0)

    def test_repeated_frame_keeps_same_event(self):
        first, latest = self.feed(
            self.on_route_points() + self.deviating_points()
        )
        _, repeated = self.call_view([latest])
        _, continuing = self.call_view(
            [self.ship(210, lon=10.007, lat=0.02)]
        )

        self.assertFalse(repeated["results"][0]["is_new"])
        self.assertFalse(continuing["results"][0]["is_new"])
        self.assertEqual(
            first["results"][0]["event_id"],
            continuing["results"][0]["event_id"],
        )
        self.assertEqual(TrajectoryPoint.objects.count(), 8)

    def test_returning_to_route_closes_event(self):
        first, _ = self.feed(
            self.on_route_points() + self.deviating_points()
        )
        first_event_id = first["results"][0]["event_id"]
        _, recovered = self.call_view(
            [self.ship(210, lon=10.007, lat=0)]
        )
        second, _ = self.feed(
            [
                (240, 10.008, 0.02),
                (270, 10.009, 0.02),
                (300, 10.010, 0.02),
            ]
        )

        self.assertEqual(recovered["count"], 0)
        self.assertTrue(second["results"][0]["is_new"])
        self.assertNotEqual(
            first_event_id,
            second["results"][0]["event_id"],
        )

    def test_gap_and_stationary_status_reset_tracking(self):
        self.feed(self.on_route_points())
        _, slow = self.call_view(
            [self.ship(120, lon=10.004, speed=0.1)]
        )
        self.assertEqual(slow["count"], 0)
        self.assertEqual(TrajectoryPoint.objects.count(), 0)

        payload, _ = self.feed(
            [
                (180, 10.005, 0),
                (210, 10.006, 0),
                (400, 10.007, 0.02),
                (430, 10.008, 0.02),
                (460, 10.009, 0.02),
            ]
        )
        self.assertEqual(payload["count"], 0)

    def test_non_underway_and_port_operations_are_excluded(self):
        for status in (1, 3, 5, 7, 8):
            _, payload = self.call_view(
                [self.ship(0, nav_status=status)]
            )
            self.assertEqual(payload["count"], 0)
            self.assertEqual(TrajectoryPoint.objects.count(), 0)

        _, payload = self.call_view(
            [self.ship(0, matched_port_name="测试港")]
        )
        self.assertEqual(payload["count"], 0)
        _, payload = self.call_view([self.ship(0, at_dock=True)])
        self.assertEqual(payload["count"], 0)

    def test_ais_timestamp_and_upsert_are_used(self):
        ship = self.ship(0)
        self.call_view([ship])
        self.call_view([ship])

        point = TrajectoryPoint.objects.get()
        self.assertEqual(TrajectoryPoint.objects.count(), 1)
        self.assertEqual(point.timestamp, self.base_time)

    def test_stale_and_future_points_do_not_pollute_history(self):
        TrajectoryPoint.objects.create(
            mmsi="123456789",
            name="测试船",
            longitude=10,
            latitude=0,
            speed=10,
            course=90,
            timestamp=self.base_time + timedelta(days=1),
        )
        _, payload = self.call_view(
            [
                self.ship(0),
                self.ship(300, lon=20, mmsi="987654321"),
            ]
        )

        self.assertEqual(payload["stale_count"], 1)
        self.assertFalse(
            TrajectoryPoint.objects.filter(
                timestamp=self.base_time + timedelta(days=1)
            ).exists()
        )
