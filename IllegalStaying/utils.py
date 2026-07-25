from math import isfinite

from django.conf import settings

from IllegalAnchored.zones import distance_m, point_in_polygon


DEFAULT_CONFIG = {
    "analysis_window_minutes": 120,
    "retention_window_minutes": 240,
    "max_speed_knots": 0.5,
    "distance_threshold_metres": 50,
    "min_duration_minutes": 30,
    "min_points": 3,
    "maximum_gap_seconds": 180,
    "max_position_age_seconds": 120,
    "future_tolerance_seconds": 120,
    "event_retention_minutes": 60,
    "forbidden_areas": [
        {
            "name": "非法驻留监控区",
            "bounds": (
                113.6279434,
                22.1376273,
                113.7935190,
                22.2008193,
            ),
            "reason": "该区域禁止未经许可的长时间驻留",
        }
    ],
}


def get_staying_config():
    configured = getattr(settings, "ILLEGAL_STAYING", {})
    config = {
        **DEFAULT_CONFIG,
        **(configured if isinstance(configured, dict) else {}),
    }
    config["analysis_window_minutes"] = max(
        1, int(config["analysis_window_minutes"])
    )
    config["retention_window_minutes"] = max(
        config["analysis_window_minutes"],
        int(config["retention_window_minutes"]),
    )
    config["max_speed_knots"] = max(
        0.0, float(config["max_speed_knots"])
    )
    config["distance_threshold_metres"] = max(
        1.0, float(config["distance_threshold_metres"])
    )
    config["min_duration_minutes"] = max(
        0.0, float(config["min_duration_minutes"])
    )
    config["min_points"] = max(2, int(config["min_points"]))
    config["maximum_gap_seconds"] = max(
        1, int(config["maximum_gap_seconds"])
    )
    config["max_position_age_seconds"] = max(
        0, int(config["max_position_age_seconds"])
    )
    config["future_tolerance_seconds"] = max(
        0, int(config["future_tolerance_seconds"])
    )
    config["event_retention_minutes"] = max(
        1, int(config["event_retention_minutes"])
    )
    if not isinstance(config["forbidden_areas"], (list, tuple)):
        config["forbidden_areas"] = DEFAULT_CONFIG["forbidden_areas"]
    return config


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _area_contains(area, lon, lat):
    bounds = area.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        cleaned = [_finite_float(value) for value in bounds]
        if all(value is not None for value in cleaned):
            min_lon, min_lat, max_lon, max_lat = cleaned
            return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat

    vertices = area.get("vertices")
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


def get_forbidden_area(lon, lat, areas=None):
    areas = (
        get_staying_config()["forbidden_areas"]
        if areas is None
        else areas
    )
    for index, area in enumerate(areas):
        if not isinstance(area, dict) or not _area_contains(area, lon, lat):
            continue
        return {
            "name": str(
                area.get("name") or f"非法驻留区{index + 1}"
            ).strip()[:100],
            "reason": str(
                area.get("reason")
                or "该区域禁止未经许可的长时间驻留"
            ).strip(),
        }
    return None


def continuous_staying_event(points, config):
    """Return the continuous stationary episode ending at the latest point."""
    if not points:
        return None
    current = points[-1]
    if current["speed"] > config["max_speed_knots"]:
        return None

    segment = []
    later_timestamp = current["timestamp"]
    for point in reversed(points):
        gap_seconds = (
            later_timestamp - point["timestamp"]
        ).total_seconds()
        if (
            point["zone_name"] != current["zone_name"]
            or point["speed"] > config["max_speed_knots"]
            or gap_seconds < 0
            or gap_seconds > config["maximum_gap_seconds"]
            or distance_m(
                point["longitude"],
                point["latitude"],
                current["longitude"],
                current["latitude"],
            )
            > config["distance_threshold_metres"]
        ):
            break
        segment.append(point)
        later_timestamp = point["timestamp"]
    segment.reverse()

    if len(segment) < config["min_points"]:
        return None
    duration_minutes = (
        segment[-1]["timestamp"] - segment[0]["timestamp"]
    ).total_seconds() / 60
    if duration_minutes < config["min_duration_minutes"]:
        return None

    speeds = [point["speed"] for point in segment]
    return {
        "started_at": segment[0]["timestamp"],
        "duration_minutes": duration_minutes,
        "point_count": len(segment),
        "average_speed_knots": sum(speeds) / len(speeds),
        "max_speed_knots": max(speeds),
    }
