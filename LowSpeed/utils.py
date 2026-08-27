from math import isfinite

from django.conf import settings

from IllegalAnchored.zones import distance_m, point_in_polygon


DEFAULT_CONFIG = {
    "default_minimum_speed_knots": 1.5,
    "minimum_duration_seconds": 300,
    "minimum_observations": 5,
    "maximum_gap_seconds": 90,
    "analysis_window_minutes": 30,
    "retention_window_minutes": 60,
    "max_position_age_seconds": 120,
    "future_tolerance_seconds": 120,
    "max_valid_speed_knots": 102.2,
    "event_retention_minutes": 30,
    "eligible_nav_statuses": [0, 15],
    "allow_missing_nav_status": True,
    "stationary_max_speed_knots": 0.5,
    "stationary_max_drift_metres": 50.0,
    "stationary_minimum_duration_seconds": 300,
    "monitored_only": False,
    "zones": [],
}


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def get_low_speed_config():
    configured = getattr(settings, "LOW_SPEED_DETECTION", {})
    config = {
        **DEFAULT_CONFIG,
        **(configured if isinstance(configured, dict) else {}),
    }
    config["default_minimum_speed_knots"] = max(
        0.1, float(config["default_minimum_speed_knots"])
    )
    config["minimum_duration_seconds"] = max(
        0, int(config["minimum_duration_seconds"])
    )
    config["minimum_observations"] = max(
        2, int(config["minimum_observations"])
    )
    config["maximum_gap_seconds"] = max(
        1, int(config["maximum_gap_seconds"])
    )
    config["analysis_window_minutes"] = max(
        1,
        int(config["analysis_window_minutes"]),
        int(config["minimum_duration_seconds"] / 60) + 1,
    )
    config["retention_window_minutes"] = max(
        config["analysis_window_minutes"],
        int(config["retention_window_minutes"]),
    )
    config["max_position_age_seconds"] = max(
        0, int(config["max_position_age_seconds"])
    )
    config["future_tolerance_seconds"] = max(
        0, int(config["future_tolerance_seconds"])
    )
    config["max_valid_speed_knots"] = max(
        config["default_minimum_speed_knots"],
        float(config["max_valid_speed_knots"]),
    )
    config["event_retention_minutes"] = max(
        1, int(config["event_retention_minutes"])
    )
    for key in ("eligible_nav_statuses",):
        raw_statuses = config.get(key)
        statuses = set()
        if isinstance(raw_statuses, (list, tuple, set)):
            for value in raw_statuses:
                try:
                    statuses.add(int(value))
                except (TypeError, ValueError):
                    continue
        config[key] = statuses
    config["allow_missing_nav_status"] = _as_bool(
        config.get("allow_missing_nav_status"),
        DEFAULT_CONFIG["allow_missing_nav_status"],
    )
    config["monitored_only"] = _as_bool(
        config.get("monitored_only"),
        DEFAULT_CONFIG["monitored_only"],
    )
    stationary_speed = _finite_float(config.get("stationary_max_speed_knots"))
    stationary_drift = _finite_float(config.get("stationary_max_drift_metres"))
    stationary_duration = _finite_float(
        config.get("stationary_minimum_duration_seconds")
    )
    config["stationary_max_speed_knots"] = max(
        0.0,
        0.5 if stationary_speed is None else stationary_speed,
    )
    config["stationary_max_drift_metres"] = max(
        0.0,
        50.0 if stationary_drift is None else stationary_drift,
    )
    config["stationary_minimum_duration_seconds"] = max(
        0,
        int(
            config["minimum_duration_seconds"]
            if stationary_duration is None
            else stationary_duration
        ),
    )
    if not isinstance(config.get("zones"), (list, tuple)):
        config["zones"] = []
    return config


def _zone_contains(zone, lon, lat):
    bounds = zone.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        cleaned = [_finite_float(value) for value in bounds]
        if all(value is not None for value in cleaned):
            min_lon, min_lat, max_lon, max_lat = cleaned
            return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat

    vertices = zone.get("vertices")
    if not isinstance(vertices, (list, tuple)) or len(vertices) < 3:
        return False
    cleaned = []
    for point in vertices:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return False
        point_lon = _finite_float(point[0])
        point_lat = _finite_float(point[1])
        if point_lon is None or point_lat is None:
            return False
        cleaned.append((point_lon, point_lat))
    return point_in_polygon(lon, lat, cleaned)


