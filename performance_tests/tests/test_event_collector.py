from django.test import SimpleTestCase

from performance_tests.event_collector import EventCollector


class EventCollectorTests(SimpleTestCase):
    def test_deduplicates_a_warning_lifecycle_and_uses_stable_id(self):
        payload = {
            "success": True,
            "results": [{"mmsi": "413000001", "detail": "warning"}],
        }
        first = EventCollector("S001", "detect-highSpeedBoat")
        second = EventCollector("S001", "detect-highSpeedBoat")
        first.collect(payload)
        first.collect(payload)
        second.collect(payload)
        self.assertEqual(first.prediction, 1)
        self.assertEqual(len(first.events), 1)
        self.assertEqual(
            first.first_event["prediction_id"],
            second.first_event["prediction_id"],
        )

