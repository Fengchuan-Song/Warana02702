import pandas as pd
from django.test import SimpleTestCase

from performance_tests.fusion_replay import replay_fusion


class RecordingMatcher:
    def __init__(self):
        self.frames = []

    def predict_tables(self, ais, radar):
        radar_time = pd.Timestamp(radar["DateTime"].max())
        ais_times = [pd.Timestamp(value) for value in ais["DateTime"]]
        self.frames.append((ais_times, radar_time))
        return {
            "model": {"algorithm": "test JPDA"},
            "windows": [
                {
                    "start_time": radar_time.isoformat(),
                    "end_time": radar_time.isoformat(),
                    "matches": [],
                    "unmatched_ais_targets": [],
                    "unmatched_radar_targets": [],
                }
            ],
        }


class FusionReplayTests(SimpleTestCase):
    def test_never_passes_future_ais_to_a_radar_frame(self):
        matcher = RecordingMatcher()
        ais = [
            {"timestamp": "2026-01-01T00:00:00Z", "mmsi": "413000001", "lat": 30, "lon": 120},
            {"timestamp": "2026-01-01T00:00:20Z", "mmsi": "413000001", "lat": 30, "lon": 120},
        ]
        radar = [
            {"timestamp": "2026-01-01T00:00:10Z", "id": "r1", "lat": 30, "lon": 120},
            {"timestamp": "2026-01-01T00:00:20Z", "id": "r1", "lat": 30, "lon": 120},
        ]
        states = list(replay_fusion(ais, radar, matcher=matcher))
        self.assertEqual(len(states), 2)
        for ais_times, radar_time in matcher.frames:
            self.assertTrue(all(value <= radar_time for value in ais_times))

