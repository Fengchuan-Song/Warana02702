"""Latest operational Radar state merged by track ID."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime


RADAR_STATE_CACHE_KEY = "radar:state:dynamic:v1"
RADAR_SNAPSHOT_CACHE_KEY = "latest_radar_data_raw"


def _equivalent(existing, candidate):
    """Ignore receiver wall-clock time when identifying a Kafka redelivery."""
    existing_value = {
        key: value for key, value in existing.items() if key != "received_at"
    }
    candidate_value = {
        key: value for key, value in candidate.items() if key != "received_at"
    }
    return existing_value == candidate_value


def _timestamp(value):
    if isinstance(value, str):
        value = parse_datetime(value)
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


@dataclass(frozen=True)
class RadarStateUpdate:
    snapshot: list
    accepted_rows: list
    current_rows: list
    removes: list
    accepted: int = 0
    duplicate: int = 0
    out_of_order: int = 0

    @property
    def changed(self):
        return bool(self.accepted_rows or self.removes)


def _state_timeout():
    retention = int(
        getattr(settings, "RADAR_LATEST_STATE_RETENTION_SECONDS", 15 * 60)
    )
    return max(30 * 60, retention * 2)


def merge_radar_state(rows):
    """Merge valid observations without allowing old events to rewind tracks."""
    cached = cache.get(RADAR_STATE_CACHE_KEY)
    state = cached.copy() if isinstance(cached, dict) else {}
    accepted_rows = []
    current_rows = []
    accepted = duplicate = out_of_order = 0

    ordered = sorted(
        (row for row in (rows or []) if isinstance(row, dict)),
        key=lambda row: _timestamp(row.get("timestamp")) or datetime.min.replace(tzinfo=dt_timezone.utc),
    )
    for incoming in ordered:
        target_id = str(incoming.get("id") or "").strip()
        incoming_time = _timestamp(incoming.get("timestamp"))
        if not target_id or incoming_time is None:
            continue
        existing = state.get(target_id)
        existing_time = _timestamp(existing.get("timestamp")) if existing else None
        if existing_time is not None and incoming_time < existing_time:
            out_of_order += 1
            continue
        candidate = incoming.copy()
        candidate["id"] = target_id
        if existing is not None and _equivalent(existing, candidate):
            duplicate += 1
            # Keep redeliveries eligible for downstream publication. If a
            # previous attempt failed after the state write but before fusion,
            # retrying the Kafka offset must complete that missing step.
            current_rows.append(candidate)
            continue
        state[target_id] = candidate
        accepted_rows.append(candidate)
        current_rows.append(candidate)
        accepted += 1

    event_times = [
        _timestamp(item.get("timestamp"))
        for item in state.values()
        if isinstance(item, dict)
    ]
    event_times = [value for value in event_times if value is not None]
    removes = []
    if event_times:
        cutoff = max(event_times) - timedelta(
            seconds=max(
                1,
                int(
                    getattr(
                        settings,
                        "RADAR_LATEST_STATE_RETENTION_SECONDS",
                        15 * 60,
                    )
                ),
            )
        )
        for target_id, item in list(state.items()):
            item_time = _timestamp(item.get("timestamp"))
            if item_time is None or item_time < cutoff:
                state.pop(target_id, None)
                removes.append(target_id)

    snapshot = [state[target_id] for target_id in sorted(state)]
    timeout = _state_timeout()
    cache.set(RADAR_STATE_CACHE_KEY, state, timeout=timeout)
    cache.set(RADAR_SNAPSHOT_CACHE_KEY, snapshot, timeout=timeout)
    return RadarStateUpdate(
        snapshot=snapshot,
        accepted_rows=accepted_rows,
        current_rows=current_rows,
        removes=sorted(removes),
        accepted=accepted,
        duplicate=duplicate,
        out_of_order=out_of_order,
    )


def load_radar_snapshot():
    snapshot = cache.get(RADAR_SNAPSHOT_CACHE_KEY)
    return snapshot if isinstance(snapshot, list) else []


def clear_radar_state():
    cache.delete_many([RADAR_STATE_CACHE_KEY, RADAR_SNAPSHOT_CACHE_KEY])
