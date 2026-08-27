"""Shared low-speed episode analysis for berthing, anchoring and staying.

This module deliberately classifies physical behaviour only.  Whether the
behaviour is legal remains the responsibility of the three existing detectors,
which keep their public feature IDs and payload contracts.
"""

from math import asin, cos, isfinite, radians, sin, sqrt

from django.utils import timezone
from django.utils.dateparse import parse_datetime


BEHAVIOR_BERTHING = "berthing"
BEHAVIOR_ANCHORING = "anchoring"
BEHAVIOR_STAYING = "staying"
BEHAVIOR_UNDERWAY = "underway"
BEHAVIOR_INSUFFICIENT = "insufficient"

AIS_NAV_STATUS_ANCHORED = 1
AIS_NAV_STATUS_MOORED = 5


def distance_metres(first, second):
    lon1, lat1 = radians(first[0]), radians(first[1])
    lon2, lat2 = radians(second[0]), radians(second[1])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    value = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    )
    return 2 * 6_371_000 * asin(sqrt(min(1.0, value)))


def circular_heading_variation(headings):
    """Return the smallest circular arc containing all valid headings."""
    values = sorted(
        float(value) % 360
        for value in headings
        if value is not None and isfinite(float(value))
    )
    if len(values) < 2:
        return 0.0
    gaps = [
        second - first
        for first, second in zip(values, values[1:])
    ]
    gaps.append(values[0] + 360 - values[-1])
    return 360 - max(gaps)


def _point_value(point, *names, default=None):
    for name in names:
        if name in point and point[name] is not None:
            return point[name]
    return default


def _normalise_point(point):
    if not isinstance(point, dict):
        return None
    try:
        lon = float(_point_value(point, "lon", "longitude"))
        lat = float(_point_value(point, "lat", "latitude"))
        speed = float(_point_value(point, "speed", default=0.0))
    except (TypeError, ValueError):
        return None
    timestamp = point.get("timestamp")
    if isinstance(timestamp, str):
        timestamp = parse_datetime(timestamp)
    if timestamp is not None and timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(
            timestamp,
            timezone.get_current_timezone(),
        )
    if (
        timestamp is None
        or not all(isfinite(value) for value in (lon, lat, speed))
        or not -180 <= lon <= 180
        or not -90 <= lat <= 90
        or speed < 0
    ):
        return None
    try:
        heading = float(point.get("heading"))
    except (TypeError, ValueError):
        heading = None
    if heading is not None and (
        not isfinite(heading) or not 0 <= heading < 360
    ):
        heading = None
    try:
        nav_status = int(float(point.get("nav_status")))
    except (TypeError, ValueError):
        nav_status = None
    return {
        **point,
        "lon": lon,
        "lat": lat,
        "speed": speed,
        "heading": heading,
        "nav_status": nav_status,
        "timestamp": timestamp,
        "at_dock": bool(point.get("at_dock", False)),
        "in_port_basin": bool(point.get("in_port_basin", False)),
        "berthing_facility": bool(point.get("berthing_facility", False)),
        "context_key": point.get("context_key"),
    }


def _empty_analysis(behavior=BEHAVIOR_INSUFFICIENT):
    return {
        "behavior": behavior,
        "qualified": False,
        "points": [],
        "point_count": 0,
        "duration_seconds": 0,
        "average_speed_knots": None,
        "max_speed_knots": None,
        "position_radius_metres": None,
        "path_distance_metres": 0.0,
        "displacement_metres": 0.0,
        "heading_variation_degrees": None,
        "heading_observation_count": 0,
        "anchor_status_ratio": 0.0,
        "moored_status_ratio": 0.0,
        "anchor_swing": False,
        "position_swing": False,
        "berth_contact": False,
        "reason": "有效低速轨迹证据不足",
    }


def _active_tail(points, config):
    if not points:
        return []
    latest = points[-1]
    if latest["speed"] > config["exit_speed_knots"]:
        return []
    segment = []
    later = latest
    for point in reversed(points):
        gap_seconds = (later["timestamp"] - point["timestamp"]).total_seconds()
        context_changed = (
            latest["context_key"] is not None
            and point["context_key"] != latest["context_key"]
        )
        if (
            point["speed"] > config["exit_speed_knots"]
            or gap_seconds < 0
            or gap_seconds > config["max_gap_seconds"]
            or context_changed
            or distance_metres(
                (point["lon"], point["lat"]),
                (latest["lon"], latest["lat"]),
            )
            > config["max_episode_distance_metres"]
        ):
            break
        segment.append(point)
        later = point
    segment.reverse()
    return segment


