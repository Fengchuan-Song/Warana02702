import math
from datetime import datetime, timedelta

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware
from django.views.decorators.http import require_GET

from .models import HighSpeedPoint


SPEED_THRESHOLD = 30.0
DURATION_THRESHOLD = 300
RETENTION_MULTIPLIER = 1.5


def _parse_event_timestamp(value):
    """Parse an AIS event timestamp and always return an aware datetime."""
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = parse_datetime(value)
    else:
        return None

    if timestamp is None:
        return None
    if is_naive(timestamp):
        timestamp = make_aware(timestamp, timezone.get_current_timezone())
    return timestamp


def _normalise_ship(ship_info):
    """Validate and normalise one cached AIS record."""
    if not isinstance(ship_info, dict):
        return None

    raw_mmsi = ship_info.get("mmsi")
    mmsi = str(raw_mmsi).strip() if raw_mmsi is not None else ""
    if not mmsi:
        return None

    try:
        speed = float(ship_info.get("speed"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(speed):
        return None

    timestamp = _parse_event_timestamp(ship_info.get("timestamp"))
    if timestamp is None:
        return None

    return {
        "mmsi": mmsi,
        "speed": speed,
        "timestamp": timestamp,
        "source": ship_info,
    }


def _empty_response(skipped_count=0):
    return JsonResponse(
        {
            "success": True,
            "type": "高速快艇预警",
            "timestamp": None,
            "count": 0,
            "results": [],
            "skipped_count": skipped_count,
            "message": "暂无有效AIS数据",
        }
    )


@require_GET
def detect_high_speed(request):
    ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return _empty_response()

    # A cache snapshot should contain one latest point per vessel. If upstream
    # sends duplicates, retain only the newest event for deterministic results.
    ships_by_mmsi = {}
    skipped_count = 0
    for ship_info in ship_list:
        ship = _normalise_ship(ship_info)
        if ship is None:
            skipped_count += 1
            continue

        existing = ships_by_mmsi.get(ship["mmsi"])
        if existing is None or ship["timestamp"] >= existing["timestamp"]:
            ships_by_mmsi[ship["mmsi"]] = ship

    ships = list(ships_by_mmsi.values())
    if not ships:
        return _empty_response(skipped_count=skipped_count)

    # The unique constraint makes this operation idempotent when the browser
    # polls the same Redis snapshot repeatedly.
    HighSpeedPoint.objects.bulk_create(
        [
            HighSpeedPoint(
                mmsi=ship["mmsi"],
                speed=ship["speed"],
                timestamp=ship["timestamp"],
            )
            for ship in ships
        ],
        ignore_conflicts=True,
    )

    latest_timestamp = max(ship["timestamp"] for ship in ships)
    retention = timedelta(seconds=RETENTION_MULTIPLIER * DURATION_THRESHOLD)
    HighSpeedPoint.objects.filter(
        timestamp__lt=latest_timestamp - retention
    ).delete()

    all_alerts = []
    for ship in ships:
        speed = ship["speed"]
        if speed <= SPEED_THRESHOLD:
            continue

        mmsi = ship["mmsi"]
        timestamp = ship["timestamp"]

        # A high-speed streak starts immediately after the last point that did
        # not exceed the threshold. The previous implementation accidentally
        # searched for the last high-speed point, resetting duration to zero.
        last_normal_timestamp = (
            HighSpeedPoint.objects.filter(
                mmsi=mmsi,
                speed__lte=SPEED_THRESHOLD,
                timestamp__lt=timestamp,
            )
            .order_by("-timestamp")
            .values_list("timestamp", flat=True)
            .first()
        )

        streak_points = HighSpeedPoint.objects.filter(
            mmsi=mmsi,
            speed__gt=SPEED_THRESHOLD,
            timestamp__lte=timestamp,
        )
        if last_normal_timestamp is not None:
            streak_points = streak_points.filter(
                timestamp__gt=last_normal_timestamp
            )

        start_timestamp = (
            streak_points.order_by("timestamp")
            .values_list("timestamp", flat=True)
            .first()
        )
        if start_timestamp is None:
            continue

        duration = (timestamp - start_timestamp).total_seconds()
        if duration >= DURATION_THRESHOLD:
            source = ship["source"]
            all_alerts.append(
                {
                    "mmsi": mmsi,
                    "location": [source.get("lon"), source.get("lat")],
                    "name": source.get("name"),
                    "details": (
                        f"检测为高速快艇，当前速度为{speed:.2f}节，"
                        f"高于规定最大航速{SPEED_THRESHOLD:.2f}节，"
                        f"且已持续高速航行 {int(duration)}s。"
                    ),
                }
            )

    return JsonResponse(
        {
            "success": True,
            "type": "高速快艇预警",
            "timestamp": latest_timestamp.isoformat(),
            "count": len(all_alerts),
            "results": all_alerts,
            "skipped_count": skipped_count,
            "message": "检测成功",
        }
    )
