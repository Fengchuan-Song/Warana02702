from collections import Counter
from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


DEFAULT_CONFIG = {
    "analysis_window_minutes": 30,
    "retention_window_minutes": 60,
    "min_points": 10,
    "min_duration_minutes": 10,
    "min_path_distance_metres": 300,
    "min_leg_distance_metres": 20,
    "min_turn_angle_degrees": 45,
    "min_turn_count": 3,
    "max_displacement_ratio": 0.65,
    "max_gap_minutes": 5,
    "min_area_point_ratio": 0.5,
    "monitored_areas": [
        {
            "name": "异常徘徊监控区",
            "bounds": (113.6109833, 22.1700302, 113.7926567, 22.2080471),
        }
    ],
}


def get_wandering_config():
    configured = getattr(settings, "ABNORMAL_WANDERING", {})
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
    config["min_points"] = max(4, int(config["min_points"]))
    config["min_duration_minutes"] = max(
        0.0, float(config["min_duration_minutes"])
    )
    config["min_path_distance_metres"] = max(
        1.0, float(config["min_path_distance_metres"])
    )
    config["min_leg_distance_metres"] = max(
        0.0, float(config["min_leg_distance_metres"])
    )
    config["min_turn_angle_degrees"] = min(
        180.0,
        max(0.0, float(config["min_turn_angle_degrees"])),
    )
    config["min_turn_count"] = max(1, int(config["min_turn_count"]))
    config["max_displacement_ratio"] = min(
        1.0,
        max(0.0, float(config["max_displacement_ratio"])),
    )
    config["max_gap_minutes"] = max(
        0.1, float(config["max_gap_minutes"])
    )
    config["min_area_point_ratio"] = min(
        1.0,
        max(0.0, float(config["min_area_point_ratio"])),
    )
    if not isinstance(config["monitored_areas"], (list, tuple)):
        config["monitored_areas"] = DEFAULT_CONFIG["monitored_areas"]
    else:
        config["monitored_areas"] = list(config["monitored_areas"])
    return config


def haversine_metres(p1, p2):
    """Return great-circle distance for two ``(lon, lat)`` points."""
    lon1, lat1 = radians(float(p1[0])), radians(float(p1[1]))
    lon2, lat2 = radians(float(p2[0])), radians(float(p2[1]))
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    value = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    )
    return 2 * 6_371_000 * asin(sqrt(min(1.0, value)))


def bearing_degrees(p1, p2):
    """Return initial bearing in degrees for two ``(lon, lat)`` points."""
    lon1, lat1 = radians(float(p1[0])), radians(float(p1[1]))
    lon2, lat2 = radians(float(p2[0])), radians(float(p2[1]))
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(x, y)) + 360) % 360


def heading_change(first, second):
    difference = abs(float(second) - float(first)) % 360
    return min(difference, 360 - difference)


def get_monitored_area(lat, lon, monitored_areas=None):
    areas = (
        monitored_areas
        if monitored_areas is not None
        else get_wandering_config()["monitored_areas"]
    )
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if not isfinite(lat) or not isfinite(lon):
        return None

    for area in areas:
        if not isinstance(area, dict):
            continue
        bounds = area.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, bounds)
        except (TypeError, ValueError):
            continue
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
            return area
    return None


def _parse_timestamp(value):
    if hasattr(value, "tzinfo"):
        timestamp = value
    else:
        timestamp = parse_datetime(str(value or ""))
    if timestamp is None:
        return None
    if timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(
            timestamp,
            timezone.get_current_timezone(),
        )
    return timestamp


def _normalise_points(rows):
    points = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            lat = float(row.get("latitude", row.get("Latitude")))
            lon = float(row.get("longitude", row.get("Longitude")))
            speed = float(row.get("speed", row.get("Speed", 0)))
            course = float(row.get("course", row.get("Heading", 0)))
        except (TypeError, ValueError):
            continue
        timestamp = _parse_timestamp(
            row.get("timestamp", row.get("Timestamp"))
        )
        if (
            timestamp is None
            or not all(
                isfinite(value) for value in (lat, lon, speed, course)
            )
            or not (-90 <= lat <= 90 and -180 <= lon <= 180)
        ):
            continue
        points.append(
            {
                "lat": lat,
                "lon": lon,
                "timestamp": timestamp,
                "speed": max(0.0, speed),
                "course": course % 360,
            }
        )
    return sorted(points, key=lambda item: item["timestamp"])


