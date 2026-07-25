import math
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime

from AISData.normalization import normalise_ais_name


EARTH_RADIUS_METRES = 6_371_000.0
KNOTS_TO_METRES_PER_SECOND = 0.514444
COLLISION_STATE_CACHE_KEY = "collision:risk_state:v1"

DEFAULT_COLLISION_CONFIG = {
    "warning_tcpa_minutes": 15.0,
    "critical_tcpa_minutes": 5.0,
    "warning_dcpa_metres": 300.0,
    "critical_dcpa_metres": 150.0,
    "immediate_distance_metres": 100.0,
    "vessel_buffer_metres": 50.0,
    "minimum_relative_speed_mps": 0.2,
    "max_position_age_seconds": 120.0,
    "event_retention_minutes": 30.0,
}


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_ais_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = parse_datetime(value)
    else:
        parsed = None

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _collision_config():
    config = DEFAULT_COLLISION_CONFIG.copy()
    configured = getattr(settings, "COLLISION_DETECTION", {})
    if isinstance(configured, dict):
        for key in config:
            value = _finite_float(configured.get(key))
            if value is not None and value >= 0:
                config[key] = value

    config["warning_tcpa_minutes"] = max(
        config["warning_tcpa_minutes"], config["critical_tcpa_minutes"]
    )
    config["warning_dcpa_metres"] = max(
        config["warning_dcpa_metres"], config["critical_dcpa_metres"]
    )
    return config


def _normalise_ship(raw_ship):
    if not isinstance(raw_ship, dict):
        return None

    mmsi = str(raw_ship.get("mmsi") or "").strip()
    longitude = _finite_float(raw_ship.get("longitude"))
    latitude = _finite_float(raw_ship.get("latitude"))
    speed = _finite_float(raw_ship.get("speed"))
    course = _finite_float(raw_ship.get("course"))
    timestamp = _parse_ais_timestamp(raw_ship.get("timestamp"))

    if (
        not mmsi
        or longitude is None
        or latitude is None
        or speed is None
        or course is None
        or timestamp is None
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
        or speed < 0
    ):
        return None

    length = _finite_float(raw_ship.get("length"))
    width = _finite_float(raw_ship.get("width"))
    return {
        "mmsi": mmsi,
        "name": normalise_ais_name(raw_ship.get("name")),
        "longitude": longitude,
        "latitude": latitude,
        "speed": speed,
        "course": course % 360,
        "timestamp": timestamp,
        "length": length if length is not None and length > 0 else None,
        "width": width if width is not None and width > 0 else None,
    }


def _velocity_metres_per_second(ship):
    speed = ship["speed"] * KNOTS_TO_METRES_PER_SECOND
    course_radians = math.radians(ship["course"])
    return (
        speed * math.sin(course_radians),
        speed * math.cos(course_radians),
    )


def _project_ship(ship, reference_time):
    projected = ship.copy()
    elapsed_seconds = max(0.0, (reference_time - ship["timestamp"]).total_seconds())
    if elapsed_seconds == 0:
        return projected

    east_speed, north_speed = _velocity_metres_per_second(ship)
    latitude_radians = math.radians(ship["latitude"])
    projected["latitude"] += math.degrees(
        north_speed * elapsed_seconds / EARTH_RADIUS_METRES
    )

    longitude_scale = EARTH_RADIUS_METRES * max(
        abs(math.cos(latitude_radians)), 1e-9
    )
    projected["longitude"] += math.degrees(
        east_speed * elapsed_seconds / longitude_scale
    )
    projected["timestamp"] = reference_time
    return projected


