import json
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from .models import HighSpeedPoint
from .views import detect_high_speed


class DetectHighSpeedTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get(
            "/HighSpeedBoat/detectHighSpeedBoat/"
        )

    def call_view(self, ship_list):
        with patch("HighSpeedBoat.views.cache.get", return_value=ship_list):
            response = detect_high_speed(self.request)
        return response, json.loads(response.content)

    @staticmethod
    def ship(timestamp, speed, mmsi="123456789"):
        return {
            "timestamp": timestamp,
            "mmsi": mmsi,
            "name": "测试船",
            "lon": 113.7,
            "lat": 22.4,
            "speed": speed,
        }

    def test_empty_cache_returns_empty_result(self):
        response, data = self.call_view([])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["results"], [])
        self.assertEqual(HighSpeedPoint.objects.count(), 0)

    def test_invalid_records_are_skipped(self):
        response, data = self.call_view(
            [
                {"mmsi": None, "speed": 35, "timestamp": "2020-01-01T00:00:00Z"},
                {"mmsi": "123", "speed": "invalid", "timestamp": "2020-01-01T00:00:00Z"},
                {"mmsi": "456", "speed": 35, "timestamp": "invalid"},
            ]
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["skipped_count"], 3)
        self.assertEqual(HighSpeedPoint.objects.count(), 0)

    def test_alerts_after_continuous_high_speed_duration(self):
        self.call_view([self.ship("2020-01-01T00:00:00Z", 31)])
        self.call_view([self.ship("2020-01-01T00:02:30Z", 32)])
        response, data = self.call_view(
            [self.ship("2020-01-01T00:05:00Z", 33)]
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["count"], 1)
        self.assertIn("300s", data["results"][0]["details"])

    def test_normal_speed_resets_high_speed_duration(self):
        self.call_view([self.ship("2020-01-01T00:00:00Z", 31)])
        self.call_view([self.ship("2020-01-01T00:02:00Z", 20)])
        response, data = self.call_view(
            [self.ship("2020-01-01T00:06:00Z", 35)]
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["count"], 0)

    def test_repeated_snapshot_is_not_inserted_twice(self):
        snapshot = [self.ship("2020-01-01T00:00:00Z", 31)]

        self.call_view(snapshot)
        self.call_view(snapshot)

        self.assertEqual(HighSpeedPoint.objects.count(), 1)

    def test_post_is_rejected(self):
        request = RequestFactory().post(
            "/HighSpeedBoat/detectHighSpeedBoat/"
        )

        response = detect_high_speed(request)

        self.assertEqual(response.status_code, 405)
