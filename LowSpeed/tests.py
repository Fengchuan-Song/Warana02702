import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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

from .models import LowSpeedPoint
from .views import LOW_SPEED_EVENT_CACHE_KEY, detect_low_speed


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "low-speed-tests",
    }
}

TEST_CONFIG = {
    "default_minimum_speed_knots": 1.5,
    "minimum_duration_seconds": 120,
    "minimum_observations": 5,
    "maximum_gap_seconds": 40,
    "analysis_window_minutes": 10,
    "retention_window_minutes": 20,
    "max_position_age_seconds": 60,
    "future_tolerance_seconds": 60,
    "max_valid_speed_knots": 102.2,
    "event_retention_minutes": 10,
    "eligible_nav_statuses": [0, 15],
    "allow_missing_nav_status": True,
    "monitored_only": False,
    "zones": [],
}


class LowSpeedRegistrationTests(SimpleTestCase):
    def test_detector_registration_and_cache_only_url(self):
        self.assertEqual(
            DETECTORS["detect-lowSpeedBoat"],
            "LowSpeed.views.detect_low_speed",
        )
        match = resolve("/LowSpeed/detectLowSpeed/")
        self.assertIs(match.func, cached_detection_result)
        self.assertEqual(
            match.kwargs["feature_id"],
            "detect-lowSpeedBoat",
        )


