import json
import math
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods
from pytz import UnknownTimeZoneError
from pytz import timezone as pytz_timezone

from AISData.normalization import normalise_ais_name
from CrossingBoundary.views import (
    _normalise_vertices as normalise_polygon_vertices,
)

from .models import SmugglingVoyagePermit, SmugglingZone


EARTH_RADIUS_METRES = 6_371_000.0
SMUGGLING_STATE_CACHE_KEY = "smuggling:voyage_state:v1"
SMUGGLING_EVENT_CACHE_KEY = "smuggling:recent_events:v1"

DEFAULT_SMUGGLING_CONFIG = {
    "max_position_age_seconds": 120.0,
    "maximum_gap_seconds": 180.0,
    "origin_minimum_duration_seconds": 60.0,
    "origin_minimum_observations": 3.0,
    "maximum_voyage_hours": 12.0,
    "state_retention_hours": 24.0,
    "landing_speed_knots": 1.5,
    "landing_minimum_duration_seconds": 300.0,
    "landing_minimum_observations": 3.0,
    "draught_change_metres": 0.5,
    "small_craft_length_metres": 50.0,
    "fast_craft_speed_knots": 15.0,
    "event_retention_minutes": 60.0,
    "night_start_hour": 20.0,
    "night_end_hour": 6.0,
    "minimum_risk_score": 40.0,
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
    result = DEFAULT_SMUGGLING_CONFIG.copy()
    configured = getattr(settings, "SMUGGLING_DETECTION", {})
    if isinstance(configured, dict):
        for key in result:
            value = _finite_float(configured.get(key))
            if value is not None and value >= 0:
                result[key] = value
    result["origin_minimum_observations"] = max(
        2, int(result["origin_minimum_observations"])
    )
    result["landing_minimum_observations"] = max(
        2, int(result["landing_minimum_observations"])
    )
    result["night_start_hour"] = min(
        23, int(result["night_start_hour"])
    )
    result["night_end_hour"] = min(
        23, int(result["night_end_hour"])
    )
    result["timezone"] = (
        configured.get("timezone", "Asia/Shanghai")
        if isinstance(configured, dict)
        else "Asia/Shanghai"
    )
    return result


def _normalise_optional_text(value):
    text = str(value or "").strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def _normalise_ship(raw, config):
    if not isinstance(raw, dict):
        return None
    mmsi = str(raw.get("mmsi") or "").strip()
    longitude = _finite_float(raw.get("lon", raw.get("longitude")))
    latitude = _finite_float(raw.get("lat", raw.get("latitude")))
    speed = _finite_float(raw.get("speed"))
    course = _finite_float(raw.get("course"))
    timestamp = _parse_timestamp(raw.get("timestamp"))
    if (
        len(mmsi) != 9
        or not mmsi.isdigit()
        or longitude is None
        or latitude is None
        or speed is None
        or timestamp is None
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
        or not 0 <= speed <= 102.2
    ):
        return None

    def positive_number(field):
        value = _finite_float(raw.get(field))
        return value if value is not None and value > 0 else None

    nav_status = _finite_float(
        raw.get("nav_status", raw.get("status"))
    )
    raw_at_dock = raw.get("at_dock", False)
    at_dock = (
        raw_at_dock
        if isinstance(raw_at_dock, bool)
        else str(raw_at_dock).strip().casefold()
        in {"1", "true", "yes", "y", "是"}
    )
    return {
        "mmsi": mmsi,
        "name": normalise_ais_name(raw.get("name")),
        "longitude": longitude,
        "latitude": latitude,
        "speed": speed,
        "course": course if course is not None and 0 <= course <= 360 else None,
        "heading": _finite_float(raw.get("heading")),
        "timestamp": timestamp,
        "nav_status": int(nav_status) if nav_status is not None else None,
        "at_dock": at_dock,
        "matched_port_name": _normalise_optional_text(
            raw.get("matched_port_name", raw.get("matchedPortName"))
        ),
        "flag": _normalise_optional_text(raw.get("flag")),
        "iso3": _normalise_optional_text(raw.get("iso3")).upper(),
        "ship_type": _normalise_optional_text(
            raw.get("ship_type", raw.get("ship_and_cargo_type"))
        ),
        "draught": positive_number("draught"),
        "length": positive_number("length"),
        "width": positive_number("width"),
        "imo": _normalise_optional_text(raw.get("imo", raw.get("IMO"))),
    }


def _point_in_polygon(longitude, latitude, vertices):
    inside = False
    for index, start in enumerate(vertices):
        end = vertices[(index + 1) % len(vertices)]
        x1, y1 = start
        x2, y2 = end
        if (y1 > latitude) != (y2 > latitude):
            crossing_longitude = (
                (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            )
            if longitude < crossing_longitude:
                inside = not inside
    return inside


def _point_segment_distance_metres(longitude, latitude, start, end):
    latitude_radians = math.radians(latitude)

    def local(point):
        return (
            math.radians(point[0] - longitude)
            * EARTH_RADIUS_METRES
            * math.cos(latitude_radians),
            math.radians(point[1] - latitude) * EARTH_RADIUS_METRES,
        )

    start_x, start_y = local(start)
    end_x, end_y = local(end)
    segment_x = end_x - start_x
    segment_y = end_y - start_y
    length_squared = segment_x**2 + segment_y**2
    if length_squared == 0:
        return math.hypot(start_x, start_y)
    projection = max(
        0.0,
        min(
            1.0,
            -(start_x * segment_x + start_y * segment_y)
            / length_squared,
        ),
    )
    return math.hypot(
        start_x + projection * segment_x,
        start_y + projection * segment_y,
    )


def _compile_zone(zone):
    vertices = normalise_polygon_vertices(zone.vertices)
    return {
        "object": zone,
        "vertices": vertices,
        "bounds": (
            min(point[0] for point in vertices),
            min(point[1] for point in vertices),
            max(point[0] for point in vertices),
            max(point[1] for point in vertices),
        ),
        "buffer_metres": float(zone.buffer_metres),
    }


def _zone_relation(ship, zone):
    min_lon, min_lat, max_lon, max_lat = zone["bounds"]
    buffer_metres = zone["buffer_metres"]
    latitude_margin = math.degrees(buffer_metres / EARTH_RADIUS_METRES)
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
        return {"inside": False, "distance_metres": math.inf, "near": False}

    inside = _point_in_polygon(
        ship["longitude"], ship["latitude"], zone["vertices"]
    )
    if inside:
        distance = 0.0
    else:
        distance = min(
            _point_segment_distance_metres(
                ship["longitude"],
                ship["latitude"],
                start,
                zone["vertices"][(index + 1) % len(zone["vertices"])],
            )
            for index, start in enumerate(zone["vertices"])
        )
    return {
        "inside": inside,
        "distance_metres": distance,
        "near": inside or distance <= buffer_metres,
    }


def _nearest_zone(ship, zones, include_buffer=True):
    candidates = []
    for zone in zones:
        relation = _zone_relation(ship, zone)
        matched = relation["near"] if include_buffer else relation["inside"]
        if matched:
            candidates.append((relation["distance_metres"], zone, relation))
    if not candidates:
        return None, None
    _, zone, relation = min(candidates, key=lambda item: item[0])
    return zone, relation


def _is_night(timestamp, config):
    try:
        local_timezone = pytz_timezone(config["timezone"])
    except (UnknownTimeZoneError, ValueError, TypeError):
        local_timezone = dt_timezone.utc
    hour = timestamp.astimezone(local_timezone).hour
    start = config["night_start_hour"]
    end = config["night_end_hour"]
    if start == end:
        return True
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def _identity_mismatch(ship):
    prefix = ship["mmsi"][:3]
    expected = "HKG" if prefix == "477" else (
        "CHN" if prefix in {"412", "413", "414"} else None
    )
    supplied = ship["iso3"].upper()
    if not supplied:
        flag = ship["flag"].upper()
        if flag in {"CHINA", "CN", "CHN", "中国"}:
            supplied = "CHN"
        elif flag in {"HONG KONG", "HKG", "香港"}:
            supplied = "HKG"
    return bool(expected and supplied in {"CHN", "HKG"} and expected != supplied)


def _load_state(reference_time, retention_hours):
    cached = cache.get(SMUGGLING_STATE_CACHE_KEY, {})
    if not isinstance(cached, dict):
        return {}
    retained = {}
    for mmsi, value in cached.items():
        if not isinstance(value, dict):
            continue
        last_seen = _parse_timestamp(value.get("last_seen"))
        if last_seen is None:
            continue
        age = (reference_time - last_seen).total_seconds()
        if 0 <= age <= retention_hours * 3600:
            retained[mmsi] = value
    return retained


def _active_permits(reference_time, mmsis):
    permits = SmugglingVoyagePermit.objects.filter(
        is_active=True,
        mmsi__in=mmsis,
        valid_from__lte=reference_time,
        valid_until__gte=reference_time,
    ).select_related("origin_zone", "destination_zone")
    result = {}
    for permit in permits:
        result.setdefault(permit.mmsi, []).append(permit)
    return result


def _matching_permit(permits, origin_zone_id, destination_zone_id):
    for permit in permits:
        if permit.origin_zone_id not in (None, origin_zone_id):
            continue
        if permit.destination_zone_id not in (None, destination_zone_id):
            continue
        return permit
    return None


def _risk_label(score):
    if score >= 80:
        return "极高风险"
    if score >= 60:
        return "高风险"
    return "中风险"


def _risk_rank(label):
    return {"中风险": 1, "高风险": 2, "极高风险": 3}.get(label, 0)


def _event_id(mmsi, destination_zone_id, departure_at):
    compact = str(departure_at).replace("-", "").replace(":", "")
    return f"smuggling:{mmsi}:{destination_zone_id}:{compact}"


def _build_event(ship, state, destination, relation, score, reasons, is_new):
    destination_object = destination["object"]
    departure_at = state["departure_at"]
    risk = _risk_label(score)
    details = (
        f"{ship['name']}（{ship['mmsi']}）已从香港起航区驶出，"
        f"当前{'进入' if relation['inside'] else '接近'}广东非设关区域"
        f"“{destination_object.name}”；风险依据："
        f"{'、'.join(reasons)}。该结果为走私风险研判，不代表执法认定。"
    )
    return {
        "mmsi": ship["mmsi"],
        "name": ship["name"],
        "location": [ship["longitude"], ship["latitude"]],
        "event": "Smuggling",
        "risk": risk,
        "risk_score": score,
        "risk_reasons": reasons,
        "origin_zone_id": state["origin_zone_id"],
        "origin_zone_name": state["origin_zone_name"],
        "destination_zone_id": destination_object.id,
        "destination_zone_name": destination_object.name,
        "distance_to_zone_metres": round(relation["distance_metres"], 1),
        "inside_destination_zone": relation["inside"],
        "departure_at": departure_at,
        "night_departure": bool(state.get("night_departure")),
        "speed": ship["speed"],
        "course": ship["course"],
        "nav_status": ship["nav_status"],
        "at_dock": ship["at_dock"],
        "matched_port_name": ship["matched_port_name"],
        "flag": ship["flag"],
        "iso3": ship["iso3"],
        "ship_type": ship["ship_type"],
        "draught": ship["draught"],
        "draught_change_metres": round(
            float(state.get("maximum_draught_change") or 0), 2
        ),
        "length": ship["length"],
        "width": ship["width"],
        "imo": ship["imo"],
        "maximum_ais_gap_seconds": round(
            float(state.get("maximum_ais_gap_seconds") or 0), 1
        ),
        "event_id": _event_id(
            ship["mmsi"], destination_object.id, departure_at
        ),
        "is_new": is_new,
        "first_detected_at": state["first_alert_at"],
        "details": details,
        "detail": details,
    }


def _retain_recent_events(reference_time, new_events, retention_minutes):
    retained = {}
    cached_events = cache.get(SMUGGLING_EVENT_CACHE_KEY, [])
    if isinstance(cached_events, list):
        for event in cached_events:
            if not isinstance(event, dict) or not event.get("event_id"):
                continue
            timestamp = _parse_timestamp(event.get("first_detected_at"))
            if timestamp is None:
                continue
            age = (reference_time - timestamp).total_seconds()
            if 0 <= age <= retention_minutes * 60:
                item = event.copy()
                item["is_new"] = False
                retained[item["event_id"]] = item
    for event in new_events:
        retained[event["event_id"]] = event
    results = sorted(
        retained.values(),
        key=lambda item: item.get("first_detected_at", ""),
        reverse=True,
    )
    cache.set(
        SMUGGLING_EVENT_CACHE_KEY,
        results,
        timeout=max(60, int(retention_minutes * 120)),
    )
    return results


def _fresh_points(raw_ships, config):
    ships = []
    skipped = 0
    for raw in raw_ships:
        ship = _normalise_ship(raw, config)
        if ship is None:
            skipped += 1
        else:
            ships.append(ship)
    if not ships:
        return [], None, skipped, 0

    reference_time = max(ship["timestamp"] for ship in ships)
    grouped = {}
    for ship in ships:
        grouped.setdefault(ship["mmsi"], {})[
            ship["timestamp"].isoformat()
        ] = ship

    fresh = []
    stale = 0
    for points in grouped.values():
        ordered = sorted(points.values(), key=lambda item: item["timestamp"])
        if (
            reference_time - ordered[-1]["timestamp"]
        ).total_seconds() > config["max_position_age_seconds"]:
            stale += 1
        else:
            fresh.extend(ordered)
    fresh.sort(key=lambda item: (item["timestamp"], item["mmsi"]))
    return fresh, reference_time, skipped, stale


def _initial_origin_state(ship, origin):
    timestamp_text = ship["timestamp"].isoformat()
    return {
        "phase": "in_origin",
        "origin_zone_id": origin["object"].id,
        "origin_zone_name": origin["object"].name,
        "origin_first_seen": timestamp_text,
        "origin_observations": 1,
        "origin_confirmed": False,
        "last_seen": timestamp_text,
        "last_location": [ship["longitude"], ship["latitude"]],
        "baseline_draught": ship["draught"],
        "maximum_draught_change": 0.0,
        "maximum_speed": ship["speed"],
        "maximum_ais_gap_seconds": 0.0,
    }


def _process_point(
    ship,
    state,
    origin_zones,
    legal_zones,
    destination_zones,
    permits_by_mmsi,
    config,
):
    mmsi = ship["mmsi"]
    previous = state.get(mmsi)
    previous_seen = (
        _parse_timestamp(previous.get("last_seen")) if previous else None
    )
    if previous_seen is not None and ship["timestamp"] <= previous_seen:
        return None, False, False

    gap_seconds = (
        (ship["timestamp"] - previous_seen).total_seconds()
        if previous_seen is not None
        else None
    )
    origin, origin_relation = _nearest_zone(
        ship, origin_zones, include_buffer=False
    )
    if origin is not None and origin_relation["inside"]:
        same_origin = (
            previous
            and previous.get("phase") == "in_origin"
            and previous.get("origin_zone_id") == origin["object"].id
            and gap_seconds is not None
            and 0 < gap_seconds <= config["maximum_gap_seconds"]
        )
        if not same_origin:
            state[mmsi] = _initial_origin_state(ship, origin)
            return None, False, False

        first_seen = _parse_timestamp(previous["origin_first_seen"])
        observations = int(previous.get("origin_observations", 1)) + 1
        duration = (
            (ship["timestamp"] - first_seen).total_seconds()
            if first_seen is not None
            else 0
        )
        previous.update(
            {
                "origin_observations": observations,
                "origin_confirmed": (
                    duration
                    >= config["origin_minimum_duration_seconds"]
                    and observations
                    >= config["origin_minimum_observations"]
                ),
                "last_seen": ship["timestamp"].isoformat(),
                "last_location": [
                    ship["longitude"],
                    ship["latitude"],
                ],
                "baseline_draught": (
                    previous.get("baseline_draught") or ship["draught"]
                ),
                "maximum_speed": max(
                    float(previous.get("maximum_speed") or 0),
                    ship["speed"],
                ),
            }
        )
        state[mmsi] = previous
        return None, False, False

    if previous is None:
        return None, False, False

    phase = previous.get("phase")
    if phase == "in_origin":
        if not previous.get("origin_confirmed"):
            state.pop(mmsi, None)
            return None, False, False
        departure_at = ship["timestamp"].isoformat()
        previous.update(
            {
                "phase": "departed",
                "departure_at": departure_at,
                "night_departure": _is_night(ship["timestamp"], config),
                "last_seen": departure_at,
                "last_location": [
                    ship["longitude"],
                    ship["latitude"],
                ],
                "maximum_speed": max(
                    float(previous.get("maximum_speed") or 0),
                    ship["speed"],
                ),
            }
        )
        state[mmsi] = previous
        return None, False, False

    if phase == "legal_arrival":
        state.pop(mmsi, None)
        return None, False, False

    departure_time = _parse_timestamp(previous.get("departure_at"))
    if (
        phase not in {"departed", "approaching"}
        or departure_time is None
        or (ship["timestamp"] - departure_time).total_seconds()
        > config["maximum_voyage_hours"] * 3600
    ):
        state.pop(mmsi, None)
        return None, False, False

    if gap_seconds is not None and gap_seconds > 0:
        previous["maximum_ais_gap_seconds"] = max(
            float(previous.get("maximum_ais_gap_seconds") or 0),
            gap_seconds,
        )
    previous["maximum_speed"] = max(
        float(previous.get("maximum_speed") or 0), ship["speed"]
    )
    baseline_draught = previous.get("baseline_draught")
    if baseline_draught and ship["draught"]:
        previous["maximum_draught_change"] = max(
            float(previous.get("maximum_draught_change") or 0),
            abs(float(ship["draught"]) - float(baseline_draught)),
        )
    previous["last_seen"] = ship["timestamp"].isoformat()
    previous["last_location"] = [ship["longitude"], ship["latitude"]]

    legal_zone, _ = _nearest_zone(ship, legal_zones, include_buffer=True)
    if legal_zone is not None:
        previous["phase"] = "legal_arrival"
        state[mmsi] = previous
        return None, False, False

    destination, relation = _nearest_zone(
        ship, destination_zones, include_buffer=True
    )
    if destination is None:
        previous["phase"] = "departed"
        previous.pop("target_zone_id", None)
        previous.pop("landing_first_seen", None)
        previous.pop("landing_observations", None)
        state[mmsi] = previous
        return None, False, False

    destination_id = destination["object"].id
    permit = _matching_permit(
        permits_by_mmsi.get(mmsi, []),
        previous.get("origin_zone_id"),
        destination_id,
    )
    if permit is not None:
        previous["phase"] = "departed"
        previous["permit_number"] = permit.permit_number
        state[mmsi] = previous
        return None, False, True

    same_target = previous.get("target_zone_id") == destination_id
    previous["phase"] = "approaching"
    previous["target_zone_id"] = destination_id
    previous["target_zone_name"] = destination["object"].name

    landing_evidence = (
        relation["inside"]
        and (
            ship["speed"] <= config["landing_speed_knots"]
            or ship["at_dock"]
            or ship["nav_status"] in {1, 5}
        )
    )
    if landing_evidence:
        if same_target and previous.get("landing_first_seen"):
            landing_first_seen = previous["landing_first_seen"]
            landing_observations = (
                int(previous.get("landing_observations", 1)) + 1
            )
        else:
            landing_first_seen = ship["timestamp"].isoformat()
            landing_observations = 1
        previous["landing_first_seen"] = landing_first_seen
        previous["landing_observations"] = landing_observations
    else:
        previous.pop("landing_first_seen", None)
        previous.pop("landing_observations", None)

    landing_start = _parse_timestamp(previous.get("landing_first_seen"))
    landing_duration = (
        (ship["timestamp"] - landing_start).total_seconds()
        if landing_start is not None
        else 0
    )
    landing_confirmed = (
        landing_evidence
        and landing_duration
        >= config["landing_minimum_duration_seconds"]
        and int(previous.get("landing_observations", 0))
        >= config["landing_minimum_observations"]
    )

    score = 40
    reasons = ["香港起航后接近广东非设关区域"]
    if previous.get("night_departure"):
        score += 15
        reasons.append("夜间起航")
    if relation["inside"]:
        score += 10
        reasons.append("已进入非设关区域")
    if landing_confirmed:
        score += 20
        reasons.append(
            f"近岸低速/停靠持续{landing_duration / 60:.1f}分钟"
        )
    if ship["at_dock"]:
        score += 15
        reasons.append("AIS停靠标志")
    if (
        float(previous.get("maximum_ais_gap_seconds") or 0)
        > config["maximum_gap_seconds"]
    ):
        score += 15
        reasons.append("航次中存在AIS异常中断")
    if (
        float(previous.get("maximum_draught_change") or 0)
        >= config["draught_change_metres"]
    ):
        score += 15
        reasons.append("航次吃水变化明显")
    if _identity_mismatch(ship):
        score += 10
        reasons.append("MMSI与船籍字段不一致")
    if (
        ship["length"] is not None
        and ship["length"] <= config["small_craft_length_metres"]
        and float(previous.get("maximum_speed") or 0)
        >= config["fast_craft_speed_knots"]
    ):
        score += 10
        reasons.append("小型高速船特征")
    score = min(100, int(score))

    risk = _risk_label(score)
    previous_risk = previous.get("alerted_risk")
    is_new = _risk_rank(risk) > _risk_rank(previous_risk)
    if not previous.get("first_alert_at"):
        previous["first_alert_at"] = ship["timestamp"].isoformat()
    if is_new or not previous_risk:
        previous["alerted_risk"] = risk
    state[mmsi] = previous
    if score < config["minimum_risk_score"]:
        return None, False, False
    return (
        _build_event(
            ship,
            previous,
            destination,
            relation,
            score,
            reasons,
            is_new,
        ),
        is_new,
        False,
    )


def _response(
    timestamp=None,
    results=None,
    new_count=0,
    skipped_count=0,
    stale_count=0,
    invalid_zone_count=0,
    authorized_count=0,
    zone_counts=None,
    config=None,
):
    results = results or []
    payload = {
        "success": True,
        "type": "海上走私风险研判",
        "configured": True,
        "timestamp": timestamp.isoformat() if timestamp else None,
        "count": len(results),
        "new_count": new_count,
        "results": results,
        "skipped_count": skipped_count,
        "stale_count": stale_count,
        "invalid_zone_count": invalid_zone_count,
        "authorized_count": authorized_count,
        "zone_counts": zone_counts or {},
        "message": "检测成功" if timestamp else "暂无有效AIS数据",
    }
    if config:
        payload["rule"] = {
            "timezone": config["timezone"],
            "night_hours": [
                config["night_start_hour"],
                config["night_end_hour"],
            ],
            "landing_speed_knots": config["landing_speed_knots"],
            "landing_minimum_duration_seconds": config[
                "landing_minimum_duration_seconds"
            ],
            "minimum_risk_score": config["minimum_risk_score"],
        }
    return JsonResponse(payload)


def detect_smuggling(request):
    raw_ships = getattr(request, "ais_ship_list", None)
    if raw_ships is None:
        raw_ships = cache.get("latest_ais_data_raw", [])
    if not isinstance(raw_ships, list) or not raw_ships:
        return _response()

    config = _config()
    ships, reference_time, skipped, stale = _fresh_points(
        raw_ships, config
    )
    if not ships:
        return _response(
            timestamp=reference_time,
            skipped_count=skipped,
            stale_count=stale,
            config=config,
        )

    compiled_by_type = {
        SmugglingZone.HONG_KONG_ORIGIN: [],
        SmugglingZone.CUSTOMS_PORT: [],
        SmugglingZone.NON_CUSTOMS_LANDING: [],
    }
    invalid_zone_count = 0
    for zone in SmugglingZone.objects.filter(is_active=True):
        try:
            compiled_by_type[zone.zone_type].append(_compile_zone(zone))
        except (KeyError, TypeError, ValueError):
            invalid_zone_count += 1

    zone_counts = {
        zone_type: len(zones)
        for zone_type, zones in compiled_by_type.items()
    }
    if (
        not compiled_by_type[SmugglingZone.HONG_KONG_ORIGIN]
        or not compiled_by_type[SmugglingZone.NON_CUSTOMS_LANDING]
    ):
        response = _response(
            timestamp=reference_time,
            skipped_count=skipped,
            stale_count=stale,
            invalid_zone_count=invalid_zone_count,
            zone_counts=zone_counts,
            config=config,
        )
        payload = json.loads(response.content)
        payload["configured"] = False
        payload["message"] = "请先配置香港起航区和广东非设关靠泊区"
        return JsonResponse(payload)

    state = _load_state(
        reference_time, config["state_retention_hours"]
    )
    permits_by_mmsi = _active_permits(
        reference_time, {ship["mmsi"] for ship in ships}
    )
    new_events = []
    new_count = 0
    authorized_count = 0
    for ship in ships:
        event, is_new, authorized = _process_point(
            ship,
            state,
            compiled_by_type[SmugglingZone.HONG_KONG_ORIGIN],
            compiled_by_type[SmugglingZone.CUSTOMS_PORT],
            compiled_by_type[SmugglingZone.NON_CUSTOMS_LANDING],
            permits_by_mmsi,
            config,
        )
        if event is not None:
            new_events.append(event)
        if is_new:
            new_count += 1
        if authorized:
            authorized_count += 1

    cache.set(
        SMUGGLING_STATE_CACHE_KEY,
        state,
        timeout=max(
            3600, int(config["state_retention_hours"] * 7200)
        ),
    )
    results = _retain_recent_events(
        reference_time,
        new_events,
        config["event_retention_minutes"],
    )
    return _response(
        timestamp=reference_time,
        results=results,
        new_count=new_count,
        skipped_count=skipped,
        stale_count=stale,
        invalid_zone_count=invalid_zone_count,
        authorized_count=authorized_count,
        zone_counts=zone_counts,
        config=config,
    )


def _json_error(message, status=400):
    return JsonResponse(
        {"success": False, "message": message}, status=status
    )


def _read_json(request):
    try:
        data = json.loads(request.body or b"{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ValueError("请求体必须是有效的JSON")
    if not isinstance(data, dict):
        raise ValueError("请求体必须是JSON对象")
    return data


def _serialize_zone(zone):
    return {
        "id": zone.id,
        "name": zone.name,
        "zone_type": zone.zone_type,
        "zone_type_label": zone.get_zone_type_display(),
        "vertices": zone.vertices,
        "buffer_metres": zone.buffer_metres,
        "legal_reference": zone.legal_reference,
        "is_active": zone.is_active,
        "notes": zone.notes,
        "created_at": zone.created_at.isoformat(),
        "updated_at": zone.updated_at.isoformat(),
    }


def _validate_zone(data, current=None, partial=False):
    cleaned = {}
    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name or len(name) > 100:
            raise ValueError("区域名称不能为空且不能超过100个字符")
        cleaned["name"] = name
    elif not partial and current is None:
        raise ValueError("区域名称不能为空")

    if "zone_type" in data:
        zone_type = str(data["zone_type"] or "").strip()
        valid_types = {choice[0] for choice in SmugglingZone.ZONE_TYPE_CHOICES}
        if zone_type not in valid_types:
            raise ValueError("区域类型无效")
        cleaned["zone_type"] = zone_type
    elif not partial and current is None:
        raise ValueError("区域类型不能为空")

    if "vertices" in data:
        cleaned["vertices"] = normalise_polygon_vertices(data["vertices"])
    elif not partial and current is None:
        raise ValueError("多边形顶点不能为空")

    if "buffer_metres" in data:
        value = _finite_float(data["buffer_metres"])
        if value is None or not 0 <= value <= 100_000:
            raise ValueError("外围预警距离必须在0至100000米之间")
        cleaned["buffer_metres"] = int(value)
    for field, maximum in (("legal_reference", 255), ("notes", 2000)):
        if field in data:
            value = str(data[field] or "").strip()
            if len(value) > maximum:
                raise ValueError(f"{field}内容过长")
            cleaned[field] = value
    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ValueError("is_active必须是布尔值")
        cleaned["is_active"] = data["is_active"]
    return cleaned


@require_http_methods(["GET", "POST"])
def zone_collection(request):
    if request.method == "GET":
        zones = SmugglingZone.objects.all()
        return JsonResponse(
            {
                "success": True,
                "count": zones.count(),
                "results": [_serialize_zone(zone) for zone in zones],
            }
        )
    try:
        zone = SmugglingZone.objects.create(
            **_validate_zone(_read_json(request))
        )
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("已存在同名区域", status=409)
    return JsonResponse(
        {"success": True, "result": _serialize_zone(zone)}, status=201
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def zone_detail(request, zone_id):
    try:
        zone = SmugglingZone.objects.get(pk=zone_id)
    except SmugglingZone.DoesNotExist:
        return _json_error("区域不存在", status=404)
    if request.method == "GET":
        return JsonResponse({"success": True, "result": _serialize_zone(zone)})
    if request.method == "DELETE":
        zone.delete()
        return JsonResponse({"success": True, "message": "区域已删除"})
    try:
        cleaned = _validate_zone(
            _read_json(request),
            current=zone,
            partial=request.method == "PATCH",
        )
        for field, value in cleaned.items():
            setattr(zone, field, value)
        zone.save()
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("已存在同名区域", status=409)
    return JsonResponse({"success": True, "result": _serialize_zone(zone)})


def _serialize_permit(permit):
    return {
        "id": permit.id,
        "mmsi": permit.mmsi,
        "permit_number": permit.permit_number,
        "origin_zone_id": permit.origin_zone_id,
        "origin_zone_name": (
            permit.origin_zone.name if permit.origin_zone else None
        ),
        "destination_zone_id": permit.destination_zone_id,
        "destination_zone_name": (
            permit.destination_zone.name
            if permit.destination_zone
            else None
        ),
        "valid_from": permit.valid_from.isoformat(),
        "valid_until": permit.valid_until.isoformat(),
        "is_active": permit.is_active,
        "notes": permit.notes,
        "created_at": permit.created_at.isoformat(),
        "updated_at": permit.updated_at.isoformat(),
    }


def _required_timestamp(value, label):
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"{label}必须是有效的ISO 8601时间")
    return parsed


def _validate_permit(data, current=None, partial=False):
    cleaned = {}
    if "mmsi" in data:
        mmsi = str(data["mmsi"] or "").strip()
        if len(mmsi) != 9 or not mmsi.isdigit():
            raise ValueError("MMSI必须是9位数字")
        cleaned["mmsi"] = mmsi
    elif not partial and current is None:
        raise ValueError("MMSI不能为空")

    if "permit_number" in data:
        number = str(data["permit_number"] or "").strip()
        if not number or len(number) > 64:
            raise ValueError("许可编号不能为空且不能超过64个字符")
        cleaned["permit_number"] = number
    elif not partial and current is None:
        raise ValueError("许可编号不能为空")

    for field, label in (
        ("valid_from", "有效期开始"),
        ("valid_until", "有效期结束"),
    ):
        if field in data:
            cleaned[field] = _required_timestamp(data[field], label)
        elif not partial and current is None:
            raise ValueError(f"{label}不能为空")

    for field, expected_type in (
        ("origin_zone_id", SmugglingZone.HONG_KONG_ORIGIN),
        ("destination_zone_id", SmugglingZone.NON_CUSTOMS_LANDING),
    ):
        if field not in data:
            continue
        value = data[field]
        model_field = field[:-3] if field.endswith("_id") else field
        if value in (None, ""):
            cleaned[model_field] = None
            continue
        try:
            zone = SmugglingZone.objects.get(pk=int(value))
        except (TypeError, ValueError, SmugglingZone.DoesNotExist):
            raise ValueError(f"{field}对应区域不存在")
        if zone.zone_type != expected_type:
            raise ValueError(f"{field}区域类型不正确")
        cleaned[model_field] = zone

    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ValueError("is_active必须是布尔值")
        cleaned["is_active"] = data["is_active"]
    if "notes" in data:
        notes = str(data["notes"] or "").strip()
        if len(notes) > 2000:
            raise ValueError("备注不能超过2000个字符")
        cleaned["notes"] = notes

    valid_from = cleaned.get(
        "valid_from", current.valid_from if current else None
    )
    valid_until = cleaned.get(
        "valid_until", current.valid_until if current else None
    )
    if valid_from and valid_until and valid_until <= valid_from:
        raise ValueError("有效期结束必须晚于开始时间")
    return cleaned


@require_http_methods(["GET", "POST"])
def permit_collection(request):
    if request.method == "GET":
        permits = SmugglingVoyagePermit.objects.select_related(
            "origin_zone", "destination_zone"
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
        permit = SmugglingVoyagePermit(
            **_validate_permit(_read_json(request))
        )
        permit.full_clean()
        permit.save()
    except (ValueError, ValidationError) as exc:
        message = (
            "; ".join(exc.messages)
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        return _json_error(message)
    return JsonResponse(
        {"success": True, "result": _serialize_permit(permit)}, status=201
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def permit_detail(request, permit_id):
    try:
        permit = SmugglingVoyagePermit.objects.select_related(
            "origin_zone", "destination_zone"
        ).get(pk=permit_id)
    except SmugglingVoyagePermit.DoesNotExist:
        return _json_error("许可不存在", status=404)
    if request.method == "GET":
        return JsonResponse(
            {"success": True, "result": _serialize_permit(permit)}
        )
    if request.method == "DELETE":
        permit.delete()
        return JsonResponse({"success": True, "message": "许可已删除"})
    try:
        cleaned = _validate_permit(
            _read_json(request),
            current=permit,
            partial=request.method == "PATCH",
        )
        for field, value in cleaned.items():
            setattr(permit, field, value)
        permit.full_clean()
        permit.save()
    except (ValueError, ValidationError) as exc:
        message = (
            "; ".join(exc.messages)
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        return _json_error(message)
    return JsonResponse(
        {"success": True, "result": _serialize_permit(permit)}
    )
