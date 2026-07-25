import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET

from AISData.normalization import normalise_ais_name

from .models import HighSpeedPoint


HIGH_SPEED_EVENT_CACHE_KEY = "high_speed:event_state:v1"
DEFAULT_HIGH_SPEED_CONFIG = {
    "default_speed_limit_knots": 30.0,
    "minimum_duration_seconds": 300.0,
    "maximum_gap_seconds": 90.0,
    "analysis_window_minutes": 30.0,
    "retention_window_minutes": 60.0,
    "max_position_age_seconds": 120.0,
    "future_tolerance_seconds": 120.0,
    "max_valid_speed_knots": 102.2,
    "high_risk_excess_knots": 10.0,
    "event_retention_minutes": 30.0,
}


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _config():
    config = DEFAULT_HIGH_SPEED_CONFIG.copy()
    configured = getattr(settings, "HIGH_SPEED_DETECTION", {})
    if isinstance(configured, dict):
        for key in config:
            value = _finite_float(configured.get(key))
            if value is not None and value >= 0:
                config[key] = value
    config["default_speed_limit_knots"] = max(
        0.1, config["default_speed_limit_knots"]
    )
    config["analysis_window_minutes"] = max(
        config["analysis_window_minutes"],
        config["minimum_duration_seconds"] / 60,
    )
    config["retention_window_minutes"] = max(
        config["retention_window_minutes"],
        config["analysis_window_minutes"],
    )
    return config


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