def get_speed_rule(lon, lat, config):
    matches = []
    for index, zone in enumerate(config["zones"]):
        if not isinstance(zone, dict) or not _zone_contains(
            zone, lon, lat
        ):
            continue
        threshold = _finite_float(zone.get("minimum_speed_knots"))
        if threshold is None or threshold <= 0:
            continue
        matches.append(
            {
                "name": str(
                    zone.get("name") or f"低速监控区{index + 1}"
                ).strip()[:100],
                "threshold": threshold,
            }
        )
    if matches:
        # In overlapping zones, use the highest required minimum speed.
        return max(matches, key=lambda rule: rule["threshold"])
    if config["monitored_only"]:
        return None
    return {
        "name": "默认水域",
        "threshold": config["default_minimum_speed_knots"],
    }


def _latest_stationary_run_end(segment, config):
    """Return the last index ending a proven stationary run, if any."""
    maximum_speed = config["stationary_max_speed_knots"]
    minimum_duration = config["stationary_minimum_duration_seconds"]
    maximum_drift = config["stationary_max_drift_metres"]
    run_start = None
    latest_end = None

    for index, point in enumerate(segment):
        if point["speed"] <= maximum_speed:
            if run_start is None:
                run_start = index
        else:
            if run_start is not None:
                run = segment[run_start:index]
                duration = (
                    run[-1]["timestamp"] - run[0]["timestamp"]
                ).total_seconds()
                drift = max(
                    distance_m(
                        item["longitude"],
                        item["latitude"],
                        run[0]["longitude"],
                        run[0]["latitude"],
                    )
                    for item in run
                )
                if duration >= minimum_duration and drift <= maximum_drift:
                    latest_end = index - 1
            run_start = None

    if run_start is not None:
        run = segment[run_start:]
        duration = (
            run[-1]["timestamp"] - run[0]["timestamp"]
        ).total_seconds()
        drift = max(
            distance_m(
                item["longitude"],
                item["latitude"],
                run[0]["longitude"],
                run[0]["latitude"],
            )
            for item in run
        )
        if duration >= minimum_duration and drift <= maximum_drift:
            latest_end = len(segment) - 1

    return latest_end


def continuous_low_speed(points, config):
    if not points:
        return None
    current = points[-1]
    if current["speed"] >= current["speed_limit"]:
        return None

    segment = []
    later_timestamp = current["timestamp"]
    for point in reversed(points):
        gap_seconds = (
            later_timestamp - point["timestamp"]
        ).total_seconds()
        if (
            point["zone_name"] != current["zone_name"]
            or abs(
                point["speed_limit"] - current["speed_limit"]
            )
            > 1e-6
            or point["speed"] >= point["speed_limit"]
            or gap_seconds < 0
            or gap_seconds > config["maximum_gap_seconds"]
        ):
            break
        segment.append(point)
        later_timestamp = point["timestamp"]
    segment.reverse()

    stationary_end = _latest_stationary_run_end(segment, config)
    if stationary_end == len(segment) - 1:
        return None
    if stationary_end is not None:
        # AIS navigation status is operator-entered and may remain 0 while a
        # vessel is actually anchored. Start a new underway episode only after
        # a stationary run has been proven by both time and displacement. A
        # single low SOG sample must not reset a moving low-speed voyage.
        segment = segment[stationary_end + 1 :]

    if len(segment) < config["minimum_observations"]:
        return None
    duration_seconds = (
        segment[-1]["timestamp"] - segment[0]["timestamp"]
    ).total_seconds()
    if duration_seconds < config["minimum_duration_seconds"]:
        return None

    speeds = [point["speed"] for point in segment]
    return {
        "started_at": segment[0]["timestamp"],
        "duration_seconds": duration_seconds,
        "observation_count": len(segment),
        "average_speed_knots": sum(speeds) / len(speeds),
        "minimum_speed_knots": min(speeds),
    }
