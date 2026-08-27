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

from AISData.monitor_area_geometry import (
    normalise_polygon_vertices,
    polygon_bounds,
    rectangle_vertices,
)
from AISData.normalization import normalise_ais_name
from .models import MonitorRegion, TrajectoryBuffer
from .utils import (
    get_monitored_area,
    get_wandering_config,
    run_loitering_analysis,
)


AREA_FIELDS = ("min_lon", "min_lat", "max_lon", "max_lat")
WANDERING_EVENT_CACHE_KEY = "abnormal_wandering:event_state:v1"


def _parse_timestamp(value):
    timestamp = value if hasattr(value, "tzinfo") else parse_datetime(str(value or ""))
    if timestamp is None:
        return None
    if timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(
            timestamp, timezone.get_current_timezone()
        )
    return timestamp


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
        duplicate = MonitorRegion.objects.filter(name=name)
        if instance is not None:
            duplicate = duplicate.exclude(pk=instance.pk)
        if duplicate.exists():
            raise ValueError("已存在同名异常徘徊监控区")
        cleaned["name"] = name
    elif not partial:
        raise ValueError("区域名称不能为空")

    labels = {
        "min_lon": "最小经度",
        "min_lat": "最小纬度",
        "max_lon": "最大经度",
        "max_lat": "最大纬度",
    }
    vertices_supplied = "vertices" in data
    if vertices_supplied:
        vertices = normalise_polygon_vertices(
            data["vertices"],
            label="异常徘徊监控区",
        )
        cleaned["vertices"] = vertices
        cleaned.update(polygon_bounds(vertices))
    else:
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
        if not vertices_supplied and (
            not partial or any(field in data for field in AREA_FIELDS)
        ):
            cleaned["vertices"] = rectangle_vertices(
                values["min_lon"],
                values["min_lat"],
                values["max_lon"],
                values["max_lat"],
            )

    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ValueError("is_active 必须是布尔值")
        cleaned["is_active"] = data["is_active"]
    return cleaned


def _serialize_area(area):
    vertices = area.vertices or rectangle_vertices(
        area.min_lon,
        area.min_lat,
        area.max_lon,
        area.max_lat,
    )
    return {
        "id": area.id,
        "name": area.name,
        "min_lon": area.min_lon,
        "min_lat": area.min_lat,
        "max_lon": area.max_lon,
        "max_lat": area.max_lat,
        "bounds": [area.min_lon, area.min_lat, area.max_lon, area.max_lat],
        "vertices": vertices,
        "is_active": area.is_active,
    }


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
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (lon, lat)):
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

    try:
        speed = float(ship_info.get("speed"))
    except (TypeError, ValueError):
        speed = -1.0
    if not math.isfinite(speed) or not 0 <= speed <= 102.2:
        speed = -1.0
    raw_course = ship_info.get("course", ship_info.get("cog"))
    try:
        course = float(raw_course)
    except (TypeError, ValueError):
        course = -1.0
    if not math.isfinite(course) or not 0 <= course < 360:
        course = -1.0

    return {
        "mmsi": mmsi,
        "name": normalise_ais_name(ship_info.get("name")),
        "lon": lon,
        "lat": lat,
        # -1 is the model-compatible missing-value sentinel.  The trajectory
        # normaliser converts it back to None before speed/COG calculations.
        "speed": speed,
        "course": course,
        "timestamp": timestamp,
        "at_dock": _as_bool(ship_info.get("at_dock", False)),
        "matched_port_name": matched_port_name,
    }


def _is_normal_operation(ship):
    """Only an explicit docked state can reset history from one AIS point.

    Port and anchorage exclusions need window-level speed/range evidence and are
    therefore evaluated by ``run_loitering_analysis`` instead of being blanket
    exclusions here.
    """
    return ship["at_dock"]


def _runtime_config():
    config = get_wandering_config()
    if MonitorRegion.objects.exists():
        config["monitored_areas"] = [
            {
                "name": region.name,
                "bounds": (
                    region.min_lon,
                    region.min_lat,
                    region.max_lon,
                    region.max_lat,
                ),
                "vertices": region.vertices or rectangle_vertices(
                    region.min_lon,
                    region.min_lat,
                    region.max_lon,
                    region.max_lat,
                ),
            }
            for region in MonitorRegion.objects.filter(is_active=True)
        ]
    return config


