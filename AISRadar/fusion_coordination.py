"""Reliable event-time coordination for operational AIS/Radar fusion."""

from datetime import timezone as dt_timezone
import hashlib
import json
import math
import time

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_redis import get_redis_connection
from redis.exceptions import ResponseError

from AISData.ais_state import load_ais_snapshot
from AISData.trajectory_history import get_ais_history


AIS_WATERMARK_CACHE_KEY = "ais:radar:fusion:ais-watermark:v1"
AIS_PARTITION_WATERMARKS_CACHE_KEY = (
    "ais:radar:fusion:ais-partition-watermarks:v1"
)
AIS_PROGRESS_CACHE_PREFIX = "ais:radar:fusion:ais-progress:v1:"
DEFAULT_STREAM_KEY = "ais:radar:fusion:events:v1"
DEFAULT_GROUP_NAME = "ais-radar-fusion-coordinator"
DEFAULT_CONSUMER_NAME = "coordinator-1"
COMPLETED_CACHE_PREFIX = "ais:radar:fusion:completed:v1:"


def coordinator_config():
    result = {
        "stream_key": DEFAULT_STREAM_KEY,
        "group_name": DEFAULT_GROUP_NAME,
        "consumer_name": DEFAULT_CONSUMER_NAME,
        "wait_ms": 500,
        "poll_block_ms": 1000,
        "error_retry_seconds": 1.0,
        # Zero means no producer-side trimming. Acknowledged entries are
        # deleted, so queued/pending work is never evicted just for age.
        "stream_max_length": 0,
        "completed_ttl_seconds": 7 * 24 * 60 * 60,
        "max_ais_time_gap_seconds": 180.0,
    }
    result.update(getattr(settings, "AIS_RADAR_FUSION_COORDINATOR", {}))
    return result


def _aware_datetime(value):
    if isinstance(value, str):
        value = parse_datetime(value)
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _partition_progress_key(topic, partition):
    identity = "%s:%s" % (str(topic), int(partition))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return AIS_PROGRESS_CACHE_PREFIX + digest


def publish_ais_ingest_progress(topic, partition, offset, event_time=None):
    """Record that the AIS consumer completed one Kafka partition offset."""
    key = _partition_progress_key(topic, partition)
    current = cache.get(key)
    current_offset = -1
    if isinstance(current, dict):
        try:
            current_offset = int(current.get("offset", -1))
        except (TypeError, ValueError):
            current_offset = -1
    offset = int(offset)
    if offset < current_offset:
        return current

    observed_at = _aware_datetime(event_time)
    progress = {
        "topic": str(topic),
        "partition": int(partition),
        "offset": offset,
        "event_time": observed_at.isoformat() if observed_at else None,
        "updated_at": timezone.now().isoformat(),
    }
    cache.set(key, progress, timeout=None)

    if observed_at is not None:
        partition_watermarks = cache.get(AIS_PARTITION_WATERMARKS_CACHE_KEY)
        if not isinstance(partition_watermarks, dict):
            partition_watermarks = {}
        partition_identity = "%s:%s" % (str(topic), int(partition))
        previous_time = _aware_datetime(
            partition_watermarks.get(partition_identity)
        )
        if previous_time is None or observed_at > previous_time:
            partition_watermarks[partition_identity] = observed_at.isoformat()
            cache.set(
                AIS_PARTITION_WATERMARKS_CACHE_KEY,
                partition_watermarks,
                timeout=None,
            )
        # A cross-topic event cannot compare Kafka offsets. Use the minimum
        # progress across known AIS partitions so one fast partition cannot
        # make the whole AIS stream appear ready.
        known_times = [
            _aware_datetime(value) for value in partition_watermarks.values()
        ]
        known_times = [value for value in known_times if value is not None]
        if known_times:
            cache.set(
                AIS_WATERMARK_CACHE_KEY,
                {
                    "event_time": min(known_times).isoformat(),
                    "updated_at": timezone.now().isoformat(),
                },
                timeout=None,
            )
    return progress


def ais_is_ready(topic, partition, offset, radar_time):
    """Check partition progress for a shared topic, otherwise event watermark."""
    ais_topic = str(
        getattr(settings, "KAFKA_AIS", {}).get("topic") or ""
    ).strip()
    if ais_topic and str(topic) == ais_topic:
        progress = cache.get(_partition_progress_key(topic, partition))
        if not isinstance(progress, dict):
            return False
        try:
            return int(progress.get("offset", -1)) >= int(offset)
        except (TypeError, ValueError):
            return False

    watermark = cache.get(AIS_WATERMARK_CACHE_KEY)
    watermark_time = _aware_datetime(
        watermark.get("event_time") if isinstance(watermark, dict) else None
    )
    event_time = _aware_datetime(radar_time)
    return bool(
        watermark_time is not None
        and event_time is not None
        and watermark_time >= event_time
    )


def wait_for_ais(event, wait_ms=None, poll_ms=25):
    """Wait briefly for AIS progress, returning False when the deadline wins."""
    config = coordinator_config()
    timeout_ms = max(0, int(config["wait_ms"] if wait_ms is None else wait_ms))
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        if ais_is_ready(
            event["topic"],
            event["partition"],
            event["offset"],
            event["sensor_timestamp"],
        ):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(max(1, int(poll_ms)) / 1000.0, remaining))


