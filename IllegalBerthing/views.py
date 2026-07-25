import bisect
import json
import math
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from AISData.detection import detection_cache_key
from AISData.normalization import normalise_ais_name

from .mmsi import chinese_mids, classify_vessel_mmsi
from .models import IllegalBerthingPermit


EARTH_RADIUS_METRES = 6_371_000.0
ILLEGAL_BERTHING_STATE_CACHE_KEY = "illegal_berthing:tracking_state:v1"

DEFAULT_ILLEGAL_BERTHING_CONFIG = {
    "base_contact_distance_metres": 100.0,
    "maximum_contact_distance_metres": 250.0,
    "length_distance_factor": 0.5,
    "contact_buffer_metres": 25.0,
    "maximum_speed_knots": 2.0,
    "maximum_relative_speed_knots": 0.5,
    "minimum_duration_seconds": 300.0,
    "minimum_observations": 5.0,
    "maximum_gap_seconds": 90.0,
    "max_position_age_seconds": 120.0,
    "max_valid_speed_knots": 102.2,
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
    config = DEFAULT_ILLEGAL_BERTHING_CONFIG.copy()
    configured = getattr(settings, "ILLEGAL_BERTHING_DETECTION", {})
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
    nationality = classify_vessel_mmsi(mmsi)
    longitude = _finite_float(
        raw_ship.get("lon", raw_ship.get("longitude"))
    )
    latitude = _finite_float(
        raw_ship.get("lat", raw_ship.get("latitude"))
    )
    speed = _finite_float(raw_ship.get("speed"))
    timestamp = _parse_timestamp(raw_ship.get("timestamp"))
    if (
        nationality is None
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
        "nationality": nationality,
        "name": normalise_ais_name(raw_ship.get("name")),
        "longitude": longitude,
        "latitude": latitude,
        "speed": speed,
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


def _candidate_pairs(chinese_ships, foreign_ships, config):
    """Find spatially close Chinese/foreign pairs with a latitude sweep."""
    maximum_distance = config["maximum_contact_distance_metres"]
    maximum_latitude_delta = math.degrees(
        maximum_distance / EARTH_RADIUS_METRES
    )
    foreign_ships = sorted(
        foreign_ships, key=lambda ship: ship["latitude"]
    )
    foreign_latitudes = [ship["latitude"] for ship in foreign_ships]
    pairs = []

    for chinese in chinese_ships:
        lower = bisect.bisect_left(
            foreign_latitudes,
            chinese["latitude"] - maximum_latitude_delta,
        )
        upper = bisect.bisect_right(
            foreign_latitudes,
            chinese["latitude"] + maximum_latitude_delta,
        )
        for foreign in foreign_ships[lower:upper]:
            threshold = _contact_distance(chinese, foreign, config)
            distance = _distance_metres(chinese, foreign)
            if distance > threshold:
                continue
            if (
                chinese["speed"] > config["maximum_speed_knots"]
                or foreign["speed"] > config["maximum_speed_knots"]
                or abs(chinese["speed"] - foreign["speed"])
                > config["maximum_relative_speed_knots"]
            ):
                continue
            pairs.append(
                {
                    "chinese": chinese,
                    "foreign": foreign,
                    "pair": (
                        f"{chinese['mmsi']}:{foreign['mmsi']}"
                    ),
                    "distance_metres": distance,
                    "contact_threshold_metres": threshold,
                }
            )
    return pairs


def _authorized_pairs(pairs, timestamp):
    if not pairs:
        return set()
    chinese_mmsis = {
        pair["chinese"]["mmsi"] for pair in pairs
    }
    foreign_mmsis = {
        pair["foreign"]["mmsi"] for pair in pairs
    }
    permits = IllegalBerthingPermit.objects.filter(
        is_active=True,
        chinese_mmsi__in=chinese_mmsis,
        foreign_mmsi__in=foreign_mmsis,
    ).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=timestamp),
        Q(valid_until__isnull=True) | Q(valid_until__gte=timestamp),
    )
    return {
        f"{permit.chinese_mmsi}:{permit.foreign_mmsi}"
        for permit in permits
    }


def _load_state(timestamp, retention_minutes):
    state = cache.get(ILLEGAL_BERTHING_STATE_CACHE_KEY, {})
    if not isinstance(state, dict):
        return {}

    retention_seconds = retention_minutes * 60
    retained = {}
    for pair, value in state.items():
        if not isinstance(value, dict):
            continue
        last_seen = _parse_timestamp(value.get("last_seen"))
        if last_seen is None:
            continue
        age = (timestamp - last_seen).total_seconds()
        if 0 <= age <= retention_seconds:
            retained[pair] = value
    return retained


def _event_id(pair, first_seen):
    compact_time = first_seen.replace(
        "-", "", 3
    ).replace(":", "", 2)
    return f"illegal-berthing:{pair}:{compact_time}"