@require_http_methods(["GET", "POST"])
def monitor_area_collection(request):
    if request.method == "GET":
        areas = MonitorRegion.objects.all()
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
                        "min_points",
                        "min_duration_minutes",
                        "max_range_metres",
                        "min_path_distance_metres",
                        "min_leg_distance_metres",
                        "min_turn_angle_degrees",
                        "min_turn_count",
                        "max_displacement_ratio",
                        "min_revisit_ratio",
                        "grid_size_metres",
                        "revisit_enabled",
                        "min_speed_knots",
                        "max_valid_speed_knots",
                        "max_jump_speed_knots",
                        "min_jump_distance_metres",
                        "max_gap_minutes",
                        "min_turn_interval_seconds",
                        "monitored_only",
                    )
                },
            }
        )

    try:
        data = _validate_area_data(_read_json(request))
        area = MonitorRegion.objects.create(**data)
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("异常徘徊监控区保存失败", status=409)
    return JsonResponse(
        {
            "success": True,
            "message": "异常徘徊监控区创建成功",
            "result": _serialize_area(area),
        },
        status=201,
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def monitor_area_detail(request, area_id):
    try:
        area = MonitorRegion.objects.get(pk=area_id)
    except MonitorRegion.DoesNotExist:
        return _json_error("异常徘徊监控区不存在", status=404)

    if request.method == "GET":
        return JsonResponse({"success": True, "result": _serialize_area(area)})
    if request.method == "DELETE":
        if MonitorRegion.objects.count() <= 1:
            return _json_error(
                "至少需要保留一个监控区；如需停止检测，请停用该区域",
                status=409,
            )
        area.delete()
        return JsonResponse(
            {"success": True, "message": "异常徘徊监控区删除成功"}
        )

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
        return _json_error("异常徘徊监控区保存失败", status=409)
    return JsonResponse(
        {
            "success": True,
            "message": "异常徘徊监控区更新成功",
            "result": _serialize_area(area),
        }
    )


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


def _store_points(ships):
    if not ships:
        return
    mmsis = {ship["mmsi"] for ship in ships}
    timestamps = {ship["timestamp"] for ship in ships}
    existing = set(
        TrajectoryBuffer.objects.filter(
            mmsi__in=mmsis,
            timestamp__in=timestamps,
        ).values_list("mmsi", "timestamp")
    )
    rows = [
        TrajectoryBuffer(
            mmsi=ship["mmsi"],
            name=ship["name"],
            longitude=ship["lon"],
            latitude=ship["lat"],
            course=ship["course"],
            speed=ship["speed"],
            timestamp=ship["timestamp"],
        )
        for ship in ships
        if (ship["mmsi"], ship["timestamp"]) not in existing
    ]
    if rows:
        TrajectoryBuffer.objects.bulk_create(rows)


@csrf_exempt
def receive_realtime_point(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    config = _runtime_config()
    ships_by_key = {}
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info)
        if ship is None:
            skipped_count += 1
            continue
        ships_by_key[(ship["mmsi"], ship["timestamp"])] = ship
    ships = sorted(
        ships_by_key.values(),
        key=lambda ship: (ship["timestamp"], ship["mmsi"]),
    )
    if not ships:
        return _empty_response(skipped_count=skipped_count)
    latest_timestamp = max(ship["timestamp"] for ship in ships)

    active_points = {}
    reset_mmsis = set()
    for ship in ships:
        mmsi = ship["mmsi"]
        monitored_area = get_monitored_area(
            ship["lat"],
            ship["lon"],
            config["monitored_areas"],
        )
        if (
            config["monitored_only"] and monitored_area is None
        ) or _is_normal_operation(ship):
            active_points[mmsi] = []
            reset_mmsis.add(mmsi)
            continue

        active_points.setdefault(mmsi, []).append(ship)

    if reset_mmsis:
        TrajectoryBuffer.objects.filter(mmsi__in=reset_mmsis).delete()
    points_to_store = [
        ship for points in active_points.values() for ship in points
    ]
    _store_points(points_to_store)
    active_latest = {
        mmsi: points[-1]
        for mmsi, points in active_points.items()
        if points
    }

    touched_mmsis = set(active_points)
    results_by_mmsi = {}
    event_state = {}
    cached_events = cache.get(WANDERING_EVENT_CACHE_KEY, {})
    if isinstance(cached_events, dict):
        for mmsi, event in cached_events.items():
            if mmsi in touched_mmsis or not isinstance(event, dict):
                continue
            last_seen = _parse_timestamp(event.get("last_seen"))
            result = event.get("result")
            if (
                last_seen is None
                or (latest_timestamp - last_seen).total_seconds()
                > config["max_gap_minutes"] * 60
                or not isinstance(result, dict)
            ):
                continue
            results_by_mmsi[mmsi] = result
            event_state[mmsi] = event
    for mmsi, ship in active_latest.items():
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

        analysis = run_loitering_analysis(
            recent_points,
            config,
            normal_behavior={
                "at_dock": ship["at_dock"],
                "matched_port_name": ship["matched_port_name"],
            },
        )
        if not analysis["is_abnormal"]:
            continue

        last_segment = analysis["abnormal_segments"][-1]
        location = last_segment["location"]
        details = (
            f"检测为异常徘徊：船舶在{last_segment['area']}内"
            f"{last_segment['duration_minutes']:.1f}分钟航行约"
            f"{last_segment['path_distance_metres']:.0f}米，"
            f"最大活动跨度{last_segment['range_metres']:.0f}米，"
            f"出现{last_segment['turn_count']}次明显转向，"
            f"直线位移/轨迹长度比为"
            f"{last_segment['displacement_ratio']:.2f}，"
            f"重复区域访问率为{last_segment['revisit_ratio']:.2f}。"
        )
        result = {
                "mmsi": mmsi,
                "location": [location["lon"], location["lat"]],
                "name": ship["name"],
                "duration_minutes": last_segment["duration_minutes"],
                "turn_count": last_segment["turn_count"],
                "path_distance_metres": last_segment[
                    "path_distance_metres"
                ],
                "range_metres": last_segment["range_metres"],
                "displacement_metres": last_segment[
                    "displacement_metres"
                ],
                "displacement_ratio": last_segment[
                    "displacement_ratio"
                ],
                "revisit_ratio": last_segment["revisit_ratio"],
                "trajectory_abnormal_count": last_segment[
                    "trajectory_abnormal_count"
                ],
                "trajectory_conditions": last_segment[
                    "trajectory_conditions"
                ],
                "area": last_segment["area"],
                "details": details,
                "detail": details,
            }
        results_by_mmsi[mmsi] = result
        event_state[mmsi] = {
            "last_seen": ship["timestamp"].isoformat(),
            "result": result,
        }

    cleanup_time = latest_timestamp - timedelta(
        minutes=config["retention_window_minutes"]
    )
    TrajectoryBuffer.objects.filter(timestamp__lt=cleanup_time).delete()
    results = sorted(
        results_by_mmsi.values(), key=lambda result: result["mmsi"]
    )
    cache.set(
        WANDERING_EVENT_CACHE_KEY,
        event_state,
        timeout=max(3600, config["retention_window_minutes"] * 120),
    )

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
                    "max_range_metres",
                    "min_path_distance_metres",
                    "min_leg_distance_metres",
                    "min_turn_angle_degrees",
                    "min_turn_count",
                    "max_displacement_ratio",
                    "min_revisit_ratio",
                    "grid_size_metres",
                    "revisit_enabled",
                    "min_speed_knots",
                    "max_valid_speed_knots",
                    "max_jump_speed_knots",
                    "min_jump_distance_metres",
                    "min_turn_interval_seconds",
                    "max_gap_minutes",
                    "monitored_only",
                    "min_area_point_ratio",
                )
            },
            "message": "检测成功",
        }
    )
