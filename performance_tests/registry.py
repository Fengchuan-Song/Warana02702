"""Stable feature-id registry for the unified evaluation command."""

from AISData.detection import DETECTORS
from AISRadar.anomaly_detection import FUSION_DETECTORS


AIS_DETECTORS = frozenset(DETECTORS)
FUSION_DETECTORS = frozenset(FUSION_DETECTORS)
SUPPORTED_DETECTORS = AIS_DETECTORS | FUSION_DETECTORS


def detector_kind(feature_id):
    if feature_id in AIS_DETECTORS:
        return "ais"
    if feature_id in FUSION_DETECTORS:
        return "fusion"
    raise ValueError(f"Unsupported evaluation feature_id: {feature_id}")

