from math import asin, isfinite, radians, sin, cos, sqrt

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from AISData.low_speed_behavior import (
    BEHAVIOR_BERTHING,
    analyse_low_speed_behavior,
    detect_moored_status_underway,
)
from AISData.maritime_zones import (
    MaritimeZoneDataError,
    PORT_ZONE_TYPES,
    zones_containing_point,
)
from IllegalAnchored.zones import classify_location, point_in_polygon


DEFAULT_CONFIG = {
    "analysis_window_minutes": 120,
    "retention_window_minutes": 240,
    "max_speed_knots": 0.5,
    "exit_speed_knots": 1.0,
    "distance_threshold_metres": 50,
    "position_exit_radius_metres": 100,
    "min_duration_minutes": 30,
    "min_points": 3,
    "max_gap_minutes": 20,
    "near_shore_distance_metres": 100,
    "max_heading_change_degrees": 25,
    "min_heading_observations": 2,
    "anchor_swing_heading_degrees": 45,
    "min_anchor_status_ratio": 0.6,
    "moored_underway_min_speed_knots": 1.0,
    "moored_underway_min_path_distance_metres": 100,
    "moored_underway_min_duration_minutes": 5,
    "moored_underway_min_points": 3,
    # 0 means an explicitly legal berth has no overtime rule configured.
    "legal_max_duration_minutes": 0,
    # Optional business polygons use the same bounds/vertices structure.
    # Facility areas prove proximity; legal areas grant use.
    "berthing_facility_areas": [],
    "legal_berthing_areas": [],
}


def get_parking_config():
    """Return validated runtime settings for abnormal-berthing detection."""
    configured = getattr(settings, "ABNORMAL_PARKING", {})
    config = {
        **DEFAULT_CONFIG,
        **(configured if isinstance(configured, dict) else {}),
    }
    config["analysis_window_minutes"] = max(1, int(config["analysis_window_minutes"]))
    config["retention_window_minutes"] = max(
        config["analysis_window_minutes"], int(config["retention_window_minutes"])
    )
    for key in (
        "max_speed_knots",
        "exit_speed_knots",
        "distance_threshold_metres",
        "position_exit_radius_metres",
        "min_duration_minutes",
        "max_gap_minutes",
        "near_shore_distance_metres",
        "max_heading_change_degrees",
        "anchor_swing_heading_degrees",
        "min_anchor_status_ratio",
        "moored_underway_min_speed_knots",
        "moored_underway_min_path_distance_metres",
        "moored_underway_min_duration_minutes",
        "legal_max_duration_minutes",
    ):
        config[key] = max(0.0, float(config[key]))
    config["exit_speed_knots"] = max(
        config["max_speed_knots"], config["exit_speed_knots"]
    )
    config["position_exit_radius_metres"] = max(
        config["distance_threshold_metres"], config["position_exit_radius_metres"]
    )
    config["max_gap_minutes"] = max(0.1, config["max_gap_minutes"])
    config["max_heading_change_degrees"] = min(
        180.0, config["max_heading_change_degrees"]
    )
    config["anchor_swing_heading_degrees"] = min(
        180.0, config["anchor_swing_heading_degrees"]
    )
    config["min_anchor_status_ratio"] = min(
        1.0, config["min_anchor_status_ratio"]
    )
    config["min_points"] = max(2, int(config["min_points"]))
    config["moored_underway_min_points"] = max(
        2, int(config["moored_underway_min_points"])
    )
    config["min_heading_observations"] = max(
        2, int(config["min_heading_observations"])
    )
    for key in (
        "berthing_facility_areas",
        "legal_berthing_areas",
    ):
        if not isinstance(config[key], (list, tuple)):
            config[key] = list(DEFAULT_CONFIG[key])
        else:
            config[key] = list(config[key])
    return config


def haversine(p1, p2):
    """Return the great-circle distance between two lon/lat points in km."""
    lon1, lat1 = float(p1[0]), float(p1[1])
    lon2, lat2 = float(p2[0]), float(p2[1])
    lat1, lon1 = radians(lat1), radians(lon1)
    lat2, lon2 = radians(lat2), radians(lon2)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371 * asin(sqrt(min(1.0, value)))


