"""Short-lived, per-vessel AIS history shared by worker processes."""

import logging
import math
from collections import defaultdict
from datetime import timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime


LOGGER = logging.getLogger(__name__)
HISTORY_CACHE_KEY_PREFIX = "ais:trajectory:history:"


def _cache_key(mmsi):
    return f"{HISTORY_CACHE_KEY_PREFIX}{mmsi}"


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _aware_datetime(value):
    if isinstance(value, str):
        value = parse_datetime(value)
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


def _history_point(source):
    if not isinstance(source, dict):
        return None
    mmsi = str(
        source.get("mmsi")
        or source.get("MMSI")
        or source.get("ais_id")
        or ""
    ).strip()
    observed_at = _aware_datetime(
        source.get("timestamp") or source.get("time")
    )
    longitude = _finite_float(source.get("lon", source.get("longitude")))
    latitude = _finite_float(source.get("lat", source.get("latitude")))
    if (
        not mmsi
        or observed_at is None
        or longitude is None
        or latitude is None
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        return None
    return {
        "mmsi": mmsi,
        "timestamp": observed_at.isoformat(),
        "lon": longitude,
        "lat": latitude,
        "speed": _finite_float(source.get("speed", source.get("sog"))),
        "course": _finite_float(source.get("course", source.get("cog"))),
        "name": str(source.get("name") or "").strip(),
    }


def append_ais_history(sources):
    """Append AIS observations while retaining a bounded source-time window."""
    grouped = defaultdict(list)
    for source in sources or []:
        point = _history_point(source)
        if point is not None:
            grouped[point["mmsi"]].append(point)
    if not grouped:
        return 0

    keys = {_cache_key(mmsi): mmsi for mmsi in grouped}
    try:
        cached = cache.get_many(keys)
    except Exception:
        LOGGER.warning("Could not read AIS trajectory history cache", exc_info=True)
        cached = {}

    window = timedelta(
        seconds=max(
            1,
            int(
                getattr(
                    settings,
                    "AIS_TRAJECTORY_HISTORY_WINDOW_SECONDS",
                    30 * 60,
                )
            ),
        )
    )
    max_points = max(
        1,
        int(
            getattr(
                settings,
                "AIS_TRAJECTORY_HISTORY_MAX_POINTS_PER_SHIP",
                500,
            )
        ),
    )
    values = {}
    total = 0
    for key, mmsi in keys.items():
        incoming = grouped[mmsi]
        previous = [
            point
            for point in (cached.get(key) or [])
            if _history_point(point) is not None
        ]
        incoming_latest = max(
            _aware_datetime(point["timestamp"])
            for point in incoming
        )
        previous_times = [
            _aware_datetime(point.get("timestamp"))
            for point in previous
        ]
        previous_times = [value for value in previous_times if value is not None]
        # A replay may restart at an earlier source time while old cache keys
        # are still alive. Treat that as a new stream instead of discarding it.
        if previous_times and incoming_latest < max(previous_times) - window:
            previous = []

        by_timestamp = {}
        for point in previous + incoming:
            normalised = _history_point(point)
            if normalised is not None:
                by_timestamp[normalised["timestamp"]] = normalised
        ordered = sorted(
            by_timestamp.values(),
            key=lambda point: _aware_datetime(point["timestamp"]),
        )
        newest = _aware_datetime(ordered[-1]["timestamp"])
        cutoff = newest - window
        ordered = [
            point
            for point in ordered
            if _aware_datetime(point["timestamp"]) >= cutoff
        ][-max_points:]
        values[key] = ordered
        total += len(incoming)

    try:
        cache.set_many(
            values,
            timeout=max(
                1,
                int(
                    getattr(
                        settings,
                        "AIS_TRAJECTORY_HISTORY_CACHE_TTL",
                        2 * 60 * 60,
                    )
                ),
            ),
        )
    except Exception:
        LOGGER.warning("Could not update AIS trajectory history cache", exc_info=True)
    return total


def get_ais_history(mmsis, end_at=None):
    """Return cached points for the requested vessels inside the history window."""
    identifiers = sorted({str(value).strip() for value in mmsis if str(value).strip()})
    if not identifiers:
        return []
    keys = {_cache_key(mmsi): mmsi for mmsi in identifiers}
    try:
        cached = cache.get_many(keys)
    except Exception:
        LOGGER.warning("Could not read AIS trajectory history cache", exc_info=True)
        return []

    end_time = _aware_datetime(end_at)
    window = timedelta(
        seconds=max(
            1,
            int(
                getattr(
                    settings,
                    "AIS_TRAJECTORY_HISTORY_WINDOW_SECONDS",
                    30 * 60,
                )
            ),
        )
    )
    points = []
    for key in keys:
        for source in cached.get(key) or []:
            point = _history_point(source)
            if point is None:
                continue
            observed_at = _aware_datetime(point["timestamp"])
            effective_end = end_time or observed_at
            if effective_end - window <= observed_at <= effective_end:
                points.append(point)
    return sorted(
        points,
        key=lambda point: (
            _aware_datetime(point["timestamp"]),
            point["mmsi"],
        ),
    )
