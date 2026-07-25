import math
from datetime import timedelta

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from AISData.normalization import normalise_ais_name
from IllegalAnchored.zones import classify_location

from .models import MonitorRegion, TrajectoryBuffer
from .utils import (
    get_monitored_area,
    get_wandering_config,
    run_loitering_analysis,
)


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
        course = float(ship_info.get("course", 0))
    except (TypeError, ValueError):
        return None
    if not all(
        math.isfinite(value) for value in (lon, lat, speed, course)
    ):
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
        "course": course % 360,
        "timestamp": timestamp,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _is_normal_operation(ship):
    if ship["at_dock"] or ship["matched_port_name"]:
        return True
    return classify_location(ship["lon"], ship["lat"])["state"] == "authorized"


def _runtime_config():
    config = get_wandering_config()
    database_areas = [
        {
            "name": region.name,
            "bounds": (
                region.min_lon,
                region.min_lat,
                region.max_lon,
                region.max_lat,
            ),
        }
        for region in MonitorRegion.objects.filter(is_active=True)
    ]
    if database_areas:
        config["monitored_areas"] = database_areas
    return config


def _empty_response(message="暂无有效AIS数据", skipped_count=0):
    return JsonResponse(
        {
            "success": True,
            "type": "异常徘徊",
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
        "course": ship["course"],
        "speed": ship["speed"],
    }
    queryset = TrajectoryBuffer.objects.filter(
        mmsi=ship["mmsi"],
        timestamp=ship["timestamp"],
    )
    if queryset.update(**values) == 0:
        TrajectoryBuffer.objects.create(
            mmsi=ship["mmsi"],
            timestamp=ship["timestamp"],
            **values,
        )


@csrf_exempt
def receive_realtime_point(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    config = _runtime_config()
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
        if monitored_area is None or _is_normal_operation(ship):
            TrajectoryBuffer.objects.filter(mmsi=mmsi).delete()
            continue

        _store_point(ship)
        window_start = ship["timestamp"] - timedelta(
            minutes=config["analysis_window_minutes"]
        )
        recent_points = TrajectoryBuffer.objects.filter(
            mmsi=mmsi,
            timestamp__gte=window_start,
            timestamp__lte=ship["timestamp"],
        ).order_by("timestamp")
        if recent_points.count() < config["min_points"]:
            continue

        analysis = run_loitering_analysis(recent_points, config)
        if not analysis["is_abnormal"]:
            continue

        last_segment = analysis["abnormal_segments"][-1]
        location = last_segment["location"]
        details = (
            f"检测为异常徘徊：船舶在{last_segment['area']}内"
            f"{last_segment['duration_minutes']:.1f}分钟航行约"
            f"{last_segment['path_distance_metres']:.0f}米，"
            f"出现{last_segment['turn_count']}次明显转向，"
            f"直线位移/轨迹长度比为"
            f"{last_segment['displacement_ratio']:.2f}。"
        )
        results.append(
            {
                "mmsi": mmsi,
                "location": [location["lon"], location["lat"]],
                "name": ship["name"],
                "duration_minutes": last_segment["duration_minutes"],
                "turn_count": last_segment["turn_count"],
                "path_distance_metres": last_segment[
                    "path_distance_metres"
                ],
                "displacement_ratio": last_segment[
                    "displacement_ratio"
                ],
                "area": last_segment["area"],
                "details": details,
                "detail": details,
            }
        )

    latest_timestamp = max(ship["timestamp"] for ship in ships)
    cleanup_time = latest_timestamp - timedelta(
        minutes=config["retention_window_minutes"]
    )
    TrajectoryBuffer.objects.filter(timestamp__lt=cleanup_time).delete()

    return JsonResponse(
        {
            "success": True,
            "type": "异常徘徊",
            "timestamp": latest_timestamp.isoformat(),
            "count": len(results),
            "results": results,
            "skipped_count": skipped_count,
            "rule": {
                key: config[key]
                for key in (
                    "analysis_window_minutes",
                    "min_points",
                    "min_duration_minutes",
                    "min_path_distance_metres",
                    "min_turn_angle_degrees",
                    "min_turn_count",
                    "max_displacement_ratio",
                    "max_gap_minutes",
                    "min_area_point_ratio",
                )
            },
            "message": "检测成功",
        }
    )
