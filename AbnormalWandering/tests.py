import json
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from IllegalAnchored.zones import AUTHORIZED_ANCHORAGES

from .models import MonitorRegion, TrajectoryBuffer
from .utils import detect_loitering_events
from .views import receive_realtime_point


TEST_CONFIG = {
    "analysis_window_minutes": 30,
    "retention_window_minutes": 60,
    "min_points": 10,
    "min_duration_minutes": 10,
    "min_path_distance_metres": 300,
    "min_leg_distance_metres": 20,
    "min_turn_angle_degrees": 45,
    "min_turn_count": 3,
    "max_displacement_ratio": 0.65,
    "max_gap_minutes": 5,
    "min_area_point_ratio": 0.5,
    "monitored_areas": [
        {
            "name": "测试徘徊监控区",
            "bounds": (114.59, 22.49, 114.61, 22.51),
        }
    ],
}

LOITERING_POSITIONS = [
    (114.6000, 22.5000),
    (114.6010, 22.5000),
    (114.6010, 22.5010),
    (114.6000, 22.5010),
    (114.6000, 22.5000),
    (114.5990, 22.5000),
    (114.5990, 22.4990),
    (114.6000, 22.4990),
    (114.6000, 22.5000),
    (114.6010, 22.5000),
]


def make_points(positions, start=None, gap_minutes=2):
    start = start or datetime(
        2020,
        12,
        27,
        tzinfo=datetime_timezone.utc,
    )
    return [
        {
            "longitude": lon,
            "latitude": lat,
            "timestamp": start + timedelta(minutes=index * gap_minutes),
            "speed": 5.0,
            "course": 0,
        }
        for index, (lon, lat) in enumerate(positions)
    ]


def polygon_centroid(points):
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


@override_settings(ABNORMAL_WANDERING=TEST_CONFIG)
class LoiteringAlgorithmTests(SimpleTestCase):
    def test_repeated_turning_is_detected(self):
        events = detect_loitering_events(
            make_points(LOITERING_POSITIONS),
        )

        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(events[0]["turn_count"], 3)
        self.assertLessEqual(events[0]["displacement_ratio"], 0.65)

    def test_straight_route_is_not_detected(self):
        positions = [
            (114.595 + index * 0.001, 22.5)
            for index in range(10)
        ]

        self.assertEqual(
            detect_loitering_events(make_points(positions)),
            [],
        )

    def test_stationary_jitter_is_not_wandering(self):
        positions = [(114.6, 22.5)] * 10

        self.assertEqual(
            detect_loitering_events(make_points(positions)),
            [],
        )

    def test_large_time_gap_splits_trajectory(self):
        points = make_points(LOITERING_POSITIONS)
        for index in range(5, len(points)):
            points[index]["timestamp"] += timedelta(minutes=10)

        self.assertEqual(detect_loitering_events(points), [])


@override_settings(ABNORMAL_WANDERING=TEST_CONFIG)
class AbnormalWanderingDetectorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.start = datetime(
            2020,
            12,
            27,
            tzinfo=datetime_timezone.utc,
        )

    def tearDown(self):
        cache.clear()

    def _detect(self, index, **overrides):
        lon, lat = LOITERING_POSITIONS[index]
        ship = {
            "timestamp": (
                self.start + timedelta(minutes=index * 2)
            ).isoformat(),
            "mmsi": "123456789",
            "name": "测试船",
            "lon": lon,
            "lat": lat,
            "speed": 5.0,
            "course": 0,
            "at_dock": False,
            "matched_port_name": "",
        }
        ship.update(overrides)
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [ship]
        return json.loads(receive_realtime_point(request).content)

    def test_empty_snapshot_is_safe(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = []

        payload = json.loads(receive_realtime_point(request).content)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["timestamp"])

    def test_historical_replay_detects_loitering(self):
        for index in range(len(LOITERING_POSITIONS)):
            payload = self._detect(index)

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["area"], "测试徘徊监控区")
        self.assertGreaterEqual(result["turn_count"], 3)
        self.assertIn("明显转向", result["details"])
        self.assertNotIn("待补充", result["details"])

    def test_duplicate_snapshot_does_not_duplicate_buffer_point(self):
        self._detect(0)
        self._detect(0)

        self.assertEqual(TrajectoryBuffer.objects.count(), 1)

    def test_port_or_docked_operation_resets_history(self):
        for index in range(5):
            self._detect(index)

        payload = self._detect(5, matched_port_name="深圳港")

        self.assertEqual(payload["count"], 0)
        self.assertFalse(TrajectoryBuffer.objects.exists())

        payload = self._detect(6, at_dock=True)
        self.assertEqual(payload["count"], 0)
        self.assertFalse(TrajectoryBuffer.objects.exists())

    def test_authorized_anchorage_is_excluded(self):
        anchorage = next(
            zone
            for zone in AUTHORIZED_ANCHORAGES
            if zone["name"] == "大鹏湾LNG专用锚地"
        )
        lon, lat = polygon_centroid(anchorage["points"])
        MonitorRegion.objects.create(
            name="合法锚地测试区",
            min_lon=lon - 0.01,
            max_lon=lon + 0.01,
            min_lat=lat - 0.01,
            max_lat=lat + 0.01,
            is_active=True,
        )

        for index in range(10):
            payload = self._detect(index, lon=lon, lat=lat)

        self.assertEqual(payload["count"], 0)
        self.assertFalse(TrajectoryBuffer.objects.exists())

    def test_active_database_region_overrides_default_region(self):
        MonitorRegion.objects.create(
            name="其他监控区",
            min_lon=115.0,
            max_lon=115.1,
            min_lat=23.0,
            max_lat=23.1,
            is_active=True,
        )

        payload = self._detect(0)

        self.assertEqual(payload["count"], 0)
        self.assertFalse(TrajectoryBuffer.objects.exists())

    def test_cleanup_uses_replay_timestamp(self):
        TrajectoryBuffer.objects.create(
            mmsi="old",
            name="旧轨迹",
            longitude=114.6,
            latitude=22.5,
            course=0,
            speed=1,
            timestamp=self.start - timedelta(minutes=61),
        )

        self._detect(0)

        self.assertFalse(
            TrajectoryBuffer.objects.filter(mmsi="old").exists()
        )

    def test_invalid_rows_are_skipped(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [
            {
                "mmsi": "bad",
                "timestamp": "invalid",
                "lon": 1,
                "lat": 2,
            },
            {
                "mmsi": "123456789",
                "timestamp": self.start.isoformat(),
                "lon": 114.6,
                "lat": 22.5,
                "speed": 1,
                "course": 0,
            },
        ]

        payload = json.loads(receive_realtime_point(request).content)

        self.assertEqual(payload["skipped_count"], 1)
        self.assertEqual(payload["count"], 0)
