from django.test import SimpleTestCase

from performance_tests.ais_replay import _normalise


class AISReplayTests(SimpleTestCase):
    def test_orders_by_absolute_source_time_not_timestamp_text(self):
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "mmsi": "413000001",
                "lon": 120,
                "lat": 30,
            },
            {
                # This is 30 minutes earlier than the first record in UTC.
                "timestamp": "2026-01-01T00:30:00+01:00",
                "mmsi": "413000001",
                "lon": 119,
                "lat": 30,
            },
        ]
        normalised, invalid = _normalise(records)
        self.assertEqual(invalid, 0)
        self.assertEqual(normalised[0][2]["lon"], 119)

