"""Thread-safe, lazy-loaded AIS/Radar trajectory matching service."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import threading
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

from .data import (
    DataValidationError,
    common_timestamps,
    iter_timestamp_windows,
    prepare_window,
    preprocess_table,
    read_table,
)
from .matching import decode_hungarian, geometry_guidance, partial_sinkhorn_scores
from .model import TrajectoryMatchingNet


DEFAULT_CHECKPOINT_SHA256 = "94B1EECE8BA74C603CED986B027CF74753761929389B290AFB4FF2AD8FFA2D4E"


class ReportedMotionFuser(nn.Module):
    """Checkpoint-compatible holder for learned six-frame reported-motion weights."""

    def __init__(self, window_size: int):
        super().__init__()
        self.reported_logits = nn.Parameter(torch.zeros(window_size))


@dataclass(frozen=True)
class InferenceConfig:
    checkpoint_path: Path
    device: str = "auto"
    window_size: int = 6
    geometry_weight: float = 0.01
    match_threshold: float = 0.0
    sinkhorn_iterations: int = 20
    expected_sha256: str = DEFAULT_CHECKPOINT_SHA256

    def __post_init__(self):
        object.__setattr__(self, "checkpoint_path", Path(self.checkpoint_path).resolve())


def resolve_device(requested: str) -> torch.device:
    value = str(requested or "auto").strip().lower()
    if value == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device = torch.device(value)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested for AIS/Radar inference but is unavailable.")
    return device


def checkpoint_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as checkpoint_file:
        for block in iter(lambda: checkpoint_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _id_key(value: Any) -> str:
    value = _json_scalar(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _ground_truth_lookup(radar_data: pd.DataFrame) -> Dict[str, str]:
    if "GTID" not in radar_data.columns:
        return {}
    result = {}
    for radar_id, group in radar_data.groupby("ID"):
        values = group["GTID"].dropna()
        if not values.empty:
            result[_id_key(radar_id)] = _id_key(values.iloc[0])
    return result


class AISRadarMatcher:
    """Load the canonical checkpoint once and match uploaded trajectory tables."""

    def __init__(self, config: InferenceConfig):
        self.config = config
        self.device = resolve_device(config.device)
        self._inference_lock = threading.RLock()
        self.model = self._load_model()

    def _load_model(self) -> TrajectoryMatchingNet:
        path = self.config.checkpoint_path
        if not path.is_file():
            raise FileNotFoundError(f"AIS/Radar checkpoint does not exist: {path}")
        if self.config.expected_sha256:
            actual_hash = checkpoint_sha256(path)
            if actual_hash != self.config.expected_sha256.upper():
                raise RuntimeError(
                    "AIS/Radar checkpoint SHA-256 mismatch: "
                    f"expected {self.config.expected_sha256}, got {actual_hash}"
                )

        model = TrajectoryMatchingNet(
            input_dim=2,
            hidden_dim=128,
            pair_features="distance,theta",
            use_embedding_features=True,
            temporal_scales=str(self.config.window_size),
        )
        model.temporal_motion_fuser = ReportedMotionFuser(self.config.window_size)
        checkpoint = torch.load(str(path), map_location=self.device)
        if not isinstance(checkpoint, dict) or "model_state_dict" not in checkpoint:
            raise RuntimeError("AIS/Radar checkpoint has an unsupported structure.")
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
        model.to(self.device)
        model.eval()
        return model

    def predict_files(
        self,
        ais_source: Any,
        radar_source: Any,
        ais_filename: str = "",
        radar_filename: str = "",
        latest_only: bool = True,
        stride: int = 1,
        max_windows: Optional[int] = None,
    ) -> Dict[str, Any]:
        ais_data = read_table(ais_source, ais_filename)
        radar_data = read_table(radar_source, radar_filename)
        return self.predict_tables(
            ais_data,
            radar_data,
            latest_only=latest_only,
            stride=stride,
            max_windows=max_windows,
        )

    def predict_tables(
        self,
        ais_data: pd.DataFrame,
        radar_data: pd.DataFrame,
        latest_only: bool = True,
        stride: int = 1,
        max_windows: Optional[int] = None,
    ) -> Dict[str, Any]:
        ais_data = preprocess_table(ais_data, "AIS input")
        radar_data = preprocess_table(radar_data, "Radar input")
        timestamps = common_timestamps(ais_data, radar_data)
        ground_truth = _ground_truth_lookup(radar_data)
        window_results = []
        skipped_windows = 0

        for timestamp_window in iter_timestamp_windows(
            timestamps,
            self.config.window_size,
            latest_only,
            stride,
        ):
            if max_windows is not None and len(window_results) >= max_windows:
                break
            try:
                window = prepare_window(ais_data, radar_data, timestamp_window)
            except DataValidationError:
                skipped_windows += 1
                continue

            ais_features = window.ais_features.to(self.device)
            radar_features = window.radar_features.to(self.device)
            with self._inference_lock, torch.no_grad():
                output = self.model(ais_features, radar_features, return_uncertainty=True)
                geometry = geometry_guidance(ais_features, radar_features)
                log_assignment = partial_sinkhorn_scores(
                    output["logits"],
                    geometry,
                    geometry_weight=self.config.geometry_weight,
                    iterations=self.config.sinkhorn_iterations,
                )
                log_assignment = torch.nan_to_num(
                    log_assignment,
                    nan=0.0,
                    posinf=1e6,
                    neginf=-1e6,
                )
                decoded = decode_hungarian(log_assignment, self.config.match_threshold)

            matches = []
            predicted_pairs = set()
            for ais_index, radar_index, confidence in decoded:
                ais_id = window.ais_ids[ais_index]
                radar_id = window.radar_ids[radar_index]
                predicted_pairs.add((_id_key(ais_id), _id_key(radar_id)))
                matches.append(
                    {
                        "ais_index": ais_index,
                        "radar_index": radar_index,
                        "ais_id": _json_scalar(ais_id),
                        "radar_id": _json_scalar(radar_id),
                        "confidence": confidence,
                    }
                )

            result = {
                "start_time": window.timestamps[0].isoformat(),
                "end_time": window.timestamps[-1].isoformat(),
                "ais_trajectories": len(window.ais_ids),
                "radar_trajectories": len(window.radar_ids),
                "matches": matches,
            }
            if ground_truth:
                true_pairs = {
                    (_id_key(ais_id), _id_key(radar_id))
                    for ais_id in window.ais_ids
                    for radar_id in window.radar_ids
                    if ground_truth.get(_id_key(radar_id)) == _id_key(ais_id)
                }
                true_positive = len(predicted_pairs & true_pairs)
                false_positive = len(predicted_pairs - true_pairs)
                false_negative = len(true_pairs - predicted_pairs)
                result["metrics"] = {
                    "true_positive": true_positive,
                    "false_positive": false_positive,
                    "false_negative": false_negative,
                    "precision": true_positive / len(predicted_pairs) if predicted_pairs else 0.0,
                    "recall": true_positive / len(true_pairs) if true_pairs else 0.0,
                }
            window_results.append(result)

        if not window_results:
            raise DataValidationError("No usable trajectory window could be constructed.")

        summary = self._summarise(window_results)
        return {
            "model": {
                "checkpoint": self.config.checkpoint_path.name,
                "device": str(self.device),
                "window_size": self.config.window_size,
                "geometry_weight": self.config.geometry_weight,
                "match_threshold": self.config.match_threshold,
            },
            "input": {
                "ais_rows": len(ais_data),
                "radar_rows": len(radar_data),
                "common_timestamps": len(timestamps),
            },
            "summary": summary,
            "skipped_windows": skipped_windows,
            "windows": window_results,
        }

    @staticmethod
    def _summarise(windows):
        summary = {
            "windows": len(windows),
            "matches": sum(len(window["matches"]) for window in windows),
        }
        metric_windows = [window["metrics"] for window in windows if "metrics" in window]
        if metric_windows:
            true_positive = sum(item["true_positive"] for item in metric_windows)
            false_positive = sum(item["false_positive"] for item in metric_windows)
            false_negative = sum(item["false_negative"] for item in metric_windows)
            summary["metrics"] = {
                "true_positive": true_positive,
                "false_positive": false_positive,
                "false_negative": false_negative,
                "precision": true_positive / (true_positive + false_positive)
                if true_positive + false_positive
                else 0.0,
                "recall": true_positive / (true_positive + false_negative)
                if true_positive + false_negative
                else 0.0,
            }
        return summary


_MATCHERS: Dict[Tuple[Any, ...], AISRadarMatcher] = {}
_MATCHERS_LOCK = threading.Lock()


def get_matcher(config: InferenceConfig) -> AISRadarMatcher:
    key = (
        str(config.checkpoint_path),
        config.device,
        config.window_size,
        config.geometry_weight,
        config.match_threshold,
        config.sinkhorn_iterations,
        config.expected_sha256,
    )
    with _MATCHERS_LOCK:
        matcher = _MATCHERS.get(key)
        if matcher is None:
            matcher = AISRadarMatcher(config)
            _MATCHERS[key] = matcher
        return matcher
