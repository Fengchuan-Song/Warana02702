import json
import math
from collections import defaultdict
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import IntegrityError, connection
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods

from AISData.behavior_recognition import behavior_results_for_request
from AISData.normalization import normalise_ais_name
from AISData.low_speed_behavior import BEHAVIOR_ANCHORING
from AISData.maritime_zones import (
    MaritimeZoneDataError,
    PORT_ZONE_TYPES,
    zones_containing_point,
)
from AISData.monitor_area_geometry import (
    normalise_polygon_vertices,
    polygon_bounds,
    rectangle_vertices,
)

from .models import IllegalStayingMonitorArea, StayingBuffer
from .utils import (
    get_forbidden_area,
    get_staying_config,
    staying_event_from_behavior_result,
)


ILLEGAL_STAYING_EVENT_CACHE_KEY = "illegal_staying:event_state:v1"
AREA_FIELDS = ("min_lon", "min_lat", "max_lon", "max_lat")


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

    if "reason" in data:
        reason = str(data["reason"] or "").strip()
        if not reason:
            raise ValueError("禁停原因不能为空")
        if len(reason) > 1000:
            raise ValueError("禁停原因不能超过1000个字符")
        cleaned["reason"] = reason
    elif not partial:
        raise ValueError("禁停原因不能为空")

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
            label="非法驻留监控区",
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
        "reason": area.reason,
        "min_lon": area.min_lon,
        "min_lat": area.min_lat,
        "max_lon": area.max_lon,
        "max_lat": area.max_lat,
        "bounds": [area.min_lon, area.min_lat, area.max_lon, area.max_lat],
        "vertices": vertices,
        "is_active": area.is_active,
        "created_at": area.created_at.isoformat(),
        "updated_at": area.updated_at.isoformat(),
    }


def _runtime_config():
    config = get_staying_config()
    if IllegalStayingMonitorArea.objects.exists():
        config["forbidden_areas"] = [
            {
                "name": area.name,
                "reason": area.reason,
                "bounds": (area.min_lon, area.min_lat, area.max_lon, area.max_lat),
                "vertices": area.vertices or rectangle_vertices(
                    area.min_lon,
                    area.min_lat,
                    area.max_lon,
                    area.max_lat,
                ),
            }
            for area in IllegalStayingMonitorArea.objects.filter(is_active=True)
        ]
    return config


@require_http_methods(["GET", "POST"])
def monitor_area_collection(request):
    if request.method == "GET":
        areas = IllegalStayingMonitorArea.objects.all()
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
        area = IllegalStayingMonitorArea.objects.create(**data)
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("已存在同名非法驻留监控区", status=409)
    return JsonResponse(
        {
            "success": True,
            "message": "非法驻留监控区创建成功",
            "result": _serialize_area(area),
        },
        status=201,
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def monitor_area_detail(request, area_id):
    try:
        area = IllegalStayingMonitorArea.objects.get(pk=area_id)
    except IllegalStayingMonitorArea.DoesNotExist:
        return _json_error("非法驻留监控区不存在", status=404)

    if request.method == "GET":
        return JsonResponse({"success": True, "result": _serialize_area(area)})
    if request.method == "DELETE":
        if IllegalStayingMonitorArea.objects.count() <= 1:
            return _json_error(
                "至少需要保留一个监控区；如需停止检测，请停用该区域",
                status=409,
            )
        area.delete()
        return JsonResponse({"success": True, "message": "非法驻留监控区删除成功"})

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
        return _json_error("已存在同名非法驻留监控区", status=409)
    return JsonResponse(
        {
            "success": True,
            "message": "非法驻留监控区更新成功",
            "result": _serialize_area(area),
        }
    )


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
    if (
        not -180 <= lon <= 180
        or not -90 <= lat <= 90
        or speed < 0
        or speed > 102.2
    ):
        return None
    timestamp = _parse_timestamp(ship_info.get("timestamp"))
    if timestamp is None:
        return None

    raw_port_name = (
        ship_info.get("matched_port_name")
        or ship_info.get("matchedPortName")
        or ""
    )
    matched_port_name = str(raw_port_name).strip()
    if matched_port_name.lower() in {"nan", "none", "null"}:
        matched_port_name = ""
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
    try:
        in_port_basin = bool(
            zones_containing_point(lon, lat, zone_types=PORT_ZONE_TYPES)
        )
    except MaritimeZoneDataError:
        in_port_basin = False
    in_port_basin = in_port_basin or bool(matched_port_name)

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
        "in_port_basin": in_port_basin,
    }


def _upsert_points(ships):
    if not ships:
        return
    points = [
        StayingBuffer(
            mmsi=ship["mmsi"],
            name=ship["name"],
            longitude=ship["lon"],
            latitude=ship["lat"],
            speed=ship["speed"],
            heading=ship["heading"],
            nav_status=ship["nav_status"],
            at_dock=ship["at_dock"],
            matched_port_name=ship["matched_port_name"],
            in_port_basin=ship["in_port_basin"],
            zone_name=ship["area"]["name"],
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
            "heading",
            "nav_status",
            "at_dock",
            "matched_port_name",
            "in_port_basin",
            "zone_name",
        ],
    }
    if connection.features.supports_update_conflicts_with_target:
        options["unique_fields"] = ["mmsi", "timestamp"]
    StayingBuffer.objects.bulk_create(points, **options)


