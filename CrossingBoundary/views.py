import json
import hashlib
import math
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from AISData.normalization import normalise_ais_name

from .models import ElectronicFence


EARTH_RADIUS_METRES = 6_371_000.0
CROSSING_STATE_CACHE_KEY = "crossing_boundary:state:v1"
CROSSING_EVENT_CACHE_KEY = "crossing_boundary:events:v1"
FENCE_MODES = {"enter", "exit", "both"}
FENCE_MODE_LABELS = {
    "enter": "禁止驶入",
    "exit": "禁止驶出",
    "both": "进出均预警",
}
DEFAULT_CROSSING_CONFIG = {
    "max_position_age_seconds": 120.0,
    "boundary_tolerance_metres": 10.0,
    "state_retention_hours": 24.0,
    "event_retention_minutes": 30.0,
    "max_vertices": 500.0,
}


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _crossing_config():
    config = DEFAULT_CROSSING_CONFIG.copy()
    configured = getattr(settings, "CROSSING_BOUNDARY_DETECTION", {})
    if isinstance(configured, dict):
        for key in config:
            value = _finite_float(configured.get(key))
            if value is not None and value >= 0:
                config[key] = value
    config["max_vertices"] = max(3, int(config["max_vertices"]))
    return config


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


def _orientation(first, second, third, epsilon=1e-12):
    value = (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )
    if abs(value) <= epsilon:
        return 0
    return 1 if value > 0 else -1


def _point_on_segment(lon, lat, start, end, epsilon=1e-10):
    point = (lon, lat)
    if _orientation(start, end, point, epsilon) != 0:
        return False
    return (
        min(start[0], end[0]) - epsilon
        <= lon
        <= max(start[0], end[0]) + epsilon
        and min(start[1], end[1]) - epsilon
        <= lat
        <= max(start[1], end[1]) + epsilon
    )


def _segments_intersect(first_start, first_end, second_start, second_end):
    orientations = (
        _orientation(first_start, first_end, second_start),
        _orientation(first_start, first_end, second_end),
        _orientation(second_start, second_end, first_start),
        _orientation(second_start, second_end, first_end),
    )
    if orientations[0] != orientations[1] and orientations[2] != orientations[3]:
        return True
    return any(
        (
            orientations[0] == 0
            and _point_on_segment(*second_start, first_start, first_end),
            orientations[1] == 0
            and _point_on_segment(*second_end, first_start, first_end),
            orientations[2] == 0
            and _point_on_segment(*first_start, second_start, second_end),
            orientations[3] == 0
            and _point_on_segment(*first_end, second_start, second_end),
        )
    )


def _polygon_area(vertices):
    return abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(
                vertices, vertices[1:] + vertices[:1]
            )
        )
    ) / 2


def _validate_simple_polygon(vertices):
    count = len(vertices)
    if _polygon_area(vertices) <= 1e-12:
        raise ValueError("电子围栏顶点不能共线或形成零面积区域")

    for index in range(count):
        first_start = vertices[index]
        first_end = vertices[(index + 1) % count]
        for other_index in range(index + 1, count):
            if (
                other_index == index
                or other_index == (index + 1) % count
                or index == (other_index + 1) % count
            ):
                continue
            second_start = vertices[other_index]
            second_end = vertices[(other_index + 1) % count]
            if _segments_intersect(
                first_start,
                first_end,
                second_start,
                second_end,
            ):
                raise ValueError("电子围栏边线不能自相交")


def _normalise_vertices(value):
    if not isinstance(value, list):
        raise ValueError("顶点坐标必须是数组")

    max_vertices = _crossing_config()["max_vertices"]
    if len(value) > max_vertices + 1:
        raise ValueError(f"电子围栏最多允许 {max_vertices} 个顶点")

    vertices = []
    for index, point in enumerate(value, start=1):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"第 {index} 个顶点必须是 [经度, 纬度]")
        longitude = _finite_float(point[0])
        latitude = _finite_float(point[1])
        if longitude is None or latitude is None:
            raise ValueError(f"第 {index} 个顶点的经纬度必须是有限数字")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError(f"第 {index} 个顶点超出合法经纬度范围")
        vertices.append([longitude, latitude])

    if len(vertices) > 1 and vertices[0] == vertices[-1]:
        vertices.pop()
    if len(vertices) < 3 or len({tuple(point) for point in vertices}) < 3:
        raise ValueError("电子围栏至少需要 3 个不同顶点")
    if any(
        first == second
        for first, second in zip(vertices, vertices[1:] + vertices[:1])
    ):
        raise ValueError("电子围栏不能包含相邻的重复顶点")
    if max(point[0] for point in vertices) - min(
        point[0] for point in vertices
    ) > 180:
        raise ValueError("暂不支持跨越国际日期变更线的电子围栏")

    _validate_simple_polygon(vertices)
    return vertices


