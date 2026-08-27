"""Persist heterogeneous detector payloads as queryable event records."""

import hashlib
import json
import logging
import math
import uuid
from datetime import timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import ViolationAISTrajectoryPoint, ViolationEventRecord
from .trajectory_history import get_ais_history


LOGGER = logging.getLogger(__name__)
CONTINUOUS_EVENT_GAP = timedelta(minutes=5)

VIOLATION_FEATURE_LABELS = {
    "detect-ais-off": "关闭AIS船舶检测",
    "detect-spoofing": "身份伪造船舶检测",
    "detect-smuggling": "海上走私风险识别",
    "detect-highSpeedBoat": "高速快艇检测",
    "detect-overload": "超载船舶检测",
    "detect-illegalFishing": "非法捕捞船舶检测",
    "detect-illegalSandMining": "盗采海砂船舶检测",
    "detect-illegalFarming": "非法养殖检测",
    "detect-illegalPollutionDis": "非法排污船舶检测",
    "detect-illegalStaying": "非法驻留船舶检测",
    "detect-abnormalWandering": "异常徘徊船舶检测",
    "detect-abnormalStaying": "异常停泊船舶检测",
    "detect-illegalAnchored": "非法抛锚船舶检测",
    "detect-CrossingBoundary": "海上围栏越界船舶检测",
    "detect-doubleDragging": "双拖船舶检测",
    "detect-abnormalTransfer": "异常接驳船舶检测",
    "detect-deviation": "偏离航道船舶检测",
    "detect-collision": "船舶碰撞检测",
    "detect-blackList": "黑名单船舶检测",
    "detect-lowSpeedBoat": "低速船舶检测",
    "detect-illegalBerthing": "非法搭靠船舶检测",
}


