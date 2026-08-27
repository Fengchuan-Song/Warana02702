from collections import Counter
from itertools import combinations
from math import asin, cos, isfinite, radians, sin, sqrt

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from IllegalAnchored.zones import classify_location, point_in_polygon


DEFAULT_CONFIG = {
    "analysis_window_minutes": 30,
    "retention_window_minutes": 60,
    "min_points": 10,
    "min_duration_minutes": 30,
    "max_range_metres": 3_000,
    "min_path_distance_metres": 300,
    "min_leg_distance_metres": 20,
    "min_turn_angle_degrees": 30,
    "min_turn_count": 4,
    "max_displacement_ratio": 0.3,
    "min_revisit_ratio": 0.4,
    "grid_size_metres": 200,
    "revisit_enabled": True,
    "min_speed_knots": 0.5,
    "max_valid_speed_knots": 102.2,
    "max_jump_speed_knots": 80,
    "min_jump_distance_metres": 500,
    "min_turn_interval_seconds": 30,
    "max_gap_minutes": 5,
    "monitored_only": True,
    "min_area_point_ratio": 0.5,
    "normal_anchorage_max_speed_knots": 0.5,
    "normal_anchorage_max_range_metres": 200,
    "normal_port_max_speed_knots": 3,
    "normal_port_max_range_metres": 1_000,
    "normal_area_point_ratio": 0.8,
    "legal_operation_areas": [],
    "monitored_areas": [
        {
            "name": "异常徘徊监控区",
            "bounds": (113.6109833, 22.1700302, 113.7926567, 22.2080471),
        }
    ],
}


def _bounded_float(config, key, minimum=0.0, maximum=None):
    value = max(minimum, float(config[key]))
    if maximum is not None:
        value = min(maximum, value)
    config[key] = value


def get_wandering_config():
    configured = getattr(settings, "ABNORMAL_WANDERING", {})
    config = {**DEFAULT_CONFIG, **(configured if isinstance(configured, dict) else {})}
    config["analysis_window_minutes"] = max(1, int(config["analysis_window_minutes"]))
    config["retention_window_minutes"] = max(
        config["analysis_window_minutes"], int(config["retention_window_minutes"])
    )
    config["min_points"] = max(4, int(config["min_points"]))
    config["min_turn_count"] = max(1, int(config["min_turn_count"]))
    for key in (
        "min_duration_minutes", "max_range_metres", "min_path_distance_metres",
        "min_leg_distance_metres", "grid_size_metres", "min_speed_knots",
        "max_valid_speed_knots", "max_jump_speed_knots", "min_jump_distance_metres",
        "min_turn_interval_seconds", "max_gap_minutes",
        "normal_anchorage_max_speed_knots", "normal_anchorage_max_range_metres",
        "normal_port_max_speed_knots", "normal_port_max_range_metres",
    ):
        _bounded_float(config, key)
    config["grid_size_metres"] = max(1.0, config["grid_size_metres"])
    config["max_gap_minutes"] = max(0.1, config["max_gap_minutes"])
    _bounded_float(config, "min_turn_angle_degrees", maximum=180.0)
    for key in (
        "max_displacement_ratio", "min_revisit_ratio", "min_area_point_ratio",
        "normal_area_point_ratio",
    ):
        _bounded_float(config, key, maximum=1.0)
    config["revisit_enabled"] = bool(config["revisit_enabled"])
    config["monitored_only"] = bool(config["monitored_only"])
    for key in ("monitored_areas", "legal_operation_areas"):
        if not isinstance(config[key], (list, tuple)):
            config[key] = list(DEFAULT_CONFIG[key])
        else:
            config[key] = list(config[key])
    return config


def haversine_metres(p1, p2):
    """Return great-circle distance for two ``(lon, lat)`` points."""
    lon1, lat1 = radians(float(p1[0])), radians(float(p1[1]))
    lon2, lat2 = radians(float(p2[0])), radians(float(p2[1]))
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6_371_000 * asin(sqrt(min(1.0, value)))


def heading_change(first, second):
    """Return the smallest circular difference between two COG values."""
    difference = abs(float(second) - float(first)) % 360
    return min(difference, 360 - difference)


