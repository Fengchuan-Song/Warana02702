from datetime import datetime, time, timedelta, timezone as dt_timezone
import json
import re

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_GET, require_http_methods

from .detection import (
    DETECTORS,
    EXTERNAL_DETECTION_FEATURES,
    get_detection_result,
)
from .models import (
    DetectionModelConfiguration,
    ViolationAISTrajectoryPoint,
    ViolationEventRecord,
)
from .model_parameters import (
    MODEL_PARAMETER_SCHEMAS,
    serialize_configuration,
    validate_parameters,
)
from .maritime_zones import (
    MaritimeZoneDataError,
    SUPPORTED_ZONE_TYPES,
    maritime_zone_geojson,
)
from .violation_records import VIOLATION_FEATURE_LABELS


@require_GET
def cached_detection_result(request, feature_id):
    """Return the most recent backend-produced result without running detection."""
    if (
        feature_id not in DETECTORS
        and feature_id not in EXTERNAL_DETECTION_FEATURES
    ):
        return JsonResponse(
            {
                "success": False,
                "feature_id": feature_id,
                "count": 0,
                "results": [],
                "message": "未知检测类型",
            },
            status=404,
        )

    return JsonResponse(get_detection_result(feature_id))


def _model_parameter_error(message, status=400):
    return JsonResponse(
        {"success": False, "message": message},
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


@require_GET
def detection_model_parameter_collection(request):
    return JsonResponse(
        {
            "success": True,
            "results": [
                serialize_configuration(feature_id)
                for feature_id in MODEL_PARAMETER_SCHEMAS
            ],
        },
        json_dumps_params={"ensure_ascii": False},
    )


@require_http_methods(["GET", "PUT", "DELETE"])
def detection_model_parameter_detail(request, feature_id):
    if feature_id not in MODEL_PARAMETER_SCHEMAS:
        return _model_parameter_error(
            "该模型没有可配置的数值参数",
            status=404,
        )

    if request.method == "GET":
        return JsonResponse(
            {
                "success": True,
                "configuration": serialize_configuration(feature_id),
            },
            json_dumps_params={"ensure_ascii": False},
        )

    if request.method == "DELETE":
        DetectionModelConfiguration.objects.filter(
            feature_id=feature_id
        ).delete()
        return JsonResponse(
            {
                "success": True,
                "message": "已恢复系统默认参数",
                "configuration": serialize_configuration(feature_id),
            },
            json_dumps_params={"ensure_ascii": False},
        )

    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        return _model_parameter_error("请求体必须是有效的 JSON")
    if not isinstance(payload, dict):
        return _model_parameter_error("请求体必须是 JSON 对象")
    try:
        parameters = validate_parameters(
            feature_id,
            payload.get("parameters"),
        )
    except ValueError as exc:
        return _model_parameter_error(str(exc))

    DetectionModelConfiguration.objects.update_or_create(
        feature_id=feature_id,
        defaults={"parameters": parameters},
    )
    return JsonResponse(
        {
            "success": True,
            "message": "模型参数已保存，将在下一次分析时生效",
            "configuration": serialize_configuration(feature_id),
        },
        json_dumps_params={"ensure_ascii": False},
    )


def _query_values(request, name):
    values = []
    for raw_value in request.GET.getlist(name):
        values.extend(
            item.strip().upper()
            for item in raw_value.split(",")
            if item.strip()
        )
    return set(values)


@require_GET
def maritime_zone_collection(request):
    """Return bundled Guangdong/Hong Kong/Macau zones as GeoJSON."""
    zone_types = _query_values(request, "type")
    unknown_types = zone_types.difference(SUPPORTED_ZONE_TYPES)
    if unknown_types:
        return JsonResponse(
            {
                "success": False,
                "message": "未知海事区域类型: "
                + ", ".join(sorted(unknown_types)),
            },
            status=400,
        )
    locodes = _query_values(request, "locode")
    try:
        payload = maritime_zone_geojson(
            zone_types=zone_types or None,
            locodes=locodes or None,
        )
    except MaritimeZoneDataError:
        return JsonResponse(
            {
                "success": False,
                "message": "海事区域加密数据暂时不可用",
            },
            status=503,
        )
    payload["success"] = True
    return JsonResponse(payload)


def _filter_datetime(value, end_of_day=False):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        parsed_date = parse_date(value)
        if parsed_date is None:
            raise ValueError("日期时间格式不正确")
        parsed = datetime.combine(parsed_date, time.min, tzinfo=dt_timezone.utc)
        if end_of_day:
            parsed += timedelta(days=1)
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _serialize_violation_record(record, include_trajectory=False):
    trajectory_count = getattr(record, "trajectory_count", None)
    if trajectory_count is None:
        trajectory_count = record.ais_trajectory.count()
    data = {
        "id": record.id,
        "feature_id": record.feature_id,
        "event_type": record.event_type,
        "target_id": record.target_id,
        "target_name": record.target_name,
        "status": record.status,
        "risk_level": record.risk_level,
        "longitude": record.longitude,
        "latitude": record.latitude,
        "event_time": record.event_time.isoformat() if record.event_time else None,
        "first_detected_at": record.first_detected_at.isoformat(),
        "last_detected_at": record.last_detected_at.isoformat(),
        "occurrence_count": record.occurrence_count,
        "details": record.details,
        "trajectory_count": trajectory_count,
    }
    if include_trajectory:
        data["ais_trajectory"] = [
            {
                "mmsi": point.mmsi,
                "timestamp": point.observed_at.isoformat(),
                "longitude": point.longitude,
                "latitude": point.latitude,
                "speed": point.speed,
                "course": point.course,
            }
            for point in record.ais_trajectory.all()
        ]
    return data


@require_GET
def violation_record_list(request):
    records = ViolationEventRecord.objects.all()
    feature_id = request.GET.get("feature_id", "").strip()
    target = request.GET.get("target", "").strip()
    if feature_id:
        records = records.filter(feature_id=feature_id)
    if target:
        records = records.filter(
            Q(target_id__icontains=target)
            | Q(target_name__icontains=target)
            | Q(details__icontains=target)
        )
    try:
        start = _filter_datetime(request.GET.get("start"))
        end = _filter_datetime(request.GET.get("end"), end_of_day=True)
    except ValueError as exc:
        return JsonResponse({"success": False, "message": str(exc)}, status=400)
    if start:
        records = records.filter(last_detected_at__gte=start)
    if end:
        records = records.filter(last_detected_at__lt=end)

    statistics_rows = list(
        records.values("feature_id", "event_type")
        .annotate(count=Count("id"))
        .order_by("-count", "event_type")
    )
    statistics_total = sum(row["count"] for row in statistics_rows)
    statistics = {
        "total": statistics_total,
        "items": [
            {
                "feature_id": row["feature_id"],
                "event_type": row["event_type"],
                "count": row["count"],
                "percentage": round(
                    row["count"] * 100 / statistics_total,
                    2,
                )
                if statistics_total
                else 0,
            }
            for row in statistics_rows
        ],
    }
    records = records.annotate(
        trajectory_count=Count("ais_trajectory")
    ).order_by("-last_detected_at", "-id")

    try:
        page_size = max(1, min(int(request.GET.get("page_size", 20)), 100))
    except (TypeError, ValueError):
        page_size = 20
    page = Paginator(records, page_size).get_page(request.GET.get("page", 1))
    return JsonResponse(
        {
            "success": True,
            "count": page.paginator.count,
            "page": page.number,
            "page_size": page_size,
            "total_pages": page.paginator.num_pages,
            "has_previous": page.has_previous(),
            "has_next": page.has_next(),
            "statistics": statistics,
            "feature_options": [
                {"value": key, "label": label}
                for key, label in VIOLATION_FEATURE_LABELS.items()
            ],
            "results": [_serialize_violation_record(record) for record in page],
        },
        json_dumps_params={"ensure_ascii": False},
    )


@require_GET
def violation_record_detail(request, record_id):
    record = get_object_or_404(ViolationEventRecord, pk=record_id)
    return JsonResponse(
        {
            "success": True,
            "record": _serialize_violation_record(
                record,
                include_trajectory=True,
            ),
        },
        json_dumps_params={"ensure_ascii": False},
    )


MMSI_PATTERN = re.compile(r"^\d{9}$")


@require_GET
def ship_trajectory(request, mmsi=None):
    """Return persisted violation-related AIS history for one MMSI."""
    mmsi = str(mmsi or request.GET.get("mmsi", "")).strip()
    if not mmsi:
        return JsonResponse(
            {"success": False, "message": "缺少必填参数 mmsi"},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )
    if MMSI_PATTERN.fullmatch(mmsi) is None:
        return JsonResponse(
            {"success": False, "message": "mmsi 必须是9位数字"},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )

    try:
        start = _filter_datetime(request.GET.get("start"))
        end_value = request.GET.get("end")
        end = (
            _filter_datetime(end_value, end_of_day=True)
            if end_value
            else timezone.now()
        )

        trajectory_time_window = timedelta(hours=5)
        if start is None:
            start = end - trajectory_time_window
    except ValueError as exc:
        return JsonResponse(
            {"success": False, "message": str(exc)},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )
    if start is not None and end is not None and start >= end:
        return JsonResponse(
            {"success": False, "message": "start 必须早于 end"},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )

    if end - start > trajectory_time_window:
        return JsonResponse(
            {"success": False, "message": "轨迹查询时间范围不能超过5小时"},
            status=400,
            json_dumps_params={"ensure_ascii": False},
        )

    points = ViolationAISTrajectoryPoint.objects.filter(mmsi=mmsi)
    if start is not None:
        points = points.filter(observed_at__gte=start)
    if end is not None:
        points = points.filter(observed_at__lt=end)

    # A single AIS observation may be attached to several violation events.
    # Return it only once when querying the vessel's combined history.
    points = (
        points.values(
            "observed_at",
            "longitude",
            "latitude",
            "speed",
            "course",
        )
        .order_by(
            "-observed_at",
            "-longitude",
            "-latitude",
            "-speed",
            "-course",
        )
        .distinct()
    )

    # 保留的单次最多 100 点实现（当前停用）：
    # try:
    #     page_size = max(
    #         1,
    #         min(int(request.GET.get("page_size", 100)), 100),
    #     )
    # except (TypeError, ValueError):
    #     page_size = 100

    try:
        page_size = max(
            1,
            min(int(request.GET.get("page_size", 50)), 50),
        )
    except (TypeError, ValueError):
        page_size = 50
    page = Paginator(points, page_size).get_page(request.GET.get("page", 1))
    # Query newest-first so page 1 contains the points nearest to `end`, then
    # serialize each page chronologically for direct map rendering.
    page_points = reversed(list(page))
    return JsonResponse(
        {
            "success": True,
            "mmsi": mmsi,
            "source": "violation_event_trajectory",
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "count": page.paginator.count,
            "page": page.number,
            "page_size": page_size,
            "total_pages": page.paginator.num_pages,
            "has_previous": page.has_previous(),
            "has_next": page.has_next(),
            "trajectory": [
                {
                    "timestamp": point["observed_at"].isoformat(),
                    "longitude": point["longitude"],
                    "latitude": point["latitude"],
                    "speed": point["speed"],
                    "course": point["course"],
                }
                for point in page_points
            ],
        },
        json_dumps_params={"ensure_ascii": False},
    )