def _text(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = " / ".join(str(item).strip() for item in value if str(item).strip())
        value = str(value).strip()
        if value:
            return value
    return ""


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coordinates(result):
    longitude = _finite_float(result.get("lon", result.get("longitude")))
    latitude = _finite_float(result.get("lat", result.get("latitude")))
    location = result.get("location")
    if (
        (longitude is None or latitude is None)
        and isinstance(location, (list, tuple))
        and len(location) >= 2
    ):
        longitude = _finite_float(location[0])
        latitude = _finite_float(location[1])
    if longitude is not None and not -180 <= longitude <= 180:
        longitude = None
    if latitude is not None and not -90 <= latitude <= 90:
        latitude = None
    return longitude, latitude


def _aware_datetime(*values):
    for value in values:
        if isinstance(value, str):
            value = parse_datetime(value)
        if value is None:
            continue
        if timezone.is_naive(value):
            value = timezone.make_aware(value, dt_timezone.utc)
        return value
    return None


def _target_mmsis(result):
    pair = result.get("pair_mmsi")
    if isinstance(pair, (list, tuple)) and pair:
        values = pair
    else:
        values = (
            result.get("mmsi"),
            result.get("other_mmsi"),
            result.get("ais_id"),
        )
    return sorted({str(value).strip() for value in values if str(value or "").strip()})


def _target_id(result):
    mmsis = _target_mmsis(result)
    if mmsis:
        return " / ".join(mmsis)
    return _text(
        result.get("radar_id"),
        result.get("target_id"),
        result.get("ship_id"),
    )


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalised_record_data(
    feature_id,
    payload,
    result,
    detected_at,
    source_namespace="operational",
    simulation_id=None,
    persistence_scope=None,
):
    target_id = _target_id(result)
    status = _text(result.get("status"), result.get("event"))
    risk = _text(result.get("risk"), result.get("risk_level"))
    details = _text(
        result.get("details"),
        result.get("detail"),
        result.get("reason"),
        payload.get("message"),
    )
    record_identifier = _text(
        result.get("prediction_id"),
        result.get("event_id"),
        result.get("alert_id"),
    )
    signature_data = {
        "feature_id": feature_id,
        "target_id": target_id,
        "status": status,
        "risk": risk,
        "zone": _text(result.get("zone"), result.get("area")),
    }
    if persistence_scope:
        signature_data["persistence_scope"] = persistence_scope
    signature = json.dumps(
        signature_data,
        ensure_ascii=False,
        sort_keys=True,
    )
    fingerprint = _hash(signature)
    event_key_source = f"{feature_id}:{record_identifier}"
    if persistence_scope:
        event_key_source = f"{persistence_scope}:{event_key_source}"
    event_key = (
        _hash(event_key_source)
        if record_identifier
        else ""
    )
    longitude, latitude = _coordinates(result)
    return {
        "source_namespace": str(source_namespace or "operational").strip().lower(),
        "simulation_id": str(simulation_id or "").strip(),
        "event_key": event_key,
        "fingerprint": fingerprint,
        "feature_id": feature_id,
        "event_type": VIOLATION_FEATURE_LABELS.get(feature_id, feature_id),
        "target_id": target_id,
        "target_name": _text(result.get("name"), result.get("ship_name")),
        "status": status,
        "risk_level": risk,
        "longitude": longitude,
        "latitude": latitude,
        "event_time": _aware_datetime(
            result.get("timestamp"),
            result.get("first_detected_at"),
            payload.get("timestamp"),
        ),
        "last_detected_at": detected_at,
        "details": details,
        "raw_data": result,
    }


def _save_one(data, detected_at):
    with transaction.atomic():
        record = None
        matched_by_continuity = False
        if data["event_key"]:
            record = (
                ViolationEventRecord.objects.select_for_update()
                .filter(event_key=data["event_key"])
                .first()
            )

        # Keep the anchoring continuity fallback for legacy records created
        # before the unified prediction_id was added.
        if record is None and (
            not data["event_key"]
            or data["feature_id"] == "detect-illegalAnchored"
        ):
            candidates = (
                ViolationEventRecord.objects.select_for_update()
                .filter(
                    source_namespace=data["source_namespace"],
                    simulation_id=data["simulation_id"],
                    fingerprint=data["fingerprint"],
                    last_detected_at__gte=detected_at - CONTINUOUS_EVENT_GAP,
                    first_detected_at__lte=detected_at + CONTINUOUS_EVENT_GAP,
                )
                .order_by("-last_detected_at")
            )
            incoming_episode_start = _text(
                data["raw_data"].get("episode_started_at")
            )
            for candidate in candidates[:20]:
                existing_episode_start = _text(
                    candidate.raw_data.get("episode_started_at")
                    if isinstance(candidate.raw_data, dict)
                    else None
                )
                if (
                    incoming_episode_start
                    and existing_episode_start
                    and incoming_episode_start != existing_episode_start
                ):
                    continue
                record = candidate
                matched_by_continuity = True
                break

        if record is None:
            create_data = data.copy()
            create_data["event_key"] = create_data["event_key"] or uuid.uuid4().hex
            create_data["first_detected_at"] = detected_at
            return ViolationEventRecord.objects.create(**create_data)

        previous_last_detected_at = record.last_detected_at
        latest_fields = (
            "event_type",
            "target_id",
            "target_name",
            "status",
            "risk_level",
            "longitude",
            "latitude",
            "event_time",
            "details",
            "raw_data",
        )
        is_latest_observation = detected_at >= previous_last_detected_at
        if is_latest_observation:
            for field in latest_fields:
                setattr(record, field, data[field])
        record.first_detected_at = min(record.first_detected_at, detected_at)
        record.last_detected_at = max(previous_last_detected_at, detected_at)
        record.occurrence_count += 1
        update_fields = [
            "first_detected_at",
            "last_detected_at",
            "occurrence_count",
        ]
        if matched_by_continuity and data["event_key"]:
            # Adopt the stable post-fix key so subsequent observations use the
            # exact-key path instead of repeatedly relying on the fallback.
            record.event_key = data["event_key"]
            update_fields.append("event_key")
        if is_latest_observation:
            update_fields.extend(latest_fields)
        record.save(update_fields=update_fields)
        return record


def _trajectory_source_points(
    result,
    ais_snapshot,
    detected_at=None,
    namespace=None,
):
    mmsis = set(_target_mmsis(result))
    if not mmsis:
        return []

    points = [
        (str(item["mmsi"]), item)
        for item in get_ais_history(
            mmsis,
            end_at=detected_at,
            namespace=namespace,
        )
    ]
    if isinstance(ais_snapshot, list):
        for item in ais_snapshot:
            if not isinstance(item, dict):
                continue
            mmsi = _text(item.get("mmsi"), item.get("MMSI"), item.get("ais_id"))
            if mmsi in mmsis:
                points.append((mmsi, item))

    has_timestamped_ais_source = any(
        _coordinates(item) != (None, None)
        and _aware_datetime(item.get("timestamp"), item.get("time")) is not None
        for _, item in points
    )
    if (
        len(mmsis) == 1
        and not has_timestamped_ais_source
        and _coordinates(result) != (None, None)
    ):
        points.append((next(iter(mmsis)), result))
    trajectory_started_at = _aware_datetime(
        result.get("trajectory_started_at")
    )
    if trajectory_started_at is not None:
        points = [
            (mmsi, item)
            for mmsi, item in points
            if (
                _aware_datetime(
                    item.get("timestamp"),
                    item.get("time"),
                    detected_at,
                )
                or trajectory_started_at
            )
            >= trajectory_started_at
        ]
    return points


def _distance_meters(first, second):
    latitude1 = math.radians(first[0])
    latitude2 = math.radians(second[0])
    latitude_delta = latitude2 - latitude1
    longitude_delta = math.radians(second[1] - first[1])
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude1)
        * math.cos(latitude2)
        * math.sin(longitude_delta / 2) ** 2
    )
    value = min(1.0, max(0.0, value))
    return 2 * 6371000 * math.asin(math.sqrt(value))