def _track_pairs(
    pairs,
    all_fresh_mmsis,
    timestamp,
    config,
):
    state = _load_state(timestamp, config["state_retention_minutes"])
    current_pair_keys = {pair["pair"] for pair in pairs}

    # If both ships are present but no longer meet the contact rule, the
    # continuity is definitely broken.
    for pair_key in list(state):
        first_mmsi, second_mmsi = pair_key.split(":", 1)
        if (
            pair_key not in current_pair_keys
            and first_mmsi in all_fresh_mmsis
            and second_mmsi in all_fresh_mmsis
        ):
            del state[pair_key]

    results = []
    timestamp_text = timestamp.isoformat()
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
            alerted = bool(previous.get("alerted"))
        elif previous is not None and gap_seconds == 0:
            first_seen = previous["first_seen"]
            observations = int(previous.get("observations", 1))
            alerted = bool(previous.get("alerted"))
        else:
            first_seen = timestamp_text
            observations = 1
            alerted = False

        first_seen_time = _parse_timestamp(first_seen) or timestamp
        duration_seconds = max(
            0.0, (timestamp - first_seen_time).total_seconds()
        )
        qualifies = (
            duration_seconds >= config["minimum_duration_seconds"]
            and observations >= config["minimum_observations"]
        )
        is_new = qualifies and not alerted
        state[pair_key] = {
            "first_seen": first_seen,
            "last_seen": timestamp_text,
            "observations": observations,
            "alerted": alerted or qualifies,
        }
        if not qualifies:
            continue

        chinese = pair["chinese"]
        foreign = pair["foreign"]
        details = (
            f"中国籍船舶{chinese['name']}（{chinese['mmsi']}）"
            f"未经有效许可疑似搭靠外籍船舶"
            f"{foreign['name']}（{foreign['mmsi']}）；"
            f"已持续 {duration_seconds / 60:.1f} 分钟，"
            f"当前船距 {pair['distance_metres']:.0f} 米。"
        )
        results.append(
            {
                "mmsi": chinese["mmsi"],
                "other_mmsi": foreign["mmsi"],
                "pair": pair_key,
                "pair_mmsi": [
                    chinese["mmsi"],
                    foreign["mmsi"],
                ],
                "name": f"{chinese['name']} / {foreign['name']}",
                "chinese_name": chinese["name"],
                "foreign_name": foreign["name"],
                "chinese_mmsi": chinese["mmsi"],
                "foreign_mmsi": foreign["mmsi"],
                "chinese_nationality": "中国籍",
                "foreign_nationality": "外籍",
                "location": [
                    round(
                        (
                            chinese["longitude"]
                            + foreign["longitude"]
                        )
                        / 2,
                        6,
                    ),
                    round(
                        (
                            chinese["latitude"]
                            + foreign["latitude"]
                        )
                        / 2,
                        6,
                    ),
                ],
                "ship_locations": [
                    [chinese["longitude"], chinese["latitude"]],
                    [foreign["longitude"], foreign["latitude"]],
                ],
                "event": "IllegalBerthing",
                "risk": "高风险",
                "authorized": False,
                "distance_metres": round(
                    pair["distance_metres"], 1
                ),
                "contact_threshold_metres": round(
                    pair["contact_threshold_metres"], 1
                ),
                "duration_seconds": round(duration_seconds, 1),
                "duration_minutes": round(duration_seconds / 60, 2),
                "observations": observations,
                "event_id": _event_id(pair_key, first_seen),
                "is_new": is_new,
                "first_detected_at": first_seen,
                "details": details,
                "detail": details,
            }
        )

    cache.set(
        ILLEGAL_BERTHING_STATE_CACHE_KEY,
        state,
        timeout=max(
            3600,
            int(config["state_retention_minutes"] * 120),
        ),
    )
    return results


def _response(
    timestamp=None,
    results=None,
    skipped_count=0,
    stale_count=0,
    candidate_pair_count=0,
    authorized_pair_count=0,
    config=None,
):
    results = results or []
    payload = {
        "success": True,
        "type": "非法搭靠预警",
        "timestamp": timestamp.isoformat() if timestamp else None,
        "count": len(results),
        "results": results,
        "skipped_count": skipped_count,
        "stale_count": stale_count,
        "candidate_pair_count": candidate_pair_count,
        "authorized_pair_count": authorized_pair_count,
        "message": "检测成功" if timestamp else "暂无有效AIS数据",
    }
    if config:
        payload["rule"] = {
            "chinese_mids": sorted(chinese_mids()),
            "maximum_speed_knots": config["maximum_speed_knots"],
            "maximum_relative_speed_knots": config[
                "maximum_relative_speed_knots"
            ],
            "contact_distance_metres": [
                config["base_contact_distance_metres"],
                config["maximum_contact_distance_metres"],
            ],
            "minimum_duration_seconds": config[
                "minimum_duration_seconds"
            ],
            "minimum_observations": config["minimum_observations"],
        }
    return JsonResponse(payload)


