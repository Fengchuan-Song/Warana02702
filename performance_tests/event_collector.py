"""Collect final warning events without publishing operational state."""

from __future__ import annotations

import json
import uuid

from AISData.detection import _event_signature


class EventCollector:
    def __init__(self, sample_id, feature_id):
        self.sample_id = sample_id
        self.feature_id = feature_id
        self._events = {}

    def collect(self, payload):
        if not isinstance(payload, dict) or not payload.get("success", True):
            return
        results = payload.get("results")
        if not isinstance(results, list):
            return
        for result in results:
            if not isinstance(result, dict):
                continue
            signature = _event_signature(self.feature_id, result)
            signature_text = json.dumps(
                signature,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if signature_text in self._events:
                continue
            prediction_id = str(result.get("prediction_id") or "").strip()
            if not prediction_id:
                prediction_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"evaluation:{self.sample_id}:{self.feature_id}:{signature_text}",
                ).hex
            event = dict(result)
            event["prediction_id"] = prediction_id
            self._events[signature_text] = event

    @property
    def prediction(self):
        return int(bool(self._events))

    @property
    def events(self):
        return list(self._events.values())

    @property
    def first_event(self):
        return self.events[0] if self._events else None

