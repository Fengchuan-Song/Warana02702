"""Offline runner for AIS-off and spoofing fusion detectors."""

from __future__ import annotations

from AISRadar.anomaly_detection import FUSION_DETECTORS, run_fusion_detector
from AISRadar.inference.predictor import get_matcher
from AISRadar.views import _config

from .event_collector import EventCollector
from .fusion_replay import replay_fusion
from .schemas import PredictionRecord


def _classification(ground_truth, prediction):
    return {
        (1, 1): "TP",
        (1, 0): "FN",
        (0, 1): "FP",
        (0, 0): "TN",
    }[(ground_truth, prediction)]


class FusionRunner:
    def __init__(self, feature_id, *, matcher=None):
        if feature_id not in FUSION_DETECTORS:
            raise ValueError(f"Unknown fusion detector: {feature_id}")
        self.feature_id = feature_id
        self.matcher = matcher

    def run(self, sample):
        if not sample.radar:
            raise ValueError(
                f"Sample {sample.sample_id}: Radar data is required for "
                f"{self.feature_id}"
            )
        matcher = self.matcher or get_matcher(_config())
        collector = EventCollector(sample.sample_id, self.feature_id)
        for fusion_state in replay_fusion(
            sample.ais,
            sample.radar,
            matcher=matcher,
        ):
            payload = run_fusion_detector(self.feature_id, fusion_state)
            collector.collect(payload)

        prediction = collector.prediction
        event = collector.first_event or {}
        detail = event.get("detail") or event.get("details") or ""
        return PredictionRecord(
            sample_id=sample.sample_id,
            track_id=sample.track_id,
            feature_id=self.feature_id,
            ground_truth=sample.ground_truth,
            prediction=prediction,
            result=_classification(sample.ground_truth, prediction),
            prediction_id=str(event.get("prediction_id") or ""),
            alert_time=str(
                event.get("alert_time") or event.get("timestamp") or ""
            ),
            detail=str(detail),
        )

