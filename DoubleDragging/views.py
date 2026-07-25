import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime

from AISData.normalization import normalise_ais_name

from .models import DoubleDraggingPoint


EARTH_RADIUS_METRES = 6_371_000.0
DOUBLE_DRAGGING_STATE_CACHE_KEY = "double_dragging:event_state:v1"

DEFAULT_DOUBLE_DRAGGING_CONFIG = {
    "analysis_window_minutes": 45.0,
    "retention_window_minutes": 60.0,
    "min_duration_minutes": 30.0,
    "min_aligned_points": 30.0,
    "alignment_tolerance_seconds": 45.0,
    "min_pair_distance_metres": 100.0,
    "max_pair_distance_metres": 2000.0,
    "candidate_distance_margin_metres": 500.0,
    "min_operating_speed_knots": 1.0,
    "max_operating_speed_knots": 8.0,
    "max_speed_difference_knots": 1.5,
    "max_course_difference_degrees": 20.0,
    "max_lateral_deviation_degrees": 30.0,
    "max_distance_std_metres": 250.0,
    "min_distance_ratio": 0.8,
    "min_course_ratio": 0.8,
    "min_lateral_ratio": 0.7,
    "min_speed_similarity_ratio": 0.8,
    "max_position_age_seconds": 120.0,
    "future_tolerance_seconds": 120.0,
    "event_retention_minutes": 30.0,
}


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_timestamp(value):
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = parse_datetime(value)
    else:
        timestamp = None

    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt_timezone.utc)
    return timestamp


def _config():
    config = DEFAULT_DOUBLE_DRAGGING_CONFIG.copy()
    configured = getattr(settings, "DOUBLE_DRAGGING_DETECTION", {})
    if isinstance(configured, dict):
        for key in config:
            value = _finite_float(configured.get(key))
            if value is not None and value >= 0:
                config[key] = value

    config["retention_window_minutes"] = max(
        config["retention_window_minutes"],
        config["analysis_window_minutes"],
        config["min_duration_minutes"],
    )
    config["analysis_window_minutes"] = max(
        config["analysis_window_minutes"],
        config["min_duration_minutes"],
    )
    config["max_pair_distance_metres"] = max(
        config["max_pair_distance_metres"],
        config["min_pair_distance_metres"],
    )
    config["min_aligned_points"] = max(
        2, int(config["min_aligned_points"])
    )
    return config


def _normalise_ship(ship_info):
    if not isinstance(ship_info, dict):
        return None

    mmsi = str(ship_info.get("mmsi") or "").strip()
    longitude = _finite_float(
        ship_info.get("lon", ship_info.get("longitude"))
    )
    latitude = _finite_float(
        ship_info.get("lat", ship_info.get("latitude"))
    )
    speed = _finite_float(ship_info.get("speed"))
    course = _finite_float(ship_info.get("course"))
    timestamp = _parse_timestamp(ship_info.get("timestamp"))

    if (
        not mmsi
        or longitude is None
        or latitude is None
        or speed is None
        or course is None
        or timestamp is None
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
        or speed < 0
    ):
        return None

    return {
        "mmsi": mmsi,
        "name": normalise_ais_name(ship_info.get("name")),
        "longitude": longitude,
        "latitude": latitude,
        "speed": speed,
        "course": course % 360,
        "timestamp": timestamp,
    }


def _angular_difference(first, second):
    return abs((first - second + 180) % 360 - 180)


def _mean_course(first, second):
    first_radians = math.radians(first)
    second_radians = math.radians(second)
    east = math.sin(first_radians) + math.sin(second_radians)
    north = math.cos(first_radians) + math.cos(second_radians)
    if abs(east) < 1e-12 and abs(north) < 1e-12:
        return first
    return math.degrees(math.atan2(east, north)) % 360


