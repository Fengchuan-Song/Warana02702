from django.test import SimpleTestCase

from .views import detect_forgery


class ForgeryDetectionTests(SimpleTestCase):
    def test_detects_only_unmatched_ais_targets(self):
        payload = detect_forgery(
            {
                "source_end_time": "2026-08-02T12:00:05",
                "matches": [{"ais_id": "413000001", "radar_id": "1-1"}],
                "unmatched_radar_targets": [
                    {"id": "2-1", "x": 29.7, "y": 122.4}
                ],
                "unmatched_ais_targets": [
                    {"id": "413000002", "x": 29.8, "y": 122.5}
                ],
            }
        )

        self.assertEqual(payload["feature_id"], "detect-spoofing")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["mmsi"], "413000002")
        self.assertEqual(payload["results"][0]["location"], [122.5, 29.8])
        self.assertNotIn("radar_id", payload["results"][0])
