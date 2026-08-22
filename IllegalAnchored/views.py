import math
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
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

from .zones import SOURCE_METADATA, classify_location, distance_m


DEFAULT_ILLEGAL_ANCHORED_CONFIG = {
    "max_speed_knots": 0.5,
    "min_duration_seconds": 300,
    "min_observations": 3,
    "max_drift_metres": 250.0,
    "history_window_seconds": 1800,
}
HISTORY_CACHE_KEY = "illegal_anchored:history:v1"
HISTORY_CACHE_TIMEOUT = 24 * 60 * 60


def _config():
    config = DEFAULT_ILLEGAL_ANCHORED_CONFIG.copy()
    configured = getattr(settings, "ILLEGAL_ANCHORED_DETECTION", {})
    if isinstance(configured, dict):
        for key in config:
            try:
                value = float(configured.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                config[key] = value

    config["min_observations"] = max(
        2,
        int(config["min_observations"]),
    )
    config["min_duration_seconds"] = int(
        config["min_duration_seconds"]
    )
    config["history_window_seconds"] = max(
        config["min_duration_seconds"],
        int(config["history_window_seconds"]),
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

    raw_mmsi = ship_info.get("mmsi")
    mmsi = str(raw_mmsi).strip() if raw_mmsi is not None else ""
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
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
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
        "lon": lon,
        "lat": lat,
        "speed": max(0.0, speed),
        "timestamp": timestamp,
        "nav_status": nav_status,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _is_anchor_candidate(ship, config):
    # Docked vessels are normal port operations, not anchor candidates.
    if ship["at_dock"]:
        return False

    # Navigation status 0 (under way using engine) is frequently stale while
    # a vessel is stationary.  Treat it as a low-speed fallback candidate;
    # duration, drift and legal-zone checks below still have to pass.
    return (
        ship["nav_status"] in {0, 1, None, 15}
        and ship["speed"] <= config["max_speed_knots"]
    )


def _is_normal_port_operation(ship, classification):
    if classification["state"] == "prohibited":
        return False
    if ship["matched_port_name"]:
        return True
    try:
        return bool(
            zones_containing_point(
                ship["lon"],
                ship["lat"],
                zone_types=PORT_ZONE_TYPES,
            )
        )
    except MaritimeZoneDataError:
        return False


def _append_history(history, ship, classification, config):
    mmsi = ship["mmsi"]
    timestamp = ship["timestamp"]
    points = history.get(mmsi, [])

    normal_port_operation = _is_normal_port_operation(
        ship,
        classification,
    )
    if (
        not _is_anchor_candidate(ship, config)
        or normal_port_operation
        or classification["state"] in {"authorized", "outside_coverage"}
    ):
        history.pop(mmsi, None)
        return []

    episode_started_at = timestamp
    if points:
        episode_started_at = (
            _parse_timestamp(points[-1].get("episode_started_at"))
            or _parse_timestamp(points[0].get("timestamp"))
            or timestamp
        )

    point = {
        "timestamp": timestamp.isoformat(),
        # Keep the episode identity outside the rolling analysis window.
        # Otherwise points[0] advances whenever the oldest observation is
        # evicted and one continuous anchoring episode receives a new ID.
        "episode_started_at": episode_started_at.isoformat(),
        "lon": ship["lon"],
        "lat": ship["lat"],
        "zone_name": classification["zone_name"],
        "state": classification["state"],
    }

    if points:
        last_timestamp = _parse_timestamp(points[-1].get("timestamp"))
        if last_timestamp is not None and timestamp < last_timestamp:
            return points
        if last_timestamp == timestamp:
            points[-1] = point
        else:
            points.append(point)
    else:
        points = [point]

    cutoff = timestamp - timedelta(
        seconds=config["history_window_seconds"]
    )
    points = [
        item
        for item in points
        if (_parse_timestamp(item.get("timestamp")) or timestamp) >= cutoff
    ]

    # A large displacement is a slow transit or a new anchoring episode, not
    # one continuous anchor swing.
    if any(
        distance_m(
            item["lon"],
            item["lat"],
            ship["lon"],
            ship["lat"],
        )
        > config["max_drift_metres"]
        for item in points[:-1]
    ):
        point["episode_started_at"] = point["timestamp"]
        points = [point]

    # Crossing into a different legal classification starts a new episode.
    if any(
        item.get("state") != point["state"]
        or item.get("zone_name") != point["zone_name"]
        for item in points[:-1]
    ):
        point["episode_started_at"] = point["timestamp"]
        points = [point]

    history[mmsi] = points
    return points


def _episode_duration(points, config):
    if len(points) < config["min_observations"]:
        return 0
    start = (
        _parse_timestamp(points[-1].get("episode_started_at"))
        or _parse_timestamp(points[0].get("timestamp"))
    )
    end = _parse_timestamp(points[-1].get("timestamp"))
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds()))