def _distance_metres(first, second):
    latitude1 = math.radians(first["latitude"])
    latitude2 = math.radians(second["latitude"])
    latitude_delta = latitude2 - latitude1
    longitude_delta = math.radians(
        second["longitude"] - first["longitude"]
    )
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude1)
        * math.cos(latitude2)
        * math.sin(longitude_delta / 2) ** 2
    )
    value = min(1.0, max(0.0, value))
    return 2 * EARTH_RADIUS_METRES * math.atan2(
        math.sqrt(value), math.sqrt(1 - value)
    )


def _bearing_degrees(first, second):
    latitude1 = math.radians(first["latitude"])
    latitude2 = math.radians(second["latitude"])
    longitude_delta = math.radians(
        second["longitude"] - first["longitude"]
    )
    east = math.sin(longitude_delta) * math.cos(latitude2)
    north = (
        math.cos(latitude1) * math.sin(latitude2)
        - math.sin(latitude1)
        * math.cos(latitude2)
        * math.cos(longitude_delta)
    )
    return math.degrees(math.atan2(east, north)) % 360


def _candidate_pairs(ships, config):
    max_distance = (
        config["max_pair_distance_metres"]
        + config["candidate_distance_margin_metres"]
    )
    max_latitude_delta = math.degrees(
        max_distance / EARTH_RADIUS_METRES
    )
    ordered = sorted(ships, key=lambda ship: ship["latitude"])
    pairs = []
    for index, first in enumerate(ordered):
        if not (
            config["min_operating_speed_knots"]
            <= first["speed"]
            <= config["max_operating_speed_knots"]
        ):
            continue
        for second in ordered[index + 1 :]:
            if second["latitude"] - first["latitude"] > max_latitude_delta:
                break
            if not (
                config["min_operating_speed_knots"]
                <= second["speed"]
                <= config["max_operating_speed_knots"]
            ):
                continue
            if (
                abs(first["speed"] - second["speed"])
                > config["max_speed_difference_knots"] * 1.5
            ):
                continue
            if (
                _angular_difference(first["course"], second["course"])
                > config["max_course_difference_degrees"] * 1.5
            ):
                continue
            if _distance_metres(first, second) > max_distance:
                continue
            pairs.append(
                tuple(
                    sorted(
                        (first, second),
                        key=lambda ship: ship["mmsi"],
                    )
                )
            )
    return pairs


def _one_to_one_alignment(first_points, second_points, tolerance_seconds):
    aligned = []
    first_index = 0
    second_index = 0
    while (
        first_index < len(first_points)
        and second_index < len(second_points)
    ):
        first = first_points[first_index]
        second = second_points[second_index]
        difference = (
            first["timestamp"] - second["timestamp"]
        ).total_seconds()
        if abs(difference) <= tolerance_seconds:
            aligned.append((first, second))
            first_index += 1
            second_index += 1
        elif difference < 0:
            first_index += 1
        else:
            second_index += 1
    return aligned


def _pearson_correlation(first_values, second_values):
    if len(first_values) < 2:
        return 0.0
    first_mean = sum(first_values) / len(first_values)
    second_mean = sum(second_values) / len(second_values)
    first_offsets = [value - first_mean for value in first_values]
    second_offsets = [value - second_mean for value in second_values]
    first_variance = sum(value**2 for value in first_offsets)
    second_variance = sum(value**2 for value in second_offsets)
    if first_variance < 1e-12 or second_variance < 1e-12:
        return 1.0 if abs(first_mean - second_mean) < 1e-9 else 0.0
    covariance = sum(
        first * second
        for first, second in zip(first_offsets, second_offsets)
    )
    return covariance / math.sqrt(first_variance * second_variance)