def get_monitored_area(lat, lon, monitored_areas=None):
    areas = monitored_areas if monitored_areas is not None else get_wandering_config()["monitored_areas"]
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


def _parse_timestamp(value):
    timestamp = value if hasattr(value, "tzinfo") else parse_datetime(str(value or ""))
    if timestamp is None:
        return None
    if timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(timestamp, timezone.get_current_timezone())
    return timestamp


def _first_present(row, names):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


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


def _normalise_points(rows, config):
    """Validate, sort and de-duplicate AIS points; invalid SOG/COG become None."""
    by_timestamp = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        lat = _optional_number(_first_present(row, ("latitude", "Latitude")))
        lon = _optional_number(_first_present(row, ("longitude", "Longitude")))
        timestamp = _parse_timestamp(_first_present(row, ("timestamp", "Timestamp")))
        if timestamp is None or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        speed = _optional_number(
            _first_present(row, ("speed", "Speed", "sog", "SOG")), 0,
            config["max_valid_speed_knots"],
        )
        # `course` is the project's COG field. Heading is compatibility fallback only.
        course = _optional_number(
            _first_present(row, ("course", "Course", "cog", "COG", "Heading")),
            0, 359.999999,
        )
        by_timestamp[timestamp] = {
            "lat": lat, "lon": lon, "timestamp": timestamp,
            "speed": speed, "course": course,
        }
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def _split_on_time_gaps(points, max_gap_minutes):
    if not points:
        return []
    segments = [[points[0]]]
    for point in points[1:]:
        gap_minutes = (point["timestamp"] - segments[-1][-1]["timestamp"]).total_seconds() / 60
        if gap_minutes > max_gap_minutes:
            segments.append([point])
        else:
            segments[-1].append(point)
    return segments


def _leg_distance(start, end):
    return haversine_metres((start["lon"], start["lat"]), (end["lon"], end["lat"]))


def _implausible_leg(start, end, config):
    seconds = (end["timestamp"] - start["timestamp"]).total_seconds()
    if seconds <= 0:
        return True
    distance = _leg_distance(start, end)
    implied_knots = distance / seconds * 1.943844492
    return distance >= config["min_jump_distance_metres"] and implied_knots > config["max_jump_speed_knots"]


def _remove_position_jumps(points, config):
    """Remove isolated AIS spikes, then reject remaining impossible legs."""
    if len(points) < 3:
        return list(points)
    without_spikes = [points[0]]
    for index in range(1, len(points) - 1):
        previous, current, following = without_spikes[-1], points[index], points[index + 1]
        if (_implausible_leg(previous, current, config)
                and _implausible_leg(current, following, config)
                and not _implausible_leg(previous, following, config)):
            continue
        without_spikes.append(current)
    without_spikes.append(points[-1])
    cleaned = [without_spikes[0]]
    for point in without_spikes[1:]:
        if not _implausible_leg(cleaned[-1], point, config):
            cleaned.append(point)
    return cleaned


def maximum_range_metres(points):
    """Exact O(n^2) maximum pairwise Haversine distance for a short window."""
    if len(points) < 2:
        return 0.0
    return max(_leg_distance(first, second) for first, second in combinations(points, 2))


def displacement_metrics(points):
    """Return ``(D_net, D_path, R_disp)``; distances use metres."""
    if len(points) < 2:
        return 0.0, 0.0, None
    path_distance = sum(_leg_distance(start, end) for start, end in zip(points, points[1:]))
    net_distance = _leg_distance(points[0], points[-1])
    ratio = net_distance / path_distance if path_distance > 1e-6 else None
    return net_distance, path_distance, ratio


def _significant_movement_track(points, min_leg_distance_metres):
    """Collapse sub-threshold position noise into a movement track.

    Distance is measured from the last accepted point rather than between every
    pair of raw samples.  This preserves genuine movement made up of several
    short AIS legs while preventing stationary GPS jitter from being accumulated
    as travelled distance.
    """
    if not points:
        return []
    track = [points[0]]
    for point in points[1:]:
        if _leg_distance(track[-1], point) >= min_leg_distance_metres:
            track.append(point)
    return track