def _actual_detection_time(result, payload, ais_snapshot):
    """Use the latest matching AIS timestamp as the detection time."""
    target_mmsis = set(_target_mmsis(result))
    ais_times = []
    if target_mmsis and isinstance(ais_snapshot, list):
        for source in ais_snapshot:
            if not isinstance(source, dict):
                continue
            source_mmsi = _text(
                source.get("mmsi"),
                source.get("MMSI"),
                source.get("ais_id"),
                source.get("id"),
            )
            if source_mmsi not in target_mmsis:
                continue
            timestamp = _aware_datetime(
                source.get("timestamp"),
                source.get("time"),
            )
            if timestamp is not None:
                ais_times.append(timestamp)
    if ais_times:
        return max(ais_times)
    return _aware_datetime(
        result.get("timestamp"),
        payload.get("timestamp"),
        payload.get("computed_at"),
    ) or timezone.now()


def save_trajectory_sources(record, sources, *fallback_timestamps):
    """Persist source points using the same temporal and spatial dedup rules."""
    candidates = []
    for mmsi, source in sources:
        longitude, latitude = _coordinates(source)
        if longitude is None or latitude is None:
            continue
        observed_at = _aware_datetime(
            source.get("timestamp"),
            source.get("time"),
            *fallback_timestamps,
        )
        if observed_at is None:
            continue
        candidates.append(
            {
                "mmsi": mmsi,
                "observed_at": observed_at,
                "longitude": longitude,
                "latitude": latitude,
                "speed": _finite_float(source.get("speed", source.get("sog"))),
                "course": _finite_float(source.get("course", source.get("cog"))),
                "raw_data": source,
            }
        )

    if not candidates:
        return 0

    existing = list(
        record.ais_trajectory.only(
            "mmsi",
            "observed_at",
            "longitude",
            "latitude",
        )
    )
    seen = {(point.mmsi, point.observed_at) for point in existing}
    minimum_distance = max(
        0.0,
        float(
            getattr(
                settings,
                "AIS_TRAJECTORY_MIN_DISTANCE_METERS",
                3.0,
            )
        ),
    )
    maximum_interval = max(
        0.0,
        float(
            getattr(
                settings,
                "AIS_TRAJECTORY_MAX_INTERVAL_SECONDS",
                180.0,
            )
        ),
    )
    timeline = [
        (
            point.observed_at,
            point.mmsi,
            0,
            point,
        )
        for point in existing
    ] + [
        (
            point["observed_at"],
            point["mmsi"],
            1,
            point,
        )
        for point in candidates
    ]
    timeline.sort(key=lambda item: item[:3])

    last_saved = {}
    points = []
    for observed_at, mmsi, source_type, point in timeline:
        if source_type == 0:
            last_saved[mmsi] = (
                observed_at,
                (point.latitude, point.longitude),
            )
            continue
        identity = (mmsi, point["observed_at"])
        if identity in seen:
            continue
        position = (point["latitude"], point["longitude"])
        previous = last_saved.get(mmsi)
        distance_below_threshold = (
            previous is not None
            and _distance_meters(previous[1], position) < minimum_distance
        )
        interval_reached = (
            previous is not None
            and maximum_interval > 0
            and (observed_at - previous[0]).total_seconds()
            >= maximum_interval
        )
        if (
            distance_below_threshold
            and not interval_reached
        ):
            continue
        seen.add(identity)
        last_saved[mmsi] = (observed_at, position)
        points.append(
            ViolationAISTrajectoryPoint(
                event=record,
                **point,
            )
        )
    if points:
        ViolationAISTrajectoryPoint.objects.bulk_create(points, ignore_conflicts=True)
    return len(points)