def extract_pair_features(first_points, second_points, config):
    aligned = _one_to_one_alignment(
        first_points,
        second_points,
        config["alignment_tolerance_seconds"],
    )
    if len(aligned) < 2:
        return {
            "aligned_points": len(aligned),
            "duration_minutes": 0.0,
            "qualifies": False,
        }

    timestamps = [
        max(first["timestamp"], second["timestamp"])
        for first, second in aligned
    ]
    duration_minutes = (
        max(timestamps) - min(timestamps)
    ).total_seconds() / 60

    distances = []
    course_matches = 0
    lateral_matches = 0
    speed_matches = 0
    first_speeds = []
    second_speeds = []
    for first, second in aligned:
        distance = _distance_metres(first, second)
        distances.append(distance)
        first_speeds.append(first["speed"])
        second_speeds.append(second["speed"])

        course_difference = _angular_difference(
            first["course"], second["course"]
        )
        if course_difference <= config["max_course_difference_degrees"]:
            course_matches += 1

        common_course = _mean_course(first["course"], second["course"])
        relative_bearing = _bearing_degrees(first, second)
        side_angle = _angular_difference(relative_bearing, common_course)
        if (
            abs(side_angle - 90)
            <= config["max_lateral_deviation_degrees"]
        ):
            lateral_matches += 1

        if (
            abs(first["speed"] - second["speed"])
            <= config["max_speed_difference_knots"]
        ):
            speed_matches += 1

    point_count = len(aligned)
    mean_distance = sum(distances) / point_count
    distance_variance = sum(
        (distance - mean_distance) ** 2 for distance in distances
    ) / max(1, point_count - 1)
    distance_std = math.sqrt(distance_variance)
    distance_ratio = sum(
        config["min_pair_distance_metres"]
        <= distance
        <= config["max_pair_distance_metres"]
        for distance in distances
    ) / point_count
    course_ratio = course_matches / point_count
    lateral_ratio = lateral_matches / point_count
    speed_ratio = speed_matches / point_count
    first_mean_speed = sum(first_speeds) / point_count
    second_mean_speed = sum(second_speeds) / point_count
    speed_correlation = _pearson_correlation(
        first_speeds, second_speeds
    )

    qualifies = all(
        (
            point_count >= config["min_aligned_points"],
            duration_minutes >= config["min_duration_minutes"],
            config["min_operating_speed_knots"]
            <= first_mean_speed
            <= config["max_operating_speed_knots"],
            config["min_operating_speed_knots"]
            <= second_mean_speed
            <= config["max_operating_speed_knots"],
            distance_ratio >= config["min_distance_ratio"],
            course_ratio >= config["min_course_ratio"],
            lateral_ratio >= config["min_lateral_ratio"],
            speed_ratio >= config["min_speed_similarity_ratio"],
            distance_std <= config["max_distance_std_metres"],
        )
    )
    return {
        "aligned_points": point_count,
        "duration_minutes": duration_minutes,
        "mean_distance_metres": mean_distance,
        "distance_std_metres": distance_std,
        "distance_ratio": distance_ratio,
        "course_ratio": course_ratio,
        "lateral_ratio": lateral_ratio,
        "speed_similarity_ratio": speed_ratio,
        "speed_correlation": speed_correlation,
        "mean_speed_knots": (
            first_mean_speed + second_mean_speed
        ) / 2,
        "qualifies": qualifies,
    }


def _load_trajectories(mmsis, start_time, end_time):
    trajectories = defaultdict(dict)
    rows = (
        DoubleDraggingPoint.objects.filter(
            mmsi__in=mmsis,
            timestamp__gte=start_time,
            timestamp__lte=end_time,
        )
        .order_by("mmsi", "timestamp")
        .values("mmsi", "lat", "lng", "sog", "cog", "timestamp")
    )
    for row in rows:
        # This also protects deployments where the uniqueness migration has
        # not yet been applied: one timestamp contributes at most one point.
        trajectories[row["mmsi"]][row["timestamp"]] = {
            "latitude": row["lat"],
            "longitude": row["lng"],
            "speed": row["sog"],
            "course": row["cog"],
            "timestamp": row["timestamp"],
        }
    return {
        mmsi: list(points.values())
        for mmsi, points in trajectories.items()
    }