def count_significant_turns(points, config):
    """Count direction changes on the de-noised movement track.

    A direction change is only counted when AIS reports the vessel moving and
    enough time has elapsed.  COG is used for the direction itself, but the
    samples must also be separated by a real position leg, so an anchored ship's
    course/position noise cannot manufacture repeated turns.
    """
    samples = []
    for point in points:
        if point["course"] is None:
            continue
        if point["speed"] is not None and point["speed"] < config["min_speed_knots"]:
            continue
        if samples and (point["timestamp"] - samples[-1]["timestamp"]).total_seconds() < config["min_turn_interval_seconds"]:
            continue
        samples.append(point)
    angles = [heading_change(first["course"], second["course"]) for first, second in zip(samples, samples[1:])]
    return sum(angle >= config["min_turn_angle_degrees"] for angle in angles)


def revisit_metrics(points, grid_size_metres):
    """Return revisit ratio/visits/unique cells after same-cell run collapse."""
    if not points:
        return 0.0, 0, 0
    reference_lat = sum(point["lat"] for point in points) / len(points)
    reference_lon = sum(point["lon"] for point in points) / len(points)
    metres_per_degree = 6_371_000 * radians(1)
    lon_scale = metres_per_degree * max(1e-6, cos(radians(reference_lat)))
    cells = []
    for point in points:
        x = (point["lon"] - reference_lon) * lon_scale
        y = (point["lat"] - reference_lat) * metres_per_degree
        cell = (int(x // grid_size_metres), int(y // grid_size_metres))
        if not cells or cells[-1] != cell:
            cells.append(cell)
    visit_count, unique_count = len(cells), len(set(cells))
    ratio = 1 - unique_count / visit_count if visit_count else 0.0
    return ratio, visit_count, unique_count


def _average_speed(points):
    speeds = [point["speed"] for point in points if point["speed"] is not None]
    return sum(speeds) / len(speeds) if speeds else None


def _normal_behavior_reason(segment, metrics, config, normal_behavior):
    average_speed = metrics["average_speed_knots"]
    max_range = metrics["range_metres"]
    path_distance = metrics["path_distance_metres"]
    if (average_speed is not None and average_speed < config["min_speed_knots"]
            and path_distance < config["min_path_distance_metres"]):
        return "低速且累计航程很小，属于驻留/锚泊类行为"
    normal_behavior = normal_behavior or {}
    if normal_behavior.get("at_dock"):
        return "AIS/港口数据表明船舶正在靠泊"
    if (normal_behavior.get("matched_port_name") and average_speed is not None
            and average_speed <= config["normal_port_max_speed_knots"]
            and max_range <= config["normal_port_max_range_metres"]):
        return "港池内低速小范围正常作业"
    authorized_count = sum(
        classify_location(point["lon"], point["lat"])["state"] == "authorized"
        for point in segment
    )
    if (authorized_count / len(segment) >= config["normal_area_point_ratio"]
            and average_speed is not None
            and average_speed <= config["normal_anchorage_max_speed_knots"]
            and max_range <= config["normal_anchorage_max_range_metres"]):
        return "合法锚地内低速小范围漂移"
    legal_count = sum(
        get_monitored_area(point["lat"], point["lon"], config["legal_operation_areas"]) is not None
        for point in segment
    )
    if config["legal_operation_areas"] and legal_count / len(segment) >= config["normal_area_point_ratio"]:
        return "位于已配置的合法作业区"
    return None


def _analyse_segment(segment, config, normal_behavior=None):
    segment = _remove_position_jumps(segment, config)
    if len(segment) < config["min_points"]:
        return None
    duration_minutes = (segment[-1]["timestamp"] - segment[0]["timestamp"]).total_seconds() / 60
    if duration_minutes < config["min_duration_minutes"]:
        return None
    range_metres = maximum_range_metres(segment)
    if range_metres > config["max_range_metres"]:
        return None
    movement_track = _significant_movement_track(
        segment,
        config["min_leg_distance_metres"],
    )
    net_distance, path_distance, displacement_ratio = displacement_metrics(
        movement_track,
    )
    average_speed = _average_speed(segment)
    # Abnormal wandering is a navigation state.  A segment with valid SOG that
    # remains below the movement threshold is stationary/drifting, not wandering.
    if average_speed is not None and average_speed < config["min_speed_knots"]:
        return None
    if path_distance < config["min_path_distance_metres"]:
        return None
    turn_count = count_significant_turns(movement_track, config)
    revisit_ratio, visit_count, unique_count = revisit_metrics(
        movement_track,
        config["grid_size_metres"],
    )
    conditions = {
        "low_displacement_efficiency": displacement_ratio is not None and displacement_ratio <= config["max_displacement_ratio"],
        "frequent_turning": turn_count >= config["min_turn_count"],
        "repeated_area_visits": config["revisit_enabled"] and revisit_ratio >= config["min_revisit_ratio"],
    }
    # Repeated direction changes are mandatory evidence of back-and-forth
    # navigation.  Low displacement efficiency and returning to an area are
    # supporting features; neither can replace actual reciprocal movement.
    supporting_condition = conditions["low_displacement_efficiency"]
    if config["revisit_enabled"]:
        supporting_condition = (
            supporting_condition or conditions["repeated_area_visits"]
        )
    if not (conditions["frequent_turning"] and supporting_condition):
        return None
    area_names = []
    for point in segment:
        area = get_monitored_area(point["lat"], point["lon"], config["monitored_areas"])
        if area is not None:
            area_names.append(area.get("name") or "异常徘徊监控区")
    if config["monitored_only"]:
        if len(area_names) / len(segment) < config["min_area_point_ratio"]:
            return None
    metrics = {
        "average_speed_knots": average_speed,
        "range_metres": range_metres,
        "path_distance_metres": path_distance,
    }
    if _normal_behavior_reason(segment, metrics, config, normal_behavior) is not None:
        return None
    area_name = (
        Counter(area_names).most_common(1)[0][0]
        if area_names
        else "全水域"
    )
    return {
        "start_time": segment[0]["timestamp"].isoformat(),
        "end_time": segment[-1]["timestamp"].isoformat(),
        "duration_minutes": round(duration_minutes, 2),
        "point_count": len(segment),
        "turn_count": turn_count,
        "wave_point_count": turn_count,
        "range_metres": round(range_metres, 2),
        "path_distance_metres": round(path_distance, 2),
        "displacement_metres": round(net_distance, 2),
        "displacement_ratio": round(displacement_ratio, 4),
        "revisit_ratio": round(revisit_ratio, 4),
        "grid_visit_count": visit_count,
        "unique_grid_count": unique_count,
        "trajectory_abnormal_count": sum(conditions.values()),
        "trajectory_conditions": conditions,
        "average_speed_knots": round(average_speed, 2) if average_speed is not None else None,
        "location": {
            "lat": sum(point["lat"] for point in segment) / len(segment),
            "lon": sum(point["lon"] for point in segment) / len(segment),
        },
        "area": area_name,
    }


def detect_loitering_events(points, config=None, normal_behavior=None):
    config = config or get_wandering_config()
    normalised = _normalise_points(points, config)
    events = []
    for segment in _split_on_time_gaps(normalised, config["max_gap_minutes"]):
        event = _analyse_segment(segment, config, normal_behavior)
        if event is not None:
            events.append(event)
    return events


def run_loitering_analysis(trajectory_queryset, config=None, normal_behavior=None):
    """Analyse a QuerySet or list without changing its public result shape."""
    config = config or get_wandering_config()
    if hasattr(trajectory_queryset, "values"):
        rows = list(trajectory_queryset.values("latitude", "longitude", "timestamp", "speed", "course"))
    else:
        rows = list(trajectory_queryset or [])
    abnormal_segments = detect_loitering_events(rows, config, normal_behavior=normal_behavior)
    return {
        "is_abnormal": bool(abnormal_segments),
        "abnormal_count": len(abnormal_segments),
        "abnormal_segments": abnormal_segments,
    }
