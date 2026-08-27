from math import isfinite

from django.conf import settings

from AISData.low_speed_behavior import (
    BEHAVIOR_ANCHORING,
    BEHAVIOR_STAYING,
    analyse_low_speed_behavior,
    distance_metres,
)
from IllegalAnchored.zones import point_in_polygon


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
    "min_heading_observations": 2,
    "anchor_swing_heading_degrees": 45,
    "min_anchor_status_ratio": 0.6,
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
    config["min_heading_observations"] = max(
        2, int(config["min_heading_observations"])
    )
    config["anchor_swing_heading_degrees"] = min(
        180.0, max(0.0, float(config["anchor_swing_heading_degrees"]))
    )
    config["min_anchor_status_ratio"] = min(
        1.0, max(0.0, float(config["min_anchor_status_ratio"]))
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
    vertices = area.get("vertices")
    if isinstance(vertices, (list, tuple)) and len(vertices) >= 3:
        cleaned_vertices = []
        for point in vertices:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                cleaned_vertices = []
                break
            point_lon = _finite_float(point[0])
            point_lat = _finite_float(point[1])
            if point_lon is None or point_lat is None:
                cleaned_vertices = []
                break
            cleaned_vertices.append((point_lon, point_lat))
        if len(cleaned_vertices) >= 3:
            return point_in_polygon(lon, lat, cleaned_vertices)

    bounds = area.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        cleaned_bounds = [_finite_float(value) for value in bounds]
        if all(value is not None for value in cleaned_bounds):
            min_lon, min_lat, max_lon, max_lat = cleaned_bounds
            return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat
    return False


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
    prepared = []
    for point in points or []:
        prepared.append(
            {
                **point,
                "context_key": (
                    point.get("zone_name"),
                    bool(point.get("in_port_basin", False)),
                    bool(point.get("at_dock", False)),
                    point.get("matched_port_name") or "",
                ),
            }
        )
    analysis = analyse_low_speed_behavior(
        prepared,
        stationary_speed_knots=config["max_speed_knots"],
        exit_speed_knots=config["max_speed_knots"],
        max_gap_seconds=config["maximum_gap_seconds"],
        max_episode_distance_metres=config["distance_threshold_metres"],
        stationary_radius_metres=config["distance_threshold_metres"],
        min_points=config["min_points"],
        min_duration_seconds=config["min_duration_minutes"] * 60,
        min_heading_observations=config["min_heading_observations"],
        anchor_swing_heading_degrees=config[
            "anchor_swing_heading_degrees"
        ],
        min_anchor_status_ratio=config["min_anchor_status_ratio"],
    )
    if (
        not analysis["qualified"]
        or analysis["behavior"]
        not in {BEHAVIOR_STAYING, BEHAVIOR_ANCHORING}
    ):
        return None
    return {
        "started_at": analysis["points"][0]["timestamp"],
        "duration_minutes": analysis["duration_seconds"] / 60,
        "point_count": analysis["point_count"],
        "average_speed_knots": analysis["average_speed_knots"],
        "max_speed_knots": analysis["max_speed_knots"],
        "behavior": analysis["behavior"],
        "behavior_reason": analysis["reason"],
        "anchor_status_ratio": analysis["anchor_status_ratio"],
        "heading_variation_degrees": analysis[
            "heading_variation_degrees"
        ],
        "position_swing": analysis["position_swing"],
    }


def staying_event_from_behavior_result(shared_result, area, config):
    """Apply forbidden-area duration/radius rules to shared behaviour."""
    if not isinstance(shared_result, dict):
        return None
    analysis = shared_result.get("analysis") or {}
    if (
        not analysis.get("qualified")
        or analysis.get("behavior")
        not in {BEHAVIOR_STAYING, BEHAVIOR_ANCHORING}
    ):
        return None

    segment = []
    later = None
    latest_point = (analysis.get("points") or [None])[-1]
    if latest_point is None:
        return None
    for point in reversed(analysis.get("points") or []):
        point_area = get_forbidden_area(
            point["lon"], point["lat"], config["forbidden_areas"]
        )
        if point_area is None or point_area["name"] != area["name"]:
            break
        if point["speed"] > config["max_speed_knots"]:
            break
        if later is not None:
            gap = (later["timestamp"] - point["timestamp"]).total_seconds()
            if gap < 0 or gap > config["maximum_gap_seconds"]:
                break
        if distance_metres(
            (point["lon"], point["lat"]),
            (latest_point["lon"], latest_point["lat"]),
        ) > config["distance_threshold_metres"]:
            break
        segment.append(point)
        later = point
    segment.reverse()
    if len(segment) < config["min_points"]:
        return None
    duration_seconds = (
        segment[-1]["timestamp"] - segment[0]["timestamp"]
    ).total_seconds()
    if duration_seconds < config["min_duration_minutes"] * 60:
        return None
    center = (
        sum(point["lon"] for point in segment) / len(segment),
        sum(point["lat"] for point in segment) / len(segment),
    )
    radius = max(
        distance_metres(center, (point["lon"], point["lat"]))
        for point in segment
    )
    if radius > config["distance_threshold_metres"]:
        return None
    speeds = [point["speed"] for point in segment]
    return {
        "started_at": segment[0]["timestamp"],
        "duration_minutes": duration_seconds / 60,
        "point_count": len(segment),
        "average_speed_knots": sum(speeds) / len(speeds),
        "max_speed_knots": max(speeds),
        "behavior": analysis["behavior"],
        "behavior_reason": analysis["reason"],
        "anchor_status_ratio": analysis.get("anchor_status_ratio", 0.0),
        "heading_variation_degrees": analysis.get(
            "heading_variation_degrees"
        ),
        "position_swing": analysis.get("position_swing", False),
    }
