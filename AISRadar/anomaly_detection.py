"""Publish detectors that consume AIS/Radar association output."""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache

from AISData.consumers import ViolationConsumer
from AISData.detection import annotate_new_results, detection_cache_key
from AISData.violation_records import persist_detection_payload
from CloseAIS.views import detect_close_ais
from Forgery.views import detect_forgery


LOGGER = logging.getLogger(__name__)
FUSION_DETECTORS = {
    "detect-ais-off": detect_close_ais,
    "detect-spoofing": detect_forgery,
}


def run_fusion_detector(feature_id, fusion_state):
    """Run one production fusion detector without publishing side effects."""
    try:
        detector = FUSION_DETECTORS[feature_id]
    except KeyError as exc:
        raise ValueError(f"Unknown fusion detector: {feature_id}") from exc
    return detector(fusion_state)


def build_fusion_detection_results(fusion_state):
    return {
        feature_id: run_fusion_detector(feature_id, fusion_state)
        for feature_id in FUSION_DETECTORS
    }


def publish_fusion_detection_result(feature_id, fusion_state, channel_layer=None):
    """Publish one detector that consumes an AIS/Radar fusion snapshot."""
    payload = run_fusion_detector(feature_id, fusion_state)
    timeout = getattr(settings, "CACHE_TTL", 300)
    try:
        previous_payload = cache.get(detection_cache_key(feature_id))
        annotate_new_results(feature_id, payload, previous_payload)
        cache.set(detection_cache_key(feature_id), payload, timeout=timeout)
        persist_detection_payload(
            feature_id,
            payload,
            ais_snapshot=fusion_state.get("unmatched_ais_targets") or [],
        )
    except Exception:
        LOGGER.warning(
            "Could not cache or persist %s fusion detection",
            feature_id,
            exc_info=True,
        )

    if channel_layer is None:
        channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                ViolationConsumer.GROUP_NAME,
                {
                    "type": "send_detection_update",
                    "results": {feature_id: payload},
                },
            )
        except Exception:
            LOGGER.warning("Could not broadcast fusion detection", exc_info=True)
    return payload


def publish_fusion_detection_results(fusion_state, channel_layer=None):
    """Cache and broadcast CloseAIS/Forgery results from one fusion window."""
    return {
        feature_id: publish_fusion_detection_result(
            feature_id,
            fusion_state,
            channel_layer=channel_layer,
        )
        for feature_id in FUSION_DETECTORS
    }
