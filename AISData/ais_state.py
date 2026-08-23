"""Per-vessel operational AIS state and incremental update construction."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from AISData.normalization import UNKNOWN_AIS_TARGET_NAME


DYNAMIC_STATE_CACHE_KEY = "ais:state:dynamic:v1"
STATIC_STATE_CACHE_KEY = "ais:state:static:v1"
LEGACY_SNAPSHOT_CACHE_KEY = "latest_ais_data_raw"
FILE_CHECKPOINT_CACHE_KEY = "ais:worker:file-checkpoints:v1"
STATE_VERSION_CACHE_KEY = "ais:state:version:v1"

STATIC_FIELDS = (
    "name",
    "imo",
    "flag",
    "iso3",
    "draught",
    "ship_type",
    "length",
    "width",
    "to_bow",
    "to_stern",
    "to_port",
    "to_starboard",
)


@dataclass(frozen=True)
class AISStateUpdate:
    snapshot: list
    upserts: list
    removes: list
    accepted: int
    duplicate: int
    out_of_order: int
    version: int

    def delta(self):
        return {
            "upserts": self.upserts,
            "removes": self.removes,
            "server_time": timezone.now().isoformat(),
            "version": self.version,
        }


def _timestamp(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = parse_datetime(value)
    else:
        result = None
    if result is None:
        return None
    if timezone.is_naive(result):
        result = timezone.make_aware(result, dt_timezone.utc)
    return result


def _mapping(value):
    return value.copy() if isinstance(value, dict) else {}


def _meaningful_static_value(field, value):
    if value is None or value == "":
        return False
    if field == "name" and value == UNKNOWN_AIS_TARGET_NAME:
        return False
    return True


def _merge_static(existing, incoming):
    merged = existing.copy()
    for field in STATIC_FIELDS:
        value = incoming.get(field)
        if _meaningful_static_value(field, value):
            merged[field] = value
    merged["mmsi"] = incoming["mmsi"]
    merged["timestamp"] = incoming["timestamp"]
    merged["received_at"] = incoming.get("received_at")
    merged["msg_type"] = incoming.get("msg_type")
    merged["source"] = incoming.get("source", "")
    return merged


def _dynamic_equivalent(first, second):
    ignored = {
        "source",
        "channel",
        "collection_type",
        "quality_flags",
        "received_at",
    }
    return {
        key: value for key, value in first.items() if key not in ignored
    } == {
        key: value for key, value in second.items() if key not in ignored
    }


def _enrich(dynamic, static):
    item = dynamic.copy()
    for field in STATIC_FIELDS:
        current = item.get(field)
        if not _meaningful_static_value(field, current):
            value = static.get(field)
            if _meaningful_static_value(field, value):
                item[field] = value
    return item


def _carry_forward_static_fields(existing, incoming):
    candidate = incoming.copy()
    if not existing:
        return candidate
    for field in STATIC_FIELDS:
        if not _meaningful_static_value(field, candidate.get(field)):
            previous = existing.get(field)
            if _meaningful_static_value(field, previous):
                candidate[field] = previous
    return candidate


def _cache_key(base_key, namespace=None):
    value = str(namespace or "").strip().strip(":")
    return f"{value}:{base_key}" if value else base_key


def _state_timeout():
    retention = int(
        getattr(settings, "AIS_LATEST_STATE_RETENTION_SECONDS", 15 * 60)
    )
    return max(30 * 60, retention * 2)


def load_ais_state_version(namespace=None):
    try:
        return max(
            0,
            int(cache.get(_cache_key(STATE_VERSION_CACHE_KEY, namespace), 0)),
        )
    except (TypeError, ValueError):
        return 0


def _next_state_version(has_changes, namespace=None):
    current = load_ais_state_version(namespace)
    if not has_changes:
        return current
    version = current + 1
    cache.set(
        _cache_key(STATE_VERSION_CACHE_KEY, namespace),
        version,
        timeout=None,
    )
    return version


def merge_ais_state(dynamic_updates, static_updates=None, namespace=None):
    """Merge ordered AIS updates and return the new snapshot plus a delta."""
    dynamic_key = _cache_key(DYNAMIC_STATE_CACHE_KEY, namespace)
    static_key = _cache_key(STATIC_STATE_CACHE_KEY, namespace)
    snapshot_key = _cache_key(LEGACY_SNAPSHOT_CACHE_KEY, namespace)
    cached_dynamic = cache.get(dynamic_key)
    dynamic_state = _mapping(cached_dynamic)
    if not isinstance(cached_dynamic, dict):
        # One-time compatibility bootstrap when deploying over the former
        # full-snapshot cache without restarting the surrounding services.
        dynamic_state = {
            str(item.get("mmsi")): item.copy()
            for item in load_ais_snapshot(namespace)
            if isinstance(item, dict) and str(item.get("mmsi") or "").strip()
        }
    static_state = _mapping(cache.get(static_key))
    changed_mmsis = set()
    accepted = duplicate = out_of_order = 0

    for incoming in static_updates or []:
        mmsi = str(incoming.get("mmsi") or "").strip()
        incoming_time = _timestamp(incoming.get("timestamp"))
        if not mmsi or incoming_time is None:
            continue
        existing = static_state.get(mmsi) or {}
        existing_time = _timestamp(existing.get("timestamp"))
        if existing_time is not None and incoming_time < existing_time:
            out_of_order += 1
            continue
        merged = _merge_static(existing, incoming)
        static_payload_fields = STATIC_FIELDS + ("msg_type", "source")
        if merged == existing or (
            existing_time == incoming_time
            and all(
                existing.get(field) == merged.get(field)
                for field in static_payload_fields
            )
        ):
            duplicate += 1
            continue
        static_state[mmsi] = merged
        if any(existing.get(field) != merged.get(field) for field in STATIC_FIELDS):
            changed_mmsis.add(mmsi)
        accepted += 1

    for incoming in dynamic_updates or []:
        mmsi = str(incoming.get("mmsi") or "").strip()
        incoming_time = _timestamp(incoming.get("timestamp"))
        if not mmsi or incoming_time is None:
            continue
        existing = dynamic_state.get(mmsi)
        incoming = _carry_forward_static_fields(existing, incoming)
        existing_time = _timestamp(existing.get("timestamp")) if existing else None
        if existing_time is not None and incoming_time < existing_time:
            out_of_order += 1
            continue
        if existing == incoming or (
            existing_time == incoming_time
            and existing is not None
            and _dynamic_equivalent(existing, incoming)
        ):
            duplicate += 1
            continue
        dynamic_state[mmsi] = incoming.copy()
        changed_mmsis.add(mmsi)
        accepted += 1

    all_dynamic_times = [
        _timestamp(item.get("timestamp")) for item in dynamic_state.values()
    ]
    all_dynamic_times = [value for value in all_dynamic_times if value is not None]
    reference_time = max(all_dynamic_times) if all_dynamic_times else None
    removes = []
    if reference_time is not None:
        cutoff = reference_time - timedelta(
            seconds=max(
                1,
                int(
                    getattr(
                        settings,
                        "AIS_LATEST_STATE_RETENTION_SECONDS",
                        15 * 60,
                    )
                ),
            )
        )
        for mmsi, item in list(dynamic_state.items()):
            item_time = _timestamp(item.get("timestamp"))
            if item_time is None or item_time < cutoff:
                dynamic_state.pop(mmsi, None)
                removes.append(mmsi)
                changed_mmsis.discard(mmsi)

        static_cutoff = reference_time - timedelta(
            seconds=max(
                1,
                int(
                    getattr(
                        settings,
                        "AIS_STATIC_STATE_RETENTION_SECONDS",
                        7 * 24 * 60 * 60,
                    )
                ),
            )
        )
        for mmsi, item in list(static_state.items()):
            item_time = _timestamp(item.get("timestamp"))
            if item_time is not None and item_time < static_cutoff:
                static_state.pop(mmsi, None)

    snapshot = [
        _enrich(dynamic_state[mmsi], static_state.get(mmsi) or {})
        for mmsi in sorted(dynamic_state)
    ]
    upserts = [
        _enrich(dynamic_state[mmsi], static_state.get(mmsi) or {})
        for mmsi in sorted(changed_mmsis)
        if mmsi in dynamic_state
    ]
    timeout = _state_timeout()
    cache.set(dynamic_key, dynamic_state, timeout=timeout)
    cache.set(static_key, static_state, timeout=None)
    # Existing detectors continue to consume a full snapshot while clients use deltas.
    cache.set(snapshot_key, snapshot, timeout=timeout)
    version = _next_state_version(bool(upserts or removes), namespace)
    return AISStateUpdate(
        snapshot=snapshot,
        upserts=upserts,
        removes=sorted(removes),
        accepted=accepted,
        duplicate=duplicate,
        out_of_order=out_of_order,
        version=version,
    )


def load_ais_snapshot(namespace=None):
    snapshot = cache.get(_cache_key(LEGACY_SNAPSHOT_CACHE_KEY, namespace))
    return snapshot if isinstance(snapshot, list) else []


def clear_ais_state(namespace=None, preserve_version=False):
    """Clear one operational or simulation namespace without touching others."""
    current_version = load_ais_state_version(namespace)
    cache.delete_many(
        [
            _cache_key(DYNAMIC_STATE_CACHE_KEY, namespace),
            _cache_key(STATIC_STATE_CACHE_KEY, namespace),
            _cache_key(LEGACY_SNAPSHOT_CACHE_KEY, namespace),
            _cache_key(STATE_VERSION_CACHE_KEY, namespace),
        ]
    )
    if preserve_version:
        version = current_version + 1
        cache.set(
            _cache_key(STATE_VERSION_CACHE_KEY, namespace),
            version,
            timeout=None,
        )
        return version
    return 0


def load_file_checkpoints():
    return _mapping(cache.get(FILE_CHECKPOINT_CACHE_KEY))


def save_file_checkpoint(filename, signature):
    checkpoints = load_file_checkpoints()
    checkpoints[str(filename)] = str(signature)
    cache.set(FILE_CHECKPOINT_CACHE_KEY, checkpoints, timeout=None)


def file_signature(path):
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"