def _validate_fence_data(data, partial=False):
    cleaned = {}

    if "name" in data:
        name = str(data["name"]).strip()
        if not name:
            raise ValueError("围栏名称不能为空")
        if len(name) > 100:
            raise ValueError("围栏名称不能超过 100 个字符")
        cleaned["name"] = name
    elif not partial:
        raise ValueError("围栏名称不能为空")

    if "description" in data:
        description = str(data["description"] or "").strip()
        if len(description) > 1000:
            raise ValueError("围栏说明不能超过 1000 个字符")
        cleaned["description"] = description

    if "vertices" in data:
        cleaned["vertices"] = _normalise_vertices(data["vertices"])
    elif not partial:
        raise ValueError("请提供电子围栏顶点坐标")

    if "crossing_mode" in data:
        crossing_mode = str(data["crossing_mode"]).strip().lower()
        if crossing_mode not in FENCE_MODES:
            raise ValueError("crossing_mode 必须是 enter、exit 或 both")
        cleaned["crossing_mode"] = crossing_mode
    elif not partial:
        cleaned["crossing_mode"] = "enter"

    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ValueError("is_active 必须是布尔值")
        cleaned["is_active"] = data["is_active"]
    return cleaned


def _serialize_fence(fence):
    return {
        "id": fence.id,
        "name": fence.name,
        "description": fence.description,
        "vertices": fence.vertices,
        "crossing_mode": fence.crossing_mode,
        "crossing_mode_label": FENCE_MODE_LABELS.get(
            fence.crossing_mode, fence.crossing_mode
        ),
        "is_active": fence.is_active,
        "created_at": fence.created_at.isoformat(),
        "updated_at": fence.updated_at.isoformat(),
    }


