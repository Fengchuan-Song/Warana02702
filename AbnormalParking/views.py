import math
from datetime import timedelta

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from AISData.normalization import normalise_ais_name
from IllegalAnchored.zones import classify_location

from .models import ParkingBuffer
from .utils import get_monitored_area, get_parking_config, run_parking_analysis


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
        speed = float(ship_info.get("speed", 0))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (lon, lat, speed)):
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None

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
        "speed": max(0.0, speed),
        "timestamp": timestamp,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _is_normal_parking(ship):
    if ship["at_dock"] or ship["matched_port_name"]:
        return True
    return classify_location(ship["lon"], ship["lat"])["state"] == "authorized"


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


def _store_point(ship):
    values = {
        "name": ship["name"],
        "longitude": ship["lon"],
        "latitude": ship["lat"],
        "speed": ship["speed"],
    }
    queryset = ParkingBuffer.objects.filter(
        mmsi=ship["mmsi"],
        timestamp=ship["timestamp"],
    )
    if queryset.update(**values) == 0:
        ParkingBuffer.objects.create(
            mmsi=ship["mmsi"],
            timestamp=ship["timestamp"],
            **values,
        )


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

    results = []
    for ship in ships:
        mmsi = ship["mmsi"]
        monitored_area = get_monitored_area(
            ship["lat"],
            ship["lon"],
            config["monitored_areas"],
        )

        # Leaving the monitored area or entering a known normal parking area
        # ends any previous abnormal-parking episode.
        if monitored_area is None or _is_normal_parking(ship):
            ParkingBuffer.objects.filter(mmsi=mmsi).delete()
            continue

        _store_point(ship)
        window_start = ship["timestamp"] - timedelta(
            minutes=config["analysis_window_minutes"]
        )
        recent_points = ParkingBuffer.objects.filter(
            mmsi=mmsi,
            timestamp__gte=window_start,
            timestamp__lte=ship["timestamp"],
        ).order_by("timestamp")

        if recent_points.count() < config["min_points"]:
            continue
        analysis = run_parking_analysis(recent_points, config)
        if not analysis["is_abnormal"]:
            continue

        last_event = analysis["events"][-1]
        location = last_event["location"]
        details = (
            f"检测为异常停泊：船舶在{last_event['area']}内约"
            f"{config['distance_threshold_metres']:.0f}米范围持续停留"
            f"{last_event['duration_minutes']:.1f}分钟，"
            f"期间有效轨迹点{last_event['point_count']}个。"
        )
        results.append(
            {
                "mmsi": mmsi,
                "location": [location["lon"], location["lat"]],
                "name": ship["name"],
                "duration_minutes": last_event["duration_minutes"],
                "area": last_event["area"],
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
                "distance_threshold_metres": config[
                    "distance_threshold_metres"
                ],
                "min_duration_minutes": config["min_duration_minutes"],
                "min_points": config["min_points"],
            },
            "message": "检测成功",
        }
    )
