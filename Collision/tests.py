import json

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from .views import COLLISION_STATE_CACHE_KEY, detect_collision


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "collision-tests",
    }
}

TEST_CONFIG = {
    "warning_tcpa_minutes": 15,
    "critical_tcpa_minutes": 5,
    "warning_dcpa_metres": 300,
    "critical_dcpa_metres": 150,
    "immediate_distance_metres": 100,
    "vessel_buffer_metres": 50,
    "minimum_relative_speed_mps": 0.2,
    "max_position_age_seconds": 120,
    "event_retention_minutes": 30,
}


@override_settings(
    CACHES=TEST_CACHES,
    COLLISION_DETECTION=TEST_CONFIG,
)
class CollisionDetectionTests(TestCase):
    def setUp(self):
        cache.delete(COLLISION_STATE_CACHE_KEY)
        self.factory = RequestFactory()

    def detect(self, ships):
        request = self.factory.get("/Collision/detectCollision/")
        request.ais_ship_list = ships
        response = detect_collision(request)
        return json.loads(response.content)

    @staticmethod
    def ship(
        mmsi,
        longitude,
        latitude=22.5,
        speed=10,
        course=90,
        timestamp="2026-07-24T00:00:00Z",
        name="",
    ):
        return {
            "mmsi": mmsi,
            "name": name,
            "longitude": longitude,
            "latitude": latitude,
            "speed": speed,
            "course": course,
            "timestamp": timestamp,
        }

    def test_head_on_ships_are_warned_before_they_are_close(self):
        payload = self.detect(
            [
                self.ship("111000111", 114.000, course=90, name="甲船"),
                self.ship("222000222", 114.020, course=270, name="乙船"),
            ]
        )

        self.assertEqual(payload["count"], 1)
        result = payload["results"][0]
        self.assertEqual(result["risk"], "高风险")
        self.assertGreater(result["current_distance_metres"], 2000)
        self.assertLess(result["dcpa_metres"], 1)
        self.assertLess(result["tcpa_minutes"], 5)
        self.assertEqual(result["mmsi"], "111000111")
        self.assertEqual(result["other_mmsi"], "222000222")
        self.assertIn("location", result)
        self.assertIn("details", result)

    def test_parallel_ships_at_same_speed_do_not_trigger(self):
        payload = self.detect(
            [
                self.ship("111000111", 114.000, latitude=22.5000),
                self.ship("222000222", 114.000, latitude=22.5005),
            ]
        )

        self.assertEqual(payload["count"], 0)

    def test_moving_ship_approaching_stationary_ship_triggers(self):
        payload = self.detect(
            [
                self.ship("111000111", 114.000, speed=10, course=90),
                self.ship("222000222", 114.010, speed=0, course=0),
            ]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["risk"], "高风险")

    def test_larger_predicted_miss_distance_is_medium_risk(self):
        payload = self.detect(
            [
                self.ship("111000111", 114.000, speed=10, course=90),
                self.ship(
                    "222000222",
                    114.010,
                    latitude=22.5018,
                    speed=0,
                    course=0,
                ),
            ]
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["risk"], "中风险")
        self.assertGreater(payload["results"][0]["dcpa_metres"], 150)
        self.assertLess(payload["results"][0]["dcpa_metres"], 300)

    def test_diverging_ships_do_not_trigger(self):
        payload = self.detect(
            [
                self.ship("111000111", 114.000, course=270),
                self.ship("222000222", 114.005, course=90),
            ]
        )

        self.assertEqual(payload["count"], 0)

    def test_empty_and_invalid_input_are_safe(self):
        empty = self.detect([])
        invalid = self.detect(
            [
                {
                    "mmsi": "bad",
                    "longitude": "not-a-number",
                    "latitude": 22.5,
                }
            ]
        )

        self.assertEqual(empty["count"], 0)
        self.assertIsNone(empty["timestamp"])
        self.assertEqual(invalid["count"], 0)
        self.assertEqual(invalid["skipped_count"], 1)

    def test_stale_positions_are_not_compared(self):
        payload = self.detect(
            [
                self.ship(
                    "111000111",
                    114.000,
                    timestamp="2026-07-24T00:00:00Z",
                ),
                self.ship(
                    "222000222",
                    114.010,
                    speed=0,
                    timestamp="2026-07-24T00:05:00Z",
                ),
            ]
        )

        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["stale_count"], 1)

    def test_repeated_pair_keeps_event_id_and_is_not_new(self):
        ships = [
            self.ship("111000111", 114.000, course=90),
            self.ship("222000222", 114.020, course=270),
        ]
        first = self.detect(ships)["results"][0]

        for ship in ships:
            ship["timestamp"] = "2026-07-24T00:00:30Z"
        second = self.detect(ships)["results"][0]

        self.assertTrue(first["is_new"])
        self.assertFalse(second["is_new"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(
            first["first_detected_at"], second["first_detected_at"]
        )
