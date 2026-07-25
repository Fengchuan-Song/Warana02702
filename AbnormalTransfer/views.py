import bisect
import json
import math
from collections import defaultdict
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from AISData.detection import detection_cache_key
from AISData.normalization import normalise_ais_name

from .models import TransferOperationPlan, _valid_vessel_mmsi


EARTH_RADIUS_METRES = 6_371_000.0
ABNORMAL_TRANSFER_STATE_CACHE_KEY = "abnormal_transfer:tracking_state:v1"

DEFAULT_ABNORMAL_TRANSFER_CONFIG = {
    "base_contact_distance_metres": 100.0,
    "maximum_contact_distance_metres": 250.0,
    "length_distance_factor": 0.5,
    "contact_buffer_metres": 25.0,
    "maximum_candidate_speed_knots": 20.0,
    "maximum_relative_speed_knots": 1.0,
    "maximum_course_difference_degrees": 30.0,
    "course_check_minimum_speed_knots": 1.0,
    "minimum_duration_seconds": 120.0,
    "minimum_observations": 3.0,
    "maximum_gap_seconds": 90.0,
    "max_position_age_seconds": 120.0,
    "max_valid_speed_knots": 102.2,
    "speed_tolerance_knots": 0.1,
    "state_retention_minutes": 30.0,
}


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = parse_datetime(value)
    else:
        parsed = None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _config():
    config = DEFAULT_ABNORMAL_TRANSFER_CONFIG.copy()
    configured = getattr(settings, "ABNORMAL_TRANSFER_DETECTION", {})
    if isinstance(configured, dict):
        for key in config:
            value = _finite_float(configured.get(key))
            if value is not None and value >= 0:
                config[key] = value
    config["maximum_contact_distance_metres"] = max(
        config["maximum_contact_distance_metres"],
        config["base_contact_distance_metres"],
    )
    config["minimum_observations"] = max(
        2, int(config["minimum_observations"])
    )
    return config


def _normalise_ship(raw_ship, config):
    if not isinstance(raw_ship, dict):
        return None

    mmsi = str(raw_ship.get("mmsi") or "").strip()
    longitude = _finite_float(
        raw_ship.get("lon", raw_ship.get("longitude"))
    )
    latitude = _finite_float(
        raw_ship.get("lat", raw_ship.get("latitude"))
    )
    speed = _finite_float(raw_ship.get("speed"))
    course = _finite_float(raw_ship.get("course"))
    timestamp = _parse_timestamp(raw_ship.get("timestamp"))
    if (
        not _valid_vessel_mmsi(mmsi)
        or longitude is None
        or latitude is None
        or speed is None
        or timestamp is None
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
        or not 0 <= speed <= config["max_valid_speed_knots"]
    ):
        return None

    length = _finite_float(raw_ship.get("length"))
    return {
        "mmsi": mmsi,
        "name": normalise_ais_name(raw_ship.get("name")),
        "longitude": longitude,
        "latitude": latitude,
        "speed": speed,
        "course": course % 360 if course is not None else None,
        "length": length if length is not None and length > 0 else None,
        "timestamp": timestamp,
    }


def _distance_metres(first, second):
    latitude1 = math.radians(first["latitude"])
    latitude2 = math.radians(second["latitude"])
    latitude_delta = latitude2 - latitude1
    longitude_delta = math.radians(
        second["longitude"] - first["longitude"]
    )
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude1)
        * math.cos(latitude2)
        * math.sin(longitude_delta / 2) ** 2
    )
    value = min(1.0, max(0.0, value))
    return 2 * EARTH_RADIUS_METRES * math.atan2(
        math.sqrt(value), math.sqrt(1 - value)
    )


def _angular_difference(first, second):
    return abs((first - second + 180) % 360 - 180)


def _contact_distance(first, second, config):
    lengths = [
        ship["length"]
        for ship in (first, second)
        if ship["length"] is not None
    ]
    size_adjusted = config["base_contact_distance_metres"]
    if lengths:
        size_adjusted = (
            max(lengths) * config["length_distance_factor"]
            + config["contact_buffer_metres"]
        )
    return min(
        config["maximum_contact_distance_metres"],
        max(config["base_contact_distance_metres"], size_adjusted),
    )


def _looks_like_transfer(first, second, distance, threshold, config):
    if distance > threshold:
        return False
    if (
        first["speed"] > config["maximum_candidate_speed_knots"]
        or second["speed"] > config["maximum_candidate_speed_knots"]
        or abs(first["speed"] - second["speed"])
        > config["maximum_relative_speed_knots"]
    ):
        return False

    course_check_speed = config["course_check_minimum_speed_knots"]
    if (
        max(first["speed"], second["speed"]) >= course_check_speed
        and first["course"] is not None
        and second["course"] is not None
        and _angular_difference(first["course"], second["course"])
        > config["maximum_course_difference_degrees"]
    ):
        return False
    return True


