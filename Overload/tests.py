from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .inference import OverloadDetector
from .management.commands.overload_stream_worker import (
    FEATURE_ID,
    publish_overload_detection,
)


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


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache"
        }
    }
)
class OverloadPublicationTests(SimpleTestCase):
    class RecordingChannelLayer:
        def __init__(self):
            self.events = []

        async def group_send(self, group, event):
            self.events.append((group, event))

    def setUp(self):
        cache.clear()
        self.channel_layer = self.RecordingChannelLayer()

    @staticmethod
    def payload(camera_key="harbor-01", confirmed=True):
        return {
            "success": True,
            "feature_id": FEATURE_ID,
            "count": 1 if confirmed else 0,
            "results": (
                [
                    {
                        "camera_key": camera_key,
                        "status": "超载预警",
                        "risk": "高风险",
                    }
                ]
                if confirmed
                else []
            ),
        }

    @patch(
        "Overload.management.commands.overload_stream_worker."
        "persist_detection_payload"
    )
    def test_overload_event_marks_first_continuing_and_reappearing_frames(
        self, persist
    ):
        first = publish_overload_detection(
            self.payload(), self.channel_layer
        )
        continuing = publish_overload_detection(
            self.payload(), self.channel_layer
        )
        publish_overload_detection(
            self.payload(confirmed=False), self.channel_layer
        )
        reappearing = publish_overload_detection(
            self.payload(), self.channel_layer
        )

        self.assertIs(first["results"][0]["is_new"], True)
        self.assertIs(continuing["results"][0]["is_new"], False)
        self.assertIs(reappearing["results"][0]["is_new"], True)
        self.assertEqual(
            first["results"][0]["prediction_id"],
            continuing["results"][0]["prediction_id"],
        )
        self.assertNotEqual(
            continuing["results"][0]["prediction_id"],
            reappearing["results"][0]["prediction_id"],
        )
        self.assertEqual(persist.call_count, 4)

    @patch(
        "Overload.management.commands.overload_stream_worker."
        "persist_detection_payload"
    )
    def test_overload_cameras_have_independent_event_state(self, persist):
        first_camera = publish_overload_detection(
            self.payload("harbor-01"), self.channel_layer
        )
        second_camera = publish_overload_detection(
            self.payload("harbor-02"), self.channel_layer
        )

        self.assertIs(first_camera["results"][0]["is_new"], True)
        self.assertIs(second_camera["results"][0]["is_new"], True)
