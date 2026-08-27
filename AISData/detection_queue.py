import json

from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection

from AISData.detection import DETECTORS
from AISData.detection_source import detection_source_id

DETECTION_QUEUE_PREFIX = "ais:detection:queue:"
DETECTION_PENDING_PREFIX = "ais:detection:pending:"
DETECTION_TRIGGER_VALUE = "latest"
SNAPSHOT_QUEUE_DETECTORS = {
    "detect-CrossingBoundary",
    "detect-smuggling",
    "detect-abnormalWandering",
    "detect-deviation",
    "detect-highSpeedBoat",
    "detect-lowSpeedBoat",
}
INCREMENTAL_QUEUE_DETECTORS = {
    "detect-CrossingBoundary",
    "detect-smuggling",
    "detect-abnormalWandering",
    "detect-deviation",
    "detect-highSpeedBoat",
    "detect-lowSpeedBoat",
}
LATEST_ONLY_DETECTORS = {
    "detect-blackList",
    # Their complete incremental tracks are retained by trajectory_history.
    # The three consumers need only the newest shared behaviour result, so old
    # full snapshots must not accumulate independently in three queues.
    "detect-abnormalStaying",
    "detect-illegalAnchored",
    "detect-illegalStaying",
}


def get_detection_queue_connection():
    return get_redis_connection("default")


def _queue_suffix(namespace=None, simulation_id=None):
    source_id = detection_source_id(namespace, simulation_id)
    return "" if source_id == "operational" else f"{source_id}:"


def detection_queue_key(feature_id, namespace=None, simulation_id=None):
    return (
        f"{DETECTION_QUEUE_PREFIX}"
        f"{_queue_suffix(namespace, simulation_id)}{feature_id}"
    )


def detection_pending_key(feature_id, namespace=None, simulation_id=None):
    return (
        f"{DETECTION_PENDING_PREFIX}"
        f"{_queue_suffix(namespace, simulation_id)}{feature_id}"
    )


def enqueue_detection(
    feature_id,
    ship_list=None,
    incremental_ship_list=None,
    namespace=None,
    simulation_id=None,
):
    """
    Enqueue a source-appropriate detection trigger.

    Instantaneous models retain only a trigger for the latest snapshot.
    Stateful trajectory models queue each accepted incremental batch so they
    do not lose intermediate event-time observations in either source mode.
    """
    if feature_id not in DETECTORS:
        raise ValueError(f"Unknown detector: {feature_id}")

    source_id = detection_source_id(namespace, simulation_id)
    connection = get_detection_queue_connection()
    preserve_snapshot = feature_id not in LATEST_ONLY_DETECTORS and (
        source_id != "operational"
        or feature_id in SNAPSHOT_QUEUE_DETECTORS
    )
    if preserve_snapshot:
        queued_ship_list = ship_list
        if (
            feature_id in INCREMENTAL_QUEUE_DETECTORS
            and incremental_ship_list is not None
        ):
            queued_ship_list = incremental_ship_list
        if queued_ship_list is None:
            from AISData.detection_source import detection_snapshot_cache_key

            queued_ship_list = cache.get(
                detection_snapshot_cache_key(namespace),
                [],
            )
        if not isinstance(queued_ship_list, list) or not queued_ship_list:
            return False
        payload = json.dumps(
            queued_ship_list,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        queue_key = detection_queue_key(
            feature_id,
            namespace,
            simulation_id,
        )
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
        detection_pending_key(feature_id, namespace, simulation_id),
        "1",
        nx=True,
        ex=pending_ttl,
    )
    if created:
        connection.rpush(
            detection_queue_key(feature_id, namespace, simulation_id),
            DETECTION_TRIGGER_VALUE,
        )
    return bool(created)


def enqueue_all_detections(
    ship_list=None,
    incremental_ship_list=None,
    namespace=None,
    simulation_id=None,
):
    """Signal every detector independently and return per-model status."""
    return {
        feature_id: enqueue_detection(
            feature_id,
            ship_list=ship_list,
            incremental_ship_list=incremental_ship_list,
            namespace=namespace,
            simulation_id=simulation_id,
        )
        for feature_id in DETECTORS
    }


def wait_for_detection_trigger(
    feature_id,
    timeout=5,
    namespace=None,
    simulation_id=None,
):
    if feature_id not in DETECTORS:
        raise ValueError(f"Unknown detector: {feature_id}")

    connection = get_detection_queue_connection()
    item = connection.blpop(
        detection_queue_key(feature_id, namespace, simulation_id),
        timeout=timeout,
    )
    if item is None:
        return None

    raw_values = [item[1]]
    source_id = detection_source_id(namespace, simulation_id)

    if feature_id in LATEST_ONLY_DETECTORS:
        queue_key = detection_queue_key(
            feature_id,
            namespace,
            simulation_id,
        )
        pending_key = detection_pending_key(
            feature_id,
            namespace,
            simulation_id,
        )
        # Older simulator processes may still enqueue every replay snapshot.
        # Atomically keep only the newest queued value, clear the backlog and
        # release the coalescing flag. Anything arriving after this transaction
        # creates the next trigger and is therefore not lost.
        pipeline = connection.pipeline(transaction=True)
        pipeline.lrange(queue_key, -1, -1)
        pipeline.delete(queue_key)
        pipeline.delete(pending_key)
        tail, _, _ = pipeline.execute()
        raw_value = tail[-1] if tail else item[1]
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8")
        if raw_value == DETECTION_TRIGGER_VALUE:
            return True
        try:
            snapshot = json.loads(raw_value)
        except (TypeError, ValueError):
            return True
        return snapshot if isinstance(snapshot, list) else True

    if source_id != "operational" or feature_id in SNAPSHOT_QUEUE_DETECTORS:
        if feature_id in INCREMENTAL_QUEUE_DETECTORS:
            batch_size = max(
                1,
                int(
                    getattr(
                        settings,
                        "DETECTION_INCREMENTAL_QUEUE_BATCH_SIZE",
                        getattr(
                            settings,
                            "CROSSING_DETECTION_QUEUE_BATCH_SIZE",
                            100,
                        ),
                    )
                ),
            )
            if batch_size > 1:
                queue_key = detection_queue_key(
                    feature_id,
                    namespace,
                    simulation_id,
                )
                pipeline = connection.pipeline(transaction=True)
                pipeline.lrange(queue_key, 0, batch_size - 2)
                pipeline.ltrim(queue_key, batch_size - 1, -1)
                tail, _ = pipeline.execute()
                raw_values.extend(tail or [])

        observations = []
        for raw_value in raw_values:
            if isinstance(raw_value, bytes):
                raw_value = raw_value.decode("utf-8")
            try:
                snapshot = json.loads(raw_value)
            except (TypeError, ValueError):
                continue
            if isinstance(snapshot, list):
                observations.extend(snapshot)
        return observations

    # Clear before computation so arrivals during a long detection run can
    # create exactly one follow-up trigger for the newest snapshot.
    connection.delete(
        detection_pending_key(feature_id, namespace, simulation_id)
    )
    return True


def clear_detection_queues(namespace=None, simulation_id=None):
    connection = get_detection_queue_connection()
    keys = []
    for feature_id in DETECTORS:
        keys.extend(
            (
                detection_queue_key(feature_id, namespace, simulation_id),
                detection_pending_key(feature_id, namespace, simulation_id),
            )
        )
    if keys:
        connection.delete(*keys)
    return len(keys)