def calculate_pair_cpa(ship1, ship2):
    """Calculate current distance, DCPA and TCPA using constant COG/SOG."""
    mean_latitude = math.radians((ship1["latitude"] + ship2["latitude"]) / 2)
    relative_position_east = (
        math.radians(ship2["longitude"] - ship1["longitude"])
        * EARTH_RADIUS_METRES
        * math.cos(mean_latitude)
    )
    relative_position_north = (
        math.radians(ship2["latitude"] - ship1["latitude"])
        * EARTH_RADIUS_METRES
    )

    ship1_east, ship1_north = _velocity_metres_per_second(ship1)
    ship2_east, ship2_north = _velocity_metres_per_second(ship2)
    relative_velocity_east = ship2_east - ship1_east
    relative_velocity_north = ship2_north - ship1_north

    current_distance = math.hypot(
        relative_position_east, relative_position_north
    )
    relative_speed_squared = (
        relative_velocity_east**2 + relative_velocity_north**2
    )
    closing_product = (
        relative_position_east * relative_velocity_east
        + relative_position_north * relative_velocity_north
    )

    if relative_speed_squared == 0:
        return {
            "current_distance_metres": current_distance,
            "dcpa_metres": current_distance,
            "tcpa_seconds": None,
            "relative_speed_mps": 0.0,
            "is_closing": False,
        }

    tcpa_seconds = -closing_product / relative_speed_squared
    closest_east = (
        relative_position_east + relative_velocity_east * tcpa_seconds
    )
    closest_north = (
        relative_position_north + relative_velocity_north * tcpa_seconds
    )
    return {
        "current_distance_metres": current_distance,
        "dcpa_metres": math.hypot(closest_east, closest_north),
        "tcpa_seconds": tcpa_seconds,
        "relative_speed_mps": math.sqrt(relative_speed_squared),
        "is_closing": closing_product < 0,
    }


def _safety_distance(ship1, ship2, config):
    known_lengths = [
        ship["length"] for ship in (ship1, ship2) if ship["length"] is not None
    ]
    vessel_distance = (
        sum(known_lengths) / 2 + config["vessel_buffer_metres"]
        if known_lengths
        else 0
    )
    return max(config["warning_dcpa_metres"], vessel_distance)


def _risk_level(cpa, safety_distance, config):
    tcpa_seconds = cpa["tcpa_seconds"]
    if (
        not cpa["is_closing"]
        or tcpa_seconds is None
        or tcpa_seconds < 0
        or cpa["relative_speed_mps"] < config["minimum_relative_speed_mps"]
        or tcpa_seconds > config["warning_tcpa_minutes"] * 60
        or cpa["dcpa_metres"] > safety_distance
    ):
        return None

    is_critical = (
        tcpa_seconds <= config["critical_tcpa_minutes"] * 60
        and cpa["dcpa_metres"] <= config["critical_dcpa_metres"]
    ) or (
        cpa["current_distance_metres"] <= config["immediate_distance_metres"]
    )
    return "高风险" if is_critical else "中风险"


def _event_state(results, timestamp, retention_minutes):
    cached_state = cache.get(COLLISION_STATE_CACHE_KEY, {})
    if not isinstance(cached_state, dict):
        cached_state = {}

    retention_seconds = retention_minutes * 60
    active_state = {}
    for pair_key, value in cached_state.items():
        if not isinstance(value, dict):
            continue
        last_seen = _parse_ais_timestamp(value.get("last_seen"))
        if last_seen is None:
            continue
        age_seconds = (timestamp - last_seen).total_seconds()
        if 0 <= age_seconds <= retention_seconds:
            active_state[pair_key] = value

    timestamp_text = timestamp.isoformat()
    for result in results:
        pair_key = result["pair"]
        previous = active_state.get(pair_key)
        first_detected_at = (
            previous.get("first_detected_at")
            if previous and previous.get("first_detected_at")
            else timestamp_text
        )
        result["event_id"] = f"collision:{pair_key}"
        result["is_new"] = previous is None
        result["first_detected_at"] = first_detected_at
        active_state[pair_key] = {
            "first_detected_at": first_detected_at,
            "last_seen": timestamp_text,
        }

    cache.set(
        COLLISION_STATE_CACHE_KEY,
        active_state,
        timeout=max(3600, int(retention_seconds * 2)),
    )


def _empty_response(skipped_count=0, stale_count=0):
    return JsonResponse(
        {
            "type": "Collision",
            "timestamp": None,
            "count": 0,
            "results": [],
            "skipped_count": skipped_count,
            "stale_count": stale_count,
        }
    )