def _candidate_pairs(ships, config):
    maximum_latitude_delta = math.degrees(
        config["maximum_contact_distance_metres"]
        / EARTH_RADIUS_METRES
    )
    ordered = sorted(ships, key=lambda ship: ship["latitude"])
    latitudes = [ship["latitude"] for ship in ordered]
    pairs = []

    for index, first in enumerate(ordered):
        upper = bisect.bisect_right(
            latitudes,
            first["latitude"] + maximum_latitude_delta,
            lo=index + 1,
        )
        for second in ordered[index + 1 : upper]:
            threshold = _contact_distance(first, second, config)
            distance = _distance_metres(first, second)
            if not _looks_like_transfer(
                first, second, distance, threshold, config
            ):
                continue
            vessel_a, vessel_b = sorted(
                (first, second), key=lambda ship: ship["mmsi"]
            )
            pairs.append(
                {
                    "pair": (
                        f"{vessel_a['mmsi']}:{vessel_b['mmsi']}"
                    ),
                    "vessel_a": vessel_a,
                    "vessel_b": vessel_b,
                    "distance_metres": distance,
                    "contact_threshold_metres": threshold,
                }
            )
    return pairs


def _load_plans(pairs):
    plans_by_pair = defaultdict(list)
    if not pairs:
        return plans_by_pair

    mmsis = {
        ship["mmsi"]
        for pair in pairs
        for ship in (pair["vessel_a"], pair["vessel_b"])
    }
    plans = TransferOperationPlan.objects.filter(
        is_active=True,
        vessel_a_mmsi__in=mmsis,
        vessel_b_mmsi__in=mmsis,
    )
    for plan in plans:
        plans_by_pair[plan.pair_key].append(plan)
    return plans_by_pair


def _select_plan(plans, timestamp):
    if not plans:
        return None, False
    in_time = [
        plan
        for plan in plans
        if plan.starts_at <= timestamp <= plan.ends_at
    ]
    if in_time:
        # If approvals overlap, any valid plan can authorize the operation.
        return max(in_time, key=lambda plan: plan.max_speed_knots), True

    def time_distance(plan):
        if timestamp < plan.starts_at:
            return (plan.starts_at - timestamp).total_seconds()
        return (timestamp - plan.ends_at).total_seconds()

    return min(plans, key=time_distance), False


def _violation(pair, plans, timestamp, config):
    plan, inside_time = _select_plan(plans, timestamp)
    maximum_speed = max(
        pair["vessel_a"]["speed"],
        pair["vessel_b"]["speed"],
    )
    codes = []
    reasons = []

    if plan is None:
        codes.append("unregistered_operation")
        reasons.append("未登记对应船舶的接驳作业计划")
    else:
        if not inside_time:
            codes.append("outside_operation_time")
            reasons.append("不在批准的作业时间内")
        if (
            maximum_speed
            > plan.max_speed_knots + config["speed_tolerance_knots"]
        ):
            codes.append("speed_exceeded")
            reasons.append(
                f"航速 {maximum_speed:.1f} 节超过"
                f"规定上限 {plan.max_speed_knots:.1f} 节"
            )

    return {
        "plan": plan,
        "inside_time": inside_time,
        "codes": codes,
        "reasons": reasons,
        "maximum_speed_knots": maximum_speed,
    }


def _load_state(timestamp, retention_minutes):
    state = cache.get(ABNORMAL_TRANSFER_STATE_CACHE_KEY, {})
    if not isinstance(state, dict):
        return {}
    retention_seconds = retention_minutes * 60
    retained = {}
    for pair_key, value in state.items():
        if not isinstance(value, dict):
            continue
        last_seen = _parse_timestamp(value.get("last_seen"))
        if last_seen is None:
            continue
        age = (timestamp - last_seen).total_seconds()
        if 0 <= age <= retention_seconds:
            retained[pair_key] = value
    return retained


def _event_id(pair_key, violation_first_seen):
    compact_time = violation_first_seen.replace(
        "-", "", 3
    ).replace(":", "", 2)
    return f"abnormal-transfer:{pair_key}:{compact_time}"


