from math import asin, cos, isfinite, radians, sin, sqrt

from django.conf import settings


DEFAULT_CONFIG = {
    "analysis_window_minutes": 120,
    "retention_window_minutes": 240,
    "max_speed_knots": 0.5,
    "distance_threshold_metres": 50,
    "min_duration_minutes": 30,
    "min_points": 3,
    "monitored_areas": [
        {
            "name": "异常停泊监控区",
            "bounds": (113.6279434, 22.1376273, 113.7935190, 22.2008193),
        }
    ],
}


def get_parking_config():
    """Return validated runtime settings for abnormal-parking detection."""
    configured = getattr(settings, "ABNORMAL_PARKING", {})
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
    config["max_speed_knots"] = max(0.0, float(config["max_speed_knots"]))
    config["distance_threshold_metres"] = max(
        1.0, float(config["distance_threshold_metres"])
    )
    config["min_duration_minutes"] = max(
        0.0, float(config["min_duration_minutes"])
    )
    config["min_points"] = max(2, int(config["min_points"]))
    if not isinstance(config["monitored_areas"], (list, tuple)):
        config["monitored_areas"] = DEFAULT_CONFIG["monitored_areas"]
    return config


def haversine(p1, p2):
    """Return the great-circle distance between two lon/lat points in km."""
    lon1, lat1 = float(p1[0]), float(p1[1])
    lon2, lat2 = float(p2[0]), float(p2[1])

    lat1, lon1 = radians(lat1), radians(lon1)
    lat2, lon2 = radians(lat2), radians(lon2)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    value = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    )
    return 2 * 6371 * asin(sqrt(min(1.0, value)))


def get_monitored_area(lat, lon, monitored_areas=None):
    """Return the configured monitored area containing the position."""
    areas = (
        monitored_areas
        if monitored_areas is not None
        else get_parking_config()["monitored_areas"]
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


def is_in_monitored_area(lat, lon, monitored_areas=None):
    return get_monitored_area(lat, lon, monitored_areas) is not None


def detect_parking_events(
    points_list,
    distance_threshold_metres=50,
    time_threshold_minutes=30,
    min_points=3,
    max_speed_knots=0.5,
):
    """
    Detect stationary episodes.

    Each point is ``(lat, lon, timestamp, speed)``. A moving point explicitly
    ends the current episode so two separate stops cannot be joined after
    speed filtering.
    """
    valid_points = []
    for point in points_list:
        if not isinstance(point, (list, tuple)) or len(point) < 4:
            continue
        try:
            lat = float(point[0])
            lon = float(point[1])
            speed = float(point[3])
        except (TypeError, ValueError):
            continue
        timestamp = point[2]
        if (
            timestamp is None
            or not isfinite(lat)
            or not isfinite(lon)
            or not isfinite(speed)
        ):
            continue
        valid_points.append((lat, lon, timestamp, max(0.0, speed)))

    if len(valid_points) < min_points:
        return []

    points_sorted = sorted(valid_points, key=lambda item: item[2])
    distance_threshold_km = distance_threshold_metres / 1000
    events = []
    segment = []

    def finish_segment():
        if len(segment) < min_points:
            return
        duration = (
            segment[-1][2] - segment[0][2]
        ).total_seconds() / 60
        if duration >= time_threshold_minutes:
            events.append(list(segment))

    for point in points_sorted:
        if point[3] > max_speed_knots:
            finish_segment()
            segment = []
            continue

        if not segment:
            segment = [point]
            continue

        origin = segment[0]
        distance_km = haversine(
            (origin[1], origin[0]),
            (point[1], point[0]),
        )
        if distance_km <= distance_threshold_km:
            segment.append(point)
        else:
            finish_segment()
            segment = [point]

    finish_segment()
    return events


def run_parking_analysis(queryset, config=None):
    """Analyse one vessel's ordered ParkingBuffer queryset."""
    config = config or get_parking_config()
    if not queryset.exists():
        return {"is_abnormal": False, "count": 0, "events": []}

    points = [
        (
            item["latitude"],
            item["longitude"],
            item["timestamp"],
            item["speed"],
        )
        for item in queryset.values(
            "latitude",
            "longitude",
            "timestamp",
            "speed",
        )
    ]
    raw_events = detect_parking_events(
        points,
        distance_threshold_metres=config["distance_threshold_metres"],
        time_threshold_minutes=config["min_duration_minutes"],
        min_points=config["min_points"],
        max_speed_knots=config["max_speed_knots"],
    )

    abnormal_events = []
    for event in raw_events:
        area = get_monitored_area(
            event[0][0],
            event[0][1],
            config["monitored_areas"],
        )
        if area is None:
            continue
        duration = (event[-1][2] - event[0][2]).total_seconds() / 60
        abnormal_events.append(
            {
                "start_time": event[0][2].isoformat(),
                "end_time": event[-1][2].isoformat(),
                "duration_minutes": round(duration, 2),
                "point_count": len(event),
                "location": {
                    "lat": event[0][0],
                    "lon": event[0][1],
                },
                "area": area.get("name") or "异常停泊监控区",
            }
        )

    return {
        "is_abnormal": bool(abnormal_events),
        "count": len(abnormal_events),
        "events": abnormal_events,
    }
