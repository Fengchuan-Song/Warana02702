import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections
from django.http import HttpRequest
from django.utils import timezone
from django.utils.module_loading import import_string

from AISData.normalization import normalise_ais_snapshot


logger = logging.getLogger(__name__)

DETECTION_CACHE_PREFIX = "detection_result:"
EXTERNAL_DETECTION_FEATURES = {
    "detect-overload",
}

# feature_id must match the checkbox names used by Demo_v10.html.
# Dotted paths keep algorithm modules lazy: Daphne and ais_worker can import
# this registry without loading pandas/scipy or the deviation knowledge base.
DETECTORS = {
    "detect-abnormalStaying": (
        "AbnormalParking.views.detectAbnormalParking"
    ),
    "detect-abnormalWandering": (
        "AbnormalWandering.views.receive_realtime_point"
    ),
    "detect-abnormalTransfer": (
        "AbnormalTransfer.views.detect_abnormal_transfer"
    ),
    "detect-blackList": "BlackList.views.detect_black_list",
    "detect-collision": "Collision.views.detect_collision",
    "detect-deviation": "Deviation.views.detect_deviation",
    "detect-doubleDragging": (
        "DoubleDragging.views.detect_double_dragging"
    ),
    "detect-CrossingBoundary": (
        "CrossingBoundary.views.detect_crossing_boundary"
    ),
    "detect-highSpeedBoat": "HighSpeedBoat.views.detect_high_speed",
    "detect-illegalStaying": (
        "IllegalStaying.views.detectIllegalStaying"
    ),
    "detect-illegalAnchored": (
        "IllegalAnchored.views.detect_illegal_anchored"
    ),
    "detect-illegalBerthing": (
        "IllegalBerthing.views.detect_illegal_berthing"
    ),
    "detect-lowSpeedBoat": "LowSpeed.views.detect_low_speed",
    "detect-smuggling": "Smuggling.views.detect_smuggling",
}


def detection_cache_key(feature_id):
    return f"{DETECTION_CACHE_PREFIX}{feature_id}"


def empty_detection_result(feature_id, message="等待后端检测结果"):
    return {
        "success": True,
        "feature_id": feature_id,
        "timestamp": None,
        "computed_at": None,
        "count": 0,
        "results": [],
        "message": message,
    }


def get_detection_result(feature_id):
    return cache.get(
        detection_cache_key(feature_id),
        empty_detection_result(feature_id),
    )


def get_cached_detection_results():
    """Return only detector results that have actually been computed."""
    results = {}
    for feature_id in set(DETECTORS) | EXTERNAL_DETECTION_FEATURES:
        payload = cache.get(detection_cache_key(feature_id))
        if payload is not None:
            results[feature_id] = payload
    return results


def _internal_request(feature_id, ship_list):
    request = HttpRequest()
    request.method = "GET"
    request.path = f"/internal/detection/{feature_id}/"
    request.ais_ship_list = ship_list
    return request


def _execute_detector(feature_id, detector, ship_list):
    computed_at = timezone.now().isoformat()
    try:
        if isinstance(detector, str):
            detector = import_string(detector)
        response = detector(_internal_request(feature_id, ship_list))
        if response.status_code != 200:
            raise RuntimeError(
                f"detector returned HTTP {response.status_code}"
            )

        payload = json.loads(response.content.decode(response.charset))
        if not isinstance(payload, dict):
            raise TypeError("detector response must be a JSON object")

        payload["feature_id"] = feature_id
        payload["computed_at"] = computed_at
        payload.setdefault("count", 0)
        payload.setdefault("results", [])
        payload.setdefault("success", True)
        return payload
    except Exception as exc:
        logger.exception("Detection failed for %s", feature_id)
        return {
            "success": False,
            "feature_id": feature_id,
            "timestamp": None,
            "computed_at": computed_at,
            "count": 0,
            "results": [],
            "message": f"后端检测失败: {exc}",
        }


def run_detector(feature_id, ship_list=None, on_result=None):
    """Run one detector in its dedicated worker process."""
    if feature_id not in DETECTORS:
        raise ValueError(f"Unknown detector: {feature_id}")
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    ship_list = normalise_ais_snapshot(ship_list)

    close_old_connections()
    try:
        payload = _execute_detector(
            feature_id,
            DETECTORS[feature_id],
            ship_list,
        )
        cache.set(
            detection_cache_key(feature_id),
            payload,
            timeout=getattr(settings, "CACHE_TTL", 300),
        )
        if on_result is not None:
            on_result(feature_id, payload)
        return payload
    finally:
        close_old_connections()