def _split_on_time_gaps(points, max_gap_minutes):
    if not points:
        return []
    segments = [[points[0]]]
    for point in points[1:]:
        gap_minutes = (
            point["timestamp"] - segments[-1][-1]["timestamp"]
        ).total_seconds() / 60
        if gap_minutes > max_gap_minutes:
            segments.append([point])
        else:
            segments[-1].append(point)
    return segments


def _analyse_segment(segment, config):
    if len(segment) < config["min_points"]:
        return None

    duration_minutes = (
        segment[-1]["timestamp"] - segment[0]["timestamp"]
    ).total_seconds() / 60
    if duration_minutes < config["min_duration_minutes"]:
        return None

    path_distance = 0.0
    meaningful_bearings = []
    for start, end in zip(segment, segment[1:]):
        start_position = (start["lon"], start["lat"])
        end_position = (end["lon"], end["lat"])
        leg_distance = haversine_metres(start_position, end_position)
        path_distance += leg_distance
        if leg_distance >= config["min_leg_distance_metres"]:
            meaningful_bearings.append(
                bearing_degrees(start_position, end_position)
            )

    if path_distance < config["min_path_distance_metres"]:
        return None

    turn_angles = [
        heading_change(first, second)
        for first, second in zip(
            meaningful_bearings,
            meaningful_bearings[1:],
        )
    ]
    turn_count = sum(
        angle >= config["min_turn_angle_degrees"]
        for angle in turn_angles
    )
    if turn_count < config["min_turn_count"]:
        return None

    displacement = haversine_metres(
        (segment[0]["lon"], segment[0]["lat"]),
        (segment[-1]["lon"], segment[-1]["lat"]),
    )
    displacement_ratio = displacement / path_distance
    if displacement_ratio > config["max_displacement_ratio"]:
        return None

    area_names = []
    for point in segment:
        area = get_monitored_area(
            point["lat"],
            point["lon"],
            config["monitored_areas"],
        )
        if area is not None:
            area_names.append(area.get("name") or "异常徘徊监控区")
    area_ratio = len(area_names) / len(segment)
    if area_ratio < config["min_area_point_ratio"]:
        return None

    area_name = Counter(area_names).most_common(1)[0][0]
    return {
        "start_time": segment[0]["timestamp"].isoformat(),
        "end_time": segment[-1]["timestamp"].isoformat(),
        "duration_minutes": round(duration_minutes, 2),
        "point_count": len(segment),
        "turn_count": turn_count,
        "wave_point_count": turn_count,
        "path_distance_metres": round(path_distance, 2),
        "displacement_metres": round(displacement, 2),
        "displacement_ratio": round(displacement_ratio, 4),
        "average_speed_knots": round(
            sum(point["speed"] for point in segment) / len(segment),
            2,
        ),
        "location": {
            "lat": sum(point["lat"] for point in segment) / len(segment),
            "lon": sum(point["lon"] for point in segment) / len(segment),
        },
        "area": area_name,
    }


def detect_loitering_events(points, config=None):
    config = config or get_wandering_config()
    normalised = _normalise_points(points)
    events = []
    for segment in _split_on_time_gaps(
        normalised,
        config["max_gap_minutes"],
    ):
        event = _analyse_segment(segment, config)
        if event is not None:
            events.append(event)
    return events


def run_loitering_analysis(trajectory_queryset, config=None):
    """Analyse a QuerySet or list of trajectory dictionaries."""
    config = config or get_wandering_config()
    if hasattr(trajectory_queryset, "values"):
        rows = list(
            trajectory_queryset.values(
                "latitude",
                "longitude",
                "timestamp",
                "speed",
                "course",
            )
        )
    else:
        rows = list(trajectory_queryset or [])

    abnormal_segments = detect_loitering_events(rows, config)
    return {
        "is_abnormal": bool(abnormal_segments),
        "abnormal_count": len(abnormal_segments),
        "abnormal_segments": abnormal_segments,
    }
