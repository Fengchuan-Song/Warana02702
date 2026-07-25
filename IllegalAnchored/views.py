import math
from datetime import datetime, timedelta

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET

from AISData.normalization import normalise_ais_name

from .zones import SOURCE_METADATA, classify_location, distance_m


MAX_ANCHOR_SPEED_KNOTS = 0.5
MIN_ANCHOR_DURATION_SECONDS = 300
MIN_OBSERVATIONS = 3
MAX_ANCHOR_DRIFT_METRES = 250
HISTORY_WINDOW_SECONDS = 1800
HISTORY_CACHE_KEY = "illegal_anchored:history:v1"
HISTORY_CACHE_TIMEOUT = 24 * 60 * 60


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


def _is_anchor_candidate(ship):
    # Docked vessels are normal port operations, not anchor candidates.
    if ship["at_dock"]:
        return False

    # AIS status 1 explicitly means "at anchor".  Low SOG is only a fallback
    # when navigation status is absent or 15 (undefined); applying it to every
    # status incorrectly classifies moored (5), underway (0), fishing (7), etc.
    if ship["nav_status"] == 1:
        return True
    return (
        ship["nav_status"] in {None, 15}
        and ship["speed"] <= MAX_ANCHOR_SPEED_KNOTS
    )


def _append_history(history, ship, classification):
    mmsi = ship["mmsi"]
    timestamp = ship["timestamp"]
    points = history.get(mmsi, [])

    is_normal_port_operation = (
        bool(ship["matched_port_name"])
        and classification["state"] != "prohibited"
    )
    if (
        not _is_anchor_candidate(ship)
        or is_normal_port_operation
        or classification["state"] in {"authorized", "outside_coverage"}
    ):
        history.pop(mmsi, None)
        return []

    point = {
        "timestamp": timestamp.isoformat(),
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

    cutoff = timestamp - timedelta(seconds=HISTORY_WINDOW_SECONDS)
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
        > MAX_ANCHOR_DRIFT_METRES
        for item in points[:-1]
    ):
        points = [point]

    # Crossing into a different legal classification starts a new episode.
    if any(
        item.get("state") != point["state"]
        or item.get("zone_name") != point["zone_name"]
        for item in points[:-1]
    ):
        points = [point]

    history[mmsi] = points
    return points


def _episode_duration(points):
    if len(points) < MIN_OBSERVATIONS:
        return 0
    start = _parse_timestamp(points[0].get("timestamp"))
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
    stale_cutoff = latest_timestamp - timedelta(seconds=HISTORY_WINDOW_SECONDS)
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
        points = _append_history(history, ship, classification)
        duration = _episode_duration(points)
        if duration < MIN_ANCHOR_DURATION_SECONDS:
            continue

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
                "location": [ship["lon"], ship["lat"]],
                "speed": ship["speed"],
                "zone": zone_name,
                "reason": reason,
                "duration_seconds": duration,
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
                "max_speed_knots": MAX_ANCHOR_SPEED_KNOTS,
                "min_duration_seconds": MIN_ANCHOR_DURATION_SECONDS,
                "min_observations": MIN_OBSERVATIONS,
                "max_drift_metres": MAX_ANCHOR_DRIFT_METRES,
            },
            "sources": SOURCE_METADATA,
            "message": "检测成功",
        }
    )
