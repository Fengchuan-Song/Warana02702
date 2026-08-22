import math
from collections import defaultdict
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET

from AISData.maritime_zones import (
    MaritimeZoneDataError,
    PORT_ZONE_TYPES,
    zones_containing_point,
)
from AISData.normalization import normalise_ais_name
from IllegalAnchored.zones import classify_location

from .models import LowSpeedPoint
from .utils import (
    continuous_low_speed,
    get_low_speed_config,
    get_speed_rule,
)


LOW_SPEED_EVENT_CACHE_KEY = "low_speed:event_state:v1"


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
    try:
        speed = float(ship_info.get("speed"))
        lon = float(ship_info.get("lon"))
        lat = float(ship_info.get("lat"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (speed, lon, lat)):
        return None
    if (
        speed < 0
        or speed > config["max_valid_speed_knots"]
        or not -180 <= lon <= 180
        or not -90 <= lat <= 90
    ):
        return None
    timestamp = _parse_timestamp(ship_info.get("timestamp"))
    if timestamp is None:
        return None

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
        "speed": speed,
        "lon": lon,
        "lat": lat,
        "timestamp": timestamp,
        "nav_status": nav_status,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _is_underway_candidate(ship, config):
    if ship["at_dock"] or ship["matched_port_name"]:
        return False
    try:
        if zones_containing_point(
            ship["lon"],
            ship["lat"],
            zone_types=PORT_ZONE_TYPES,
        ):
            return False
    except MaritimeZoneDataError:
        # Keep detection available when the optional encrypted zone dataset
        # cannot be loaded; upstream dock/port annotations still apply.
        pass
    if classify_location(ship["lon"], ship["lat"])["state"] == "authorized":
        return False
    if ship["nav_status"] is None:
        return config["allow_missing_nav_status"]
    return ship["nav_status"] in config["eligible_nav_statuses"]


def _upsert_points(ships):
    if not ships:
        return
    points = [
        LowSpeedPoint(
            mmsi=ship["mmsi"],
            name=ship["name"],
            longitude=ship["lon"],
            latitude=ship["lat"],
            speed=ship["speed"],
            speed_limit=ship["rule"]["threshold"],
            zone_name=ship["rule"]["name"],
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
            "speed_limit",
            "zone_name",
        ],
    }
    if connection.features.supports_update_conflicts_with_target:
        options["unique_fields"] = ["mmsi", "timestamp"]
    LowSpeedPoint.objects.bulk_create(points, **options)


def _load_trajectories(mmsis, start_time, end_time):
    trajectories = defaultdict(list)
    rows = (
        LowSpeedPoint.objects.filter(
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
            "zone_name",
            "timestamp",
        )
    )
    for row in rows:
        trajectories[row["mmsi"]].append(row)
    return trajectories


def _previous_events(reference_time, retention_minutes):
    cached = cache.get(LOW_SPEED_EVENT_CACHE_KEY, {})
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
            "type": "低速船舶预警",
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
def detect_low_speed(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    config = get_low_speed_config()
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
        age_seconds = (reference_time - ship["timestamp"]).total_seconds()
        if age_seconds > config["max_position_age_seconds"]:
            stale_count += 1
            reset_mmsis.append(ship["mmsi"])
            continue
        rule = get_speed_rule(ship["lon"], ship["lat"], config)
        if rule is None:
            unmonitored_count += 1
            reset_mmsis.append(ship["mmsi"])
            continue
        if (
            not _is_underway_candidate(ship, config)
            or ship["speed"] >= rule["threshold"]
        ):
            reset_mmsis.append(ship["mmsi"])
            continue
        ship["rule"] = rule
        candidates.append(ship)

    retention_start = reference_time - timedelta(
        minutes=config["retention_window_minutes"]
    )
    future_limit = reference_time + timedelta(
        seconds=config["future_tolerance_seconds"]
    )
    LowSpeedPoint.objects.filter(timestamp__lt=retention_start).delete()
    LowSpeedPoint.objects.filter(timestamp__gt=future_limit).delete()
    if reset_mmsis:
        LowSpeedPoint.objects.filter(mmsi__in=reset_mmsis).delete()
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
        episode = continuous_low_speed(points, config)
        if episode is None:
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
            and previous.get("zone_name") == ship["rule"]["name"]
            and ship["timestamp"] >= previous_last_seen
            and (
                ship["timestamp"] - previous_last_seen
            ).total_seconds()
            <= config["maximum_gap_seconds"]
        )
        started_at = (
            previous.get("started_at")
            if same_event and previous.get("started_at")
            else episode["started_at"].isoformat()
        )
        started_timestamp = _parse_timestamp(started_at)
        duration_seconds = (
            (ship["timestamp"] - started_timestamp).total_seconds()
            if started_timestamp is not None
            else episode["duration_seconds"]
        )
        event_id = (
            f"low-speed:{ship['mmsi']}:"
            f"{ship['rule']['name']}:{started_at}"
        )
        details = (
            f"检测到持续低速航行：{ship['name']}（{ship['mmsi']}）"
            f"在{ship['rule']['name']}当前航速{ship['speed']:.2f}节，"
            f"低于{ship['rule']['threshold']:.2f}节最低航速，"
            f"已持续{duration_seconds / 60:.1f}分钟。"
        )
        results.append(
            {
                "mmsi": ship["mmsi"],
                "name": ship["name"],
                "location": [ship["lon"], ship["lat"]],
                "event": "LowSpeed",
                "risk": "待核查",
                "zone": ship["rule"]["name"],
                "speed_knots": round(ship["speed"], 3),
                "minimum_speed_limit_knots": round(
                    ship["rule"]["threshold"], 3
                ),
                "duration_seconds": round(duration_seconds, 1),
                "observation_count": episode["observation_count"],
                "average_speed_knots": round(
                    episode["average_speed_knots"], 3
                ),
                "minimum_observed_speed_knots": round(
                    episode["minimum_speed_knots"], 3
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
            "zone_name": ship["rule"]["name"],
            "last_seen": ship["timestamp"].isoformat(),
        }

    cache.set(
        LOW_SPEED_EVENT_CACHE_KEY,
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
            "type": "低速船舶预警",
            "timestamp": reference_time.isoformat(),
            "count": len(results),
            "results": results,
            "skipped_count": skipped_count,
            "stale_count": stale_count,
            "unmonitored_count": unmonitored_count,
            "rule": {
                "default_minimum_speed_knots": config[
                    "default_minimum_speed_knots"
                ],
                "minimum_duration_seconds": config[
                    "minimum_duration_seconds"
                ],
                "minimum_observations": config[
                    "minimum_observations"
                ],
                "maximum_gap_seconds": config[
                    "maximum_gap_seconds"
                ],
                "eligible_nav_statuses": sorted(
                    config["eligible_nav_statuses"]
                ),
                "allow_missing_nav_status": config[
                    "allow_missing_nav_status"
                ],
                "monitored_only": config["monitored_only"],
            },
            "message": "检测成功",
        }
    )
