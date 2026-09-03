from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from AISData.behavior_recognition import (
    behavior_results_for_request,
    update_behavior_results,
)
from AISData.detection_queue import LATEST_ONLY_DETECTORS
from AISData.trajectory_history import get_ais_history


class SharedBehaviorRecognitionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.started_at = timezone.now()

    def tearDown(self):
        cache.clear()

    def _ship(self, seconds=0, **overrides):
        ship = {
            "mmsi": "413000001",
            "name": "共享行为测试船",
            "timestamp": (
                self.started_at + timedelta(seconds=seconds)
            ).isoformat(),
            "lon": 113.7,
            "lat": 22.3,
            "speed": 0.1,
            "heading": 90,
            "nav_status": 1,
            "at_dock": False,
            "matched_port_name": "",
        }
        ship.update(overrides)
        return ship

    def test_same_observation_is_classified_only_once(self):
        from AISData import behavior_recognition

        ship = self._ship()
        with patch.object(
            behavior_recognition,
            "_calculate",
            wraps=behavior_recognition._calculate,
        ) as calculate:
            first = behavior_results_for_request([ship])
            second = behavior_results_for_request([ship])

        self.assertEqual(calculate.call_count, 1)
        self.assertEqual(first[ship["mmsi"]], second[ship["mmsi"]])

    def test_new_observation_updates_once_and_retains_all_evidence(self):
        behavior_results_for_request([self._ship()])
        result = behavior_results_for_request(
            [
                self._ship(
                    30,
                    heading=135,
                    nav_status=5,
                    at_dock=True,
                    matched_port_name="测试港池",
                )
            ]
        )["413000001"]

        history = get_ais_history(["413000001"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["heading"], 135)
        self.assertEqual(history[-1]["nav_status"], 5)
        self.assertTrue(history[-1]["at_dock"])
        self.assertEqual(history[-1]["matched_port_name"], "测试港池")
        self.assertEqual(result["observed_at"], history[-1]["timestamp"])

    def test_out_of_order_observation_does_not_roll_result_back(self):
        newest = self._ship(60)
        current = update_behavior_results(
            [newest], append_history=True
        )["413000001"]
        late = update_behavior_results(
            [self._ship(30)], append_history=True
        )["413000001"]

        self.assertEqual(late["observed_at"], current["observed_at"])

    def test_three_consumers_coalesce_to_latest_trigger(self):
        self.assertTrue(
            {
                "detect-abnormalStaying",
                "detect-illegalAnchored",
                "detect-illegalStaying",
            }.issubset(LATEST_ONLY_DETECTORS)
        )
