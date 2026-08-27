"""Compute low-speed vessel behaviour once and share it between detectors.

The three public warning models remain independent legality consumers.  This
module owns the physical trajectory classification and caches one result for
each vessel observation, so the same AIS tail is not analysed three times.
"""

import logging
from collections import defaultdict

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from AISData.detection_source import detection_source_id
from AISData.low_speed_behavior import (
    analyse_low_speed_behavior,
    detect_moored_status_underway,
)
from AISData.trajectory_history import (
    _aware_datetime,
    _history_point,
    append_ais_history,
    clear_ais_history,
    get_ais_history,
)


LOGGER = logging.getLogger(__name__)
BEHAVIOR_CACHE_PREFIX = "ais:behavior:result:v1:"
DEFAULT_CONFIG = {
    "history_window_seconds": 120 * 60,
    "stationary_speed_knots": 0.5,
    "exit_speed_knots": 1.0,
    "max_gap_seconds": 1200,
    "max_episode_distance_metres": 250.0,
    "stationary_radius_metres": 250.0,
    "min_points": 3,
    "min_heading_observations": 2,
    "max_berthing_heading_degrees": 25.0,
    "anchor_swing_heading_degrees": 45.0,
    "min_anchor_status_ratio": 0.6,
    "min_moored_status_ratio": 0.6,
    "moored_underway_min_speed_knots": 1.0,
    "moored_underway_min_path_distance_metres": 100.0,
    "moored_underway_min_duration_seconds": 300,
    "moored_underway_min_points": 3,
}


def get_behavior_config():
    configured = getattr(settings, "LOW_SPEED_BEHAVIOR_RECOGNITION", {})
    return {
        **DEFAULT_CONFIG,
        **(configured if isinstance(configured, dict) else {}),
    }


def _source_values(namespace=None, simulation_id=None):
    namespace_value = str(namespace or "operational").strip().lower()
    history_namespace = None if namespace_value == "operational" else namespace_value
    return history_namespace, detection_source_id(namespace_value, simulation_id)


def _cache_key(source_id, mmsi):
    return f"{BEHAVIOR_CACHE_PREFIX}{source_id}:{mmsi}"


def _source_signature(point):
    return tuple(
        point.get(name)
        for name in (
            "timestamp",
            "lon",
            "lat",
            "speed",
            "heading",
            "nav_status",
            "at_dock",
            "matched_port_name",
        )
    )


def _latest_sources(sources):
    latest = {}
    for source in sources or []:
        point = _history_point(source)
        if point is None:
            continue
        current = latest.get(point["mmsi"])
        if current is None or point["timestamp"] >= current["timestamp"]:
            latest[point["mmsi"]] = point
    return latest


def _spatial_points(points):
    # Spatial evidence is shared too: port-basin/facility contact is physical
    # context, while each detector still applies its own legal-area decision.
    from AbnormalParking.utils import _add_spatial_context, get_parking_config

    parking_config = get_parking_config()
    enriched = []
    spatial_cache = {}
    for point in points:
        key = (
            point["lon"],
            point["lat"],
            point.get("matched_port_name") or "",
            bool(point.get("at_dock", False)),
        )
        context = spatial_cache.get(key)
        if context is None:
            spatial = _add_spatial_context(point, parking_config)
            context = {
                name: spatial.get(name)
                for name in (
                    "near_fixed_facility",
                    "shore_distance_metres",
                    "facility_name",
                    "in_port_basin",
                    "berthing_facility",
                    "legal_berthing",
                    "legal_area",
                    "authorized_anchorage",
                    "anchorage_name",
                )
            }
            spatial_cache[key] = context
        enriched.append({**point, **context, "context_key": None})
    return enriched


def _calculate(points, config):
    enriched = _spatial_points(points)
    analysis = analyse_low_speed_behavior(
        enriched,
        stationary_speed_knots=float(config["stationary_speed_knots"]),
        exit_speed_knots=float(config["exit_speed_knots"]),
        max_gap_seconds=float(config["max_gap_seconds"]),
        max_episode_distance_metres=float(
            config["max_episode_distance_metres"]
        ),
        stationary_radius_metres=float(config["stationary_radius_metres"]),
        min_points=max(2, int(config["min_points"])),
        min_duration_seconds=0,
        min_heading_observations=max(
            2, int(config["min_heading_observations"])
        ),
        max_berthing_heading_degrees=float(
            config["max_berthing_heading_degrees"]
        ),
        anchor_swing_heading_degrees=float(
            config["anchor_swing_heading_degrees"]
        ),
        min_anchor_status_ratio=float(config["min_anchor_status_ratio"]),
        min_moored_status_ratio=float(config["min_moored_status_ratio"]),
    )
    moored_underway = detect_moored_status_underway(
        enriched,
        min_speed_knots=float(
            config["moored_underway_min_speed_knots"]
        ),
        min_path_distance_metres=float(
            config["moored_underway_min_path_distance_metres"]
        ),
        min_points=max(2, int(config["moored_underway_min_points"])),
        min_duration_seconds=float(
            config["moored_underway_min_duration_seconds"]
        ),
        max_gap_seconds=float(config["max_gap_seconds"]),
    )
    return analysis, moored_underway


