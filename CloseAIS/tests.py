from django.test import SimpleTestCase, override_settings

from AISRadar.alert_confirmation import clear_confirmation_state
from .views import detect_close_ais


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
class CloseAISDetectionTests(SimpleTestCase):
    def setUp(self):
        clear_confirmation_state()

    def tearDown(self):
        clear_confirmation_state()

    @staticmethod
    def _state(second, unmatched=True, event_id=None):
        return {
            "fusion_event_id": event_id or f"radar:0:{second}",
            "source_end_time": f"2026-08-02T12:00:{second:02d}+00:00",
            "matches": [{"ais_id": "413000001", "radar_id": "1-1"}],
            "unmatched_radar_targets": (
                [{"id": "2-1", "x": 29.7, "y": 122.4}]
                if unmatched
                else []
            ),
            "unmatched_ais_targets": [
                {"id": "413000002", "x": 29.8, "y": 122.5}
            ],
        }

    def test_alerts_only_on_third_consecutive_unmatched_frame(self):
        first = detect_close_ais(self._state(5))
        second = detect_close_ais(self._state(10))
        payload = detect_close_ais(self._state(15))

        self.assertEqual(first["count"], 0)
        self.assertEqual(second["count"], 0)
        self.assertEqual(payload["feature_id"], "detect-ais-off")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["results"][0]["radar_id"], "2-1")
        self.assertEqual(payload["results"][0]["location"], [122.4, 29.7])
        self.assertEqual(payload["results"][0]["confirmation_frames"], 3)
        self.assertNotIn("mmsi", payload["results"][0])

    def test_retry_does_not_increment_and_a_matched_frame_resets_streak(self):
        frame = self._state(5, event_id="radar:0:5")
        detect_close_ais(frame)
        retry = detect_close_ais(frame)
        second = detect_close_ais(self._state(10))
        matched = detect_close_ais(self._state(15, unmatched=False))
        restarted = detect_close_ais(self._state(20))

        self.assertEqual(retry["count"], 0)
        self.assertEqual(second["count"], 0)
        self.assertEqual(matched["candidate_count"], 0)
        self.assertEqual(restarted["count"], 0)

    def test_large_frame_gap_starts_a_new_streak(self):
        detect_close_ais(self._state(5))
        detect_close_ais(self._state(10))

        after_gap = detect_close_ais(
            {
                **self._state(15, event_id="radar:0:100"),
                "source_end_time": "2026-08-02T12:01:00+00:00",
            }
        )

        self.assertEqual(after_gap["count"], 0)