def analyse_low_speed_behavior(points, **overrides):
    """Classify the active trajectory tail without applying legal rules."""
    config = {
        "stationary_speed_knots": 0.5,
        "exit_speed_knots": 0.5,
        "max_gap_seconds": 180,
        "max_episode_distance_metres": 250.0,
        "stationary_radius_metres": 250.0,
        "min_points": 3,
        "min_duration_seconds": 0,
        "min_heading_observations": 2,
        "max_berthing_heading_degrees": 25.0,
        "anchor_swing_heading_degrees": 45.0,
        "min_anchor_status_ratio": 0.6,
        "min_moored_status_ratio": 0.6,
        "berth_contact_overrides_anchor_status": False,
    }
    config.update(overrides)
    by_timestamp = {}
    for raw in points or []:
        point = _normalise_point(raw)
        if point is not None:
            by_timestamp[point["timestamp"]] = point
    normalised = [by_timestamp[key] for key in sorted(by_timestamp)]
    segment = _active_tail(normalised, config)
    if not segment:
        behavior = (
            BEHAVIOR_UNDERWAY
            if normalised
            and normalised[-1]["speed"] > config["exit_speed_knots"]
            else BEHAVIOR_INSUFFICIENT
        )
        return _empty_analysis(behavior)

    duration_seconds = max(
        0.0,
        (segment[-1]["timestamp"] - segment[0]["timestamp"]).total_seconds(),
    )
    speeds = [point["speed"] for point in segment]
    center = (
        sum(point["lon"] for point in segment) / len(segment),
        sum(point["lat"] for point in segment) / len(segment),
    )
    radius = max(
        distance_metres(center, (point["lon"], point["lat"]))
        for point in segment
    )
    path_distance = sum(
        distance_metres(
            (first["lon"], first["lat"]),
            (second["lon"], second["lat"]),
        )
        for first, second in zip(segment, segment[1:])
    )
    displacement = distance_metres(
        (segment[0]["lon"], segment[0]["lat"]),
        (segment[-1]["lon"], segment[-1]["lat"]),
    )
    headings = [
        point["heading"]
        for point in segment
        if point["heading"] is not None
    ]
    heading_available = len(headings) >= config["min_heading_observations"]
    heading_variation = (
        circular_heading_variation(headings) if heading_available else None
    )
    nav_statuses = [
        point["nav_status"]
        for point in segment
        if point["nav_status"] is not None
    ]
    # Missing navigation status is missing evidence, not a positive vote.
    denominator = len(segment)
    anchor_status_ratio = (
        sum(status == AIS_NAV_STATUS_ANCHORED for status in nav_statuses)
        / denominator
    )
    moored_status_ratio = (
        sum(status == AIS_NAV_STATUS_MOORED for status in nav_statuses)
        / denominator
    )
    result = {
        **_empty_analysis(),
        "points": segment,
        "point_count": len(segment),
        "duration_seconds": duration_seconds,
        "average_speed_knots": sum(speeds) / len(speeds),
        "max_speed_knots": max(speeds),
        "position_radius_metres": radius,
        "path_distance_metres": path_distance,
        "displacement_metres": displacement,
        "heading_variation_degrees": heading_variation,
        "heading_observation_count": len(headings),
        "anchor_status_ratio": anchor_status_ratio,
        "moored_status_ratio": moored_status_ratio,
    }
    if (
        len(segment) < config["min_points"]
        or duration_seconds < config["min_duration_seconds"]
    ):
        return result
    if (
        result["average_speed_knots"] > config["stationary_speed_knots"]
        or radius > config["stationary_radius_metres"]
    ):
        result.update(
            behavior=BEHAVIOR_UNDERWAY,
            qualified=True,
            reason="有效航速或活动范围表明船舶仍在航行",
        )
        return result

    heading_swing = (
        heading_available
        and heading_variation >= config["anchor_swing_heading_degrees"]
    )
    displacement_ratio = (
        displacement / path_distance if path_distance > 1e-6 else None
    )
    position_swing = (
        radius >= 10.0
        and path_distance >= max(50.0, radius * 4)
        and displacement_ratio is not None
        and displacement_ratio <= 0.35
    )
    anchor_swing = heading_swing or position_swing
    anchor_status = anchor_status_ratio >= config["min_anchor_status_ratio"]
    latest = segment[-1]
    strong_berth_contact = (
        latest["berthing_facility"]
        or (latest["in_port_basin"] and latest["at_dock"])
    )
    stable_heading = (
        not heading_available
        or heading_variation <= config["max_berthing_heading_degrees"]
    )
    port_berthing = (
        latest["in_port_basin"]
        and stable_heading
        and not anchor_status
        and not anchor_swing
        and (
            latest["at_dock"]
            or moored_status_ratio >= config["min_moored_status_ratio"]
            or not nav_statuses
        )
    )
    berth_contact = (
        strong_berth_contact
        and stable_heading
        and (
            config["berth_contact_overrides_anchor_status"]
            or not anchor_status
        )
        and not anchor_swing
    ) or port_berthing
    if berth_contact:
        behavior = BEHAVIOR_BERTHING
        reason = "位于港池/靠泊设施内且轨迹符合稳定靠泊"
    elif anchor_status or anchor_swing:
        behavior = BEHAVIOR_ANCHORING
        reason = (
            "连续锚泊状态与位置受限共同支持锚泊行为"
            if anchor_status
            else "低速位置受限且艏向呈锚泊摆动"
        )
    else:
        behavior = BEHAVIOR_STAYING
        reason = "低速位置受限，但缺少充分靠泊或锚泊证据"
    result.update(
        behavior=behavior,
        qualified=True,
        anchor_swing=anchor_swing,
        position_swing=position_swing,
        berth_contact=berth_contact,
        reason=reason,
    )
    return result