def update_behavior_results(
    sources,
    *,
    namespace=None,
    simulation_id=None,
    append_history=False,
    allow_rewind=False,
):
    """Update and return the one shared result for every changed vessel."""
    history_namespace, source_id = _source_values(namespace, simulation_id)
    if append_history:
        append_ais_history(sources, namespace=history_namespace)
    latest = _latest_sources(sources)
    if not latest:
        return {}

    keys = {
        _cache_key(source_id, mmsi): mmsi
        for mmsi in latest
    }
    try:
        cached = cache.get_many(keys)
    except Exception:
        LOGGER.warning("Could not read shared behaviour cache", exc_info=True)
        cached = {}

    results = {}
    missing = []
    for key, mmsi in keys.items():
        value = cached.get(key)
        if (
            isinstance(value, dict)
            and value.get("observed_at") == latest[mmsi]["timestamp"]
            and value.get("source_signature")
            == _source_signature(latest[mmsi])
        ):
            results[mmsi] = value
        elif (
            isinstance(value, dict)
            and _aware_datetime(value.get("observed_at")) is not None
            and _aware_datetime(value.get("observed_at"))
            > _aware_datetime(latest[mmsi]["timestamp"])
        ):
            if allow_rewind:
                # Direct detector calls are also used for isolated historical
                # sequences. A backwards first observation starts a new local
                # sequence; normal ingestion keeps allow_rewind disabled.
                clear_ais_history([mmsi], namespace=history_namespace)
                append_ais_history([latest[mmsi]], namespace=history_namespace)
                cache.delete(key)
                missing.append(mmsi)
            else:
                # Late/out-of-order AIS belongs in trajectory history, but it
                # must not roll the live shared result backwards.
                results[mmsi] = value
        else:
            missing.append(mmsi)
    if not missing:
        return results

    config = get_behavior_config()
    histories = get_ais_history(
        missing,
        namespace=history_namespace,
        window_seconds=config["history_window_seconds"],
    )
    grouped = defaultdict(list)
    for point in histories:
        grouped[point["mmsi"]].append(point)
    values = {}
    for mmsi in missing:
        end_at = _aware_datetime(latest[mmsi]["timestamp"])
        points = [
            point
            for point in grouped.get(mmsi, [])
            if _aware_datetime(point["timestamp"]) <= end_at
        ]
        if not points:
            points = [latest[mmsi]]
        analysis, moored_underway = _calculate(points, config)
        value = {
            "mmsi": mmsi,
            "observed_at": latest[mmsi]["timestamp"],
            "source_signature": _source_signature(latest[mmsi]),
            "computed_at": timezone.now().isoformat(),
            "analysis": analysis,
            "moored_underway": moored_underway,
        }
        results[mmsi] = value
        values[_cache_key(source_id, mmsi)] = value
    try:
        cache.set_many(
            values,
            timeout=max(
                1,
                int(getattr(settings, "LOW_SPEED_BEHAVIOR_CACHE_TTL", 7200)),
            ),
        )
    except Exception:
        LOGGER.warning("Could not update shared behaviour cache", exc_info=True)
    return results


def behavior_results_for_request(ship_list, context=None):
    """Compatibility entry for detector HTTP/internal requests.

    Normal ingestion has already computed these values.  Appending here is
    idempotent and keeps direct endpoint calls and tests functional.
    """
    context = context if isinstance(context, dict) else {}
    return update_behavior_results(
        ship_list,
        namespace=context.get("namespace"),
        simulation_id=context.get("simulation_id"),
        append_history=True,
        allow_rewind=not bool(context),
    )


def clear_behavior_results(mmsis, *, namespace=None, simulation_id=None):
    _history_namespace, source_id = _source_values(namespace, simulation_id)
    identifiers = {str(value).strip() for value in mmsis if str(value).strip()}
    if identifiers:
        cache.delete_many([_cache_key(source_id, mmsi) for mmsi in identifiers])
    return len(identifiers)