@override_settings(
    CACHES=TEST_CACHES,
    LOW_SPEED_DETECTION=TEST_CONFIG,
)
class LowSpeedDetectorTests(TestCase):
    base_time = datetime(2020, 12, 29, tzinfo=timezone.utc)

    def setUp(self):
        cache.delete(LOW_SPEED_EVENT_CACHE_KEY)
        self.factory = RequestFactory()

    def ship(
        self,
        seconds,
        speed=1,
        mmsi="123456789",
        lon=10,
        lat=10,
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
            "nav_status": nav_status,
        }
        ship.update(extra)
        return ship

    def call_view(self, ship_list, method="get"):
        request = getattr(self.factory, method)("/internal/detection/")
        request.ais_ship_list = ship_list
        response = detect_low_speed(request)
        payload = None
        if response.headers.get("Content-Type", "").startswith(
            "application/json"
        ):
            payload = json.loads(response.content)
        return response, payload

    def feed(
        self,
        seconds_list=(0, 30, 60, 90, 120),
        **overrides,
    ):
        payload = None
        latest_ship = None
        for seconds in seconds_list:
            latest_ship = self.ship(seconds, **overrides)
            _, payload = self.call_view([latest_ship])
        return payload, latest_ship

    def test_empty_snapshot_and_post_are_safe(self):
        response, payload = self.call_view([])
        post_response, _ = self.call_view([], method="post")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(post_response.status_code, 405)

    def test_invalid_records_are_skipped(self):
        _, payload = self.call_view(
            [
                self.ship(0, speed=None, mmsi="1"),
                self.ship(0, speed=102.3, mmsi="2"),
                self.ship(0, lon=999, mmsi="3"),
                {"mmsi": "4", "speed": 1},
            ]
        )

        self.assertEqual(payload["skipped_count"], 4)
        self.assertEqual(LowSpeedPoint.objects.count(), 0)

    def test_continuous_low_speed_alerts_at_duration_boundary(self):
        payload, _ = self.feed()

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["event"], "LowSpeed")
        self.assertEqual(result["name"], "未知目标")
        self.assertEqual(result["duration_seconds"], 120)
        self.assertEqual(result["observation_count"], 5)
        self.assertEqual(result["speed_knots"], 1)
        self.assertTrue(result["is_new"])

    def test_large_ais_gap_breaks_continuity(self):
        payload, _ = self.feed((0, 30, 180, 210, 240))

        self.assertEqual(payload["count"], 0)

    def test_normal_speed_ends_episode(self):
        self.feed((0, 30, 60))
        _, normal = self.call_view([self.ship(90, speed=1.5)])
        payload, _ = self.feed((120, 150, 180, 210))

        self.assertEqual(normal["count"], 0)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(LowSpeedPoint.objects.count(), 4)

    def test_non_underway_statuses_do_not_alert(self):
        for status in (1, 3, 5, 7, 8):
            LowSpeedPoint.objects.all().delete()
            cache.delete(LOW_SPEED_EVENT_CACHE_KEY)
            payload, _ = self.feed(nav_status=status)
            self.assertEqual(payload["count"], 0)
            self.assertEqual(LowSpeedPoint.objects.count(), 0)

    def test_missing_status_is_allowed_but_port_and_dock_are_not(self):
        payload, _ = self.feed(nav_status=None)
        self.assertEqual(payload["count"], 1)

        self.call_view(
            [self.ship(150, matched_port_name="测试港")]
        )
        self.assertEqual(LowSpeedPoint.objects.count(), 0)

        payload, _ = self.feed(
            (180, 210, 240, 270, 300),
            at_dock=True,
        )
        self.assertEqual(payload["count"], 0)
        self.assertEqual(LowSpeedPoint.objects.count(), 0)

    @patch(
        "LowSpeed.views.zones_containing_point",
        return_value=(object(),),
    )
    def test_position_inside_port_or_terminal_zone_does_not_alert(
        self,
        mock_zones_containing_point,
    ):
        payload, _ = self.feed()

        self.assertEqual(payload["count"], 0)
        self.assertEqual(LowSpeedPoint.objects.count(), 0)
        mock_zones_containing_point.assert_called()

    def test_latest_record_per_mmsi_is_used(self):
        _, payload = self.call_view(
            [
                self.ship(0, speed=1),
                self.ship(1, speed=5),
            ]
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(LowSpeedPoint.objects.count(), 0)

    def test_repeated_frames_upsert_and_keep_one_event(self):
        first, latest_ship = self.feed()
        _, repeated = self.call_view([latest_ship])
        _, continuing = self.call_view([self.ship(150)])

        self.assertFalse(repeated["results"][0]["is_new"])
        self.assertFalse(continuing["results"][0]["is_new"])
        self.assertEqual(
            first["results"][0]["event_id"],
            continuing["results"][0]["event_id"],
        )
        self.assertEqual(LowSpeedPoint.objects.count(), 6)

    def test_stale_ship_is_not_joined_to_history(self):
        self.feed((0, 30, 60))
        _, payload = self.call_view(
            [
                self.ship(90),
                self.ship(
                    300,
                    speed=5,
                    mmsi="987654321",
                    lon=12,
                ),
            ]
        )

        self.assertEqual(payload["stale_count"], 1)
        self.assertFalse(
            LowSpeedPoint.objects.filter(mmsi="123456789").exists()
        )

    def test_future_replay_points_are_removed(self):
        LowSpeedPoint.objects.create(
            mmsi="123456789",
            name="测试船",
            longitude=10,
            latitude=10,
            speed=1,
            speed_limit=1.5,
            zone_name="默认水域",
            timestamp=self.base_time + timedelta(days=1),
        )

        self.call_view([self.ship(0, speed=5)])

        self.assertFalse(
            LowSpeedPoint.objects.filter(
                timestamp=self.base_time + timedelta(days=1)
            ).exists()
        )

    @override_settings(
        LOW_SPEED_DETECTION={
            **TEST_CONFIG,
            "monitored_only": True,
            "zones": [
                {
                    "name": "航道最低航速区",
                    "bounds": [9, 9, 11, 11],
                    "minimum_speed_knots": 2,
                }
            ],
        }
    )
    def test_zone_threshold_and_monitored_only(self):
        _, outside = self.call_view([self.ship(0, lon=12)])
        inside, _ = self.feed(speed=1.8)

        self.assertEqual(outside["unmonitored_count"], 1)
        self.assertEqual(inside["count"], 1)
        self.assertEqual(
            inside["results"][0]["zone"],
            "航道最低航速区",
        )
        self.assertEqual(
            inside["results"][0]["minimum_speed_limit_knots"],
            2,
        )
