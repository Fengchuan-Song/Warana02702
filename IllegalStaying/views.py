import math
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

from .models import StayingBuffer
from .utils import (
    continuous_staying_event,
    get_forbidden_area,
    get_staying_config,
)


ILLEGAL_STAYING_EVENT_CACHE_KEY = "illegal_staying:event_state:v1"


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


def _normalise_ship(ship_info):
    if not isinstance(ship_info, dict):
        return None
    mmsi = str(ship_info.get("mmsi") or "").strip()
    if not mmsi:
        return None
    try:
        lon = float(ship_info.get("lon"))
        lat = float(ship_info.get("lat"))
        speed = float(ship_info.get("speed"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (lon, lat, speed)):
        return None
    if (
        not -180 <= lon <= 180
        or not -90 <= lat <= 90
        or speed < 0
        or speed > 102.2
    ):
        return None
    timestamp = _parse_timestamp(ship_info.get("timestamp"))
    if timestamp is None:
        return None

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
        "timestamp": timestamp,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _is_normal_operation(ship):
    if ship["at_dock"] or ship["matched_port_name"]:
        return True
    return classify_location(ship["lon"], ship["lat"])["state"] == "authorized"


def _upsert_points(ships):
    if not ships:
        return
    points = [
        StayingBuffer(
            mmsi=ship["mmsi"],
            name=ship["name"],
            longitude=ship["lon"],
            latitude=ship["lat"],
            speed=ship["speed"],
            zone_name=ship["area"]["name"],
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
            "zone_name",
        ],
    }
    if connection.features.supports_update_conflicts_with_target:
        options["unique_fields"] = ["mmsi", "timestamp"]
    StayingBuffer.objects.bulk_create(points, **options)


def _load_trajectories(mmsis, start_time, end_time):
    trajectories = defaultdict(list)
    rows = (
        StayingBuffer.objects.filter(
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
            "zone_name",
            "timestamp",
        )
    )
    for row in rows:
        trajectories[row["mmsi"]].append(row)
    return trajectories


def _previous_events(reference_time, retention_minutes):
    cached = cache.get(ILLEGAL_STAYING_EVENT_CACHE_KEY, {})
    if not isinstance(cached, dict):
        return {}
    retention_seconds = retention_minutes * 60
    retained = {}
    for mmsi, event in cached.items():
        if not isinstance(event, dict):
            continue
        last_seen = _parse_timestamp(event.get("last_seen"))
        if last_seen is None:
            continue
        age = (reference_time - last_seen).total_seconds()
        if 0 <= age <= retention_seconds:
            retained[mmsi] = event
    return retained


def _empty_response(message="暂无有效AIS数据", skipped_count=0):
    return JsonResponse(
        {
            "success": True,
            "type": "疑似非法驻留预警",
            "timestamp": None,
            "count": 0,
            "results": [],
            "skipped_count": skipped_count,
            "stale_count": 0,
            "message": message,
        }
    )


@require_GET
def detectIllegalStaying(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    config = get_staying_config()
    ships_by_mmsi = {}
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info)
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
    for ship in ships_by_mmsi.values():
        age_seconds = (reference_time - ship["timestamp"]).total_seconds()
        if age_seconds > config["max_position_age_seconds"]:
            stale_count += 1
            reset_mmsis.append(ship["mmsi"])
            continue
        area = get_forbidden_area(
            ship["lon"],
            ship["lat"],
            config["forbidden_areas"],
        )
        if (
            area is None
            or _is_normal_operation(ship)
            or ship["speed"] > config["max_speed_knots"]
        ):
            reset_mmsis.append(ship["mmsi"])
            continue
        ship["area"] = area
        candidates.append(ship)

    retention_start = reference_time - timedelta(
        minutes=config["retention_window_minutes"]
    )
    future_limit = reference_time + timedelta(
        seconds=config["future_tolerance_seconds"]
    )
    StayingBuffer.objects.filter(timestamp__lt=retention_start).delete()
    StayingBuffer.objects.filter(timestamp__gt=future_limit).delete()
    if reset_mmsis:
        StayingBuffer.objects.filter(mmsi__in=reset_mmsis).delete()
    _upsert_points(candidates)

    previous_events = _previous_events(
        reference_time,
        config["event_retention_minutes"],
    )
    analysis_start = reference_time - timedelta(
        minutes=config["analysis_window_minutes"]
    )
    trajectories = _load_trajectories(
        [ship["mmsi"] for ship in candidates],
        analysis_start,
        reference_time,
    )

    results = []
    active_events = {}
    for ship in candidates:
        points = [
            point
            for point in trajectories.get(ship["mmsi"], [])
            if point["timestamp"] <= ship["timestamp"]
        ]
        event = continuous_staying_event(points, config)
        if event is None:
            continue

        previous = previous_events.get(ship["mmsi"])
        previous_last_seen = (
            _parse_timestamp(previous.get("last_seen"))
            if previous
            else None
        )
        same_event = (
            previous is not None
            and previous_last_seen is not None
            and previous.get("zone_name") == ship["area"]["name"]
            and ship["timestamp"] >= previous_last_seen
            and (
                ship["timestamp"] - previous_last_seen
            ).total_seconds()
            <= config["maximum_gap_seconds"]
        )
        started_at = (
            previous.get("started_at")
            if same_event and previous.get("started_at")
            else event["started_at"].isoformat()
        )
        started_timestamp = _parse_timestamp(started_at)
        duration_minutes = (
            (ship["timestamp"] - started_timestamp).total_seconds() / 60
            if started_timestamp is not None
            else event["duration_minutes"]
        )
        event_id = (
            f"illegal-staying:{ship['mmsi']}:"
            f"{ship['area']['name']}:{started_at}"
        )
        details = (
            f"疑似非法驻留：{ship['area']['reason']}；"
            f"所在区域：{ship['area']['name']}；"
            f"航速{ship['speed']:.2f}节，在约"
            f"{config['distance_threshold_metres']:.0f}米范围内已持续"
            f"{duration_minutes:.1f}分钟。"
            "该结果为AIS规则预警，需结合驻留许可和现场执法确认。"
        )
        results.append(
            {
                "mmsi": ship["mmsi"],
                "name": ship["name"],
                "location": [ship["lon"], ship["lat"]],
                "event": "IllegalStaying",
                "risk": "高风险",
                "zone": ship["area"]["name"],
                "reason": ship["area"]["reason"],
                "speed_knots": round(ship["speed"], 2),
                "duration_minutes": round(duration_minutes, 2),
                "point_count": event["point_count"],
                "average_speed_knots": round(
                    event["average_speed_knots"], 2
                ),
                "max_speed_knots": round(
                    event["max_speed_knots"], 2
                ),
                "event_id": event_id,
                "is_new": not same_event,
                "first_detected_at": started_at,
                "details": details,
                "detail": details,
            }
        )
        active_events[ship["mmsi"]] = {
            "event_id": event_id,
            "started_at": started_at,
            "zone_name": ship["area"]["name"],
            "last_seen": ship["timestamp"].isoformat(),
        }

    cache.set(
        ILLEGAL_STAYING_EVENT_CACHE_KEY,
        active_events,
        timeout=max(
            3600,
            config["event_retention_minutes"] * 120,
        ),
    )
    results.sort(key=lambda result: result["mmsi"])
    return JsonResponse(
        {
            "success": True,
            "type": "疑似非法驻留预警",
            "timestamp": reference_time.isoformat(),
            "count": len(results),
            "results": results,
            "skipped_count": skipped_count,
            "stale_count": stale_count,
            "rule": {
                "max_speed_knots": config["max_speed_knots"],
                "distance_threshold_metres": config[
                    "distance_threshold_metres"
                ],
                "min_duration_minutes": config[
                    "min_duration_minutes"
                ],
                "min_points": config["min_points"],
                "maximum_gap_seconds": config[
                    "maximum_gap_seconds"
                ],
            },
            "message": "检测成功",
        }
    )