def _distance_metres(first, second):
    return haversine(first, second) * 1000


def get_configured_area(lat, lon, areas):
    """Return the configured business polygon/bounds containing the position."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not isfinite(lat) or not isfinite(lon):
        return None
    for area in areas:
        if not isinstance(area, dict):
            continue
        vertices = area.get("vertices")
        if isinstance(vertices, (list, tuple)) and len(vertices) >= 3:
            cleaned_vertices = []
            for point in vertices:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    cleaned_vertices = []
                    break
                try:
                    point_lon, point_lat = map(float, point)
                except (TypeError, ValueError):
                    cleaned_vertices = []
                    break
                if not isfinite(point_lon) or not isfinite(point_lat):
                    cleaned_vertices = []
                    break
                cleaned_vertices.append((point_lon, point_lat))
            if len(cleaned_vertices) >= 3:
                if point_in_polygon(lon, lat, cleaned_vertices):
                    return area
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


def circular_heading_variation(headings):
    """Return the smallest arc covering all valid headings in degrees."""
    values = sorted(float(value) % 360 for value in headings if value is not None)
    if len(values) < 2:
        return None
    gaps = [second - first for first, second in zip(values, values[1:])]
    gaps.append(values[0] + 360 - values[-1])
    return 360 - max(gaps)


def _parse_timestamp(value):
    timestamp = value if hasattr(value, "tzinfo") else parse_datetime(str(value or ""))
    if timestamp is None:
        return None
    if timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(timestamp, timezone.get_current_timezone())
    return timestamp


def _optional_number(value, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _normalise_point(raw):
    if isinstance(raw, dict):
        lat = raw.get("latitude", raw.get("lat"))
        lon = raw.get("longitude", raw.get("lon"))
        timestamp = raw.get("timestamp")
        speed = raw.get("speed")
        heading = raw.get("heading")
        nav_status = raw.get("nav_status")
        at_dock = bool(raw.get("at_dock", False))
        matched_port_name = str(raw.get("matched_port_name") or "").strip()
    elif isinstance(raw, (list, tuple)) and len(raw) >= 4:
        lat, lon, timestamp, speed = raw[:4]
        heading = raw[4] if len(raw) > 4 else None
        nav_status = raw[5] if len(raw) > 5 else None
        at_dock = bool(raw[6]) if len(raw) > 6 else False
        matched_port_name = str(raw[7] or "").strip() if len(raw) > 7 else ""
    else:
        return None
    lat = _optional_number(lat, -90, 90)
    lon = _optional_number(lon, -180, 180)
    speed = _optional_number(speed, 0, 102.2)
    timestamp = _parse_timestamp(timestamp)
    if lat is None or lon is None or speed is None or timestamp is None:
        return None
    heading = _optional_number(heading, 0, 359.999999)
    try:
        nav_status = int(float(nav_status))
    except (TypeError, ValueError):
        nav_status = None
    return {
        "lat": lat,
        "lon": lon,
        "timestamp": timestamp,
        "speed": speed,
        "heading": heading,
        "nav_status": nav_status,
        "at_dock": at_dock,
        "matched_port_name": matched_port_name,
    }


def _near_fixed_facility(point, config):
    explicit = get_configured_area(
        point["lat"], point["lon"], config["berthing_facility_areas"]
    )
    if explicit is not None:
        return True, 0.0, explicit.get("name") or "配置靠泊设施", True
    if point["matched_port_name"]:
        return True, 0.0, point["matched_port_name"], False
    try:
        port_matches = zones_containing_point(
            point["lon"], point["lat"], zone_types=PORT_ZONE_TYPES
        )
    except MaritimeZoneDataError:
        port_matches = ()
    if port_matches:
        port = port_matches[0]
        return True, 0.0, port.name or port.description or "港池/码头区", False
    # Natural coastline proximity is intentionally not berthing evidence.
    # Normal berthing must occur in a port basin or configured facility.
    return False, None, None, False


def _legal_berthing(point, config):
    explicit = get_configured_area(
        point["lat"], point["lon"], config["legal_berthing_areas"]
    )
    if explicit is not None:
        return True, explicit.get("name") or "配置合法停泊区"
    if point["matched_port_name"]:
        return True, point["matched_port_name"]
    try:
        matches = zones_containing_point(
            point["lon"], point["lat"], zone_types=PORT_ZONE_TYPES
        )
    except MaritimeZoneDataError:
        matches = ()
    if matches:
        return True, matches[0].name or matches[0].description or "港口/码头区"
    return False, None


def _add_spatial_context(point, config):
    near, coast_distance, facility_name, configured_facility = (
        _near_fixed_facility(point, config)
    )
    legal, legal_area = _legal_berthing(point, config)
    anchorage = classify_location(point["lon"], point["lat"])
    return {
        **point,
        "near_fixed_facility": near,
        "shore_distance_metres": coast_distance,
        "facility_name": facility_name,
        "in_port_basin": near,
        "berthing_facility": configured_facility,
        "legal_berthing": legal,
        "legal_area": legal_area,
        "authorized_anchorage": anchorage["state"] == "authorized",
        "anchorage_name": anchorage.get("zone_name"),
    }


def _position_metrics(points):
    center = (
        sum(point["lon"] for point in points) / len(points),
        sum(point["lat"] for point in points) / len(points),
    )
    radius = max(
        _distance_metres(center, (point["lon"], point["lat"]))
        for point in points
    )
    return center, radius


def _same_spatial_state(first, second):
    return (
        first["legal_berthing"] == second["legal_berthing"]
        and first["legal_area"] == second["legal_area"]
        and first["authorized_anchorage"] == second["authorized_anchorage"]
    )


def detect_parking_events(
    points_list,
    distance_threshold_metres=50,
    time_threshold_minutes=30,
    min_points=3,
    max_speed_knots=0.5,
    *,
    config=None,
):
    """Detect the currently active abnormal mooring/berthing episode.

    The legacy numeric arguments remain supported. New detector calls pass the
    complete configuration so GIS, heading, gap and hysteresis rules are used.
    """
    if config is None:
        config = {
            **get_parking_config(),
            "distance_threshold_metres": float(distance_threshold_metres),
            "min_duration_minutes": float(time_threshold_minutes),
            "min_points": int(min_points),
            "max_speed_knots": float(max_speed_knots),
        }
    else:
        config = {**DEFAULT_CONFIG, **config}
    by_timestamp = {}
    for raw in points_list:
        point = _normalise_point(raw)
        if point is not None:
            by_timestamp[point["timestamp"]] = point
    points = [_add_spatial_context(by_timestamp[key], config) for key in sorted(by_timestamp)]
    if not points:
        return []

    moored_underway = detect_moored_status_underway(
        points,
        min_speed_knots=config["moored_underway_min_speed_knots"],
        min_path_distance_metres=config[
            "moored_underway_min_path_distance_metres"
        ],
        min_points=config["moored_underway_min_points"],
        min_duration_seconds=(
            config["moored_underway_min_duration_minutes"] * 60
        ),
        max_gap_seconds=config["max_gap_minutes"] * 60,
    )
    if moored_underway is not None:
        segment = moored_underway["points"]
        center, radius = _position_metrics(segment)
        return [
            {
                "start_time": segment[0]["timestamp"].isoformat(),
                "end_time": segment[-1]["timestamp"].isoformat(),
                "duration_minutes": round(
                    moored_underway["duration_seconds"] / 60, 2
                ),
                "point_count": moored_underway["point_count"],
                "mean_speed_knots": round(
                    moored_underway["average_speed_knots"], 3
                ),
                "position_radius_metres": round(radius, 2),
                "heading_variation_degrees": None,
                "heading_observation_count": 0,
                "near_fixed_facility": False,
                "facility": "AIS停泊状态航行轨迹",
                "shore_distance_metres": None,
                "legal_berthing": False,
                "legal_area": None,
                "reason": moored_underway["reason"],
                "behavior": "underway_with_moored_status",
                "path_distance_metres": round(
                    moored_underway["path_distance_metres"], 2
                ),
                "location": {"lon": center[0], "lat": center[1]},
            }
        ]

    for point in points:
        point["context_key"] = (
            point["legal_berthing"],
            point["legal_area"],
            point["authorized_anchorage"],
            point["facility_name"],
        )
    analysis = analyse_low_speed_behavior(
        points,
        stationary_speed_knots=config["max_speed_knots"],
        exit_speed_knots=config["exit_speed_knots"],
        max_gap_seconds=config["max_gap_minutes"] * 60,
        max_episode_distance_metres=config["position_exit_radius_metres"],
        stationary_radius_metres=config["distance_threshold_metres"],
        min_points=config["min_points"],
        min_duration_seconds=config["min_duration_minutes"] * 60,
        min_heading_observations=config["min_heading_observations"],
        max_berthing_heading_degrees=config["max_heading_change_degrees"],
        anchor_swing_heading_degrees=config[
            "anchor_swing_heading_degrees"
        ],
        min_anchor_status_ratio=config["min_anchor_status_ratio"],
        # Legacy utility compatibility only. Runtime detectors consume the
        # unified classifier, where sufficient anchor evidence takes priority.
        berth_contact_overrides_anchor_status=True,
    )
    if not analysis["qualified"] or analysis["behavior"] != BEHAVIOR_BERTHING:
        return []
    segment = analysis["points"]
    center, _ = _position_metrics(segment)
    duration = analysis["duration_seconds"] / 60
    mean_speed = analysis["average_speed_knots"]
    radius = analysis["position_radius_metres"]
    heading_variation = analysis["heading_variation_degrees"]
    heading_evidence_available = (
        analysis["heading_observation_count"]
        >= config["min_heading_observations"]
    )
    latest = segment[-1]
    if latest["authorized_anchorage"]:
        return []
    if not latest["near_fixed_facility"]:
        return []

    legal = latest["legal_berthing"]
    allowed_duration = config["legal_max_duration_minutes"]
    overtime = legal and allowed_duration > 0 and duration > allowed_duration
    if legal and not overtime:
        return []
    reason = "超过合法停泊允许时长" if overtime else "不在合法停泊区域"
    return [
        {
            "start_time": segment[0]["timestamp"].isoformat(),
            "end_time": segment[-1]["timestamp"].isoformat(),
            "duration_minutes": round(duration, 2),
            "point_count": len(segment),
            "mean_speed_knots": round(mean_speed, 3),
            "position_radius_metres": round(radius, 2),
            "heading_variation_degrees": (
                round(heading_variation, 2) if heading_evidence_available else None
            ),
            "heading_observation_count": analysis[
                "heading_observation_count"
            ],
            "near_fixed_facility": True,
            "facility": latest["facility_name"] or "港池/固定靠泊设施",
            "shore_distance_metres": (
                round(latest["shore_distance_metres"], 2)
                if latest["shore_distance_metres"] is not None
                and isfinite(latest["shore_distance_metres"])
                else None
            ),
            "legal_berthing": legal,
            "legal_area": latest["legal_area"],
            "reason": reason,
            "behavior": BEHAVIOR_BERTHING,
            "location": {"lon": center[0], "lat": center[1]},
        }
    ]


def parking_event_from_behavior_result(shared_result, config=None):
    """Apply abnormal-parking legality rules to one shared behaviour result."""
    config = config or get_parking_config()
    if not isinstance(shared_result, dict):
        return None

    moored_underway = shared_result.get("moored_underway")
    if moored_underway is not None:
        if (
            moored_underway["point_count"] < config["moored_underway_min_points"]
            or moored_underway["duration_seconds"]
            < config["moored_underway_min_duration_minutes"] * 60
            or moored_underway["average_speed_knots"]
            < config["moored_underway_min_speed_knots"]
            or moored_underway["path_distance_metres"]
            < config["moored_underway_min_path_distance_metres"]
        ):
            return None
        segment = moored_underway["points"]
        center, radius = _position_metrics(segment)
        return {
            "start_time": segment[0]["timestamp"].isoformat(),
            "end_time": segment[-1]["timestamp"].isoformat(),
            "duration_minutes": round(
                moored_underway["duration_seconds"] / 60, 2
            ),
            "point_count": moored_underway["point_count"],
            "mean_speed_knots": round(
                moored_underway["average_speed_knots"], 3
            ),
            "position_radius_metres": round(radius, 2),
            "heading_variation_degrees": None,
            "heading_observation_count": 0,
            "near_fixed_facility": False,
            "facility": "AIS停泊状态航行轨迹",
            "shore_distance_metres": None,
            "legal_berthing": False,
            "legal_area": None,
            "reason": moored_underway["reason"],
            "behavior": "underway_with_moored_status",
            "path_distance_metres": round(
                moored_underway["path_distance_metres"], 2
            ),
            "location": {"lon": center[0], "lat": center[1]},
        }

    analysis = shared_result.get("analysis") or {}
    if (
        not analysis.get("qualified")
        or analysis.get("behavior") != BEHAVIOR_BERTHING
        or analysis.get("point_count", 0) < config["min_points"]
        or analysis.get("duration_seconds", 0)
        < config["min_duration_minutes"] * 60
        or analysis.get("average_speed_knots") is None
        or analysis["average_speed_knots"] > config["max_speed_knots"]
        or analysis.get("position_radius_metres") is None
        or analysis["position_radius_metres"]
        > config["distance_threshold_metres"]
    ):
        return None
    segment = analysis.get("points") or []
    if not segment:
        return None
    latest = segment[-1]
    if latest.get("authorized_anchorage") or not latest.get(
        "near_fixed_facility"
    ):
        return None
    duration = analysis["duration_seconds"] / 60
    legal = bool(latest.get("legal_berthing"))
    allowed_duration = config["legal_max_duration_minutes"]
    overtime = legal and allowed_duration > 0 and duration > allowed_duration
    if legal and not overtime:
        return None
    center, _radius = _position_metrics(segment)
    heading_variation = analysis.get("heading_variation_degrees")
    return {
        "start_time": segment[0]["timestamp"].isoformat(),
        "end_time": segment[-1]["timestamp"].isoformat(),
        "duration_minutes": round(duration, 2),
        "point_count": analysis["point_count"],
        "mean_speed_knots": round(analysis["average_speed_knots"], 3),
        "position_radius_metres": round(
            analysis["position_radius_metres"], 2
        ),
        "heading_variation_degrees": (
            round(heading_variation, 2)
            if heading_variation is not None
            else None
        ),
        "heading_observation_count": analysis.get(
            "heading_observation_count", 0
        ),
        "near_fixed_facility": True,
        "facility": latest.get("facility_name") or "港池/固定靠泊设施",
        "shore_distance_metres": latest.get("shore_distance_metres"),
        "legal_berthing": legal,
        "legal_area": latest.get("legal_area"),
        "reason": "超过合法停泊允许时长" if overtime else "不在合法停泊区域",
        "behavior": BEHAVIOR_BERTHING,
        "location": {"lon": center[0], "lat": center[1]},
    }


def run_parking_analysis(queryset, config=None):
    """Analyse one vessel's ordered ParkingBuffer queryset."""
    config = config or get_parking_config()
    if not queryset.exists():
        return {"is_abnormal": False, "count": 0, "events": []}
    rows = list(
        queryset.values(
            "latitude",
            "longitude",
            "timestamp",
            "speed",
            "heading",
            "nav_status",
            "at_dock",
            "matched_port_name",
        )
    )
    events = detect_parking_events(rows, config=config)
    return {
        "is_abnormal": bool(events),
        "count": len(events),
        "events": events,
    }