def build_causal_ais_rows(event_time):
    """Build JPDA AIS rows at or before one Radar event time."""
    snapshot = load_ais_snapshot()
    mmsis = {
        str(item.get("mmsi") or "").strip()
        for item in snapshot
        if isinstance(item, dict) and str(item.get("mmsi") or "").strip()
    }
    if not mmsis:
        return []

    window_seconds = max(
        1,
        int(
            getattr(
                settings,
                "KAFKA_RADAR_AIS_HISTORY_SECONDS",
                getattr(settings, "AIS_TRAJECTORY_HISTORY_WINDOW_SECONDS", 1800),
            )
        ),
    )
    sources = get_ais_history(
        mmsis,
        end_at=event_time,
        window_seconds=window_seconds,
    )
    sources.extend(snapshot)
    event_datetime = _aware_datetime(event_time)
    rows_by_key = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        observed_at = _aware_datetime(source.get("timestamp"))
        target_id = str(source.get("mmsi") or "").strip()
        latitude = _finite_float(source.get("lat"))
        longitude = _finite_float(source.get("lon"))
        if (
            not target_id
            or observed_at is None
            or event_datetime is None
            or observed_at > event_datetime
            or latitude is None
            or longitude is None
        ):
            continue
        row = {
            "DateTime": observed_at.isoformat(),
            "ID": target_id,
            "X": latitude,
            "Y": longitude,
        }
        for field_name in ("speed", "course"):
            value = _finite_float(source.get(field_name))
            if value is not None:
                row[field_name] = value
        rows_by_key[(target_id, row["DateTime"])] = row
    return sorted(
        rows_by_key.values(),
        key=lambda row: (row["DateTime"], row["ID"]),
    )


def radar_event_id(topic, partition, offset):
    return "%s:%s:%s" % (str(topic), int(partition), int(offset))


def enqueue_radar_fusion_event(
    *,
    topic,
    partition,
    offset,
    sensor_timestamp,
    radar_rows,
):
    """Append one durable operational Radar event to the Redis Stream."""
    config = coordinator_config()
    event = {
        "event_id": radar_event_id(topic, partition, offset),
        "topic": str(topic),
        "partition": int(partition),
        "offset": int(offset),
        "sensor_timestamp": str(sensor_timestamp),
        "radar_rows": list(radar_rows or []),
        "enqueued_at": timezone.now().isoformat(),
    }
    connection = get_redis_connection("default")
    fields = {
        "payload": json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    }
    max_length = int(config.get("stream_max_length", 0))
    if max_length > 0:
        message_id = connection.xadd(
            str(config["stream_key"]),
            fields,
            maxlen=max_length,
            approximate=True,
        )
    else:
        message_id = connection.xadd(str(config["stream_key"]), fields)
    return message_id, event


def ensure_coordinator_group(connection, config=None):
    config = config or coordinator_config()
    try:
        connection.xgroup_create(
            str(config["stream_key"]),
            str(config["group_name"]),
            id="0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _decode_event(raw_fields):
    payload = raw_fields.get(b"payload", raw_fields.get("payload"))
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    event = json.loads(payload)
    required = (
        "event_id",
        "topic",
        "partition",
        "offset",
        "sensor_timestamp",
        "radar_rows",
    )
    if not isinstance(event, dict) or any(name not in event for name in required):
        raise ValueError("Radar fusion event is missing required fields")
    if not isinstance(event["radar_rows"], list):
        raise ValueError("Radar fusion event radar_rows must be a list")
    return event


def read_coordinator_events(connection, config=None, count=1):
    """Read pending work first, then block for new Redis Stream events."""
    config = config or coordinator_config()
    stream = str(config["stream_key"])
    group = str(config["group_name"])
    consumer = str(config["consumer_name"])
    pending = connection.xreadgroup(
        group,
        consumer,
        {stream: "0"},
        count=max(1, int(count)),
    )
    messages = pending or connection.xreadgroup(
        group,
        consumer,
        {stream: ">"},
        count=max(1, int(count)),
        block=max(1, int(config["poll_block_ms"])),
    )
    result = []
    for _, entries in messages or []:
        for message_id, raw_fields in entries:
            result.append((message_id, _decode_event(raw_fields)))
    return result


def _completed_key(event_id):
    digest = hashlib.sha256(str(event_id).encode("utf-8")).hexdigest()
    return COMPLETED_CACHE_PREFIX + digest


def fusion_event_completed(event_id):
    return bool(cache.get(_completed_key(event_id)))


def mark_fusion_event_completed(event_id):
    ttl = max(1, int(coordinator_config()["completed_ttl_seconds"]))
    cache.set(_completed_key(event_id), True, timeout=ttl)


def acknowledge_coordinator_event(connection, message_id, config=None):
    config = config or coordinator_config()
    stream = str(config["stream_key"])
    pipeline = connection.pipeline(transaction=True)
    pipeline.xack(stream, str(config["group_name"]), message_id)
    pipeline.xdel(stream, message_id)
    acknowledged, _ = pipeline.execute()
    return acknowledged
