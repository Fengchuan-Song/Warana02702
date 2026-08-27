"""Causal AIS/Radar association adapted from ``method_JPDA``.

The reference experiment accepts two synchronous pandas frames containing
``lat/lon`` and returns a set of ID pairs.  The production input instead uses
``DateTime, ID, X, Y`` (where the migrated scenes store latitude in ``X`` and
longitude in ``Y``) and AIS/Radar messages are asynchronous.  This module is
the narrow adapter between those two contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from .data import DataValidationError, preprocess_table, read_table


LOGGER = logging.getLogger(__name__)
KNOT_TO_METRES_PER_SECOND = 0.514444
METRES_PER_DEGREE_LATITUDE = 111_320.0


@dataclass(frozen=True)
class JPDAParameters:
    """Parameters inherited from the experiment, plus causal-prediction noise.

    The 2 km gate and 500 m exponential scale are the values in the reference
    ``method_JPDA``.  ``sigma_multiplier`` is only used to widen the gate after
    an AIS state has been extrapolated; it does not alter synchronous scoring.
    """

    distance_gate_m: float = 2_000.0
    distance_scale_m: float = 500.0
    sigma_multiplier: float = 3.0


def _id_text(value: Any) -> str:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_wgs84(latitude: float, longitude: float) -> bool:
    return abs(latitude) <= 90.0 and abs(longitude) <= 180.0


def _position_to_metres(latitude: float, longitude: float, origin_latitude: float, origin_longitude: float) -> Tuple[float, float]:
    """Return east/north metres; use input metres unchanged for local XY data."""
    if not _is_wgs84(latitude, longitude):
        return latitude, longitude
    north = (latitude - origin_latitude) * METRES_PER_DEGREE_LATITUDE
    east = (
        (longitude - origin_longitude)
        * METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(origin_latitude))
    )
    return east, north


def _metres_to_position(east: float, north: float, origin_latitude: float, origin_longitude: float, geographic: bool) -> Tuple[float, float]:
    if not geographic:
        return east, north
    latitude = origin_latitude + north / METRES_PER_DEGREE_LATITUDE
    longitude = origin_longitude + east / (
        METRES_PER_DEGREE_LATITUDE * math.cos(math.radians(origin_latitude))
    )
    return latitude, longitude


def _velocity_from_report(row: pd.Series) -> Optional[Tuple[float, float]]:
    """Convert migrated ``speed/course`` (knots/degrees) into east/north m/s."""
    speed = _finite(row.get("speed", row.get("sog")))
    course = _finite(row.get("course", row.get("cog")))
    if speed is None or course is None:
        return None
    speed_mps = speed * KNOT_TO_METRES_PER_SECOND
    course_radians = math.radians(course)
    return speed_mps * math.sin(course_radians), speed_mps * math.cos(course_radians)


def _history_velocity(
    history: pd.DataFrame,
    origin_latitude: float,
    origin_longitude: float,
) -> Optional[Tuple[float, float]]:
    if len(history) < 2:
        return None
    previous, latest = history.iloc[-2], history.iloc[-1]
    dt = (pd.Timestamp(latest["DateTime"]) - pd.Timestamp(previous["DateTime"])).total_seconds()
    if dt <= 0:
        return None
    previous_x, previous_y = _position_to_metres(
        float(previous["X"]), float(previous["Y"]), origin_latitude, origin_longitude
    )
    latest_x, latest_y = _position_to_metres(
        float(latest["X"]), float(latest["Y"]), origin_latitude, origin_longitude
    )
    return (latest_x - previous_x) / dt, (latest_y - previous_y) / dt


def _empirical_prediction_error_rate(
    history: pd.DataFrame,
    origin_latitude: float,
    origin_longitude: float,
) -> float:
    """Estimate per-track dead-reckoning error from its last two AIS reports."""
    if len(history) < 2:
        return 0.0
    previous, latest = history.iloc[-2], history.iloc[-1]
    dt = (pd.Timestamp(latest["DateTime"]) - pd.Timestamp(previous["DateTime"])).total_seconds()
    reported_velocity = _velocity_from_report(previous)
    if dt <= 0 or reported_velocity is None:
        return 0.0
    previous_x, previous_y = _position_to_metres(
        float(previous["X"]), float(previous["Y"]), origin_latitude, origin_longitude
    )
    latest_x, latest_y = _position_to_metres(
        float(latest["X"]), float(latest["Y"]), origin_latitude, origin_longitude
    )
    predicted_x = previous_x + reported_velocity[0] * dt
    predicted_y = previous_y + reported_velocity[1] * dt
    return math.hypot(latest_x - predicted_x, latest_y - predicted_y) / dt


def method_JPDA(ais_frame: pd.DataFrame, radar_frame: pd.DataFrame, params: Optional[JPDAParameters] = None) -> List[Tuple[int, int, float, float]]:
    """Run the reference JPDA cost and Hungarian decoding on adapted XY metres.

    This preserves the reference method's core: valid pairs have cost
    ``1 - exp(-distance / 500)`` and are solved one-to-one by Hungarian
    assignment.  The adapter may provide a per-pair scale/gate for AIS
    extrapolation uncertainty; for synchronous data these are exactly 500 m
    and 2 km respectively.
    """
    # JPDA AIS-Radar fusion
    params = params or JPDAParameters()
    invalid_cost = 1.0
    costs = np.full((len(ais_frame), len(radar_frame)), invalid_cost, dtype=float)
    probabilities = np.zeros_like(costs)
    distances = np.full_like(costs, np.inf)

    for ais_index, (_, ais_row) in enumerate(ais_frame.iterrows()):
        for radar_index, (_, radar_row) in enumerate(radar_frame.iterrows()):
            distance = math.hypot(
                float(ais_row["x_m"]) - float(radar_row["x_m"]),
                float(ais_row["y_m"]) - float(radar_row["y_m"]),
            )
            scale = max(float(ais_row.get("distance_scale_m", params.distance_scale_m)), 1e-6)
            gate = max(float(ais_row.get("distance_gate_m", params.distance_gate_m)), 0.0)
            if distance < gate:
                probability = math.exp(-distance / scale)
                costs[ais_index, radar_index] = 1.0 - probability
                probabilities[ais_index, radar_index] = probability
                distances[ais_index, radar_index] = distance

    if not len(ais_frame) or not len(radar_frame):
        return []
    rows, columns = linear_sum_assignment(costs)
    return [
        (int(ais_index), int(radar_index), float(probabilities[ais_index, radar_index]), float(distances[ais_index, radar_index]))
        for ais_index, radar_index in zip(rows, columns)
        if costs[ais_index, radar_index] < invalid_cost
    ]


def _nominal_interval_seconds(data: pd.DataFrame) -> Optional[float]:
    timestamps = pd.Series(data["DateTime"].drop_duplicates()).sort_values()
    deltas = timestamps.diff().dropna().dt.total_seconds()
    deltas = deltas[deltas > 0]
    return float(deltas.median()) if not deltas.empty else None


class JPDAMatcher:
    """Default causal AIS/Radar matcher with the legacy HTTP result contract."""

    def __init__(self, config):
        self.config = config
        self.params = JPDAParameters()

    def predict_files(self, ais_source: Any, radar_source: Any, ais_filename: str = "", radar_filename: str = "", **kwargs) -> Dict[str, Any]:
        return self.predict_tables(
            read_table(ais_source, ais_filename), read_table(radar_source, radar_filename), **kwargs
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
        if stride < 1:
            raise DataValidationError("stride must be at least 1.")
        radar_times = sorted(pd.Timestamp(value) for value in radar_data["DateTime"].unique())
        if latest_only:
            radar_times = radar_times[-1:]
        else:
            radar_times = radar_times[::stride]
        if max_windows is not None:
            radar_times = radar_times[:max_windows]
        if not radar_times:
            raise DataValidationError("Radar input contains no valid timestamp.")

        nominal_ais_interval = _nominal_interval_seconds(ais_data)
        max_ais_gap = getattr(self.config, "max_ais_time_gap_seconds", None)
        if max_ais_gap is None:
            if nominal_ais_interval is None:
                raise DataValidationError(
                    "AIS contains only one timestamp; configure max_ais_time_gap_seconds explicitly."
                )
            max_ais_gap = 3.0 * nominal_ais_interval
        max_ais_gap = float(max_ais_gap)
        if max_ais_gap <= 0:
            raise DataValidationError("max_ais_time_gap_seconds must be positive.")

        origin_latitude = float(pd.concat([ais_data["X"], radar_data["X"]]).median())
        origin_longitude = float(pd.concat([ais_data["Y"], radar_data["Y"]]).median())
        geographic = _is_wgs84(origin_latitude, origin_longitude)
        results = [
            self._match_radar_frame(
                ais_data, radar_data, timestamp, origin_latitude, origin_longitude, geographic, max_ais_gap
            )
            for timestamp in radar_times
        ]
        return {
            "model": {
                "algorithm": "JPDA AIS-Radar fusion",
                "device": str(getattr(self.config, "device", "cpu")),
                "distance_gate_m": self.params.distance_gate_m,
                "distance_scale_m": self.params.distance_scale_m,
                "max_ais_time_gap_seconds": max_ais_gap,
                "max_radar_time_gap_seconds": getattr(self.config, "max_radar_time_gap_seconds", None),
                "nominal_ais_interval_seconds": nominal_ais_interval,
            },
            "input": {
                "ais_rows": len(ais_data),
                "radar_rows": len(radar_data),
                "radar_timestamps": len(radar_times),
                "coordinate_system": "WGS84 latitude/longitude" if geographic else "local XY metres",
            },
            "summary": {"windows": len(results), "matches": sum(len(item["matches"]) for item in results)},
            "skipped_windows": 0,
            "windows": results,
        }

    def _match_radar_frame(self, ais_data, radar_data, timestamp, origin_latitude, origin_longitude, geographic, max_ais_gap):
        radar_frame = radar_data[radar_data["DateTime"] == timestamp].drop_duplicates("ID", keep="last")
        historical_ais = ais_data[ais_data["DateTime"] <= timestamp]
        candidates: List[Dict[str, Any]] = []
        for ais_id, history in historical_ais.groupby("ID", sort=False):
            history = history.sort_values("DateTime")
            latest = history.iloc[-1]
            ais_timestamp = pd.Timestamp(latest["DateTime"])
            dt_seconds = (timestamp - ais_timestamp).total_seconds()
            if dt_seconds < 0 or dt_seconds > max_ais_gap:
                continue
            raw_latitude, raw_longitude = float(latest["X"]), float(latest["Y"])
            raw_x, raw_y = _position_to_metres(raw_latitude, raw_longitude, origin_latitude, origin_longitude)
            velocity = _velocity_from_report(latest) or _history_velocity(history, origin_latitude, origin_longitude) or (0.0, 0.0)
            predicted_x = raw_x + velocity[0] * dt_seconds
            predicted_y = raw_y + velocity[1] * dt_seconds
            predicted_latitude, predicted_longitude = _metres_to_position(
                predicted_x, predicted_y, origin_latitude, origin_longitude, geographic
            )
            error_rate = _finite(getattr(self.config, "ais_prediction_error_rate_mps", None))
            if error_rate is None:
                error_rate = _empirical_prediction_error_rate(history, origin_latitude, origin_longitude)
            prediction_sigma = max(0.0, error_rate * dt_seconds)
            candidates.append(
                {
                    "id": _id_text(ais_id), "x_m": predicted_x, "y_m": predicted_y,
                    "x": predicted_latitude, "y": predicted_longitude,
                    "raw_x": raw_latitude, "raw_y": raw_longitude,
                    "ais_timestamp": ais_timestamp, "dt_seconds": dt_seconds,
                    "prediction_sigma_m": prediction_sigma,
                    "distance_scale_m": self.params.distance_scale_m + prediction_sigma,
                    "distance_gate_m": self.params.distance_gate_m + self.params.sigma_multiplier * prediction_sigma,
                }
            )
        ais_frame = pd.DataFrame(candidates)
        radar_candidates = []
        for _, row in radar_frame.iterrows():
            x, y = _position_to_metres(float(row["X"]), float(row["Y"]), origin_latitude, origin_longitude)
            radar_candidates.append({"id": _id_text(row["ID"]), "x_m": x, "y_m": y, "x": float(row["X"]), "y": float(row["Y"])})
        radar_frame_adapted = pd.DataFrame(radar_candidates)
        decoded = method_JPDA(ais_frame, radar_frame_adapted, self.params)
        matched_ais, matched_radar = set(), set()
        matches = []
        for ais_index, radar_index, probability, distance_m in decoded:
            ais_target, radar_target = candidates[ais_index], radar_candidates[radar_index]
            matched_ais.add(ais_index)
            matched_radar.add(radar_index)
            matches.append({
                "ais_index": ais_index, "radar_index": radar_index,
                "ais_id": ais_target["id"], "radar_id": radar_target["id"],
                "mmsi": ais_target["id"], "radar_track_id": radar_target["id"],
                "confidence": probability, "association_probability": probability,
                "distance_m": distance_m, "dt_seconds": ais_target["dt_seconds"],
                "ais_timestamp": ais_target["ais_timestamp"].isoformat(),
                "radar_timestamp": timestamp.isoformat(),
            })
        if getattr(self.config, "debug_jpda", False):
            LOGGER.debug(
                "JPDA frame=%s AIS=%d Radar=%d pairs=%s",
                timestamp.isoformat(), len(candidates), len(radar_candidates),
                [
                    {
                        "mmsi": match["mmsi"], "radar_track_id": match["radar_track_id"],
                        "ais_timestamp": match["ais_timestamp"], "radar_timestamp": match["radar_timestamp"],
                        "dt_seconds": round(match["dt_seconds"], 3),
                        "raw_ais": (candidates[match["ais_index"]]["raw_x"], candidates[match["ais_index"]]["raw_y"]),
                        "predicted_ais": (candidates[match["ais_index"]]["x"], candidates[match["ais_index"]]["y"]),
                        "radar": (radar_candidates[match["radar_index"]]["x"], radar_candidates[match["radar_index"]]["y"]),
                        "probability": round(match["association_probability"], 5),
                    }
                    for match in matches
                ],
            )
        targets = [{"id": item["id"], "x": item["x"], "y": item["y"]} for item in candidates]
        radar_targets = [{"id": item["id"], "x": item["x"], "y": item["y"]} for item in radar_candidates]
        return {
            "start_time": timestamp.isoformat(), "end_time": timestamp.isoformat(),
            "ais_trajectories": len(candidates), "radar_trajectories": len(radar_candidates),
            "ais_targets": targets, "radar_targets": radar_targets, "matches": matches,
            "unmatched_ais_targets": [item for index, item in enumerate(targets) if index not in matched_ais],
            "unmatched_radar_targets": [item for index, item in enumerate(radar_targets) if index not in matched_radar],
        }