def _load_trajectories(mmsis, start_time, end_time):
    trajectories = defaultdict(list)
    rows = (
        StayingBuffer.objects.filter(
            mmsi__in=mmsis,
            timestamp__gte=start_time,
            timestamp__lte=end_time,
        )
        .order_by("mmsi", "timestamp")
        .values(
            "mmsi",
            "longitude",
            "latitude",
            "speed",
            "heading",
            "nav_status",
            "at_dock",
            "matched_port_name",
            "in_port_basin",
            "zone_name",
            "timestamp",
        )
    )
    for row in rows:
        trajectories[row["mmsi"]].append(row)
    return trajectories


def _previous_events(reference_time, retention_minutes):
    cached = cache.get(ILLEGAL_STAYING_EVENT_CACHE_KEY, {})
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
            "type": "疑似非法驻留预警",
            "timestamp": None,
            "count": 0,
            "results": [],
            "skipped_count": skipped_count,
            "stale_count": 0,
            "message": message,
        }
    )


@require_GET
def detectIllegalStaying(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    config = _runtime_config()
    ships_by_mmsi = {}
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info)
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
    shared_results = behavior_results_for_request(
        list(ships_by_mmsi.values()),
        getattr(request, "ais_context", None),
    )
    candidates = []
    reset_mmsis = []
    stale_count = 0
    for ship in ships_by_mmsi.values():
        age_seconds = (reference_time - ship["timestamp"]).total_seconds()
        if age_seconds > config["max_position_age_seconds"]:
            stale_count += 1
            reset_mmsis.append(ship["mmsi"])
            continue
        area = get_forbidden_area(
            ship["lon"],
            ship["lat"],
            config["forbidden_areas"],
        )
        if (
            area is None
            or ship["speed"] > config["max_speed_knots"]
        ):
            reset_mmsis.append(ship["mmsi"])
            continue
        ship["area"] = area
        candidates.append(ship)

    retention_start = reference_time - timedelta(
        minutes=config["retention_window_minutes"]
    )
    future_limit = reference_time + timedelta(
        seconds=config["future_tolerance_seconds"]
    )
    StayingBuffer.objects.filter(timestamp__lt=retention_start).delete()
    StayingBuffer.objects.filter(timestamp__gt=future_limit).delete()
    if reset_mmsis:
        StayingBuffer.objects.filter(mmsi__in=reset_mmsis).delete()
    _upsert_points(candidates)

    previous_events = _previous_events(
        reference_time,
        config["event_retention_minutes"],
    )

    results = []
    active_events = {}
    for ship in candidates:
        event = staying_event_from_behavior_result(
            shared_results.get(ship["mmsi"]), ship["area"], config
        )
        if event is None:
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
            and previous.get("zone_name") == ship["area"]["name"]
            and event["started_at"] <= previous_last_seen
            and ship["timestamp"] >= previous_last_seen
            and (
                ship["timestamp"] - previous_last_seen
            ).total_seconds()
            <= config["maximum_gap_seconds"]
        )
        started_at = (
            previous.get("started_at")
            if same_event and previous.get("started_at")
            else event["started_at"].isoformat()
        )
        started_timestamp = _parse_timestamp(started_at)
        duration_minutes = (
            (ship["timestamp"] - started_timestamp).total_seconds() / 60
            if started_timestamp is not None
            else event["duration_minutes"]
        )
        event_id = (
            f"illegal-staying:{ship['mmsi']}:"
            f"{ship['area']['name']}:{started_at}"
        )
        behavior_label = (
            "锚泊行为同时命中非法驻留规则"
            if event["behavior"] == BEHAVIOR_ANCHORING
            else "普通驻留行为"
        )
        details = (
            f"疑似非法驻留：{ship['area']['reason']}；"
            f"所在区域：{ship['area']['name']}；"
            f"行为分类：{behavior_label}；"
            f"航速{ship['speed']:.2f}节，在约"
            f"{config['distance_threshold_metres']:.0f}米范围内已持续"
            f"{duration_minutes:.1f}分钟。"
            "该结果为AIS规则预警，需结合驻留许可和现场执法确认。"
        )
        results.append(
            {
                "mmsi": ship["mmsi"],
                "name": ship["name"],
                "location": [ship["lon"], ship["lat"]],
                "event": "IllegalStaying",
                "risk": "高风险",
                "zone": ship["area"]["name"],
                "reason": ship["area"]["reason"],
                "speed_knots": round(ship["speed"], 2),
                "duration_minutes": round(duration_minutes, 2),
                "point_count": event["point_count"],
                "average_speed_knots": round(
                    event["average_speed_knots"], 2
                ),
                "max_speed_knots": round(
                    event["max_speed_knots"], 2
                ),
                "behavior": event["behavior"],
                "behavior_reason": event["behavior_reason"],
                "position_swing": event["position_swing"],
                "related_primary_feature_id": (
                    "detect-illegalAnchored"
                    if event["behavior"] == BEHAVIOR_ANCHORING
                    else None
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
            "zone_name": ship["area"]["name"],
            "last_seen": ship["timestamp"].isoformat(),
        }

    cache.set(
        ILLEGAL_STAYING_EVENT_CACHE_KEY,
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
            "type": "疑似非法驻留预警",
            "timestamp": reference_time.isoformat(),
            "count": len(results),
            "results": results,
            "skipped_count": skipped_count,
            "stale_count": stale_count,
            "rule": {
                "max_speed_knots": config["max_speed_knots"],
                "distance_threshold_metres": config[
                    "distance_threshold_metres"
                ],
                "min_duration_minutes": config[
                    "min_duration_minutes"
                ],
                "min_points": config["min_points"],
                "maximum_gap_seconds": config[
                    "maximum_gap_seconds"
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
            },
            "message": "检测成功",
        }
    )
