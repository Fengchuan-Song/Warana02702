import json
from datetime import datetime, timedelta, timezone

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from .models import DoubleDraggingPoint
from .views import (
    DOUBLE_DRAGGING_STATE_CACHE_KEY,
    detect_double_dragging,
)


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "double-dragging-tests",
    }
}

TEST_CONFIG = {
    "analysis_window_minutes": 5,
    "retention_window_minutes": 10,
    "min_duration_minutes": 2,
    "min_aligned_points": 5,
    "alignment_tolerance_seconds": 20,
    "min_pair_distance_metres": 100,
    "max_pair_distance_metres": 1000,
    "candidate_distance_margin_metres": 200,
    "min_operating_speed_knots": 1,
    "max_operating_speed_knots": 8,
    "max_speed_difference_knots": 1,
    "max_course_difference_degrees": 15,
    "max_lateral_deviation_degrees": 20,
    "max_distance_std_metres": 50,
    "min_distance_ratio": 0.8,
    "min_course_ratio": 0.8,
    "min_lateral_ratio": 0.8,
    "min_speed_similarity_ratio": 0.8,
    "max_position_age_seconds": 60,
    "future_tolerance_seconds": 60,
    "event_retention_minutes": 10,
}


@override_settings(
    CACHES=TEST_CACHES,
    DOUBLE_DRAGGING_DETECTION=TEST_CONFIG,
)
class DoubleDraggingDetectionTests(TestCase):
    base_time = datetime(2026, 7, 24, tzinfo=timezone.utc)

    def setUp(self):
        cache.delete(DOUBLE_DRAGGING_STATE_CACHE_KEY)
        self.factory = RequestFactory()

    def detect(self, ships):
        request = self.factory.get(
            "/DoubleDragging/detectDoubleDragging/"
        )
        request.ais_ship_list = ships
        response = detect_double_dragging(request)
        return json.loads(response.content)

    @staticmethod
    def ship(
        mmsi,
        longitude,
        latitude,
        timestamp,
        speed=4,
        course=90,
        name="",
    ):
        return {
            "mmsi": mmsi,
            "name": name,
            "lon": longitude,
            "lat": latitude,
            "speed": speed,
            "course": course,
            "timestamp": timestamp.isoformat(),
        }

    def feed_pair(
        self,
        formation="lateral",
        first_course=90,
        second_course=90,
        first_speed=4,
        second_speed=4,
        point_count=5,
        interval_seconds=30,
    ):
        payload = None
        latest_ships = None
        for index in range(point_count):
            timestamp = self.base_time + timedelta(
                seconds=index * interval_seconds
            )
            longitude = 114 + index * 0.0005
            if formation == "lateral":
                second_longitude = longitude
                second_latitude = 22.5027
            else:
                second_longitude = longitude + 0.003
                second_latitude = 22.5
            latest_ships = [
                self.ship(
                    "111000111",
                    longitude,
                    22.5,
                    timestamp,
                    speed=first_speed,
                    course=first_course,
                    name="甲船",
                ),
                self.ship(
                    "222000222",
                    second_longitude,
                    second_latitude,
                    timestamp,
                    speed=second_speed,
                    course=second_course,
                    name="乙船",
                ),
            ]
            payload = self.detect(latest_ships)
        return payload, latest_ships

    def test_sustained_parallel_lateral_pair_triggers_once(self):
        payload, _ = self.feed_pair()

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["candidate_pair_count"], 1)
        self.assertEqual(payload["compared_pair_count"], 1)
        result = payload["results"][0]
        self.assertEqual(
            result["pair_mmsi"],
            ["111000111", "222000222"],
        )
        self.assertEqual(result["risk"], "高风险")
        self.assertGreaterEqual(result["duration_minutes"], 2)
        self.assertGreaterEqual(result["lateral_formation_ratio"], 0.8)
        self.assertIn("other_mmsi", result)
        self.assertIn("location", result)
        self.assertIn("details", result)

    def test_following_convoy_is_not_pair_trawling(self):
        payload, _ = self.feed_pair(formation="following")

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["compared_pair_count"], 1)

    def test_opposite_courses_are_rejected_by_candidate_filter(self):
        payload, _ = self.feed_pair(second_course=270)

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["candidate_pair_count"], 0)

    def test_large_speed_difference_is_rejected(self):
        payload, _ = self.feed_pair(second_speed=7)

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["candidate_pair_count"], 0)

    def test_minimum_duration_is_required(self):
        payload, _ = self.feed_pair(interval_seconds=20)

        self.assertEqual(payload["count"], 0)

    def test_duplicate_frame_does_not_duplicate_points_or_event(self):
        first_payload, latest_ships = self.feed_pair()
        repeated_payload = self.detect(latest_ships)

        self.assertTrue(first_payload["results"][0]["is_new"])
        self.assertFalse(repeated_payload["results"][0]["is_new"])
        self.assertEqual(
            first_payload["results"][0]["event_id"],
            repeated_payload["results"][0]["event_id"],
        )
        self.assertEqual(DoubleDraggingPoint.objects.count(), 10)

    def test_empty_invalid_and_stale_input_are_safe(self):
        empty = self.detect([])
        invalid = self.detect(
            [
                {
                    "mmsi": None,
                    "lon": "bad",
                    "lat": 22.5,
                }
            ]
        )
        stale = self.detect(
            [
                self.ship(
                    "111000111",
                    114,
                    22.5,
                    self.base_time,
                ),
                self.ship(
                    "222000222",
                    114,
                    22.5027,
                    self.base_time + timedelta(minutes=5),
                ),
            ]
        )

        self.assertEqual(empty["count"], 0)
        self.assertIsNone(empty["timestamp"])
        self.assertEqual(invalid["skipped_count"], 1)
        self.assertEqual(stale["stale_count"], 1)

    def test_future_points_from_an_old_replay_are_removed(self):
        future_timestamp = self.base_time + timedelta(days=1)
        DoubleDraggingPoint.objects.create(
            mmsi="111000111",
            lat=22.5,
            lng=114,
            sog=4,
            cog=90,
            timestamp=future_timestamp,
        )

        self.detect(
            [
                self.ship(
                    "111000111",
                    114,
                    22.5,
                    self.base_time,
                )
            ]
        )

        self.assertFalse(
            DoubleDraggingPoint.objects.filter(
                timestamp=future_timestamp
            ).exists()
        )
