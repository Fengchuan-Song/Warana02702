import json

from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection

from AISData.detection import DETECTORS

DETECTION_QUEUE_PREFIX = "ais:detection:queue:"
DETECTION_PENDING_PREFIX = "ais:detection:pending:"
DETECTION_TRIGGER_VALUE = "latest"
SNAPSHOT_QUEUE_DETECTORS = {
    "detect-CrossingBoundary",
    "detect-smuggling",
}


def get_detection_queue_connection():
    return get_redis_connection("default")


def detection_queue_key(feature_id):
    return f"{DETECTION_QUEUE_PREFIX}{feature_id}"


def detection_pending_key(feature_id):
    return f"{DETECTION_PENDING_PREFIX}{feature_id}"


def enqueue_detection(feature_id, ship_list=None):
    """
    Enqueue at most one pending detection trigger.

    While detection is running, many AIS snapshots may arrive. Coalescing
    those signals prevents an unbounded queue; the next run reads the newest
    snapshot from the shared cache.
    """
    if feature_id not in DETECTORS:
        raise ValueError(f"Unknown detector: {feature_id}")

    connection = get_detection_queue_connection()
    if feature_id in SNAPSHOT_QUEUE_DETECTORS:
        if ship_list is None:
            ship_list = cache.get("latest_ais_data_raw", [])
        if not isinstance(ship_list, list) or not ship_list:
            return False
        payload = json.dumps(
            ship_list,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        queue_key = detection_queue_key(feature_id)
        connection.rpush(queue_key, payload)
        max_snapshots = max(
            1,
            int(
                getattr(
                    settings,
                    "TRAJECTORY_DETECTION_QUEUE_MAX_SNAPSHOTS",
                    500,
                )
            ),
        )
        connection.ltrim(queue_key, -max_snapshots, -1)
        return True

    pending_ttl = getattr(settings, "DETECTION_PENDING_TTL", 3600)
    created = connection.set(
        detection_pending_key(feature_id),
        "1",
        nx=True,
        ex=pending_ttl,
    )
    if created:
        connection.rpush(
            detection_queue_key(feature_id),
            DETECTION_TRIGGER_VALUE,
        )
    return bool(created)


def enqueue_all_detections(ship_list=None):
    """Signal every detector independently and return per-model status."""
    return {
        feature_id: enqueue_detection(feature_id, ship_list=ship_list)
        for feature_id in DETECTORS
    }


def wait_for_detection_trigger(feature_id, timeout=5):
    if feature_id not in DETECTORS:
        raise ValueError(f"Unknown detector: {feature_id}")

    connection = get_detection_queue_connection()
    item = connection.blpop(
        detection_queue_key(feature_id),
        timeout=timeout,
    )
    if item is None:
        return None

    raw_value = item[1]
    if feature_id in SNAPSHOT_QUEUE_DETECTORS:
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8")
        try:
            snapshot = json.loads(raw_value)
        except (TypeError, ValueError):
            return []
        return snapshot if isinstance(snapshot, list) else []

    # Clear before computation so arrivals during a long detection run can
    # create exactly one follow-up trigger for the newest snapshot.
    connection.delete(detection_pending_key(feature_id))
    return True