def _save_trajectory(
    record,
    result,
    payload,
    ais_snapshot,
    detected_at,
    trajectory_namespace=None,
):
    return save_trajectory_sources(
        record,
        _trajectory_source_points(
            result,
            ais_snapshot,
            detected_at=detected_at,
            namespace=trajectory_namespace,
        ),
        result.get("timestamp"),
        payload.get("timestamp"),
        detected_at,
    )


def _crossing_retained_event_is_active(result):
    """Return whether a retained enter event may receive more AIS points."""
    if result.get("is_new", True):
        return True
    if result.get("crossing_direction") != "enter":
        return False

    fence_id = _text(result.get("fence_id"))
    mmsis = _target_mmsis(result)
    if not fence_id or len(mmsis) != 1:
        return False

    # The crossing detector updates this state before persistence runs.  It
    # remains authoritative even when an enter-only fence intentionally emits
    # no result for the later exit transition.
    from CrossingBoundary.views import CROSSING_STATE_CACHE_KEY

    state = cache.get(CROSSING_STATE_CACHE_KEY, {})
    if not isinstance(state, dict):
        return False
    vessel_state = state.get(f"{fence_id}:{mmsis[0]}")
    return (
        isinstance(vessel_state, dict)
        and vessel_state.get("stable_inside") is True
    )


def persist_detection_payload(
    feature_id,
    payload,
    ais_snapshot=None,
    trajectory_namespace=None,
    source_namespace="operational",
    simulation_id=None,
    persistence_scope=None,
):
    """Persist alert results without allowing database errors to stop inference."""
    if not isinstance(payload, dict) or not payload.get("success", True):
        return []
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return []
    # Crossing results remain in the public payload for a retention window,
    # but display retention is not an active trajectory lifecycle.  A retained
    # enter event may append AIS only while detector state is still inside the
    # same fence. Retained exit/transit events never append post-crossing AIS.
    try:
        if int(payload.get("count", len(results))) <= 0:
            return []
    except (TypeError, ValueError):
        pass

    saved = []
    try:
        for result in results:
            if not isinstance(result, dict):
                continue
            crossing_retained = (
                feature_id == "detect-CrossingBoundary"
                and not result.get("is_new", True)
            )
            if (
                crossing_retained
                and not _crossing_retained_event_is_active(result)
            ):
                continue
            detected_at = _actual_detection_time(
                result,
                payload,
                ais_snapshot,
            )
            data = _normalised_record_data(
                feature_id,
                payload,
                result,
                detected_at,
                source_namespace=source_namespace,
                simulation_id=simulation_id,
                persistence_scope=persistence_scope,
            )
            if crossing_retained:
                # Keep the original occurrence metadata. Retained display
                # frames are not new detections; they only carry later AIS for
                # an enter event whose vessel remains inside the fence.
                record = (
                    ViolationEventRecord.objects.filter(
                        event_key=data["event_key"]
                    ).first()
                )
                if record is None:
                    record = _save_one(data, detected_at)
            else:
                record = _save_one(data, detected_at)
            _save_trajectory(
                record,
                result,
                payload,
                ais_snapshot,
                detected_at,
                trajectory_namespace=trajectory_namespace,
            )
            saved.append(record)
    except Exception:
        LOGGER.exception("Could not persist violation records for %s", feature_id)
    return saved
