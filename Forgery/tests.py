from django.test import SimpleTestCase, override_settings

from AISRadar.alert_confirmation import clear_confirmation_state
from .views import detect_forgery


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    AIS_RADAR_ALERT_CONFIRMATION={
        "default": {
            "confirmation_frames": 3,
            "max_gap_seconds": 30,
            "state_ttl_seconds": 600,
        }
    },
)
class ForgeryDetectionTests(SimpleTestCase):
    def setUp(self):
        clear_confirmation_state()

    def tearDown(self):
        clear_confirmation_state()

    @staticmethod
    def _state(second, unmatched=True):
        return {
                "fusion_event_id": f"radar:0:{second}",
                "source_end_time": f"2026-08-02T12:00:{second:02d}+00:00",
                "matches": [{"ais_id": "413000001", "radar_id": "1-1"}],
                "unmatched_radar_targets": [
                    {"id": "2-1", "x": 29.7, "y": 122.4}
                ],
                "unmatched_ais_targets": ([
                    {"id": "413000002", "x": 29.8, "y": 122.5}
                ] if unmatched else []),
            }

    def test_alerts_only_on_third_consecutive_unmatched_frame(self):
        first = detect_forgery(self._state(5))
        second = detect_forgery(self._state(10))
        payload = detect_forgery(self._state(15))

        self.assertEqual(first["count"], 0)
        self.assertEqual(second["count"], 0)
        self.assertEqual(payload["feature_id"], "detect-spoofing")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["results"][0]["mmsi"], "413000002")
        self.assertEqual(payload["results"][0]["location"], [122.5, 29.8])
        self.assertEqual(payload["results"][0]["confirmation_frames"], 3)
        self.assertNotIn("radar_id", payload["results"][0])

    def test_a_radar_match_resets_the_ais_only_streak(self):
        detect_forgery(self._state(5))
        detect_forgery(self._state(10))
        matched = detect_forgery(self._state(15, unmatched=False))
        restarted = detect_forgery(self._state(20))

        self.assertEqual(matched["candidate_count"], 0)
        self.assertEqual(restarted["count"], 0)
