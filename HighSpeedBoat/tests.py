import json
from datetime import datetime, timedelta, timezone

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from .models import HighSpeedPoint
from .views import HIGH_SPEED_EVENT_CACHE_KEY, detect_high_speed


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "high-speed-tests",
    }
}

TEST_CONFIG = {
    "default_speed_limit_knots": 30,
    "minimum_duration_seconds": 120,
    "maximum_gap_seconds": 40,
    "analysis_window_minutes": 10,
    "retention_window_minutes": 20,
    "max_position_age_seconds": 60,
    "future_tolerance_seconds": 60,
    "max_valid_speed_knots": 102.2,
    "high_risk_excess_knots": 10,
    "event_retention_minutes": 10,
}


@override_settings(
    CACHES=TEST_CACHES,
    HIGH_SPEED_DETECTION=TEST_CONFIG,
)
class DetectHighSpeedTests(TestCase):
    base_time = datetime(2026, 7, 24, tzinfo=timezone.utc)

    def setUp(self):
        cache.delete(HIGH_SPEED_EVENT_CACHE_KEY)
        self.factory = RequestFactory()

    def call_view(self, ship_list, method="get"):
        request = getattr(self.factory, method)(
            "/HighSpeedBoat/detectHighSpeedBoat/"
        )
        request.ais_ship_list = ship_list
        response = detect_high_speed(request)
        payload = (
            json.loads(response.content)
            if response.content
            else {}
        )
        return response, payload

    def ship(
        self,
        seconds,
        speed,
        mmsi="123456789",
        lon=113.7,
        lat=22.4,
        **extra,
    ):
        ship = {
            "timestamp": (
                self.base_time + timedelta(seconds=seconds)
            ).isoformat(),
            "mmsi": mmsi,
            "name": "测试船",
            "lon": lon,
            "lat": lat,
            "speed": speed,
        }
        ship.update(extra)
        return ship

    def feed(
        self,
        speeds,
        start_seconds=0,
        interval_seconds=30,
        mmsi="123456789",
        **extra,
    ):
        payload = None
        latest_ship = None
        for index, speed in enumerate(speeds):
            latest_ship = self.ship(
                start_seconds + index * interval_seconds,
                speed,
                mmsi=mmsi,
                **extra,
            )
            _, payload = self.call_view([latest_ship])
        return payload, latest_ship

    def test_empty_and_invalid_records_are_safe(self):
        _, empty = self.call_view([])
        _, invalid = self.call_view(
            [
                {
                    "mmsi": None,
                    "speed": 35,
                    "timestamp": self.base_time.isoformat(),
                },
                self.ship(0, "invalid", mmsi="123"),
                self.ship(0, 35, mmsi="456", lon="bad"),
                self.ship(0, 102.3, mmsi="789"),
            ]
        )

        self.assertEqual(empty["count"], 0)
        self.assertIsNone(empty["timestamp"])
        self.assertEqual(invalid["skipped_count"], 4)
        self.assertEqual(HighSpeedPoint.objects.count(), 0)

    def test_requires_speed_above_30_for_full_duration(self):
        _, at_threshold = self.call_view([self.ship(0, 30)])
        before_duration, _ = self.feed(
            [30.01, 31, 32, 33],
            start_seconds=1,
        )
        _, after_duration = self.call_view([self.ship(121, 34)])

        self.assertEqual(at_threshold["count"], 0)
        self.assertEqual(before_duration["count"], 0)
        self.assertEqual(after_duration["count"], 1)
        result = after_duration["results"][0]
        self.assertEqual(result["event"], "HighSpeedBoat")
        self.assertEqual(result["speed_limit_knots"], 30)
        self.assertEqual(result["duration_seconds"], 120)
        self.assertEqual(result["observation_count"], 5)
        self.assertTrue(result["is_new"])
        self.assertFalse(after_duration["rule"]["uses_ship_type"])

    def test_ship_type_is_not_required_or_used(self):
        without_type, _ = self.feed([35, 35, 35, 35, 35])
        cargo_ship, _ = self.feed(
            [36, 36, 36, 36, 36],
            mmsi="987654321",
            ship_type="Cargo",
        )

        self.assertEqual(without_type["count"], 1)
        self.assertEqual(cargo_ship["count"], 1)
        self.assertEqual(
            cargo_ship["results"][0]["ship_type"],
            "Cargo",
        )

    def test_large_ais_gap_breaks_continuous_duration(self):
        self.call_view([self.ship(0, 35)])
        self.call_view([self.ship(30, 35)])
        for seconds in (180, 210, 240, 270):
            _, payload = self.call_view([self.ship(seconds, 35)])

        self.assertEqual(payload["count"], 0)

    def test_all_incremental_records_are_stored_and_latest_drives_output(self):
        _, payload = self.call_view(
            [
                self.ship(0, 35),
                self.ship(1, 20),
            ]
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(HighSpeedPoint.objects.count(), 2)
        self.assertEqual(
            list(
                HighSpeedPoint.objects.order_by("timestamp").values_list(
                    "speed", flat=True
                )
            ),
            [35, 20],
        )

    def test_one_incremental_batch_can_establish_high_speed_duration(self):
        ships = [
            self.ship(seconds, 35)
            for seconds in (0, 30, 60, 90, 120)
        ]

        _, payload = self.call_view(ships)

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["duration_seconds"], 120)
        self.assertEqual(HighSpeedPoint.objects.count(), 5)

    def test_unmodified_active_vessel_is_retained_until_incremental_gap(self):
        self.feed([35, 35, 35, 35, 35])

        _, retained = self.call_view(
            [self.ship(150, 20, mmsi="987654321")]
        )
        _, expired = self.call_view(
            [self.ship(180, 20, mmsi="987654321")]
        )

        self.assertEqual(retained["count"], 1)
        self.assertEqual(retained["results"][0]["mmsi"], "123456789")
        self.assertFalse(retained["results"][0]["is_new"])
        self.assertEqual(expired["count"], 0)

    def test_repeated_high_speed_frames_keep_one_event(self):
        first, latest_ship = self.feed([35, 35, 35, 35, 35])
        _, repeated = self.call_view([latest_ship])
        _, continuing = self.call_view([self.ship(150, 36)])

        self.assertTrue(first["results"][0]["is_new"])
        self.assertFalse(repeated["results"][0]["is_new"])
        self.assertFalse(continuing["results"][0]["is_new"])
        self.assertEqual(
            first["results"][0]["event_id"],
            continuing["results"][0]["event_id"],
        )
        self.assertEqual(
            continuing["results"][0]["duration_seconds"],
            150,
        )
        self.assertEqual(HighSpeedPoint.objects.count(), 6)

    def test_speed_at_or_below_30_ends_event(self):
        first, _ = self.feed([35, 35, 35, 35, 35])
        first_event_id = first["results"][0]["event_id"]
        _, cleared = self.call_view([self.ship(150, 30)])
        second, _ = self.feed(
            [35, 35, 35, 35, 35],
            start_seconds=180,
        )

        self.assertEqual(cleared["count"], 0)
        self.assertTrue(second["results"][0]["is_new"])
        self.assertNotEqual(
            first_event_id,
            second["results"][0]["event_id"],
        )

    def test_stale_ship_is_not_evaluated(self):
        _, payload = self.call_view(
            [
                self.ship(0, 35),
                self.ship(300, 10, mmsi="987654321"),
            ]
        )

        self.assertEqual(payload["stale_count"], 1)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(HighSpeedPoint.objects.count(), 1)

    def test_future_points_from_previous_replay_are_removed(self):
        HighSpeedPoint.objects.create(
            mmsi="123456789",
            speed=35,
            longitude=113.7,
            latitude=22.4,
            speed_limit=30,
            zone_name="默认水域",
            timestamp=self.base_time + timedelta(days=1),
        )

        self.call_view([self.ship(0, 20)])

        self.assertFalse(
            HighSpeedPoint.objects.filter(
                timestamp=self.base_time + timedelta(days=1)
            ).exists()
        )

    def test_post_is_rejected(self):
        response, _ = self.call_view([], method="post")

        self.assertEqual(response.status_code, 405)