def _annotate_event_state(results, timestamp, retention_minutes):
    state = cache.get(DOUBLE_DRAGGING_STATE_CACHE_KEY, {})
    if not isinstance(state, dict):
        state = {}

    retention_seconds = retention_minutes * 60
    retained = {}
    for pair_key, value in state.items():
        if not isinstance(value, dict):
            continue
        last_seen = _parse_timestamp(value.get("last_seen"))
        if last_seen is None:
            continue
        age_seconds = (timestamp - last_seen).total_seconds()
        if 0 <= age_seconds <= retention_seconds:
            retained[pair_key] = value

    timestamp_text = timestamp.isoformat()
    for result in results:
        pair_key = result["pair"]
        previous = retained.get(pair_key)
        first_detected_at = (
            previous.get("first_detected_at")
            if previous and previous.get("first_detected_at")
            else timestamp_text
        )
        result["event_id"] = f"double-dragging:{pair_key}"
        result["is_new"] = previous is None
        result["first_detected_at"] = first_detected_at
        retained[pair_key] = {
            "first_detected_at": first_detected_at,
            "last_seen": timestamp_text,
        }

    cache.set(
        DOUBLE_DRAGGING_STATE_CACHE_KEY,
        retained,
        timeout=max(3600, int(retention_seconds * 2)),
    )


def _response(
    timestamp=None,
    results=None,
    skipped_count=0,
    stale_count=0,
    candidate_pair_count=0,
    compared_pair_count=0,
    config=None,
):
    results = results or []
    payload = {
        "success": True,
        "type": "双拖捕鱼预警",
        "timestamp": timestamp.isoformat() if timestamp else None,
        "count": len(results),
        "results": results,
        "skipped_count": skipped_count,
        "stale_count": stale_count,
        "candidate_pair_count": candidate_pair_count,
        "compared_pair_count": compared_pair_count,
        "message": "检测成功" if timestamp else "暂无有效AIS数据",
    }
    if config is not None:
        payload["rule"] = {
            "analysis_window_minutes": config[
                "analysis_window_minutes"
            ],
            "min_duration_minutes": config["min_duration_minutes"],
            "min_aligned_points": config["min_aligned_points"],
            "pair_distance_metres": [
                config["min_pair_distance_metres"],
                config["max_pair_distance_metres"],
            ],
            "max_course_difference_degrees": config[
                "max_course_difference_degrees"
            ],
            "max_speed_difference_knots": config[
                "max_speed_difference_knots"
            ],
        }
    return JsonResponse(payload)


