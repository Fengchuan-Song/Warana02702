from pathlib import Path
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
import pandas as pd
import torch

from AISData.consumers import AisConsumer
from .anomaly_detection import build_fusion_detection_results
from .fusion_state import build_fusion_state, clear_fusion_state
from .inference.data import common_timestamps, prepare_window, preprocess_table
from .inference.matching import decode_hungarian, partial_sinkhorn_scores
from .inference.predictor import AISRadarMatcher, InferenceConfig, checkpoint_sha256
from .management.commands.ais_radar_replay import (
    build_ais_snapshot,
    build_radar_snapshot,
    discover_scene_pairs,
)


APP_DIR = Path(__file__).resolve().parent
CHECKPOINT = APP_DIR / "weights" / "mainline_seed42_epoch40.pth"


def sample_tables():
    timestamps = list(range(1_700_000_000, 1_700_000_006))
    ais_rows = []
    radar_rows = []
    for step, timestamp in enumerate(timestamps):
        for target_id, offset in ((1001, 0.0), (1002, 0.02)):
            ais_rows.append(
                {"DateTime": timestamp, "ID": target_id, "X": 29.7 + offset, "Y": 122.4 + step * 1e-5}
            )
        for radar_id, gtid, offset in (("1-1", 1001, 0.0), ("2-1", 1002, 0.02)):
            radar_rows.append(
                {
                    "DateTime": timestamp,
                    "ID": radar_id,
                    "GTID": gtid,
                    "X": 29.7 + offset,
                    "Y": 122.4 + step * 1e-5,
                }
            )
    return pd.DataFrame(ais_rows), pd.DataFrame(radar_rows)


class WindowPreparationTests(SimpleTestCase):
    def test_builds_six_frame_window(self):
        ais, radar = sample_tables()
        ais = preprocess_table(ais, "AIS")
        radar = preprocess_table(radar, "Radar")
        timestamps = common_timestamps(ais, radar)
        window = prepare_window(ais, radar, timestamps)
        self.assertEqual(tuple(window.ais_features.shape), (2, 6, 2))
        self.assertEqual(tuple(window.radar_features.shape), (2, 6, 2))


class MatchingTests(SimpleTestCase):
    def test_sinkhorn_and_hungarian_return_finite_matches(self):
        logits = torch.tensor([[5.0, -5.0], [-5.0, 5.0]])
        geometry = torch.zeros_like(logits)
        assignment = partial_sinkhorn_scores(logits, geometry)
        matches = decode_hungarian(assignment, 0.0)
        self.assertTrue(torch.isfinite(assignment).all())
        self.assertEqual([(row, col) for row, col, _ in matches], [(0, 0), (1, 1)])


