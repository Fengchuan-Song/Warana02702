from django.test import SimpleTestCase

from performance_tests.metrics import calculate_metrics
from performance_tests.schemas import PredictionRecord


def _record(ground_truth, prediction):
    return PredictionRecord(
        sample_id=f"{ground_truth}-{prediction}",
        track_id="track",
        feature_id="detect-highSpeedBoat",
        ground_truth=ground_truth,
        prediction=prediction,
        result="",
    )


class MetricsTests(SimpleTestCase):
    def test_binary_metrics_and_errors(self):
        records = [
            _record(1, 1),
            _record(1, 0),
            _record(0, 1),
            _record(0, 0),
            _record(1, None),
        ]
        result = calculate_metrics(records, feature_id="detect-highSpeedBoat")
        self.assertEqual(
            (result["tp"], result["fp"], result["tn"], result["fn"]),
            (1, 1, 1, 1),
        )
        self.assertEqual(result["accuracy"], 0.5)
        self.assertEqual(result["false_alarm_rate"], 0.5)
        self.assertEqual(result["miss_rate"], 0.5)
        self.assertEqual(result["error_count"], 1)

    def test_undefined_denominators_return_none(self):
        result = calculate_metrics([_record(1, 1)])
        self.assertIsNone(result["false_alarm_rate"])
        self.assertEqual(result["miss_rate"], 0)
        self.assertTrue(result["warnings"])

