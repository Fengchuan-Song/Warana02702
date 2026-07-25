import json
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from IllegalAnchored.zones import AUTHORIZED_ANCHORAGES

from .models import ParkingBuffer
from .utils import detect_parking_events
from .views import detectAbnormalParking


TEST_CONFIG = {
    "analysis_window_minutes": 120,
    "retention_window_minutes": 240,
    "max_speed_knots": 0.5,
    "distance_threshold_metres": 50,
    "min_duration_minutes": 30,
    "min_points": 3,
    "monitored_areas": [
        {
            "name": "测试监控区",
            "bounds": (114.40, 22.30, 114.70, 22.65),
        }
    ],
}


def polygon_centroid(points):
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


class ParkingEventAlgorithmTests(SimpleTestCase):
    def test_exact_point_and_duration_thresholds_are_inclusive(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            (22.5, 114.6, start, 0.1),
            (22.5, 114.6, start + timedelta(minutes=15), 0.1),
            (22.5, 114.6, start + timedelta(minutes=30), 0.1),
        ]

        events = detect_parking_events(
            points,
            distance_threshold_metres=50,
            time_threshold_minutes=30,
            min_points=3,
            max_speed_knots=0.5,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]), 3)

    def test_moving_point_breaks_stationary_episode(self):
        start = datetime(2020, 12, 27, tzinfo=datetime_timezone.utc)
        points = [
            (22.5, 114.6, start, 0.1),
            (22.5, 114.6, start + timedelta(minutes=15), 0.1),
            (22.5, 114.6, start + timedelta(minutes=20), 3.0),
            (22.5, 114.6, start + timedelta(minutes=30), 0.1),
            (22.5, 114.6, start + timedelta(minutes=60), 0.1),
        ]

        events = detect_parking_events(
            points,
            distance_threshold_metres=50,
            time_threshold_minutes=30,
            min_points=3,
            max_speed_knots=0.5,
        )

        self.assertEqual(events, [])


@override_settings(ABNORMAL_PARKING=TEST_CONFIG)
class AbnormalParkingDetectorTests(TestCase):
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

    def _detect(self, timestamp, **overrides):
        ship = {
            "timestamp": timestamp.isoformat(),
            "mmsi": "123456789",
            "name": "测试船",
            "lon": 114.60,
            "lat": 22.50,
            "speed": 0.1,
            "at_dock": False,
            "matched_port_name": "",
        }
        ship.update(overrides)
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [ship]
        return json.loads(detectAbnormalParking(request).content)

    def test_empty_snapshot_is_safe(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = []

        payload = json.loads(detectAbnormalParking(request).content)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["timestamp"], None)

    def test_historical_replay_uses_ais_time_window(self):
        self.assertEqual(self._detect(self.start)["count"], 0)
        self.assertEqual(
            self._detect(self.start + timedelta(minutes=15))["count"],
            0,
        )

        payload = self._detect(self.start + timedelta(minutes=30))

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["area"], "测试监控区")
        self.assertEqual(payload["results"][0]["duration_minutes"], 30)
        self.assertIn("异常停泊", payload["results"][0]["details"])
        self.assertNotIn("异常徘徊", payload["results"][0]["details"])

    def test_duplicate_snapshot_does_not_duplicate_buffer_point(self):
        self._detect(self.start)
        self._detect(self.start)

        self.assertEqual(ParkingBuffer.objects.count(), 1)

    def test_moving_sample_resets_episode(self):
        self._detect(self.start)
        self._detect(self.start + timedelta(minutes=15))
        self._detect(self.start + timedelta(minutes=20), speed=4.0)
        self._detect(self.start + timedelta(minutes=30))

        payload = self._detect(self.start + timedelta(minutes=60))

        self.assertEqual(payload["count"], 0)

    def test_port_matched_or_docked_ship_resets_history(self):
        self._detect(self.start)
        self._detect(self.start + timedelta(minutes=15))

        payload = self._detect(
            self.start + timedelta(minutes=30),
            matched_port_name="深圳港",
        )

        self.assertEqual(payload["count"], 0)
        self.assertFalse(
            ParkingBuffer.objects.filter(mmsi="123456789").exists()
        )

        payload = self._detect(
            self.start + timedelta(minutes=45),
            at_dock=True,
        )
        self.assertEqual(payload["count"], 0)
        self.assertFalse(
            ParkingBuffer.objects.filter(mmsi="123456789").exists()
        )

    def test_authorized_anchorage_does_not_alert_or_buffer(self):
        anchorage = next(
            zone
            for zone in AUTHORIZED_ANCHORAGES
            if zone["name"] == "大鹏湾LNG专用锚地"
        )
        lon, lat = polygon_centroid(anchorage["points"])

        for minutes in (0, 15, 30):
            payload = self._detect(
                self.start + timedelta(minutes=minutes),
                lon=lon,
                lat=lat,
            )

        self.assertEqual(payload["count"], 0)
        self.assertFalse(ParkingBuffer.objects.exists())

    def test_cleanup_uses_replay_timestamp(self):
        ParkingBuffer.objects.create(
            mmsi="old",
            name="旧轨迹",
            longitude=114.6,
            latitude=22.5,
            speed=0,
            timestamp=self.start - timedelta(hours=5),
        )

        self._detect(self.start)

        self.assertFalse(ParkingBuffer.objects.filter(mmsi="old").exists())

    def test_invalid_rows_are_skipped(self):
        request = self.factory.get("/internal/detection/")
        request.ais_ship_list = [
            {"mmsi": "bad", "timestamp": "invalid", "lon": 1, "lat": 2},
            {
                "mmsi": "123456789",
                "timestamp": self.start.isoformat(),
                "lon": 114.6,
                "lat": 22.5,
                "speed": 0.1,
            },
        ]

        payload = json.loads(detectAbnormalParking(request).content)

        self.assertEqual(payload["skipped_count"], 1)
        self.assertEqual(payload["count"], 0)
