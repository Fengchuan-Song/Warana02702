from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt

from django.conf import settings


DEFAULT_CONFIG = {
    "minimum_speed_knots": 2.0,
    "minimum_duration_seconds": 120,
    "minimum_observations": 5,
    "maximum_gap_seconds": 90,
    "minimum_displacement_metres": 500,
    "route_entry_distance_metres": 750,
    "deviation_distance_metres": 1500,
    "confirmation_observations": 3,
    "confirmation_duration_seconds": 60,
    "direction_tolerance_degrees": 45,
    "minimum_direction_coherence": 0.5,
    "coverage_margin_metres": 20_000,
    "analysis_window_minutes": 30,
    "retention_window_minutes": 60,
    "max_position_age_seconds": 120,
    "future_tolerance_seconds": 120,
    "max_valid_speed_knots": 102.2,
    "state_retention_minutes": 30,
    "eligible_nav_statuses": [0, 15],
    "allow_missing_nav_status": True,
}


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def get_deviation_config():
    configured = getattr(settings, "DEVIATION_DETECTION", {})
    config = {
        **DEFAULT_CONFIG,
        **(configured if isinstance(configured, dict) else {}),
    }
    float_keys = (
        "minimum_speed_knots",
        "minimum_displacement_metres",
        "route_entry_distance_metres",
        "deviation_distance_metres",
        "direction_tolerance_degrees",
        "minimum_direction_coherence",
        "coverage_margin_metres",
        "max_valid_speed_knots",
    )
    int_keys = (
        "minimum_duration_seconds",
        "minimum_observations",
        "maximum_gap_seconds",
        "confirmation_observations",
        "confirmation_duration_seconds",
        "analysis_window_minutes",
        "retention_window_minutes",
        "max_position_age_seconds",
        "future_tolerance_seconds",
        "state_retention_minutes",
    )
    for key in float_keys:
        config[key] = max(0.0, float(config[key]))
    for key in int_keys:
        config[key] = max(0, int(config[key]))
    config["minimum_observations"] = max(
        2, config["minimum_observations"]
    )
    config["maximum_gap_seconds"] = max(
        1, config["maximum_gap_seconds"]
    )
    config["confirmation_observations"] = max(
        2, config["confirmation_observations"]
    )
    config["analysis_window_minutes"] = max(
        1,
        config["analysis_window_minutes"],
        int(config["minimum_duration_seconds"] / 60) + 1,
    )
    config["retention_window_minutes"] = max(
        config["analysis_window_minutes"],
        config["retention_window_minutes"],
    )
    config["deviation_distance_metres"] = max(
        config["route_entry_distance_metres"] + 1,
        config["deviation_distance_metres"],
    )
    config["minimum_direction_coherence"] = min(
        1.0, config["minimum_direction_coherence"]
    )
    config["direction_tolerance_degrees"] = min(
        90.0, config["direction_tolerance_degrees"]
    )
    config["max_valid_speed_knots"] = max(
        config["minimum_speed_knots"],
        config["max_valid_speed_knots"],
    )
    statuses = set()
    raw_statuses = config.get("eligible_nav_statuses")
    if isinstance(raw_statuses, (list, tuple, set)):
        for value in raw_statuses:
            try:
                statuses.add(int(value))
            except (TypeError, ValueError):
                continue
    config["eligible_nav_statuses"] = statuses
    config["allow_missing_nav_status"] = _as_bool(
        config.get("allow_missing_nav_status"),
        DEFAULT_CONFIG["allow_missing_nav_status"],
    )
    return config


def distance_metres(first, second):
    lon1, lat1 = first
    lon2, lat2 = second
    lon_delta = radians(lon2 - lon1)
    lat_delta = radians(lat2 - lat1)
    lat1_rad = radians(lat1)
    lat2_rad = radians(lat2)
    value = (
        sin(lat_delta / 2) ** 2
        + cos(lat1_rad) * cos(lat2_rad) * sin(lon_delta / 2) ** 2
    )
    return 2 * 6_371_000 * asin(sqrt(min(1.0, value)))


def bearing_degrees(first, second):
    lon1, lat1 = map(radians, first)
    lon2, lat2 = map(radians, second)
    lon_delta = lon2 - lon1
    y = sin(lon_delta) * cos(lat2)
    x = (
        cos(lat1) * sin(lat2)
        - sin(lat1) * cos(lat2) * cos(lon_delta)
    )
    return (degrees(atan2(y, x)) + 360) % 360


def undirected_angle_difference(first, second):
    if first is None or second is None:
        return None
    difference = abs(float(first) - float(second)) % 180
    return min(difference, 180 - difference)


def finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None
