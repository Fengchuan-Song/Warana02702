"""Causal as-fast-as-possible replay for AIS/Radar evaluation samples."""

from __future__ import annotations

import math

import pandas as pd

from AISRadar.fusion_core import run_fusion_frame
from AISRadar.fusion_state import build_fusion_state
from AISRadar.inference.data import DataValidationError, preprocess_table


class FusionReplayError(ValueError):
    pass


def _first(record, *names):
    for name in names:
        if record.get(name) not in (None, ""):
            return record[name]
    return None


def _canonical_rows(records, sensor):
    rows = []
    for record in records:
        timestamp = _first(
            record, "DateTime", "timestamp", "event_time", "datetime", "time"
        )
        if sensor == "AIS":
            target_id = _first(record, "ID", "mmsi", "MMSI", "ais_id")
        else:
            target_id = _first(record, "ID", "id", "radar_id", "target_id")
        latitude = _first(record, "X", "lat", "latitude")
        longitude = _first(record, "Y", "lon", "longitude")
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            continue
        if (
            timestamp in (None, "")
            or target_id in (None, "")
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
        ):
            continue
        if isinstance(target_id, float) and target_id.is_integer():
            target_id = int(target_id)
        target_id = str(target_id).strip()
        if not target_id or target_id.casefold() in {"nan", "none", "null"}:
            continue
        row = {
            "DateTime": timestamp,
            "ID": target_id,
            "X": latitude,
            "Y": longitude,
        }
        for output, aliases in {
            "speed": ("speed", "sog"),
            "course": ("course", "cog"),
            "GTID": ("GTID", "gtid"),
        }.items():
            value = _first(record, *aliases)
            if value not in (None, ""):
                row[output] = value
        rows.append(row)
    if not rows:
        raise FusionReplayError(f"{sensor} sample contains no valid records")
    try:
        return preprocess_table(pd.DataFrame(rows), f"evaluation {sensor}")
    except DataValidationError as exc:
        raise FusionReplayError(str(exc)) from exc


def replay_fusion(ais_records, radar_records, *, matcher):
    """Yield fusion states only for Radar events, never using future AIS."""
    ais_data = _canonical_rows(ais_records, "AIS")
    radar_data = _canonical_rows(radar_records, "Radar")
    timestamps = sorted(
        set(ais_data["DateTime"].unique()) | set(radar_data["DateTime"].unique())
    )
    ais_history = {}
    successful_frames = 0
    radar_frames = 0
    for timestamp in timestamps:
        ais_frame = ais_data[ais_data["DateTime"] == timestamp]
        for _, row in ais_frame.iterrows():
            target_id = str(row["ID"])
            history = ais_history.setdefault(target_id, [])
            history.append(row.to_dict())
            del history[:-2]

        radar_frame = radar_data[radar_data["DateTime"] == timestamp]
        if radar_frame.empty:
            continue
        radar_frames += 1
        causal_rows = [row for history in ais_history.values() for row in history]
        if not causal_rows:
            continue
        if any(pd.Timestamp(row["DateTime"]) > pd.Timestamp(timestamp) for row in causal_rows):
            raise FusionReplayError("Future AIS data reached a Radar fusion frame")
        try:
            result = run_fusion_frame(
                causal_rows,
                radar_frame.to_dict("records"),
                matcher=matcher,
            )
        except DataValidationError:
            # Production skips frames until enough causal AIS history exists.
            continue
        successful_frames += 1
        yield build_fusion_state(result)

    if not radar_frames:
        raise FusionReplayError("Radar sample contains no event frames")
    if not successful_frames:
        raise FusionReplayError(
            "No usable causal fusion frame was produced; provide sufficient "
            "AIS history or configure the production maximum AIS time gap"
        )