def _track_and_evaluate(
    pairs,
    plans_by_pair,
    fresh_mmsis,
    timestamp,
    config,
):
    state = _load_state(timestamp, config["state_retention_minutes"])
    current_pair_keys = {pair["pair"] for pair in pairs}
    for pair_key in list(state):
        first_mmsi, second_mmsi = pair_key.split(":", 1)
        if (
            pair_key not in current_pair_keys
            and first_mmsi in fresh_mmsis
            and second_mmsi in fresh_mmsis
        ):
            del state[pair_key]

    timestamp_text = timestamp.isoformat()
    confirmed_count = 0
    results = []
    for pair in pairs:
        pair_key = pair["pair"]
        previous = state.get(pair_key)
        last_seen = (
            _parse_timestamp(previous.get("last_seen"))
            if previous
            else None
        )
        gap_seconds = (
            (timestamp - last_seen).total_seconds()
            if last_seen is not None
            else None
        )
        continuous = (
            previous is not None
            and gap_seconds is not None
            and 0 < gap_seconds <= config["maximum_gap_seconds"]
        )
        if continuous:
            first_seen = previous["first_seen"]
            observations = int(previous.get("observations", 1)) + 1
        elif previous is not None and gap_seconds == 0:
            first_seen = previous["first_seen"]
            observations = int(previous.get("observations", 1))
        else:
            first_seen = timestamp_text
            observations = 1
            previous = None

        first_seen_time = _parse_timestamp(first_seen) or timestamp
        duration_seconds = max(
            0.0, (timestamp - first_seen_time).total_seconds()
        )
        confirmed = (
            duration_seconds >= config["minimum_duration_seconds"]
            and observations >= config["minimum_observations"]
        )
        state_value = {
            "first_seen": first_seen,
            "last_seen": timestamp_text,
            "observations": observations,
        }
        if not confirmed:
            state[pair_key] = state_value
            continue

        confirmed_count += 1
        violation = _violation(
            pair,
            plans_by_pair.get(pair_key, []),
            timestamp,
            config,
        )
        if not violation["codes"]:
            state[pair_key] = state_value
            continue

        signature = ":".join(violation["codes"])
        previous_signature = (
            previous.get("violation_signature") if previous else None
        )
        if previous_signature == signature:
            violation_first_seen = previous["violation_first_seen"]
            alerted = bool(previous.get("alerted"))
        else:
            violation_first_seen = timestamp_text
            alerted = False

        state_value.update(
            {
                "violation_signature": signature,
                "violation_first_seen": violation_first_seen,
                "alerted": True,
            }
        )
        state[pair_key] = state_value

        vessel_a = pair["vessel_a"]
        vessel_b = pair["vessel_b"]
        plan = violation["plan"]
        reasons_text = "；".join(violation["reasons"])
        details = (
            f"检测到{vessel_a['name']}（{vessel_a['mmsi']}）与"
            f"{vessel_b['name']}（{vessel_b['mmsi']}）疑似异常接驳："
            f"{reasons_text}；接驳状态已持续"
            f" {duration_seconds / 60:.1f} 分钟，"
            f"当前船距 {pair['distance_metres']:.0f} 米。"
        )
        results.append(
            {
                "mmsi": vessel_a["mmsi"],
                "other_mmsi": vessel_b["mmsi"],
                "pair": pair_key,
                "pair_mmsi": [
                    vessel_a["mmsi"],
                    vessel_b["mmsi"],
                ],
                "name": f"{vessel_a['name']} / {vessel_b['name']}",
                "location": [
                    round(
                        (
                            vessel_a["longitude"]
                            + vessel_b["longitude"]
                        )
                        / 2,
                        6,
                    ),
                    round(
                        (
                            vessel_a["latitude"]
                            + vessel_b["latitude"]
                        )
                        / 2,
                        6,
                    ),
                ],
                "ship_locations": [
                    [vessel_a["longitude"], vessel_a["latitude"]],
                    [vessel_b["longitude"], vessel_b["latitude"]],
                ],
                "event": "AbnormalTransfer",
                "risk": "高风险",
                "violation_codes": violation["codes"],
                "violation_reasons": violation["reasons"],
                "operation_plan_id": plan.id if plan else None,
                "operation_name": plan.operation_name if plan else "",
                "approval_number": plan.approval_number if plan else "",
                "scheduled_start": (
                    plan.starts_at.isoformat() if plan else None
                ),
                "scheduled_end": (
                    plan.ends_at.isoformat() if plan else None
                ),
                "max_allowed_speed_knots": (
                    plan.max_speed_knots if plan else None
                ),
                "maximum_pair_speed_knots": round(
                    violation["maximum_speed_knots"], 2
                ),
                "distance_metres": round(
                    pair["distance_metres"], 1
                ),
                "contact_threshold_metres": round(
                    pair["contact_threshold_metres"], 1
                ),
                "duration_seconds": round(duration_seconds, 1),
                "duration_minutes": round(duration_seconds / 60, 2),
                "observations": observations,
                "event_id": _event_id(
                    pair_key, violation_first_seen
                ),
                "is_new": not alerted,
                "first_detected_at": violation_first_seen,
                "details": details,
                "detail": details,
            }
        )

    cache.set(
        ABNORMAL_TRANSFER_STATE_CACHE_KEY,
        state,
        timeout=max(
            3600,
            int(config["state_retention_minutes"] * 120),
        ),
    )
    return results, confirmed_count