def detect_collision(request):
    """
    Detect collision risk using DCPA/TCPA under a constant-course,
    constant-speed assumption.
    """
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    valid_ships = []
    skipped_count = 0
    for raw_ship in ship_list:
        ship = _normalise_ship(raw_ship)
        if ship is None:
            skipped_count += 1
        else:
            valid_ships.append(ship)

    if not valid_ships:
        return _empty_response(skipped_count=skipped_count)

    # One MMSI must take part in at most one position per frame.
    latest_by_mmsi = {}
    for ship in valid_ships:
        previous = latest_by_mmsi.get(ship["mmsi"])
        if previous is None or ship["timestamp"] > previous["timestamp"]:
            latest_by_mmsi[ship["mmsi"]] = ship

    reference_time = max(
        ship["timestamp"] for ship in latest_by_mmsi.values()
    )
    config = _collision_config()
    fresh_ships = []
    stale_count = 0
    for ship in latest_by_mmsi.values():
        age_seconds = (reference_time - ship["timestamp"]).total_seconds()
        if age_seconds > config["max_position_age_seconds"]:
            stale_count += 1
            continue
        fresh_ships.append(_project_ship(ship, reference_time))

    fresh_ships.sort(key=lambda ship: ship["mmsi"])
    results = []
    for first_index, ship1 in enumerate(fresh_ships):
        for ship2 in fresh_ships[first_index + 1 :]:
            cpa = calculate_pair_cpa(ship1, ship2)
            safety_distance = _safety_distance(ship1, ship2, config)
            risk = _risk_level(cpa, safety_distance, config)
            if risk is None:
                continue

            tcpa_minutes = cpa["tcpa_seconds"] / 60
            pair_key = f"{ship1['mmsi']}:{ship2['mmsi']}"
            ship1_label = ship1["name"]
            ship2_label = ship2["name"]
            details = (
                f"{ship1_label}（{ship1['mmsi']}）与"
                f"{ship2_label}（{ship2['mmsi']}）存在碰撞风险；"
                f"预计 {tcpa_minutes:.1f} 分钟后最近会遇，"
                f"DCPA {cpa['dcpa_metres']:.0f} 米，"
                f"当前距离 {cpa['current_distance_metres']:.0f} 米。"
            )
            results.append(
                {
                    "mmsi": ship1["mmsi"],
                    "other_mmsi": ship2["mmsi"],
                    "pair": pair_key,
                    "pair_mmsi": [ship1["mmsi"], ship2["mmsi"]],
                    "name": f"{ship1_label} / {ship2_label}",
                    "location": [
                        round(
                            (ship1["longitude"] + ship2["longitude"]) / 2, 6
                        ),
                        round(
                            (ship1["latitude"] + ship2["latitude"]) / 2, 6
                        ),
                    ],
                    "ship_locations": [
                        [ship1["longitude"], ship1["latitude"]],
                        [ship2["longitude"], ship2["latitude"]],
                    ],
                    "event": "Collision",
                    "risk": risk,
                    "current_distance_metres": round(
                        cpa["current_distance_metres"], 1
                    ),
                    "dcpa_metres": round(cpa["dcpa_metres"], 1),
                    "tcpa_minutes": round(tcpa_minutes, 2),
                    "relative_speed_knots": round(
                        cpa["relative_speed_mps"]
                        / KNOTS_TO_METRES_PER_SECOND,
                        2,
                    ),
                    "safety_distance_metres": round(safety_distance, 1),
                    "details": details,
                    "detail": details,
                }
            )

    _event_state(results, reference_time, config["event_retention_minutes"])
    results.sort(key=lambda item: (item["tcpa_minutes"], item["pair"]))
    return JsonResponse(
        {
            "type": "Collision",
            "timestamp": reference_time.isoformat(),
            "count": len(results),
            "results": results,
            "skipped_count": skipped_count,
            "stale_count": stale_count,
            "rule": {
                "model": "DCPA/TCPA（恒航向、恒航速）",
                "warning_tcpa_minutes": config["warning_tcpa_minutes"],
                "warning_dcpa_metres": config["warning_dcpa_metres"],
                "critical_tcpa_minutes": config["critical_tcpa_minutes"],
                "critical_dcpa_metres": config["critical_dcpa_metres"],
            },
        }
    )