def detect_moored_status_underway(points, **overrides):
    """Detect a continuous AIS-moored status contradicted by real movement."""
    config = {
        "min_speed_knots": 1.0,
        "min_path_distance_metres": 100.0,
        "min_points": 3,
        "min_duration_seconds": 300,
        "max_gap_seconds": 1200,
    }
    config.update(overrides)
    by_timestamp = {}
    for raw in points or []:
        point = _normalise_point(raw)
        if point is not None:
            by_timestamp[point["timestamp"]] = point
    normalised = [by_timestamp[key] for key in sorted(by_timestamp)]
    if not normalised or normalised[-1]["nav_status"] != AIS_NAV_STATUS_MOORED:
        return None
    segment = []
    later = normalised[-1]
    for point in reversed(normalised):
        gap = (later["timestamp"] - point["timestamp"]).total_seconds()
        if (
            point["nav_status"] != AIS_NAV_STATUS_MOORED
            or gap < 0
            or gap > config["max_gap_seconds"]
        ):
            break
        segment.append(point)
        later = point
    segment.reverse()
    if len(segment) < config["min_points"]:
        return None
    duration_seconds = (
        segment[-1]["timestamp"] - segment[0]["timestamp"]
    ).total_seconds()
    if duration_seconds < config["min_duration_seconds"]:
        return None
    average_speed = sum(point["speed"] for point in segment) / len(segment)
    path_distance = sum(
        distance_metres(
            (first["lon"], first["lat"]),
            (second["lon"], second["lat"]),
        )
        for first, second in zip(segment, segment[1:])
    )
    if (
        average_speed < config["min_speed_knots"]
        or path_distance < config["min_path_distance_metres"]
    ):
        return None
    return {
        "behavior": BEHAVIOR_UNDERWAY,
        "points": segment,
        "point_count": len(segment),
        "duration_seconds": duration_seconds,
        "average_speed_knots": average_speed,
        "max_speed_knots": max(point["speed"] for point in segment),
        "path_distance_metres": path_distance,
        "displacement_metres": distance_metres(
            (segment[0]["lon"], segment[0]["lat"]),
            (segment[-1]["lon"], segment[-1]["lat"]),
        ),
        "reason": "AIS报文持续显示停泊，但有效航速和轨迹表明船舶仍在航行",
    }