def _normalise_ship(ship_info, config):
    if not isinstance(ship_info, dict):
        return None
    mmsi = str(ship_info.get("mmsi") or "").strip()
    speed = _finite_float(ship_info.get("speed"))
    longitude = _finite_float(
        ship_info.get("lon", ship_info.get("longitude"))
    )
    latitude = _finite_float(
        ship_info.get("lat", ship_info.get("latitude"))
    )
    timestamp = _parse_timestamp(ship_info.get("timestamp"))
    if (
        not mmsi
        or speed is None
        or longitude is None
        or latitude is None
        or timestamp is None
        or speed < 0
        or speed > config["max_valid_speed_knots"]
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        return None
    return {
        "mmsi": mmsi,
        "name": normalise_ais_name(ship_info.get("name")),
        "speed": speed,
        "longitude": longitude,
        "latitude": latitude,
        "timestamp": timestamp,
        "ship_type": str(
            ship_info.get("ship_type")
            or ship_info.get("vessel_type")
            or ""
        ).strip(),
    }


def _replace_current_points(ships):
    points = [
        HighSpeedPoint(
            mmsi=ship["mmsi"],
            speed=ship["speed"],
            longitude=ship["longitude"],
            latitude=ship["latitude"],
            speed_limit=ship["rule"]["limit"],
            zone_name=ship["rule"]["name"],
            timestamp=ship["timestamp"],
        )
        for ship in ships
    ]
    options = {
        "update_conflicts": True,
        "update_fields": [
            "speed",
            "longitude",
            "latitude",
            "speed_limit",
            "zone_name",
        ],
    }
    if connection.features.supports_update_conflicts_with_target:
        options["unique_fields"] = ["mmsi", "timestamp"]
    HighSpeedPoint.objects.bulk_create(points, **options)


def _load_trajectories(mmsis, start_time, end_time):
    trajectories = defaultdict(list)
    rows = (
        HighSpeedPoint.objects.filter(
            mmsi__in=mmsis,
            timestamp__gte=start_time,
            timestamp__lte=end_time,
            longitude__isnull=False,
            latitude__isnull=False,
        )
        .order_by("mmsi", "timestamp")
        .values(
            "mmsi",
            "speed",
            "speed_limit",
            "timestamp",
        )
    )
    for row in rows:
        trajectories[row["mmsi"]].append(row)
    return trajectories


def _continuous_high_speed(points, config):
    if not points:
        return None

    current = points[-1]
    if current["speed"] <= current["speed_limit"]:
        return None

    included = []
    later_timestamp = current["timestamp"]
    for point in reversed(points):
        gap_seconds = (
            later_timestamp - point["timestamp"]
        ).total_seconds()
        if (
            abs(
                point["speed_limit"] - current["speed_limit"]
            )
            > 1e-6
            or point["speed"] <= point["speed_limit"]
            or gap_seconds > config["maximum_gap_seconds"]
        ):
            break
        included.append(point)
        later_timestamp = point["timestamp"]
    included.reverse()

    duration_seconds = (
        included[-1]["timestamp"] - included[0]["timestamp"]
    ).total_seconds()
    if duration_seconds < config["minimum_duration_seconds"]:
        return None

    speeds = [point["speed"] for point in included]
    return {
        "started_at": included[0]["timestamp"],
        "duration_seconds": duration_seconds,
        "observation_count": len(included),
        "average_speed_knots": sum(speeds) / len(speeds),
        "max_speed_knots": max(speeds),
    }


def _active_event_state(reference_time, retention_minutes):
    cached = cache.get(HIGH_SPEED_EVENT_CACHE_KEY, {})
    if not isinstance(cached, dict):
        return {}
    retained = {}
    retention_seconds = retention_minutes * 60
    for mmsi, value in cached.items():
        if not isinstance(value, dict):
            continue
        last_seen = _parse_timestamp(value.get("last_seen"))
        if last_seen is None:
            continue
        age_seconds = (reference_time - last_seen).total_seconds()
        if 0 <= age_seconds <= retention_seconds:
            retained[mmsi] = value
    return retained


def _empty_response(
    timestamp=None,
    results=None,
    skipped_count=0,
    stale_count=0,
    unmonitored_count=0,
    config=None,
):
    results = results or []
    payload = {
        "success": True,
        "type": "高速快艇预警",
        "timestamp": timestamp.isoformat() if timestamp else None,
        "count": len(results),
        "results": results,
        "skipped_count": skipped_count,
        "stale_count": stale_count,
        "unmonitored_count": unmonitored_count,
        "message": "检测成功" if timestamp else "暂无有效AIS数据",
    }
    if config is not None:
        payload["rule"] = {
            "speed_threshold_knots": config[
                "default_speed_limit_knots"
            ],
            "operator": ">",
            "minimum_duration_seconds": config[
                "minimum_duration_seconds"
            ],
            "maximum_gap_seconds": config["maximum_gap_seconds"],
            "uses_ship_type": False,
        }
    return JsonResponse(payload)


@require_GET
def detect_high_speed(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    config = _config()
    ships_by_mmsi = {}
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info, config)
        if ship is None:
            skipped_count += 1
            continue
        previous = ships_by_mmsi.get(ship["mmsi"])
        if previous is None or ship["timestamp"] > previous["timestamp"]:
            ships_by_mmsi[ship["mmsi"]] = ship
    if not ships_by_mmsi:
        return _empty_response(skipped_count=skipped_count)

    reference_time = max(
        ship["timestamp"] for ship in ships_by_mmsi.values()
    )
    monitored_ships = []
    stale_count = 0
    for ship in ships_by_mmsi.values():
        if (
            reference_time - ship["timestamp"]
        ).total_seconds() > config["max_position_age_seconds"]:
            stale_count += 1
            continue
        ship["rule"] = {
            "name": "默认水域",
            "limit": config["default_speed_limit_knots"],
        }
        monitored_ships.append(ship)

    retention_start = reference_time - timedelta(
        minutes=config["retention_window_minutes"]
    )
    future_limit = reference_time + timedelta(
        seconds=config["future_tolerance_seconds"]
    )
    HighSpeedPoint.objects.filter(
        timestamp__lt=retention_start
    ).delete()
    HighSpeedPoint.objects.filter(
        timestamp__gt=future_limit
    ).delete()

    if not monitored_ships:
        cache.set(
            HIGH_SPEED_EVENT_CACHE_KEY,
            {},
            timeout=3600,
        )
        return _empty_response(
            timestamp=reference_time,
            skipped_count=skipped_count,
            stale_count=stale_count,
            config=config,
        )

    _replace_current_points(monitored_ships)
    previous_events = _active_event_state(
        reference_time,
        config["event_retention_minutes"],
    )
    candidate_ships = [
        ship
        for ship in monitored_ships
        if ship["speed"] > ship["rule"]["limit"]
    ]
    analysis_start = reference_time - timedelta(
        minutes=config["analysis_window_minutes"]
    )
    trajectories = _load_trajectories(
        [ship["mmsi"] for ship in candidate_ships],
        analysis_start,
        reference_time,
    )

    results = []
    active_events = {}
    timestamp_text = reference_time.isoformat()
    for ship in candidate_ships:
        streak = _continuous_high_speed(
            trajectories.get(ship["mmsi"], []),
            config,
        )
        if streak is None:
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
            and ship["timestamp"] >= previous_last_seen
            and (
                ship["timestamp"] - previous_last_seen
            ).total_seconds()
            <= config["maximum_gap_seconds"]
        )
        started_at = (
            previous.get("started_at")
            if same_event and previous.get("started_at")
            else streak["started_at"].isoformat()
        )
        started_timestamp = _parse_timestamp(started_at)
        duration_seconds = (
            (ship["timestamp"] - started_timestamp).total_seconds()
            if started_timestamp is not None
            else 0
        )
        is_new = not same_event
        event_id = (
            f"high-speed:{ship['mmsi']}:"
            f"{started_at}"
        )
        excess = ship["speed"] - ship["rule"]["limit"]
        risk = (
            "高风险"
            if excess >= config["high_risk_excess_knots"]
            else "中风险"
        )
        details = (
            f"检测到高速快艇：{ship['name']}（{ship['mmsi']}）"
            f"当前航速 {ship['speed']:.1f} 节，"
            f"已连续超过 {ship['rule']['limit']:.1f} 节"
            f" {duration_seconds / 60:.1f} 分钟。"
        )
        results.append(
            {
                "mmsi": ship["mmsi"],
                "name": ship["name"],
                "ship_type": ship["ship_type"],
                "location": [
                    ship["longitude"],
                    ship["latitude"],
                ],
                "event": "HighSpeedBoat",
                "risk": risk,
                "zone": ship["rule"]["name"],
                "speed_knots": round(ship["speed"], 2),
                "speed_limit_knots": round(
                    ship["rule"]["limit"], 2
                ),
                "excess_speed_knots": round(excess, 2),
                "duration_seconds": round(duration_seconds, 1),
                "observation_count": streak["observation_count"],
                "average_speed_knots": round(
                    streak["average_speed_knots"], 2
                ),
                "max_speed_knots": round(
                    streak["max_speed_knots"], 2
                ),
                "event_id": event_id,
                "is_new": is_new,
                "first_detected_at": started_at,
                "details": details,
                "detail": details,
            }
        )
        active_events[ship["mmsi"]] = {
            "event_id": event_id,
            "started_at": started_at,
            "speed_limit_knots": ship["rule"]["limit"],
            "last_seen": timestamp_text,
        }

    cache.set(
        HIGH_SPEED_EVENT_CACHE_KEY,
        active_events,
        timeout=max(
            3600,
            int(config["event_retention_minutes"] * 120),
        ),
    )
    results.sort(key=lambda result: result["mmsi"])
    return _empty_response(
        timestamp=reference_time,
        results=results,
        skipped_count=skipped_count,
        stale_count=stale_count,
        config=config,
    )
