import math
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET

from AISData.behavior_recognition import behavior_results_for_request
from AISData.maritime_zones import (
    MaritimeZoneDataError,
    PORT_ZONE_TYPES,
    zones_containing_point,
)
from AISData.normalization import normalise_ais_name
from AISData.low_speed_behavior import BEHAVIOR_ANCHORING

from .zones import SOURCE_METADATA, classify_location, distance_m


DEFAULT_ILLEGAL_ANCHORED_CONFIG = {
    "max_speed_knots": 0.5,
    "min_duration_seconds": 300,
    "min_observations": 3,
    "max_drift_metres": 250.0,
    "history_window_seconds": 1800,
    "max_gap_seconds": 300,
    "min_heading_observations": 2,
    "anchor_swing_heading_degrees": 45.0,
    "min_anchor_status_ratio": 0.6,
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
    config["max_gap_seconds"] = max(1, int(config["max_gap_seconds"]))
    config["min_heading_observations"] = max(
        2, int(config["min_heading_observations"])
    )
    config["anchor_swing_heading_degrees"] = min(
        180.0, config["anchor_swing_heading_degrees"]
    )
    config["min_anchor_status_ratio"] = min(
        1.0, config["min_anchor_status_ratio"]
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
    try:
        heading = float(ship_info.get("heading"))
    except (TypeError, ValueError):
        heading = None
    if heading is not None and (
        not math.isfinite(heading) or not 0 <= heading < 360
    ):
        heading = None

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
        "heading": heading,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _is_anchor_candidate(ship, config):
    # Docked vessels are normal port operations, not anchor candidates.
    if ship["at_dock"]:
        return False

    # Low speed starts a shared behavior episode.  Navigation status and heading
    # are evidence used by the classifier, not a substitute for classification.
    return ship["speed"] <= config["max_speed_knots"]


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
        "speed": ship["speed"],
        "heading": ship["heading"],
        "nav_status": ship["nav_status"],
        "at_dock": ship["at_dock"],
        "in_port_basin": False,
        "berthing_facility": False,
        "zone_name": classification["zone_name"],
        "state": classification["state"],
    }

    if points:
        last_timestamp = _parse_timestamp(points[-1].get("timestamp"))
        if (
            last_timestamp is not None
            and (timestamp - last_timestamp).total_seconds()
            > config["max_gap_seconds"]
        ):
            point["episode_started_at"] = point["timestamp"]
            points = []

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

    latest_timestamp = max(ship["timestamp"] for ship in ships)
    shared_results = behavior_results_for_request(
        ships,
        getattr(request, "ais_context", None),
    )

    alerts = []
    for ship in ships:
        classification = classify_location(ship["lon"], ship["lat"])
        if (
            not _is_anchor_candidate(ship, config)
            or _is_normal_port_operation(ship, classification)
            or classification["state"] in {"authorized", "outside_coverage"}
        ):
            continue
        shared = shared_results.get(ship["mmsi"]) or {}
        behavior = shared.get("analysis") or {}
        if (
            not behavior.get("qualified")
            or behavior.get("behavior") != BEHAVIOR_ANCHORING
            or behavior.get("point_count", 0) < config["min_observations"]
            or behavior.get("position_radius_metres") is None
            or behavior["position_radius_metres"] > config["max_drift_metres"]
        ):
            continue

        # The physical episode is shared.  Only trim it at legal-zone borders
        # here so prohibited-area duration and event identity remain model
        # specific without re-running trajectory classification.
        legal_segment = []
        later = None
        for point in reversed(behavior.get("points") or []):
            if point["speed"] > config["max_speed_knots"]:
                break
            if later is not None:
                gap = (later["timestamp"] - point["timestamp"]).total_seconds()
                if gap < 0 or gap > config["max_gap_seconds"]:
                    break
            point_classification = classify_location(point["lon"], point["lat"])
            if (
                point_classification["state"] != classification["state"]
                or point_classification.get("zone_name")
                != classification.get("zone_name")
            ):
                break
            legal_segment.append(point)
            later = point
        legal_segment.reverse()
        if len(legal_segment) < config["min_observations"]:
            continue
        episode_started_at = _parse_timestamp(
            legal_segment[0].get("timestamp")
        )
        episode_ended_at = _parse_timestamp(
            legal_segment[-1].get("timestamp")
        )
        if episode_started_at is None or episode_ended_at is None:
            continue
        duration = max(
            0, int((episode_ended_at - episode_started_at).total_seconds())
        )
        if duration < config["min_duration_seconds"]:
            continue
        episode_started_at_text = episode_started_at.isoformat()

        reason = classification["reason"]
        zone_name = classification["zone_name"] or "未知水域"
        details = (
            f"疑似非法抛锚：{reason}；所在区域：{zone_name}；"
            f"航速{ship['speed']:.2f}节，低速/锚泊状态已持续"
            f"{duration // 60}分{duration % 60}秒。"
            f"行为证据：{behavior['reason']}。"
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
                "behavior": BEHAVIOR_ANCHORING,
                "behavior_reason": behavior["reason"],
                "anchor_status_ratio": round(
                    behavior["anchor_status_ratio"], 3
                ),
                "heading_variation_degrees": (
                    round(behavior["heading_variation_degrees"], 2)
                    if behavior["heading_variation_degrees"] is not None
                    else None
                ),
                "position_swing": behavior["position_swing"],
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
                "max_gap_seconds": config["max_gap_seconds"],
                "min_heading_observations": config[
                    "min_heading_observations"
                ],
                "anchor_swing_heading_degrees": config[
                    "anchor_swing_heading_degrees"
                ],
                "min_anchor_status_ratio": config[
                    "min_anchor_status_ratio"
                ],
            },
            "sources": SOURCE_METADATA,
            "message": "检测成功",
        }
    )
