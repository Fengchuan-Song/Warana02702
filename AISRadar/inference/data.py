"""Input validation and six-frame trajectory window preparation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Sequence, Tuple

import numpy as np
import pandas as pd
import torch
from torch_geometric.utils import dense_to_sparse


TRAJECTORY_COLUMNS = ("X", "Y")
REQUIRED_COLUMNS = ("DateTime", "ID", "X", "Y")


class DataValidationError(ValueError):
    """Raised when AIS/Radar input cannot form a valid model window."""


@dataclass(frozen=True)
class PreparedWindow:
    timestamps: Tuple[pd.Timestamp, ...]
    ais_features: torch.Tensor
    radar_features: torch.Tensor
    ais_ids: Tuple[Any, ...]
    radar_ids: Tuple[Any, ...]


def build_temporal_edge_index(num_nodes: int, seq_len: int) -> torch.Tensor:
    """Build the directed temporal graph expected by ``TrajectoryMatchingNet``."""
    adjacency = torch.zeros((seq_len, seq_len))
    for time_index in range(seq_len - 1):
        adjacency[time_index, time_index + 1] = 1
    edge_index_single, _ = dense_to_sparse(adjacency)
    return torch.cat(
        [edge_index_single + node_index * seq_len for node_index in range(num_nodes)],
        dim=1,
    )


def parse_datetime_column(series: pd.Series) -> pd.Series:
    numeric_values = pd.to_numeric(series, errors="coerce")
    if numeric_values.notna().all():
        max_abs = numeric_values.abs().max()
        if max_abs < 1e11:
            return pd.to_datetime(numeric_values, unit="s")
        if max_abs < 1e14:
            return pd.to_datetime(numeric_values, unit="ms")
        if max_abs < 1e17:
            return pd.to_datetime(numeric_values, unit="us")
        return pd.to_datetime(numeric_values, unit="ns")
    return pd.to_datetime(series, errors="raise")


def _source_name(source: Any, filename: str = "") -> str:
    if filename:
        return filename
    if isinstance(source, (str, Path)):
        return str(source)
    return str(getattr(source, "name", ""))


def read_table(source: Any, filename: str = "") -> pd.DataFrame:
    """Read a CSV/Excel path or uploaded file-like object."""
    name = _source_name(source, filename).lower()
    if name.endswith((".csv", ".csv.gz", ".csv.bz2", ".csv.xz")):
        return pd.read_csv(source)
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(source)
    raise DataValidationError("Only CSV, compressed CSV, XLSX and XLS inputs are supported.")


def preprocess_table(data: pd.DataFrame, source_label: str) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in data.columns]
    if missing:
        raise DataValidationError(f"{source_label} is missing required columns: {missing}")
    if data.empty:
        raise DataValidationError(f"{source_label} is empty.")

    prepared = data.copy()
    prepared["DateTime"] = parse_datetime_column(prepared["DateTime"])
    for column in TRAJECTORY_COLUMNS:
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
    prepared = prepared.dropna(subset=["DateTime", "ID"])
    if prepared.empty:
        raise DataValidationError(f"{source_label} contains no valid timestamp/ID rows.")
    return prepared.sort_values(["DateTime", "ID"]).reset_index(drop=True)


def common_timestamps(ais_data: pd.DataFrame, radar_data: pd.DataFrame) -> List[pd.Timestamp]:
    return sorted(set(ais_data["DateTime"].unique()) & set(radar_data["DateTime"].unique()))


def _interpolate_trajectory(values: np.ndarray) -> np.ndarray:
    frame = pd.DataFrame(values)
    return (
        frame.interpolate(method="linear", limit_direction="both")
        .ffill()
        .bfill()
        .fillna(0.0)
        .to_numpy(dtype=np.float32)
    )


def extract_trajectories(
    data: pd.DataFrame,
    timestamps: Sequence[pd.Timestamp],
) -> Tuple[torch.Tensor, Tuple[Any, ...]]:
    data_in_window = data[data["DateTime"].isin(timestamps)]
    trajectories = []
    target_ids = []
    for target_id, group in data_in_window.groupby("ID"):
        group = group.sort_values("DateTime")
        values = []
        for timestamp in timestamps:
            row = group[group["DateTime"] == timestamp]
            if row.empty:
                values.append([np.nan, np.nan])
            else:
                first = row.iloc[0]
                values.append([float(first["X"]), float(first["Y"])])
        trajectory = np.asarray(values, dtype=np.float32)
        valid_points = int((~np.isnan(trajectory[:, :2]).any(axis=1)).sum())
        if valid_points >= 3:
            trajectories.append(_interpolate_trajectory(trajectory))
            target_ids.append(target_id)

    if not trajectories:
        return torch.empty((0, len(timestamps), 2), dtype=torch.float32), tuple()
    return torch.from_numpy(np.stack(trajectories)), tuple(target_ids)


def prepare_window(
    ais_data: pd.DataFrame,
    radar_data: pd.DataFrame,
    timestamps: Sequence[pd.Timestamp],
) -> PreparedWindow:
    ais_features, ais_ids = extract_trajectories(ais_data, timestamps)
    radar_features, radar_ids = extract_trajectories(radar_data, timestamps)
    if ais_features.size(0) == 0 or radar_features.size(0) == 0:
        raise DataValidationError("Window contains no AIS/Radar trajectory with at least three valid points.")
    return PreparedWindow(
        timestamps=tuple(pd.Timestamp(value) for value in timestamps),
        ais_features=ais_features,
        radar_features=radar_features,
        ais_ids=ais_ids,
        radar_ids=radar_ids,
    )


def iter_timestamp_windows(
    timestamps: Sequence[pd.Timestamp],
    window_size: int,
    latest_only: bool,
    stride: int,
) -> Iterable[Tuple[pd.Timestamp, ...]]:
    if window_size < 3:
        raise DataValidationError("window_size must be at least 3.")
    if stride < 1:
        raise DataValidationError("stride must be at least 1.")
    if len(timestamps) < window_size:
        raise DataValidationError(
            f"Only {len(timestamps)} common timestamps are available; {window_size} are required."
        )
    if latest_only:
        yield tuple(timestamps[-window_size:])
        return
    for start_index in range(0, len(timestamps) - window_size + 1, stride):
        yield tuple(timestamps[start_index : start_index + window_size])