class CheckpointSmokeTests(SimpleTestCase):
    def test_checkpoint_loads_and_runs_on_cpu(self):
        self.assertEqual(
            checkpoint_sha256(CHECKPOINT),
            "94B1EECE8BA74C603CED986B027CF74753761929389B290AFB4FF2AD8FFA2D4E",
        )
        matcher = AISRadarMatcher(InferenceConfig(checkpoint_path=CHECKPOINT, device="cpu"))
        ais, radar = sample_tables()
        result = matcher.predict_tables(ais, radar)
        self.assertEqual(result["summary"]["windows"], 1)
        self.assertEqual(result["model"]["device"], "cpu")


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class FusionStateTests(SimpleTestCase):
    def setUp(self):
        clear_fusion_state()

    def tearDown(self):
        clear_fusion_state()

    def test_state_uses_only_latest_window_and_normalises_ids(self):
        state = build_fusion_state(
            {
                "windows": [
                    {"matches": [{"ais_id": 111, "radar_id": "old"}]},
                    {
                        "start_time": "2026-08-02T12:00:00",
                        "end_time": "2026-08-02T12:00:05",
                        "matches": [
                            {"ais_id": 222000333.0, "radar_id": "7-1", "confidence": 0.9}
                        ],
                    },
                ]
            }
        )
        self.assertEqual(state["ais_ids"], ["222000333"])
        self.assertEqual(state["radar_ids"], ["7-1"])
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["source_end_time"], "2026-08-02T12:00:05")

    def test_fusion_state_drives_close_ais_and_forgery_detectors(self):
        state = build_fusion_state(
            {
                "windows": [
                    {
                        "end_time": "2026-08-02T12:00:05",
                        "matches": [],
                        "unmatched_ais_targets": [
                            {"id": 413000002, "x": 29.8, "y": 122.5}
                        ],
                        "unmatched_radar_targets": [
                            {"id": "2-1", "x": 29.7, "y": 122.4}
                        ],
                    }
                ]
            }
        )
        detections = build_fusion_detection_results(state)

        self.assertEqual(detections["detect-ais-off"]["count"], 1)
        self.assertEqual(detections["detect-spoofing"]["count"], 1)

    @patch("AISRadar.views.get_matcher")
    def test_match_publishes_targets_for_ui(self, get_matcher):
        matcher = Mock()
        matcher.predict_files.return_value = {
            "summary": {"windows": 1, "matches": 1},
            "windows": [
                {
                    "start_time": "2026-08-02T12:00:00",
                    "end_time": "2026-08-02T12:00:05",
                    "matches": [
                        {"ais_id": 413123456, "radar_id": "1-1", "confidence": 0.85}
                    ],
                }
            ],
        }
        get_matcher.return_value = matcher

        response = self.client.post(
            reverse("ais_radar:match"),
            {
                "ais_file": SimpleUploadedFile("ais.csv", b"ais"),
                "radar_file": SimpleUploadedFile("radar.csv", b"radar"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fusion_state"]["ais_ids"], ["413123456"])
        self.assertIn("detect-ais-off", response.json()["detections"])
        self.assertIn("detect-spoofing", response.json()["detections"])

        state_response = self.client.get(reverse("ais_radar:fused_targets"))
        self.assertEqual(state_response.status_code, 200)
        self.assertEqual(state_response.json()["ais_ids"], ["413123456"])


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class RealtimeReplayTests(SimpleTestCase):
    def test_migrated_data_contains_seventeen_complete_scene_pairs(self):
        pairs = discover_scene_pairs(APP_DIR.parent / "Data" / "AIS-Rdar")
        self.assertEqual(sorted(pairs), [f"{index:02d}" for index in range(1, 18)])

    def test_builds_frontend_ais_and_radar_snapshots(self):
        timestamp = pd.Timestamp("2026-08-02T12:00:00")
        ais_frame = pd.DataFrame(
            [{"ID": 413123456.0, "X": 29.7, "Y": 122.4, "speed": 8.5, "course": 90}]
        )
        radar_frame = pd.DataFrame(
            [{"ID": "1-1", "GTID": 413123456.0, "X": 29.71, "Y": 122.41}]
        )

        ais_snapshot = build_ais_snapshot(ais_frame, timestamp)
        radar_snapshot = build_radar_snapshot(radar_frame, timestamp)

        self.assertEqual(ais_snapshot[0]["mmsi"], "413123456")
        self.assertEqual(ais_snapshot[0]["lon"], 122.4)
        self.assertEqual(radar_snapshot[0]["id"], "1-1")
        self.assertEqual(radar_snapshot[0]["gtid"], "413123456")

    def test_replay_keeps_operational_ais_cache_and_event_separate(self):
        class RecordingChannelLayer:
            def __init__(self):
                self.events = []

            async def group_send(self, group, event):
                self.events.append((group, event))

        pairs = discover_scene_pairs(APP_DIR.parent / "Data" / "AIS-Rdar")
        channel_layer = RecordingChannelLayer()
        cache.set("latest_ais_data_raw", [{"mmsi": "operational"}], timeout=300)

        from .management.commands.ais_radar_replay import Command

        Command()._replay_scene(
            "08",
            pairs["08"],
            channel_layer,
            matcher=None,
            options={"max_frames": 1, "interval": 0},
        )

        self.assertEqual(
            cache.get("latest_ais_data_raw"),
            [{"mmsi": "operational"}],
        )
        self.assertTrue(cache.get(AisConsumer.AIS_RADAR_REPLAY_AIS_CACHE_KEY))
        event_types = [event["type"] for _, event in channel_layer.events]
        self.assertIn("send_ais_radar_replay_update", event_types)
        self.assertIn("send_radar_update", event_types)
        self.assertNotIn("send_ais_update", event_types)
