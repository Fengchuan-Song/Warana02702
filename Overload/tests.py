from django.test import SimpleTestCase

from .inference import OverloadDetector


class OverloadDetectorClassificationTests(SimpleTestCase):
    def test_no_vessel_does_not_trigger_overload(self):
        inference = OverloadDetector._classify_detections([], [])

        self.assertFalse(inference.vessel_present)
        self.assertFalse(inference.overloaded)
        self.assertIn("未检测到船舶", inference.reason)

    def test_vessel_without_load_line_triggers_overload(self):
        vessel = {
            "class_id": 8,
            "class_name": "boat",
            "confidence": 0.82,
            "xyxy": [10, 20, 100, 120],
        }

        inference = OverloadDetector._classify_detections([], [vessel])

        self.assertTrue(inference.vessel_present)
        self.assertTrue(inference.overloaded)
        self.assertEqual(inference.vessel_confidence, 0.82)
        self.assertIn("未检测到载重线", inference.reason)

    def test_light_load_is_normal(self):
        load_line = {
            "class_id": 0,
            "class_name": "light load",
            "confidence": 0.9,
            "xyxy": [10, 20, 100, 120],
        }

        inference = OverloadDetector._classify_detections([load_line])

        self.assertTrue(inference.vessel_present)
        self.assertFalse(inference.overloaded)

    def test_full_load_triggers_overload(self):
        load_line = {
            "class_id": 1,
            "class_name": "full load",
            "confidence": 0.88,
            "xyxy": [10, 20, 100, 120],
        }

        inference = OverloadDetector._classify_detections([load_line])

        self.assertTrue(inference.vessel_present)
        self.assertTrue(inference.overloaded)

# Create your tests here.