@require_http_methods(["GET", "POST"])
def fence_collection(request):
    if request.method == "GET":
        fences = ElectronicFence.objects.all()
        return JsonResponse(
            {
                "success": True,
                "count": fences.count(),
                "results": [
                    _serialize_fence(fence) for fence in fences
                ],
            }
        )

    try:
        data = _validate_fence_data(_read_json(request))
        fence = ElectronicFence.objects.create(**data)
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("已存在同名电子围栏", status=409)
    return JsonResponse(
        {
            "success": True,
            "message": "电子围栏创建成功",
            "result": _serialize_fence(fence),
        },
        status=201,
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def fence_detail(request, fence_id):
    try:
        fence = ElectronicFence.objects.get(pk=fence_id)
    except ElectronicFence.DoesNotExist:
        return _json_error("电子围栏不存在", status=404)

    if request.method == "GET":
        return JsonResponse(
            {"success": True, "result": _serialize_fence(fence)}
        )
    if request.method == "DELETE":
        fence.delete()
        return JsonResponse(
            {"success": True, "message": "电子围栏删除成功"}
        )

    try:
        data = _validate_fence_data(
            _read_json(request),
            partial=request.method == "PATCH",
        )
        for field, value in data.items():
            setattr(fence, field, value)
        fence.save()
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("已存在同名电子围栏", status=409)
    return JsonResponse(
        {
            "success": True,
            "message": "电子围栏更新成功",
            "result": _serialize_fence(fence),
        }
    )


def point_in_polygon(lon, lat, vertices):
    """Return True for polygon interior and exact boundary points."""
    inside = False
    count = len(vertices)
    for index in range(count):
        start = vertices[index]
        end = vertices[(index + 1) % count]
        if _point_on_segment(lon, lat, start, end):
            return True
        x1, y1 = start
        x2, y2 = end
        if (y1 > lat) != (y2 > lat):
            intersection_lon = (
                (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            )
            if lon < intersection_lon:
                inside = not inside
    return inside


def _point_segment_distance_metres(lon, lat, start, end):
    latitude_radians = math.radians(lat)

    def local(point):
        return (
            math.radians(point[0] - lon)
            * EARTH_RADIUS_METRES
            * math.cos(latitude_radians),
            math.radians(point[1] - lat) * EARTH_RADIUS_METRES,
        )

    start_x, start_y = local(start)
    end_x, end_y = local(end)
    segment_x = end_x - start_x
    segment_y = end_y - start_y
    segment_length_squared = segment_x**2 + segment_y**2
    if segment_length_squared == 0:
        return math.hypot(start_x, start_y)
    projection = max(
        0.0,
        min(
            1.0,
            -(start_x * segment_x + start_y * segment_y)
            / segment_length_squared,
        ),
    )
    closest_x = start_x + projection * segment_x
    closest_y = start_y + projection * segment_y
    return math.hypot(closest_x, closest_y)


def classify_point(lon, lat, vertices, boundary_tolerance_metres=0):
    for index, start in enumerate(vertices):
        end = vertices[(index + 1) % len(vertices)]
        if _point_segment_distance_metres(lon, lat, start, end) <= (
            boundary_tolerance_metres
        ):
            return "boundary"
    return "inside" if point_in_polygon(lon, lat, vertices) else "outside"


def _segment_intersection_parameter(start, end, edge_start, edge_end):
    segment = (end[0] - start[0], end[1] - start[1])
    edge = (
        edge_end[0] - edge_start[0],
        edge_end[1] - edge_start[1],
    )
    denominator = segment[0] * edge[1] - segment[1] * edge[0]
    if abs(denominator) <= 1e-12:
        return None
    offset = (
        edge_start[0] - start[0],
        edge_start[1] - start[1],
    )
    segment_parameter = (
        offset[0] * edge[1] - offset[1] * edge[0]
    ) / denominator
    edge_parameter = (
        offset[0] * segment[1] - offset[1] * segment[0]
    ) / denominator
    if (
        0 <= segment_parameter <= 1
        and 0 <= edge_parameter <= 1
    ):
        return segment_parameter
    return None


def segment_crosses_polygon(start, end, vertices):
    intersections = set()
    for index, edge_start in enumerate(vertices):
        edge_end = vertices[(index + 1) % len(vertices)]
        parameter = _segment_intersection_parameter(
            start, end, edge_start, edge_end
        )
        if parameter is not None:
            intersections.add(round(parameter, 10))
    return len(intersections) >= 2


def _parse_timestamp(value):
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = parse_datetime(value)
    else:
        timestamp = None
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt_timezone.utc)
    return timestamp


def _normalise_ship(ship_info):
    if not isinstance(ship_info, dict):
        return None
    mmsi = str(ship_info.get("mmsi") or "").strip()
    longitude = _finite_float(
        ship_info.get("lon", ship_info.get("longitude"))
    )
    latitude = _finite_float(
        ship_info.get("lat", ship_info.get("latitude"))
    )
    timestamp = _parse_timestamp(ship_info.get("timestamp"))
    if (
        not mmsi
        or longitude is None
        or latitude is None
        or timestamp is None
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        return None
    return {
        "mmsi": mmsi,
        "name": normalise_ais_name(ship_info.get("name")),
        "longitude": longitude,
        "latitude": latitude,
        "timestamp": timestamp,
    }


def _compile_fence(fence):
    vertices = _normalise_vertices(fence.vertices)
    geometry_version = hashlib.sha256(
        json.dumps(
            vertices,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return {
        "object": fence,
        "vertices": vertices,
        "bounds": (
            min(point[0] for point in vertices),
            min(point[1] for point in vertices),
            max(point[0] for point in vertices),
            max(point[1] for point in vertices),
        ),
        # Only geometry changes invalidate the positional state. Updating a
        # name, description or warning mode must not hide the next crossing.
        "version": geometry_version,
        # Accept one state written by the previous release so deployment does
        # not force every vessel through another first-observation cycle.
        "legacy_version": fence.updated_at.isoformat(),
    }


def _classify_with_bounds(ship, compiled, tolerance_metres):
    min_lon, min_lat, max_lon, max_lat = compiled["bounds"]
    latitude_margin = math.degrees(
        tolerance_metres / EARTH_RADIUS_METRES
    )
    longitude_margin = latitude_margin / max(
        abs(math.cos(math.radians(ship["latitude"]))), 1e-9
    )
    if not (
        min_lon - longitude_margin
        <= ship["longitude"]
        <= max_lon + longitude_margin
        and min_lat - latitude_margin
        <= ship["latitude"]
        <= max_lat + latitude_margin
    ):
        return "outside"
    return classify_point(
        ship["longitude"],
        ship["latitude"],
        compiled["vertices"],
        tolerance_metres,
    )


def _event_result(ship, previous, fence, direction):
    direction_labels = {
        "enter": "驶入",
        "exit": "驶出",
        "transit": "穿越",
    }
    direction_label = direction_labels[direction]
    details = (
        f"检测到海上围栏越界：{ship['name']}（{ship['mmsi']}）"
        f"{direction_label}电子围栏“{fence.name}”；"
        f"围栏规则为"
        f"{FENCE_MODE_LABELS.get(fence.crossing_mode, fence.crossing_mode)}。"
    )
    timestamp_text = ship["timestamp"].isoformat()
    return {
        "mmsi": ship["mmsi"],
        "name": ship["name"],
        "location": [ship["longitude"], ship["latitude"]],
        "previous_location": previous["location"],
        "event": "CrossingBoundary",
        "risk": "高风险",
        "fence_id": fence.id,
        "fence_name": fence.name,
        "crossing_mode": fence.crossing_mode,
        "crossing_direction": direction,
        "crossing_direction_label": direction_label,
        "event_id": (
            f"crossing-boundary:{fence.id}:{ship['mmsi']}:"
            f"{direction}:{timestamp_text}"
        ),
        "is_new": True,
        "first_detected_at": timestamp_text,
        "details": details,
        "detail": details,
    }


def _empty_response(
    timestamp=None,
    results=None,
    new_count=0,
    skipped_count=0,
    stale_count=0,
    initialised_state_count=0,
    invalid_fence_count=0,
):
    results = results or []
    return JsonResponse(
        {
            "success": True,
            "type": "海上围栏越界",
            "timestamp": timestamp.isoformat() if timestamp else None,
            "count": len(results),
            "new_count": new_count,
            "results": results,
            "skipped_count": skipped_count,
            "stale_count": stale_count,
            "initialised_state_count": initialised_state_count,
            "invalid_fence_count": invalid_fence_count,
            "message": "检测成功" if timestamp else "暂无有效 AIS 数据",
        }
    )


def _retain_recent_events(
    reference_time,
    new_results,
    retention_minutes,
    fence_modes=None,
):
    fence_modes = {
        str(key): value
        for key, value in (fence_modes or {}).items()
    }
    retained = {}
    cached_events = cache.get(CROSSING_EVENT_CACHE_KEY, [])
    if isinstance(cached_events, list):
        for event in cached_events:
            if not isinstance(event, dict):
                continue
            if fence_modes:
                current_mode = fence_modes.get(str(event.get("fence_id")))
                direction = event.get("crossing_direction")
                if current_mode is None or not (
                    direction == "transit"
                    or direction == current_mode
                    or current_mode == "both"
                ):
                    # A rule change must immediately stop publishing a cached
                    # result that the current fence would no longer emit.
                    continue
            event_timestamp = _parse_timestamp(
                event.get("first_detected_at")
            )
            if event_timestamp is None:
                continue
            age_seconds = (reference_time - event_timestamp).total_seconds()
            if 0 <= age_seconds <= retention_minutes * 60:
                retained_event = event.copy()
                retained_event["is_new"] = False
                retained[retained_event.get("event_id")] = retained_event

    for event in new_results:
        retained[event["event_id"]] = event

    results = sorted(
        retained.values(),
        key=lambda item: (
            item.get("first_detected_at", ""),
            item.get("fence_id", 0),
            item.get("mmsi", ""),
        ),
        reverse=True,
    )
    cache.set(
        CROSSING_EVENT_CACHE_KEY,
        results,
        timeout=max(60, int(retention_minutes * 120)),
    )
    return results


def detect_crossing_boundary(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    ships = []
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info)
        if ship is None:
            skipped_count += 1
        else:
            ships.append(ship)
    if not ships:
        return _empty_response(skipped_count=skipped_count)

    points_by_mmsi = {}
    for ship in ships:
        timestamp_key = ship["timestamp"].isoformat()
        points_by_mmsi.setdefault(ship["mmsi"], {})[
            timestamp_key
        ] = ship

    reference_time = max(
        ship["timestamp"] for ship in ships
    )
    config = _crossing_config()
    fresh_ships = []
    stale_count = 0
    for timestamp_points in points_by_mmsi.values():
        ordered_points = sorted(
            timestamp_points.values(),
            key=lambda item: item["timestamp"],
        )
        latest_point = ordered_points[-1]
        if (
            reference_time - latest_point["timestamp"]
        ).total_seconds() > config["max_position_age_seconds"]:
            stale_count += 1
        else:
            # Preserve every point for an active vessel. Dropping the
            # intermediate points loses outside -> inside -> outside events
            # whenever several AIS frames reach the detector together.
            fresh_ships.extend(ordered_points)
    fresh_ships.sort(
        key=lambda item: (item["timestamp"], item["mmsi"])
    )

    fences = []
    invalid_fence_count = 0
    for fence in ElectronicFence.objects.filter(is_active=True):
        try:
            fences.append(_compile_fence(fence))
        except (TypeError, ValueError):
            invalid_fence_count += 1
    if not fresh_ships or not fences:
        return _empty_response(
            timestamp=reference_time,
            skipped_count=skipped_count,
            stale_count=stale_count,
            invalid_fence_count=invalid_fence_count,
        )

    cached_state = cache.get(CROSSING_STATE_CACHE_KEY, {})
    if not isinstance(cached_state, dict):
        cached_state = {}
    state = {}
    retention_seconds = config["state_retention_hours"] * 3600
    active_fence_ids = {
        str(compiled["object"].id) for compiled in fences
    }
    for key, value in cached_state.items():
        if not isinstance(value, dict):
            continue
        if key.split(":", 1)[0] not in active_fence_ids:
            continue
        last_timestamp = _parse_timestamp(value.get("timestamp"))
        if last_timestamp is None:
            continue
        age_seconds = (reference_time - last_timestamp).total_seconds()
        if 0 <= age_seconds <= retention_seconds:
            state[key] = value

    results = []
    initialised_state_count = 0
    for ship in fresh_ships:
        for compiled in fences:
            fence = compiled["object"]
            key = f"{fence.id}:{ship['mmsi']}"
            current_classification = _classify_with_bounds(
                ship,
                compiled,
                config["boundary_tolerance_metres"],
            )
            previous = state.get(key)
            previous_version = (
                previous.get("fence_version")
                if previous is not None
                else None
            )
            if (
                previous is None
                or previous_version
                not in {
                    compiled["version"],
                    compiled["legacy_version"],
                }
            ):
                state[key] = {
                    "stable_inside": (
                        current_classification == "inside"
                        if current_classification != "boundary"
                        else None
                    ),
                    "location": [
                        ship["longitude"],
                        ship["latitude"],
                    ],
                    "stable_location": (
                        [
                            ship["longitude"],
                            ship["latitude"],
                        ]
                        if current_classification != "boundary"
                        else None
                    ),
                    "timestamp": ship["timestamp"].isoformat(),
                    "fence_version": compiled["version"],
                }
                initialised_state_count += 1
                continue

            previous_timestamp = _parse_timestamp(
                previous.get("timestamp")
            )
            if (
                previous_timestamp is None
                or ship["timestamp"] <= previous_timestamp
            ):
                continue

            previous_inside = previous.get("stable_inside")
            current_inside = (
                previous_inside
                if current_classification == "boundary"
                else current_classification == "inside"
            )
            previous_stable_location = (
                previous.get("stable_location")
                or previous.get("location")
            )
            direction = None
            if previous_inside is False and current_inside is True:
                direction = "enter"
            elif previous_inside is True and current_inside is False:
                direction = "exit"
            elif (
                previous_inside is None
                and current_classification != "boundary"
            ):
                # If the first usable fix was on the fence line, the first
                # stable movement still provides a crossing direction.
                direction = "enter" if current_inside else "exit"
            elif (
                previous_inside is False
                and current_inside is False
                and current_classification != "boundary"
                and segment_crosses_polygon(
                    previous_stable_location,
                    [ship["longitude"], ship["latitude"]],
                    compiled["vertices"],
                )
            ):
                direction = "transit"

            should_alert = (
                direction == "transit"
                or direction == fence.crossing_mode
                or fence.crossing_mode == "both"
            )
            if direction is not None and should_alert:
                event_previous = previous.copy()
                event_previous["location"] = previous_stable_location
                results.append(
                    _event_result(
                        ship,
                        event_previous,
                        fence,
                        direction,
                    )
                )

            state[key] = {
                "stable_inside": current_inside,
                "location": [ship["longitude"], ship["latitude"]],
                # Boundary fixes advance the last-seen timestamp, but they
                # must not erase the last stable point. It is needed to
                # recognise a complete crossing through a narrow fence.
                "stable_location": (
                    previous_stable_location
                    if current_classification == "boundary"
                    else [ship["longitude"], ship["latitude"]]
                ),
                "timestamp": ship["timestamp"].isoformat(),
                "fence_version": compiled["version"],
            }

    cache.set(
        CROSSING_STATE_CACHE_KEY,
        state,
        timeout=max(3600, int(retention_seconds * 2)),
    )
    new_count = len(results)
    results = _retain_recent_events(
        reference_time,
        results,
        config["event_retention_minutes"],
        fence_modes={
            compiled["object"].id: compiled["object"].crossing_mode
            for compiled in fences
        },
    )
    return _empty_response(
        timestamp=reference_time,
        results=results,
        new_count=new_count,
        skipped_count=skipped_count,
        stale_count=stale_count,
        initialised_state_count=initialised_state_count,
        invalid_fence_count=invalid_fence_count,
    )