def detect_double_dragging(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _response()

    ships = []
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info)
        if ship is None:
            skipped_count += 1
        else:
            ships.append(ship)
    if not ships:
        return _response(skipped_count=skipped_count)

    latest_by_mmsi = {}
    for ship in ships:
        previous = latest_by_mmsi.get(ship["mmsi"])
        if previous is None or ship["timestamp"] > previous["timestamp"]:
            latest_by_mmsi[ship["mmsi"]] = ship

    reference_time = max(
        ship["timestamp"] for ship in latest_by_mmsi.values()
    )
    config = _config()
    fresh_ships = []
    stale_count = 0
    for ship in latest_by_mmsi.values():
        age_seconds = (reference_time - ship["timestamp"]).total_seconds()
        if age_seconds > config["max_position_age_seconds"]:
            stale_count += 1
        else:
            fresh_ships.append(ship)

    if not fresh_ships:
        return _response(
            timestamp=reference_time,
            skipped_count=skipped_count,
            stale_count=stale_count,
            config=config,
        )

    retention_start = reference_time - timedelta(
        minutes=config["retention_window_minutes"]
    )
    future_limit = reference_time + timedelta(
        seconds=config["future_tolerance_seconds"]
    )
    DoubleDraggingPoint.objects.filter(
        timestamp__lt=retention_start
    ).delete()
    DoubleDraggingPoint.objects.filter(
        timestamp__gt=future_limit
    ).delete()

    DoubleDraggingPoint.objects.bulk_create(
        [
            DoubleDraggingPoint(
                mmsi=ship["mmsi"],
                lat=ship["latitude"],
                lng=ship["longitude"],
                sog=ship["speed"],
                cog=ship["course"],
                timestamp=ship["timestamp"],
            )
            for ship in fresh_ships
        ],
        ignore_conflicts=True,
    )

    candidate_pairs = _candidate_pairs(fresh_ships, config)
    if not candidate_pairs:
        return _response(
            timestamp=reference_time,
            skipped_count=skipped_count,
            stale_count=stale_count,
            config=config,
        )

    candidate_mmsis = {
        ship["mmsi"]
        for pair in candidate_pairs
        for ship in pair
    }
    analysis_start = reference_time - timedelta(
        minutes=config["analysis_window_minutes"]
    )
    trajectories = _load_trajectories(
        candidate_mmsis,
        analysis_start,
        reference_time,
    )

    results = []
    compared_pair_count = 0
    for first, second in candidate_pairs:
        first_points = trajectories.get(first["mmsi"], [])
        second_points = trajectories.get(second["mmsi"], [])
        if (
            len(first_points) < config["min_aligned_points"]
            or len(second_points) < config["min_aligned_points"]
        ):
            continue
        compared_pair_count += 1
        features = extract_pair_features(
            first_points, second_points, config
        )
        if not features["qualifies"]:
            continue

        pair_key = f"{first['mmsi']}:{second['mmsi']}"
        first_name = first["name"]
        second_name = second["name"]
        details = (
            f"疑似双拖捕鱼：{first_name}（{first['mmsi']}）与"
            f"{second_name}（{second['mmsi']}）已保持近似并排、"
            f"同向同速航行 {features['duration_minutes']:.1f} 分钟；"
            f"平均船距 {features['mean_distance_metres']:.0f} 米，"
            f"船距标准差 {features['distance_std_metres']:.0f} 米。"
        )
        results.append(
            {
                "mmsi": first["mmsi"],
                "other_mmsi": second["mmsi"],
                "pair": pair_key,
                "pair_mmsi": [first["mmsi"], second["mmsi"]],
                "name": f"{first_name} / {second_name}",
                "location": [
                    round(
                        (first["longitude"] + second["longitude"]) / 2,
                        6,
                    ),
                    round(
                        (first["latitude"] + second["latitude"]) / 2,
                        6,
                    ),
                ],
                "ship_locations": [
                    [first["longitude"], first["latitude"]],
                    [second["longitude"], second["latitude"]],
                ],
                "event": "DoubleDragging",
                "risk": "高风险",
                "duration_minutes": round(
                    features["duration_minutes"], 2
                ),
                "aligned_points": features["aligned_points"],
                "mean_distance_metres": round(
                    features["mean_distance_metres"], 1
                ),
                "distance_std_metres": round(
                    features["distance_std_metres"], 1
                ),
                "course_match_ratio": round(
                    features["course_ratio"], 3
                ),
                "lateral_formation_ratio": round(
                    features["lateral_ratio"], 3
                ),
                "speed_similarity_ratio": round(
                    features["speed_similarity_ratio"], 3
                ),
                "speed_correlation": round(
                    features["speed_correlation"], 3
                ),
                "details": details,
                "detail": details,
            }
        )

    results.sort(key=lambda result: result["pair"])
    _annotate_event_state(
        results,
        reference_time,
        config["event_retention_minutes"],
    )
    return _response(
        timestamp=reference_time,
        results=results,
        skipped_count=skipped_count,
        stale_count=stale_count,
        candidate_pair_count=len(candidate_pairs),
        compared_pair_count=compared_pair_count,
        config=config,
    )