def _response(
    timestamp=None,
    results=None,
    skipped_count=0,
    stale_count=0,
    candidate_pair_count=0,
    confirmed_pair_count=0,
    config=None,
):
    results = results or []
    payload = {
        "success": True,
        "type": "异常接驳预警",
        "timestamp": timestamp.isoformat() if timestamp else None,
        "count": len(results),
        "results": results,
        "skipped_count": skipped_count,
        "stale_count": stale_count,
        "candidate_pair_count": candidate_pair_count,
        "confirmed_pair_count": confirmed_pair_count,
        "message": "检测成功" if timestamp else "暂无有效AIS数据",
    }
    if config:
        payload["rule"] = {
            "contact_distance_metres": [
                config["base_contact_distance_metres"],
                config["maximum_contact_distance_metres"],
            ],
            "minimum_duration_seconds": config[
                "minimum_duration_seconds"
            ],
            "minimum_observations": config["minimum_observations"],
            "maximum_relative_speed_knots": config[
                "maximum_relative_speed_knots"
            ],
            "speed_limit_source": "接驳作业计划/主管机关批准条件",
        }
    return JsonResponse(payload)


def detect_abnormal_transfer(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _response()

    config = _config()
    ships = []
    skipped_count = 0
    for raw_ship in ship_list:
        ship = _normalise_ship(raw_ship, config)
        if ship is None:
            skipped_count += 1
        else:
            ships.append(ship)
    if not ships:
        return _response(skipped_count=skipped_count)

    latest_by_mmsi = {}
    for ship in ships:
        previous = latest_by_mmsi.get(ship["mmsi"])
        if previous is None or ship["timestamp"] > previous["timestamp"]:
            latest_by_mmsi[ship["mmsi"]] = ship

    reference_time = max(
        ship["timestamp"] for ship in latest_by_mmsi.values()
    )
    fresh_ships = []
    stale_count = 0
    for ship in latest_by_mmsi.values():
        age = (reference_time - ship["timestamp"]).total_seconds()
        if age > config["max_position_age_seconds"]:
            stale_count += 1
        else:
            fresh_ships.append(ship)

    pairs = _candidate_pairs(fresh_ships, config)
    plans_by_pair = _load_plans(pairs)
    results, confirmed_count = _track_and_evaluate(
        pairs,
        plans_by_pair,
        {ship["mmsi"] for ship in fresh_ships},
        reference_time,
        config,
    )
    results.sort(key=lambda result: result["pair"])
    return _response(
        timestamp=reference_time,
        results=results,
        skipped_count=skipped_count,
        stale_count=stale_count,
        candidate_pair_count=len(pairs),
        confirmed_pair_count=confirmed_count,
        config=config,
    )


def _json_error(message, status=400):
    return JsonResponse(
        {"success": False, "message": message},
        status=status,
    )


def _read_json(request):
    try:
        data = json.loads(request.body or b"{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ValueError("请求体必须是有效的 JSON")
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def _validate_plan(data, current=None, partial=False):
    cleaned = {}
    for field, label in (
        ("vessel_a_mmsi", "船舶 A MMSI"),
        ("vessel_b_mmsi", "船舶 B MMSI"),
    ):
        if field in data:
            value = str(data[field] or "").strip()
            if not _valid_vessel_mmsi(value):
                raise ValueError(f"{label}必须是有效的9位船舶 MMSI")
            cleaned[field] = value
        elif current is None or not partial:
            raise ValueError(f"{label}不能为空")

    first = cleaned.get(
        "vessel_a_mmsi",
        getattr(current, "vessel_a_mmsi", None),
    )
    second = cleaned.get(
        "vessel_b_mmsi",
        getattr(current, "vessel_b_mmsi", None),
    )
    if first and second and first == second:
        raise ValueError("接驳双方不能是同一艘船")

    for field, label, max_length in (
        ("operation_name", "作业名称", 100),
        ("approval_number", "批准/通告编号", 64),
    ):
        if field in data:
            value = str(data[field] or "").strip()
            if len(value) > max_length:
                raise ValueError(
                    f"{label}不能超过 {max_length} 个字符"
                )
            cleaned[field] = value
        elif current is None:
            cleaned[field] = ""

    for field, label in (
        ("starts_at", "批准开始时间"),
        ("ends_at", "批准结束时间"),
    ):
        if field in data:
            parsed = _parse_timestamp(data[field])
            if parsed is None:
                raise ValueError(f"{label}必须是有效的 ISO 8601 时间")
            cleaned[field] = parsed
        elif current is None or not partial:
            raise ValueError(f"{label}不能为空")

    starts_at = cleaned.get(
        "starts_at", getattr(current, "starts_at", None)
    )
    ends_at = cleaned.get(
        "ends_at", getattr(current, "ends_at", None)
    )
    if starts_at and ends_at and ends_at <= starts_at:
        raise ValueError("批准结束时间必须晚于开始时间")

    if "max_speed_knots" in data:
        speed = _finite_float(data["max_speed_knots"])
        if speed is None or speed < 0:
            raise ValueError("最大规定航速必须是非负有限数字")
        cleaned["max_speed_knots"] = speed
    elif current is None or not partial:
        raise ValueError("最大规定航速不能为空")

    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ValueError("is_active 必须是布尔值")
        cleaned["is_active"] = data["is_active"]
    elif current is None:
        cleaned["is_active"] = True

    if "notes" in data:
        notes = str(data["notes"] or "").strip()
        if len(notes) > 1000:
            raise ValueError("备注不能超过 1000 个字符")
        cleaned["notes"] = notes
    elif current is None:
        cleaned["notes"] = ""
    return cleaned


def _serialize_plan(plan):
    return {
        "id": plan.id,
        "vessel_a_mmsi": plan.vessel_a_mmsi,
        "vessel_b_mmsi": plan.vessel_b_mmsi,
        "operation_name": plan.operation_name,
        "approval_number": plan.approval_number,
        "starts_at": plan.starts_at.isoformat(),
        "ends_at": plan.ends_at.isoformat(),
        "max_speed_knots": plan.max_speed_knots,
        "is_active": plan.is_active,
        "notes": plan.notes,
        "created_at": plan.created_at.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
    }


def _invalidate_detection():
    cache.delete(detection_cache_key("detect-abnormalTransfer"))
    cache.delete(ABNORMAL_TRANSFER_STATE_CACHE_KEY)


@require_http_methods(["GET", "POST"])
def operation_collection(request):
    if request.method == "GET":
        plans = TransferOperationPlan.objects.all()
        query = request.GET.get("q", "").strip()
        if query:
            plans = plans.filter(
                Q(vessel_a_mmsi__icontains=query)
                | Q(vessel_b_mmsi__icontains=query)
                | Q(operation_name__icontains=query)
                | Q(approval_number__icontains=query)
                | Q(notes__icontains=query)
            )
        return JsonResponse(
            {
                "success": True,
                "count": plans.count(),
                "results": [
                    _serialize_plan(plan) for plan in plans
                ],
            }
        )

    try:
        plan = TransferOperationPlan.objects.create(
            **_validate_plan(_read_json(request))
        )
    except ValueError as exc:
        return _json_error(str(exc))
    _invalidate_detection()
    return JsonResponse(
        {
            "success": True,
            "message": "接驳作业计划创建成功",
            "result": _serialize_plan(plan),
        },
        status=201,
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def operation_detail(request, plan_id):
    try:
        plan = TransferOperationPlan.objects.get(pk=plan_id)
    except TransferOperationPlan.DoesNotExist:
        return _json_error("接驳作业计划不存在", status=404)

    if request.method == "GET":
        return JsonResponse(
            {"success": True, "result": _serialize_plan(plan)}
        )
    if request.method == "DELETE":
        plan.delete()
        _invalidate_detection()
        return JsonResponse(
            {"success": True, "message": "接驳作业计划删除成功"}
        )

    try:
        cleaned = _validate_plan(
            _read_json(request),
            current=plan,
            partial=request.method == "PATCH",
        )
        for field, value in cleaned.items():
            setattr(plan, field, value)
        plan.save()
    except ValueError as exc:
        return _json_error(str(exc))
    _invalidate_detection()
    return JsonResponse(
        {
            "success": True,
            "message": "接驳作业计划更新成功",
            "result": _serialize_plan(plan),
        }
    )
