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


def build_fusion_detection_results(fusion_state):
    return {
        "detect-ais-off": detect_close_ais(fusion_state),
        "detect-spoofing": detect_forgery(fusion_state),
    }


def publish_fusion_detection_results(fusion_state, channel_layer=None):
    """Cache and broadcast CloseAIS/Forgery results from one fusion window."""
    payloads = build_fusion_detection_results(fusion_state)
    timeout = getattr(settings, "CACHE_TTL", 300)
    for feature_id, payload in payloads.items():
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
                    "results": payloads,
                },
            )
        except Exception:
            LOGGER.warning("Could not broadcast fusion detections", exc_info=True)
    return payloads