def _empty_response(skipped_count=0):
    return JsonResponse(
        {
            "success": True,
            "type": "疑似非法抛锚预警",
            "timestamp": None,
            "count": 0,
            "results": [],
            "skipped_count": skipped_count,
            "message": "暂无有效AIS数据",
        }
    )


@require_GET
def detect_illegal_anchored(request):
    config = _config()
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    ships_by_mmsi = {}
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info)
        if ship is None:
            skipped_count += 1
            continue
        existing = ships_by_mmsi.get(ship["mmsi"])
        if existing is None or ship["timestamp"] >= existing["timestamp"]:
            ships_by_mmsi[ship["mmsi"]] = ship

    ships = list(ships_by_mmsi.values())
    if not ships:
        return _empty_response(skipped_count)

    history = cache.get(HISTORY_CACHE_KEY, {})
    if not isinstance(history, dict):
        history = {}

    latest_timestamp = max(ship["timestamp"] for ship in ships)
    stale_cutoff = latest_timestamp - timedelta(
        seconds=config["history_window_seconds"]
    )
    for mmsi, points in list(history.items()):
        if not points:
            history.pop(mmsi, None)
            continue
        last_timestamp = _parse_timestamp(points[-1].get("timestamp"))
        if last_timestamp is None or last_timestamp < stale_cutoff:
            history.pop(mmsi, None)

    alerts = []
    for ship in ships:
        classification = classify_location(ship["lon"], ship["lat"])
        points = _append_history(history, ship, classification, config)
        duration = _episode_duration(points, config)
        if duration < config["min_duration_seconds"]:
            continue

        episode_started_at = (
            _parse_timestamp(points[-1].get("episode_started_at"))
            or _parse_timestamp(points[0].get("timestamp"))
        )
        episode_started_at_text = episode_started_at.isoformat()

        reason = classification["reason"]
        zone_name = classification["zone_name"] or "未知水域"
        details = (
            f"疑似非法抛锚：{reason}；所在区域：{zone_name}；"
            f"航速{ship['speed']:.2f}节，低速/锚泊状态已持续"
            f"{duration // 60}分{duration % 60}秒。"
            "该结果为AIS规则预警，需结合锚泊申请、应急报告和现场执法确认。"
        )
        alerts.append(
            {
                "mmsi": ship["mmsi"],
                "name": ship["name"],
                "timestamp": ship["timestamp"].isoformat(),
                "location": [ship["lon"], ship["lat"]],
                "speed": ship["speed"],
                "zone": zone_name,
                "reason": reason,
                "duration_seconds": duration,
                "first_detected_at": episode_started_at_text,
                "trajectory_started_at": episode_started_at_text,
                "episode_started_at": episode_started_at_text,
                "event_id": (
                    f"illegal-anchored:{ship['mmsi']}:"
                    f"{classification['state']}:{zone_name}:"
                    f"{episode_started_at_text}"
                ),
                "risk": (
                    "高风险"
                    if classification["state"] == "prohibited"
                    else "待核查"
                ),
                "details": details,
                "detail": details,
            }
        )

    cache.set(
        HISTORY_CACHE_KEY,
        history,
        timeout=HISTORY_CACHE_TIMEOUT,
    )

    return JsonResponse(
        {
            "success": True,
            "type": "疑似非法抛锚预警",
            "timestamp": latest_timestamp.isoformat(),
            "count": len(alerts),
            "results": alerts,
            "skipped_count": skipped_count,
            "rule": {
                "max_speed_knots": config["max_speed_knots"],
                "min_duration_seconds": config["min_duration_seconds"],
                "min_observations": config["min_observations"],
                "max_drift_metres": config["max_drift_metres"],
                "history_window_seconds": config[
                    "history_window_seconds"
                ],
            },
            "sources": SOURCE_METADATA,
            "message": "检测成功",
        }
    )
