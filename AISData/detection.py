import json
import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.http import HttpRequest
from django.utils import timezone
from django.utils.module_loading import import_string

from AISData.normalization import normalise_ais_snapshot
from AISData.model_parameters import apply_runtime_parameter_override
from AISData.detection_source import (
    detection_source_id,
    internal_detection_cache_key,
    is_detection_source_active,
)


logger = logging.getLogger(__name__)

DETECTION_CACHE_PREFIX = "detection_result:"
EXTERNAL_DETECTION_FEATURES = {
    "detect-overload",
    "detect-ais-off",
    "detect-spoofing",
}

EVENT_ID_FIELDS = (
    "event_id",
    "alert_id",
)
PREDICTION_ID_FIELD = "prediction_id"
EVENT_TARGET_FIELDS = (
    "mmsi",
    "other_mmsi",
    "ais_id",
    "radar_id",
    "target_id",
    "ship_id",
    "blacklist_id",
    "camera_key",
)
EVENT_CLASSIFICATION_FIELDS = (
    "pair",
    "fence_id",
    "crossing_direction",
    "zone",
    "area",
    "behavior",
    "facility",
    "status",
    "event",
)

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


def _event_id(result):
    for field in EVENT_ID_FIELDS:
        value = str(result.get(field) or "").strip()
        if value:
            return value
    return ""


def _event_signature(feature_id, result):
    """Build a stable signature for detectors without their own event IDs."""

    pair_mmsi = result.get("pair_mmsi")
    if isinstance(pair_mmsi, (list, tuple, set)):
        pair_mmsi = tuple(
            sorted(str(value).strip() for value in pair_mmsi if str(value).strip())
        )
    else:
        pair_mmsi = str(pair_mmsi or "").strip()

    identity = [("pair_mmsi", pair_mmsi)] if pair_mmsi else []
    for field in EVENT_TARGET_FIELDS + EVENT_CLASSIFICATION_FIELDS:
        value = result.get(field)
        if value is None or value == "":
            continue
        identity.append((field, str(value).strip()))
    if identity:
        return (feature_id, tuple(identity))

    # All current warning models expose an event or target identifier.  The
    # fallback keeps anonymous results distinct without depending on list order.
    return (
        feature_id,
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {
                    *EVENT_ID_FIELDS,
                    PREDICTION_ID_FIELD,
                    "is_new",
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
    )


def annotate_new_results(feature_id, payload, previous_payload=None):
    """Assign one uniform prediction ID to each warning lifecycle."""
    if not isinstance(payload, dict):
        return payload
    results = payload.get("results")
    if not isinstance(results, list):
        return payload

    previous_results = (
        previous_payload.get("results", [])
        if isinstance(previous_payload, dict)
        else []
    )
    previous_predictions_by_event_id = {}
    previous_predictions_by_signature = {}
    for previous in previous_results:
        if not isinstance(previous, dict):
            continue
        prediction_id = str(
            previous.get(PREDICTION_ID_FIELD) or ""
        ).strip()
        if not prediction_id:
            continue
        event_id = _event_id(previous)
        if event_id:
            previous_predictions_by_event_id.setdefault(
                event_id, prediction_id
            )
        else:
            previous_predictions_by_signature.setdefault(
                _event_signature(feature_id, previous), prediction_id
            )

    for result in results:
        if not isinstance(result, dict):
            continue
        event_id = _event_id(result)
        if event_id:
            continued_prediction_id = (
                previous_predictions_by_event_id.get(event_id)
            )
        else:
            continued_prediction_id = (
                previous_predictions_by_signature.get(
                    _event_signature(feature_id, result)
                )
            )

        detector_is_new = result.get("is_new")
        is_new = (
            detector_is_new is True
            or continued_prediction_id is None
        )
        result[PREDICTION_ID_FIELD] = (
            uuid.uuid4().hex
            if is_new
            else continued_prediction_id
        )
        result["is_new"] = is_new
    return payload


def _internal_request(feature_id, ship_list, detection_context=None):
    request = HttpRequest()
    request.method = "GET"
    request.path = f"/internal/detection/{feature_id}/"
    request.ais_ship_list = ship_list
    request.ais_context = dict(detection_context or {})
    return request


def run_detector_core(
    feature_id,
    detector,
    ship_list,
    detection_context=None,
):
    """Execute one detector without cache, persistence, or WebSocket effects.

    The callable is shared by operational workers and the offline evaluation
    runner.  Detector-owned rolling state is deliberately left intact here;
    callers choose the appropriate state boundary (the evaluation runner uses
    an isolated cache plus a rolled-back database transaction per sample).
    """
    computed_at = timezone.now().isoformat()
    try:
        if isinstance(detector, str):
            detector = import_string(detector)
        from .maritime_zone_registry import maritime_zone_snapshot

        with apply_runtime_parameter_override(feature_id), maritime_zone_snapshot():
            response = detector(
                _internal_request(
                    feature_id,
                    ship_list,
                    detection_context,
                )
            )
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


def run_detector(
    feature_id,
    ship_list=None,
    on_result=None,
    detection_context=None,
):
    """Run one detector in its dedicated worker process."""
    if feature_id not in DETECTORS:
        raise ValueError(f"Unknown detector: {feature_id}")
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    ship_list = normalise_ais_snapshot(ship_list)
    context = dict(detection_context or {})
    namespace = context.get("namespace") or "operational"
    simulation_id = context.get("simulation_id") or None
    source_id = context.get("source_id") or detection_source_id(
        namespace,
        simulation_id,
    )
    source_active = is_detection_source_active(namespace, simulation_id)

    if not connection.in_atomic_block:
        close_old_connections()
    try:
        result_cache_key = internal_detection_cache_key(
            feature_id,
            namespace,
            simulation_id,
        )
        previous_payload = cache.get(result_cache_key)
        payload = run_detector_core(
            feature_id,
            DETECTORS[feature_id],
            ship_list,
            context,
        )
        annotate_new_results(feature_id, payload, previous_payload)
        cache.set(
            result_cache_key,
            payload,
            timeout=getattr(settings, "CACHE_TTL", 300),
        )
        if source_active:
            cache.set(
                detection_cache_key(feature_id),
                payload,
                timeout=getattr(settings, "CACHE_TTL", 300),
            )
            try:
                from AISData.violation_records import persist_detection_payload

                persist_detection_payload(
                    feature_id,
                    payload,
                    ais_snapshot=ship_list,
                    trajectory_namespace=(
                        namespace if namespace != "operational" else None
                    ),
                    source_namespace=namespace,
                    simulation_id=simulation_id,
                    persistence_scope=(
                        source_id if namespace != "operational" else None
                    ),
                )
            except Exception:
                logger.exception(
                    "Could not persist violation result for %s",
                    feature_id,
                )
            if on_result is not None:
                on_result(feature_id, payload)
        return payload
    finally:
        if not connection.in_atomic_block:
            close_old_connections()
