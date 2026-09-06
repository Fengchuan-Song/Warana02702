"""Offline runner for the fourteen integrated AIS warning detectors."""

from __future__ import annotations

import hashlib

from AISData.detection import DETECTORS, run_detector_core
from AISData.detection_queue import INCREMENTAL_QUEUE_DETECTORS

from .ais_replay import replay_ais
from .event_collector import EventCollector
from .schemas import PredictionRecord


def _classification(ground_truth, prediction):
    return {
        (1, 1): "TP",
        (1, 0): "FN",
        (0, 1): "FP",
        (0, 0): "TN",
    }[(ground_truth, prediction)]


class AISRunner:
    def __init__(self, feature_id):
        try:
            self.detector = DETECTORS[feature_id]
        except KeyError as exc:
            raise ValueError(f"Unknown AIS detector: {feature_id}") from exc
        self.feature_id = feature_id

    def run(self, sample):
        collector = EventCollector(sample.sample_id, self.feature_id)
        simulation_id = hashlib.sha256(
            f"{self.feature_id}:{sample.sample_id}".encode("utf-8")
        ).hexdigest()[:20]
        context = {
            "namespace": "evaluation",
            "simulation_id": simulation_id,
            "source_id": f"evaluation:{simulation_id}",
            "evaluation": True,
        }
        for frame in replay_ais(sample.ais, simulation_id=simulation_id):
            detector_input = (
                frame.incremental
                if self.feature_id in INCREMENTAL_QUEUE_DETECTORS
                else frame.snapshot
            )
            if not detector_input:
                continue
            payload = run_detector_core(
                self.feature_id,
                self.detector,
                detector_input,
                context,
            )
            if not payload.get("success", False):
                raise RuntimeError(
                    payload.get("message") or "detector evaluation failed"
                )
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
                event.get("alert_time")
                or event.get("timestamp")
                or event.get("first_detected_at")
                or ""
            ),
            detail=str(detail),
        )

