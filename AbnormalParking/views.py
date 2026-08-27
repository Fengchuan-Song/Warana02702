import math
from datetime import timedelta

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from AISData.behavior_recognition import behavior_results_for_request
from AISData.normalization import normalise_ais_name
from .models import ParkingBuffer
from .utils import get_parking_config, parking_event_from_behavior_result


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
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    if not 0 <= speed <= 102.2:
        return None

    try:
        heading = float(ship_info.get("heading"))
    except (TypeError, ValueError):
        heading = None
    if heading is not None and (
        not math.isfinite(heading) or not 0 <= heading < 360
    ):
        heading = None
    try:
        nav_status = int(float(ship_info.get("nav_status")))
    except (TypeError, ValueError):
        nav_status = None

    timestamp = parse_datetime(str(ship_info.get("timestamp") or ""))
    if timestamp is None:
        return None
    if timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(
            timestamp,
            timezone.get_current_timezone(),
        )

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
        "heading": heading,
        "nav_status": nav_status,
        "timestamp": timestamp,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _empty_response(message="暂无有效AIS数据", skipped_count=0):
    return JsonResponse(
        {
            "success": True,
            "type": "异常停泊",
            "timestamp": None,
            "count": 0,
            "results": [],
            "skipped_count": skipped_count,
            "message": message,
        }
    )


def _store_points(ships):
    """Mirror incoming observations in one batch for legacy audit/admin use."""
    if not ships:
        return
    mmsis = {ship["mmsi"] for ship in ships}
    timestamps = {ship["timestamp"] for ship in ships}
    existing = set(
        ParkingBuffer.objects.filter(
            mmsi__in=mmsis,
            timestamp__in=timestamps,
        ).values_list("mmsi", "timestamp")
    )
    rows = [
        ParkingBuffer(
            mmsi=ship["mmsi"],
            name=ship["name"],
            longitude=ship["lon"],
            latitude=ship["lat"],
            speed=ship["speed"],
            heading=ship["heading"],
            nav_status=ship["nav_status"],
            at_dock=ship["at_dock"],
            matched_port_name=ship["matched_port_name"],
            timestamp=ship["timestamp"],
        )
        for ship in ships
        if (ship["mmsi"], ship["timestamp"]) not in existing
    ]
    if rows:
        ParkingBuffer.objects.bulk_create(rows)


@csrf_exempt
def detectAbnormalParking(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    config = get_parking_config()
    ships = []
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info)
        if ship is None:
            skipped_count += 1
            continue
        ships.append(ship)
    if not ships:
        return _empty_response(skipped_count=skipped_count)

    _store_points(ships)
    shared_results = behavior_results_for_request(
        ships,
        getattr(request, "ais_context", None),
    )
    results = []
    for ship in ships:
        mmsi = ship["mmsi"]
        last_event = parking_event_from_behavior_result(
            shared_results.get(mmsi), config
        )
        if last_event is None:
            continue
        location = last_event["location"]
        if last_event.get("behavior") == "underway_with_moored_status":
            details = (
                "检测为异常停泊状态：AIS报文持续显示船舶处于停泊状态，"
                f"但船舶在{last_event['duration_minutes']:.1f}分钟内以平均"
                f"{last_event['mean_speed_knots']:.2f}节航行约"
                f"{last_event['path_distance_metres']:.0f}米；"
                f"{last_event['reason']}。"
            )
        else:
            details = (
                f"检测为异常停泊：船舶在{last_event['facility']}附近约"
                f"{last_event['position_radius_metres']:.0f}米活动半径内稳定靠泊"
                f"{last_event['duration_minutes']:.1f}分钟，平均航速"
                f"{last_event['mean_speed_knots']:.2f}节；"
                f"{last_event['reason']}。"
            )
        results.append(
            {
                "mmsi": mmsi,
                "location": [location["lon"], location["lat"]],
                "name": ship["name"],
                "duration_minutes": last_event["duration_minutes"],
                "mean_speed_knots": last_event["mean_speed_knots"],
                "position_radius_metres": last_event[
                    "position_radius_metres"
                ],
                "heading_variation_degrees": last_event[
                    "heading_variation_degrees"
                ],
                "facility": last_event["facility"],
                "reason": last_event["reason"],
                "behavior": last_event.get("behavior", "berthing"),
                "path_distance_metres": last_event.get(
                    "path_distance_metres", 0
                ),
                "details": details,
                "detail": details,
            }
        )

    latest_timestamp = max(ship["timestamp"] for ship in ships)
    cleanup_time = latest_timestamp - timedelta(
        minutes=config["retention_window_minutes"]
    )
    ParkingBuffer.objects.filter(timestamp__lt=cleanup_time).delete()
    return JsonResponse(
        {
            "success": True,
            "type": "异常停泊",
            "timestamp": latest_timestamp.isoformat(),
            "count": len(results),
            "results": results,
            "skipped_count": skipped_count,
            "rule": {
                "analysis_window_minutes": config[
                    "analysis_window_minutes"
                ],
                "max_speed_knots": config["max_speed_knots"],
                "exit_speed_knots": config["exit_speed_knots"],
                "distance_threshold_metres": config[
                    "distance_threshold_metres"
                ],
                "position_exit_radius_metres": config[
                    "position_exit_radius_metres"
                ],
                "min_duration_minutes": config["min_duration_minutes"],
                "min_points": config["min_points"],
                "max_gap_minutes": config["max_gap_minutes"],
                "near_shore_distance_metres": config[
                    "near_shore_distance_metres"
                ],
                "max_heading_change_degrees": config[
                    "max_heading_change_degrees"
                ],
                "min_heading_observations": config[
                    "min_heading_observations"
                ],
                "anchor_swing_heading_degrees": config[
                    "anchor_swing_heading_degrees"
                ],
                "min_anchor_status_ratio": config[
                    "min_anchor_status_ratio"
                ],
                "moored_underway_min_speed_knots": config[
                    "moored_underway_min_speed_knots"
                ],
                "moored_underway_min_path_distance_metres": config[
                    "moored_underway_min_path_distance_metres"
                ],
                "moored_underway_min_duration_minutes": config[
                    "moored_underway_min_duration_minutes"
                ],
                "moored_underway_min_points": config[
                    "moored_underway_min_points"
                ],
                "legal_max_duration_minutes": config[
                    "legal_max_duration_minutes"
                ],
            },
            "message": "检测成功",
        }
    )