def detect_illegal_berthing(request):
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

    chinese_ships = [
        ship
        for ship in fresh_ships
        if ship["nationality"] == "chinese"
    ]
    foreign_ships = [
        ship
        for ship in fresh_ships
        if ship["nationality"] == "foreign"
    ]
    candidates = _candidate_pairs(
        chinese_ships, foreign_ships, config
    )
    authorized = _authorized_pairs(candidates, reference_time)
    unauthorized_candidates = [
        pair for pair in candidates if pair["pair"] not in authorized
    ]
    results = _track_pairs(
        unauthorized_candidates,
        {ship["mmsi"] for ship in fresh_ships},
        reference_time,
        config,
    )
    results.sort(key=lambda item: item["pair"])
    return _response(
        timestamp=reference_time,
        results=results,
        skipped_count=skipped_count,
        stale_count=stale_count,
        candidate_pair_count=len(candidates),
        authorized_pair_count=len(authorized),
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


def _optional_timestamp(value, label):
    if value in (None, ""):
        return None
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"{label}必须是有效的 ISO 8601 时间")
    return parsed


def _validate_permit(data, current=None, partial=False):
    cleaned = {}
    for field, nationality, label in (
        ("chinese_mmsi", "chinese", "中国籍船舶 MMSI"),
        ("foreign_mmsi", "foreign", "外籍船舶 MMSI"),
    ):
        if field in data:
            value = str(data[field] or "").strip()
            if classify_vessel_mmsi(value) != nationality:
                expected = (
                    "必须使用中国 MID（412/413/414）"
                    if nationality == "chinese"
                    else "必须是非中国 MID 的有效船舶 MMSI"
                )
                raise ValueError(f"{label}{expected}")
            cleaned[field] = value
        elif current is None or not partial:
            raise ValueError(f"{label}不能为空")

    if "permit_number" in data:
        value = str(data["permit_number"] or "").strip()
        if len(value) > 64:
            raise ValueError("许可编号不能超过 64 个字符")
        cleaned["permit_number"] = value
    elif current is None:
        cleaned["permit_number"] = ""

    for field, label in (
        ("valid_from", "有效期开始"),
        ("valid_until", "有效期结束"),
    ):
        if field in data:
            cleaned[field] = _optional_timestamp(data[field], label)
        elif current is None:
            cleaned[field] = None

    valid_from = cleaned.get(
        "valid_from", getattr(current, "valid_from", None)
    )
    valid_until = cleaned.get(
        "valid_until", getattr(current, "valid_until", None)
    )
    if (
        valid_from is not None
        and valid_until is not None
        and valid_until <= valid_from
    ):
        raise ValueError("有效期结束必须晚于有效期开始")

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


def _serialize_permit(permit):
    return {
        "id": permit.id,
        "chinese_mmsi": permit.chinese_mmsi,
        "foreign_mmsi": permit.foreign_mmsi,
        "permit_number": permit.permit_number,
        "valid_from": (
            permit.valid_from.isoformat() if permit.valid_from else None
        ),
        "valid_until": (
            permit.valid_until.isoformat() if permit.valid_until else None
        ),
        "is_active": permit.is_active,
        "notes": permit.notes,
        "created_at": permit.created_at.isoformat(),
        "updated_at": permit.updated_at.isoformat(),
    }


def _invalidate_detection():
    cache.delete(detection_cache_key("detect-illegalBerthing"))
    cache.delete(ILLEGAL_BERTHING_STATE_CACHE_KEY)


@require_http_methods(["GET", "POST"])
def permit_collection(request):
    if request.method == "GET":
        permits = IllegalBerthingPermit.objects.all()
        query = request.GET.get("q", "").strip()
        if query:
            permits = permits.filter(
                Q(chinese_mmsi__icontains=query)
                | Q(foreign_mmsi__icontains=query)
                | Q(permit_number__icontains=query)
                | Q(notes__icontains=query)
            )
        return JsonResponse(
            {
                "success": True,
                "count": permits.count(),
                "results": [
                    _serialize_permit(permit) for permit in permits
                ],
            }
        )

    try:
        permit = IllegalBerthingPermit.objects.create(
            **_validate_permit(_read_json(request))
        )
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("搭靠许可创建失败", status=409)
    _invalidate_detection()
    return JsonResponse(
        {
            "success": True,
            "message": "搭靠许可创建成功",
            "result": _serialize_permit(permit),
        },
        status=201,
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def permit_detail(request, permit_id):
    try:
        permit = IllegalBerthingPermit.objects.get(pk=permit_id)
    except IllegalBerthingPermit.DoesNotExist:
        return _json_error("搭靠许可不存在", status=404)

    if request.method == "GET":
        return JsonResponse(
            {"success": True, "result": _serialize_permit(permit)}
        )
    if request.method == "DELETE":
        permit.delete()
        _invalidate_detection()
        return JsonResponse(
            {"success": True, "message": "搭靠许可删除成功"}
        )

    try:
        cleaned = _validate_permit(
            _read_json(request),
            current=permit,
            partial=request.method == "PATCH",
        )
        for field, value in cleaned.items():
            setattr(permit, field, value)
        permit.save()
    except ValueError as exc:
        return _json_error(str(exc))
    _invalidate_detection()
    return JsonResponse(
        {
            "success": True,
            "message": "搭靠许可更新成功",
            "result": _serialize_permit(permit),
        }
    )
