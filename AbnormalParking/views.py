import json
import math
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from AISData.normalization import normalise_ais_name
from IllegalAnchored.zones import classify_location

from .models import ParkingBuffer, ParkingMonitorArea
from .utils import get_monitored_area, get_parking_config, run_parking_analysis


AREA_FIELDS = ("min_lon", "min_lat", "max_lon", "max_lat")


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def _json_error(message, status=400):
    return JsonResponse({"success": False, "message": message}, status=status)


def _read_json(request):
    try:
        data = json.loads(request.body or b"{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ValueError("请求体必须是有效的 JSON")
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def _finite_float(value, field_label):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_label}必须是有效数字")
    if not math.isfinite(number):
        raise ValueError(f"{field_label}必须是有限数字")
    return number


def _validate_area_data(data, partial=False, instance=None):
    cleaned = {}
    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name:
            raise ValueError("区域名称不能为空")
        if len(name) > 100:
            raise ValueError("区域名称不能超过100个字符")
        cleaned["name"] = name
    elif not partial:
        raise ValueError("区域名称不能为空")

    labels = {
        "min_lon": "最小经度",
        "min_lat": "最小纬度",
        "max_lon": "最大经度",
        "max_lat": "最大纬度",
    }
    for field in AREA_FIELDS:
        if field in data:
            cleaned[field] = _finite_float(data[field], labels[field])
        elif not partial:
            raise ValueError(f"{labels[field]}不能为空")

    values = {}
    for field in AREA_FIELDS:
        if field in cleaned:
            values[field] = cleaned[field]
        elif instance is not None:
            values[field] = getattr(instance, field)
    if len(values) == len(AREA_FIELDS):
        if not -180 <= values["min_lon"] <= 180 or not -180 <= values["max_lon"] <= 180:
            raise ValueError("经度必须在 -180 到 180 之间")
        if not -90 <= values["min_lat"] <= 90 or not -90 <= values["max_lat"] <= 90:
            raise ValueError("纬度必须在 -90 到 90 之间")
        if values["min_lon"] >= values["max_lon"]:
            raise ValueError("最小经度必须小于最大经度")
        if values["min_lat"] >= values["max_lat"]:
            raise ValueError("最小纬度必须小于最大纬度")

    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ValueError("is_active 必须是布尔值")
        cleaned["is_active"] = data["is_active"]
    return cleaned


def _serialize_area(area):
    return {
        "id": area.id,
        "name": area.name,
        "min_lon": area.min_lon,
        "min_lat": area.min_lat,
        "max_lon": area.max_lon,
        "max_lat": area.max_lat,
        "bounds": [area.min_lon, area.min_lat, area.max_lon, area.max_lat],
        "is_active": area.is_active,
        "created_at": area.created_at.isoformat(),
        "updated_at": area.updated_at.isoformat(),
    }


def _runtime_config():
    config = get_parking_config()
    if ParkingMonitorArea.objects.exists():
        config["monitored_areas"] = [
            {
                "name": area.name,
                "bounds": (area.min_lon, area.min_lat, area.max_lon, area.max_lat),
            }
            for area in ParkingMonitorArea.objects.filter(is_active=True)
        ]
    return config


@require_http_methods(["GET", "POST"])
def monitor_area_collection(request):
    if request.method == "GET":
        areas = ParkingMonitorArea.objects.all()
        config = _runtime_config()
        return JsonResponse(
            {
                "success": True,
                "count": areas.count(),
                "results": [_serialize_area(area) for area in areas],
                "rule": {
                    key: config[key]
                    for key in (
                        "analysis_window_minutes",
                        "max_speed_knots",
                        "distance_threshold_metres",
                        "min_duration_minutes",
                        "min_points",
                    )
                },
            }
        )

    try:
        data = _validate_area_data(_read_json(request))
        area = ParkingMonitorArea.objects.create(**data)
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("已存在同名异常停泊监控区", status=409)
    return JsonResponse(
        {
            "success": True,
            "message": "异常停泊监控区创建成功",
            "result": _serialize_area(area),
        },
        status=201,
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def monitor_area_detail(request, area_id):
    try:
        area = ParkingMonitorArea.objects.get(pk=area_id)
    except ParkingMonitorArea.DoesNotExist:
        return _json_error("异常停泊监控区不存在", status=404)

    if request.method == "GET":
        return JsonResponse({"success": True, "result": _serialize_area(area)})
    if request.method == "DELETE":
        if ParkingMonitorArea.objects.count() <= 1:
            return _json_error(
                "至少需要保留一个监控区；如需停止检测，请停用该区域",
                status=409,
            )
        area.delete()
        return JsonResponse({"success": True, "message": "异常停泊监控区删除成功"})

    try:
        data = _validate_area_data(
            _read_json(request),
            partial=request.method == "PATCH",
            instance=area,
        )
        for field, value in data.items():
            setattr(area, field, value)
        area.save()
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("已存在同名异常停泊监控区", status=409)
    return JsonResponse(
        {
            "success": True,
            "message": "异常停泊监控区更新成功",
            "result": _serialize_area(area),
        }
    )


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
