"""Replay paired AIS/Radar CSV scenes as a real-time sensor stream."""

from dataclasses import replace
import math
from pathlib import Path
import re
import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
import pandas as pd

from AISData.consumers import AisConsumer
from AISData.trajectory_history import append_ais_history
from AISRadar.anomaly_detection import publish_fusion_detection_results
from AISRadar.fusion_state import clear_fusion_state, publish_fusion_result
from AISRadar.inference.data import (
    DataValidationError,
    common_timestamps,
    preprocess_table,
)
from AISRadar.inference.predictor import get_matcher
from AISRadar.views import _config


SCENE_FILE_PATTERN = re.compile(
    r"^(AIS|Radar)_(\d{2})_(.+)\.csv$",
    re.IGNORECASE,
)


def _id_text(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _finite_number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def discover_scene_pairs(data_directory):
    """Return complete AIS/Radar pairs keyed by their two-digit scene ID."""
    pairs = {}
    for path in Path(data_directory).glob("*.csv"):
        matched = SCENE_FILE_PATTERN.match(path.name)
        if not matched:
            continue
        sensor_name, scene_id, suffix = matched.groups()
        scene = pairs.setdefault(scene_id, {"suffixes": {}})
        scene[sensor_name.lower()] = path
        scene["suffixes"][sensor_name.lower()] = suffix

    complete_pairs = {}
    for scene_id, pair in pairs.items():
        if "ais" not in pair or "radar" not in pair:
            continue
        if pair["suffixes"]["ais"] != pair["suffixes"]["radar"]:
            continue
        complete_pairs[scene_id] = {
            "ais": pair["ais"],
            "radar": pair["radar"],
        }
    return complete_pairs


def build_ais_snapshot(frame, timestamp):
    snapshot = []
    for _, row in frame.drop_duplicates(subset=["ID"], keep="last").iterrows():
        target_id = _id_text(row["ID"])
        latitude = _finite_number(row["X"], default=None)
        longitude = _finite_number(row["Y"], default=None)
        if not target_id or latitude is None or longitude is None:
            continue
        snapshot.append(
            {
                "timestamp": pd.Timestamp(timestamp).isoformat(),
                "mmsi": target_id,
                "name": f"AIS目标 {target_id}",
                "lon": longitude,
                "lat": latitude,
                "course": _finite_number(row.get("course")),
                "speed": _finite_number(row.get("speed")),
            }
        )
    return snapshot


def build_radar_snapshot(frame, timestamp):
    snapshot = []
    for _, row in frame.drop_duplicates(subset=["ID"], keep="last").iterrows():
        target_id = _id_text(row["ID"])
        latitude = _finite_number(row["X"], default=None)
        longitude = _finite_number(row["Y"], default=None)
        if not target_id or latitude is None or longitude is None:
            continue
        item = {
            "timestamp": pd.Timestamp(timestamp).isoformat(),
            "id": target_id,
            "lon": longitude,
            "lat": latitude,
            "course": _finite_number(row.get("course")),
            "speed": _finite_number(row.get("speed")),
        }
        if "GTID" in row and pd.notna(row["GTID"]):
            item["gtid"] = _id_text(row["GTID"])
        snapshot.append(item)
    return snapshot


class Command(BaseCommand):
    help = (
        "Replay paired AIS/Radar CSV files by timestamp, publish both sensor "
        "streams over WebSocket, and run rolling six-frame fusion inference."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-dir",
            default=str(Path(settings.BASE_DIR) / "Data" / "AIS-Rdar"),
            help="Directory containing paired AIS_XX_*.csv and Radar_XX_*.csv files.",
        )
        parser.add_argument(
            "--scene",
            action="append",
            dest="scenes",
            help="Two-digit scene ID; repeat for multiple scenes or use 'all' (default: 08).",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=30.0,
            help="Wall-clock seconds between simulated frames (default: 1.0).",
        )
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Restart the selected scene sequence after the final frame.",
        )
        parser.add_argument(
            "--no-inference",
            action="store_true",
            help="Push AIS/Radar frames without running the fusion model.",
        )
        parser.add_argument(
            "--device",
            choices=("auto", "cpu", "cuda"),
            default=None,
            help="Override the configured inference device.",
        )
        parser.add_argument(
            "--max-frames",
            type=int,
            default=None,
            help="Optional per-scene frame limit, primarily for smoke tests.",
        )

    def handle(self, *args, **options):
        data_directory = Path(options["data_dir"]).resolve()
        if not data_directory.is_dir():
            raise CommandError(f"AIS/Radar data directory does not exist: {data_directory}")
        if options["interval"] < 0:
            raise CommandError("--interval cannot be negative")
        if options["max_frames"] is not None and options["max_frames"] < 1:
            raise CommandError("--max-frames must be at least 1")

        pairs = discover_scene_pairs(data_directory)
        if not pairs:
            raise CommandError(f"No complete AIS/Radar scene pairs found in {data_directory}")

        requested_scenes = options["scenes"] or ["08"]
        if any(str(value).lower() == "all" for value in requested_scenes):
            selected_scenes = sorted(pairs)
        else:
            selected_scenes = [str(value).zfill(2) for value in requested_scenes]
        missing_scenes = [scene for scene in selected_scenes if scene not in pairs]
        if missing_scenes:
            raise CommandError(
                "Missing complete AIS/Radar scene pair(s): " + ", ".join(missing_scenes)
            )

        channel_layer = get_channel_layer()
        if channel_layer is None:
            raise CommandError("Django Channels layer is not configured")

        matcher = None
        if not options["no_inference"]:
            inference_config = _config()
            if options["device"]:
                inference_config = replace(inference_config, device=options["device"])
            matcher = get_matcher(inference_config)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Fusion model loaded on {matcher.device}; window={inference_config.window_size}"
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                "AIS/Radar replay ready: "
                f"scenes={','.join(selected_scenes)}, interval={options['interval']}s"
            )
        )

        try:
            while True:
                for scene_id in selected_scenes:
                    self._replay_scene(
                        scene_id,
                        pairs[scene_id],
                        channel_layer,
                        matcher,
                        options,
                    )
                if not options["loop"]:
                    break
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("AIS/Radar replay stopped by user."))

    def _replay_scene(self, scene_id, pair, channel_layer, matcher, options):
        ais_data = preprocess_table(pd.read_csv(pair["ais"]), f"scene {scene_id} AIS")
        radar_data = preprocess_table(pd.read_csv(pair["radar"]), f"scene {scene_id} Radar")
        timestamps = common_timestamps(ais_data, radar_data)
        if not timestamps:
            raise CommandError(f"Scene {scene_id} has no common AIS/Radar timestamps")

        if options["max_frames"] is not None:
            timestamps = timestamps[: options["max_frames"]]

        clear_fusion_state()
        empty_fusion_state = {
            "available": False,
            "updated_at": None,
            "source_start_time": None,
            "source_end_time": None,
            "count": 0,
            "ais_ids": [],
            "radar_ids": [],
            "matches": [],
            "unmatched_ais_targets": [],
            "unmatched_radar_targets": [],
        }
        self._send_fusion_state(channel_layer, empty_fusion_state)
        publish_fusion_detection_results(empty_fusion_state, channel_layer)
        self.stdout.write(
            self.style.NOTICE(
                f"Scene {scene_id}: {len(timestamps)} common frames, "
                f"AIS={pair['ais'].name}, Radar={pair['radar'].name}"
            )
        )

        window_size = matcher.config.window_size if matcher else 6
        for frame_index, timestamp in enumerate(timestamps):
            ais_frame = ais_data[ais_data["DateTime"] == timestamp]
            radar_frame = radar_data[radar_data["DateTime"] == timestamp]
            ais_snapshot = build_ais_snapshot(ais_frame, timestamp)
            radar_snapshot = build_radar_snapshot(radar_frame, timestamp)

            append_ais_history(ais_snapshot)
            timeout = getattr(settings, "CACHE_TTL", 300)
            cache.set(
                AisConsumer.AIS_RADAR_REPLAY_AIS_CACHE_KEY,
                ais_snapshot,
                timeout=timeout,
            )
            cache.set(AisConsumer.RADAR_CACHE_KEY, radar_snapshot, timeout=timeout)
            async_to_sync(channel_layer.group_send)(
                AisConsumer.AIS_GROUP_NAME,
                {"type": "send_ais_radar_replay_update", "data": ais_snapshot},
            )
            async_to_sync(channel_layer.group_send)(
                AisConsumer.AIS_GROUP_NAME,
                {"type": "send_radar_update", "data": radar_snapshot},
            )

            match_count = 0
            if matcher is not None and frame_index + 1 >= window_size:
                window_timestamps = timestamps[frame_index - window_size + 1 : frame_index + 1]
                window_ais = ais_data[ais_data["DateTime"].isin(window_timestamps)]
                window_radar = radar_data[radar_data["DateTime"].isin(window_timestamps)]
                try:
                    result = matcher.predict_tables(window_ais, window_radar)
                    fusion_state = publish_fusion_result(result)
                    match_count = fusion_state["count"]
                    self._send_fusion_state(channel_layer, fusion_state)
                    publish_fusion_detection_results(fusion_state, channel_layer)
                except DataValidationError as exc:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Scene {scene_id} frame {frame_index + 1}: fusion skipped ({exc})"
                        )
                    )

            self.stdout.write(
                f"scene={scene_id} frame={frame_index + 1}/{len(timestamps)} "
                f"time={pd.Timestamp(timestamp).isoformat()} "
                f"ais={len(ais_snapshot)} radar={len(radar_snapshot)} matches={match_count}"
            )
            if options["interval"]:
                time.sleep(options["interval"])

    @staticmethod
    def _send_fusion_state(channel_layer, fusion_state):
        async_to_sync(channel_layer.group_send)(
            AisConsumer.AIS_GROUP_NAME,
            {"type": "send_ais_radar_fusion_update", "data": fusion_state},
        )
