"""Data contracts used by the detector evaluation pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class EvaluationSample:
    sample_id: str
    track_id: str
    feature_id: str
    ground_truth: int
    ais: tuple[dict[str, Any], ...]
    radar: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationDataset:
    source: Path
    samples: tuple[EvaluationSample, ...]
    version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""


@dataclass
class PredictionRecord:
    sample_id: str
    track_id: str
    feature_id: str
    ground_truth: int
    prediction: Optional[int]
    result: str
    prediction_id: str = ""
    alert_time: str = ""
    detail: str = ""
    error: str = ""

    def as_csv_row(self):
        return {
            "sample_id": self.sample_id,
            "track_id": self.track_id,
            "feature_id": self.feature_id,
            "ground_truth": self.ground_truth,
            "prediction": "" if self.prediction is None else self.prediction,
            "result": self.result,
            "prediction_id": self.prediction_id,
            "alert_time": self.alert_time,
            "detail": self.detail,
        }

