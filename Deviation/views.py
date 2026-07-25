from collections import defaultdict
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET

from AISData.normalization import normalise_ais_name
from IllegalAnchored.zones import classify_location

from .models import TrajectoryPoint
from .route_index import get_route_index
from .utils import (
    bearing_degrees,
    distance_metres,
    finite_float,
    get_deviation_config,
    undirected_angle_difference,
)


DEVIATION_STATE_CACHE_KEY = "deviation:tracking_state:v2"


def _parse_timestamp(value):
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = parse_datetime(value)
    else:
        timestamp = None
    if timestamp is None:
        return None
    if timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(
            timestamp,
            timezone.get_current_timezone(),
        )
    return timestamp


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def _normalise_ship(ship_info, config):
    if not isinstance(ship_info, dict):
        return None
    mmsi = str(ship_info.get("mmsi") or "").strip()
    if not mmsi:
        return None
    lon = finite_float(ship_info.get("lon"))
    lat = finite_float(ship_info.get("lat"))
    speed = finite_float(ship_info.get("speed"))
    if (
        lon is None
        or lat is None
        or speed is None
        or not -180 <= lon <= 180
        or not -90 <= lat <= 90
        or speed < 0
        or speed > config["max_valid_speed_knots"]
    ):
        return None
    timestamp = _parse_timestamp(ship_info.get("timestamp"))
    if timestamp is None:
        return None
    course = finite_float(
        ship_info.get("course", ship_info.get("cog"))
    )
    if course is None or not 0 <= course < 360:
        course = None
    try:
        nav_status = int(float(ship_info.get("nav_status")))
    except (TypeError, ValueError):
        nav_status = None
    raw_port_name = (
        ship_info.get("matched_port_name")
        or ship_info.get("matchedPortName")
        or ""
    )
    matched_port_name = str(raw_port_name).strip()
    if matched_port_name.lower() in {"nan", "none", "null"}:
        matched_port_name = ""
    return {
        "mmsi": mmsi,
        "name": normalise_ais_name(ship_info.get("name")),
        "lon": lon,
        "lat": lat,
        "speed": speed,
        "course": course,
        "timestamp": timestamp,
        "nav_status": nav_status,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _is_moving_candidate(ship, config):
    if ship["speed"] < config["minimum_speed_knots"]:
        return False
    if ship["at_dock"] or ship["matched_port_name"]:
        return False
    if classify_location(ship["lon"], ship["lat"])["state"] == "authorized":
        return False
    if ship["nav_status"] is None:
        return config["allow_missing_nav_status"]
    return ship["nav_status"] in config["eligible_nav_statuses"]


def _upsert_points(ships):
    if not ships:
        return
    points = [
        TrajectoryPoint(
            mmsi=ship["mmsi"],
            name=ship["name"],
            longitude=ship["lon"],
            latitude=ship["lat"],
            speed=ship["speed"],
            course=ship["course"],
            timestamp=ship["timestamp"],
        )
        for ship in ships
    ]
    options = {
        "update_conflicts": True,
        "update_fields": [
            "name",
            "longitude",
            "latitude",
            "speed",
            "course",
        ],
    }
    if connection.features.supports_update_conflicts_with_target:
        options["unique_fields"] = ["mmsi", "timestamp"]
    TrajectoryPoint.objects.bulk_create(points, **options)


def _load_trajectories(mmsis, start_time, end_time):
    trajectories = defaultdict(list)
    rows = (
        TrajectoryPoint.objects.filter(
            mmsi__in=mmsis,
            timestamp__gte=start_time,
            timestamp__lte=end_time,
        )
        .order_by("mmsi", "timestamp")
        .values(
            "mmsi",
            "longitude",
            "latitude",
            "speed",
            "course",
            "timestamp",
        )
    )
    for row in rows:
        trajectories[row["mmsi"]].append(row)
    return trajectories


def _continuous_segment(points, config):
    if not points:
        return None
    segment = []
    later_timestamp = points[-1]["timestamp"]
    for point in reversed(points):
        gap = (later_timestamp - point["timestamp"]).total_seconds()
        if (
            point["speed"] < config["minimum_speed_knots"]
            or gap < 0
            or gap > config["maximum_gap_seconds"]
        ):
            break
        segment.append(point)
        later_timestamp = point["timestamp"]
    segment.reverse()
    if len(segment) < config["minimum_observations"]:
        return None
    duration = (
        segment[-1]["timestamp"] - segment[0]["timestamp"]
    ).total_seconds()
    displacement = distance_metres(
        (segment[0]["longitude"], segment[0]["latitude"]),
        (segment[-1]["longitude"], segment[-1]["latitude"]),
    )
    if (
        duration < config["minimum_duration_seconds"]
        or displacement < config["minimum_displacement_metres"]
    ):
        return None
    return {
        "points": segment,
        "duration_seconds": duration,
        "displacement_metres": displacement,
    }


def _load_states(reference_time, retention_minutes):
    cached = cache.get(DEVIATION_STATE_CACHE_KEY, {})
    if not isinstance(cached, dict):
        return {}
    retained = {}
    retention_seconds = retention_minutes * 60
    for mmsi, state in cached.items():
        if not isinstance(state, dict):
            continue
        last_seen = _parse_timestamp(state.get("last_seen"))
        if last_seen is None:
            continue
        age = (reference_time - last_seen).total_seconds()
        if 0 <= age <= retention_seconds:
            retained[mmsi] = state
    return retained


def _route_observations(route_index, points, heading, config):
    queried = route_index.query(
        [
            (point["longitude"], point["latitude"])
            for point in points
        ]
    )
    observations = []
    for index, point in enumerate(points):
        axis = float(queried["axis_degrees"][index])
        coherence = float(queried["coherence"][index])
        point_heading = (
            point["course"]
            if point.get("course") is not None
            else heading
        )
        angle_difference = undirected_angle_difference(
            point_heading,
            axis,
        )
        aligned = (
            coherence < config["minimum_direction_coherence"]
            or angle_difference is None
            or angle_difference <= config["direction_tolerance_degrees"]
        )
        observations.append(
            {
                "timestamp": point["timestamp"],
                "distance_metres": float(
                    queried["distance_metres"][index]
                ),
                "axis_degrees": axis,
                "coherence": coherence,
                "angle_difference": angle_difference,
                "on_route": (
                    float(queried["distance_metres"][index])
                    <= config["route_entry_distance_metres"]
                    and aligned
                ),
            }
        )
    return observations


def _empty_response(message="暂无有效AIS数据", skipped_count=0):
    return JsonResponse(
        {
            "success": True,
            "type": "疑似航道偏离预警",
            "timestamp": None,
            "count": 0,
            "results": [],
            "skipped_count": skipped_count,
            "stale_count": 0,
            "unmonitored_count": 0,
            "message": message,
        }
    )


@require_GET
def detect_deviation(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    route_index = get_route_index()
    if route_index is None:
        return JsonResponse(
            {
                "success": False,
                "type": "疑似航道偏离预警",
                "timestamp": None,
                "count": 0,
                "results": [],
                "message": "航道知识索引不可用，偏航检测未执行",
            },
            status=503,
        )

    config = get_deviation_config()
    ships_by_mmsi = {}
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info, config)
        if ship is None:
            skipped_count += 1
            continue
        previous = ships_by_mmsi.get(ship["mmsi"])
        if previous is None or ship["timestamp"] >= previous["timestamp"]:
            ships_by_mmsi[ship["mmsi"]] = ship
    if not ships_by_mmsi:
        return _empty_response(skipped_count=skipped_count)

    reference_time = max(
        ship["timestamp"] for ship in ships_by_mmsi.values()
    )
    candidates = []
    reset_mmsis = []
    stale_count = 0
    unmonitored_count = 0
    for ship in ships_by_mmsi.values():
        age = (reference_time - ship["timestamp"]).total_seconds()
        if age > config["max_position_age_seconds"]:
            stale_count += 1
            reset_mmsis.append(ship["mmsi"])
            continue
        if not route_index.contains(
            ship["lon"],
            ship["lat"],
            config["coverage_margin_metres"],
        ):
            unmonitored_count += 1
            reset_mmsis.append(ship["mmsi"])
            continue
        if not _is_moving_candidate(ship, config):
            reset_mmsis.append(ship["mmsi"])
            continue
        candidates.append(ship)

    retention_start = reference_time - timedelta(
        minutes=config["retention_window_minutes"]
    )
    future_limit = reference_time + timedelta(
        seconds=config["future_tolerance_seconds"]
    )
    TrajectoryPoint.objects.filter(timestamp__lt=retention_start).delete()
    TrajectoryPoint.objects.filter(timestamp__gt=future_limit).delete()
    if reset_mmsis:
        TrajectoryPoint.objects.filter(mmsi__in=reset_mmsis).delete()
    _upsert_points(candidates)

    states = _load_states(
        reference_time,
        config["state_retention_minutes"],
    )
    next_states = states.copy()
    for mmsi in reset_mmsis:
        next_states.pop(mmsi, None)
    analysis_start = reference_time - timedelta(
        minutes=config["analysis_window_minutes"]
    )
    trajectories = _load_trajectories(
        [ship["mmsi"] for ship in candidates],
        analysis_start,
        reference_time,
    )

    results = []
    for ship in candidates:
        points = [
            point
            for point in trajectories.get(ship["mmsi"], [])
            if point["timestamp"] <= ship["timestamp"]
        ]
        episode = _continuous_segment(points, config)
        if episode is None:
            next_states.pop(ship["mmsi"], None)
            continue
        segment = episode["points"]
        heading = ship["course"]
        if heading is None:
            heading = bearing_degrees(
                (
                    segment[0]["longitude"],
                    segment[0]["latitude"],
                ),
                (
                    segment[-1]["longitude"],
                    segment[-1]["latitude"],
                ),
            )
        observations = _route_observations(
            route_index,
            segment,
            heading,
            config,
        )
        previous = states.get(ship["mmsi"], {})
        previous_last_seen = _parse_timestamp(previous.get("last_seen"))
        state_is_continuous = (
            previous_last_seen is not None
            and ship["timestamp"] >= previous_last_seen
            and (
                ship["timestamp"] - previous_last_seen
            ).total_seconds()
            <= config["maximum_gap_seconds"]
        )
        previously_on_route = (
            bool(previous.get("on_route_seen"))
            if state_is_continuous
            else False
        )
        on_route_seen = previously_on_route or any(
            observation["on_route"] for observation in observations
        )
        current = observations[-1]

        if current["on_route"]:
            off_route = []
            on_route_seen = True
        elif (
            on_route_seen
            and current["distance_metres"]
            >= config["deviation_distance_metres"]
        ):
            off_route = []
            for observation in reversed(observations):
                if (
                    observation["distance_metres"]
                    < config["deviation_distance_metres"]
                ):
                    break
                off_route.append(observation)
            off_route.reverse()
        else:
            off_route = []

        previous_off_start = (
            _parse_timestamp(previous.get("off_route_started_at"))
            if state_is_continuous
            else None
        )
        if off_route:
            off_started = previous_off_start or off_route[0]["timestamp"]
            off_count = len(off_route)
            if previous_off_start is not None:
                previous_count = int(previous.get("off_route_count", 0))
                if ship["timestamp"] > previous_last_seen:
                    off_count = max(off_count, previous_count + 1)
                else:
                    off_count = max(off_count, previous_count)
            off_duration = (
                ship["timestamp"] - off_started
            ).total_seconds()
        else:
            off_started = None
            off_count = 0
            off_duration = 0

        is_alert = (
            off_started is not None
            and off_count >= config["confirmation_observations"]
            and off_duration >= config["confirmation_duration_seconds"]
        )
        was_active = (
            state_is_continuous
            and bool(previous.get("active"))
            and previous_off_start == off_started
        )
        state = {
            "on_route_seen": on_route_seen,
            "off_route_started_at": (
                off_started.isoformat() if off_started else None
            ),
            "off_route_count": off_count,
            "active": is_alert,
            "last_seen": ship["timestamp"].isoformat(),
        }
        next_states[ship["mmsi"]] = state
        if not is_alert:
            continue

        event_id = (
            f"deviation:{ship['mmsi']}:{off_started.isoformat()}"
        )
        angle_text = (
            f"，与参考航向轴夹角"
            f"{current['angle_difference']:.1f}度"
            if current["angle_difference"] is not None
            else ""
        )
        details = (
            f"疑似航道偏离：{ship['name']}（{ship['mmsi']}）"
            f"距历史航道走廊约{current['distance_metres']:.0f}米"
            f"{angle_text}，已连续偏离{off_duration:.0f}秒。"
            "该结果基于历史航迹走廊筛查，需结合计划航线和现场情况确认。"
        )
        results.append(
            {
                "mmsi": ship["mmsi"],
                "name": ship["name"],
                "location": [ship["lon"], ship["lat"]],
                "event": "Deviation",
                "risk": (
                    "高风险"
                    if current["distance_metres"]
                    >= config["deviation_distance_metres"] * 2
                    else "待核查"
                ),
                "route_distance_metres": round(
                    current["distance_metres"], 1
                ),
                "reference_axis_degrees": round(
                    current["axis_degrees"], 1
                ),
                "direction_difference_degrees": (
                    round(current["angle_difference"], 1)
                    if current["angle_difference"] is not None
                    else None
                ),
                "duration_seconds": round(off_duration, 1),
                "observation_count": off_count,
                "event_id": event_id,
                "is_new": not was_active,
                "first_detected_at": off_started.isoformat(),
                "details": details,
                "detail": details,
            }
        )

    cache.set(
        DEVIATION_STATE_CACHE_KEY,
        next_states,
        timeout=max(
            3600,
            config["state_retention_minutes"] * 120,
        ),
    )
    results.sort(key=lambda result: result["mmsi"])
    return JsonResponse(
        {
            "success": True,
            "type": "疑似航道偏离预警",
            "timestamp": reference_time.isoformat(),
            "count": len(results),
            "results": results,
            "skipped_count": skipped_count,
            "stale_count": stale_count,
            "unmonitored_count": unmonitored_count,
            "rule": {
                "minimum_speed_knots": config["minimum_speed_knots"],
                "minimum_duration_seconds": config[
                    "minimum_duration_seconds"
                ],
                "minimum_observations": config[
                    "minimum_observations"
                ],
                "maximum_gap_seconds": config[
                    "maximum_gap_seconds"
                ],
                "route_entry_distance_metres": config[
                    "route_entry_distance_metres"
                ],
                "deviation_distance_metres": config[
                    "deviation_distance_metres"
                ],
                "confirmation_observations": config[
                    "confirmation_observations"
                ],
                "confirmation_duration_seconds": config[
                    "confirmation_duration_seconds"
                ],
            },
            "knowledge": route_index.metadata,
            "message": "检测成功",
        }
    )
