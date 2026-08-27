import json
import math

from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from AISData.detection import detection_cache_key
from AISData.normalization import (
    UNKNOWN_AIS_TARGET_NAME,
    normalise_ais_name,
)

from .models import BlackList


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


def _aliased_value(data, snake_name, model_name):
    if snake_name in data:
        return True, data[snake_name]
    if model_name in data:
        return True, data[model_name]
    return False, None


def _optional_dimension(value, label):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是数字")
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}必须是大于 0 的有限数字")
    return number


def _validate_blacklist_data(data, current=None, partial=False):
    cleaned = {}

    has_mmsi, raw_mmsi = _aliased_value(data, "mmsi", "mmsi")
    if has_mmsi:
        mmsi = str(raw_mmsi or "").strip()
        if len(mmsi) != 9 or not mmsi.isdigit():
            raise ValueError("MMSI 必须是 9 位数字")
        cleaned["mmsi"] = mmsi
    elif current is None or not partial:
        raise ValueError("MMSI 不能为空")

    for api_name, model_name, label, max_length in (
        ("ship_name", "shipName", "船舶名称", 32),
        ("ship_type", "shipType", "船舶类型", 32),
    ):
        has_value, raw_value = _aliased_value(data, api_name, model_name)
        if has_value:
            value = str(raw_value or "").strip()
            if len(value) > max_length:
                raise ValueError(f"{label}不能超过 {max_length} 个字符")
            cleaned[model_name] = value
        elif current is None:
            cleaned[model_name] = ""

    for field, label in (("length", "船长"), ("width", "船宽")):
        if field in data:
            cleaned[field] = _optional_dimension(data[field], label)
        elif current is None:
            cleaned[field] = None

    if "reason" in data:
        reason = str(data["reason"] or "").strip()
        if len(reason) > 1000:
            raise ValueError("列入原因不能超过 1000 个字符")
        cleaned["reason"] = reason
    elif current is None:
        cleaned["reason"] = ""

    if "is_active" in data:
        if not isinstance(data["is_active"], bool):
            raise ValueError("is_active 必须是布尔值")
        cleaned["is_active"] = data["is_active"]
    elif current is None:
        cleaned["is_active"] = True

    return cleaned


def _serialize_blacklist(entry):
    return {
        "id": entry.id,
        "mmsi": entry.mmsi,
        "ship_name": entry.shipName,
        "ship_type": entry.shipType,
        "length": entry.length,
        "width": entry.width,
        "reason": entry.reason,
        "is_active": entry.is_active,
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }


def _invalidate_detection_result():
    cache.delete(detection_cache_key("detect-blackList"))


def _latest_ship_timestamp(ship_list):
    latest_value = None
    latest_datetime = None
    for ship_info in ship_list:
        if not isinstance(ship_info, dict):
            continue
        value = ship_info.get("timestamp")
        parsed = parse_datetime(value) if isinstance(value, str) else None
        if parsed is None:
            if latest_value is None and value is not None:
                latest_value = value
            continue
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        if latest_datetime is None or parsed > latest_datetime:
            latest_datetime = parsed
            latest_value = value
    return latest_value


@require_http_methods(["GET", "POST"])
def blacklist_collection(request):
    if request.method == "GET":
        entries = BlackList.objects.all()
        query = request.GET.get("q", "").strip()
        if query:
            entries = entries.filter(
                Q(mmsi__icontains=query)
                | Q(shipName__icontains=query)
                | Q(shipType__icontains=query)
                | Q(reason__icontains=query)
            )

        active = request.GET.get("active")
        if active is not None:
            normalized = active.strip().lower()
            if normalized not in {"true", "false", "1", "0"}:
                return _json_error("active 参数必须是 true 或 false")
            entries = entries.filter(
                is_active=normalized in {"true", "1"}
            )

        return JsonResponse(
            {
                "success": True,
                "count": entries.count(),
                "results": [
                    _serialize_blacklist(entry) for entry in entries
                ],
            }
        )

    try:
        data = _validate_blacklist_data(_read_json(request))
        entry = BlackList.objects.create(**data)
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("该 MMSI 已存在于黑名单", status=409)
    _invalidate_detection_result()
    return JsonResponse(
        {
            "success": True,
            "message": "黑名单船舶创建成功",
            "result": _serialize_blacklist(entry),
        },
        status=201,
    )


@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def blacklist_detail(request, entry_id):
    try:
        entry = BlackList.objects.get(pk=entry_id)
    except BlackList.DoesNotExist:
        return _json_error("黑名单船舶不存在", status=404)

    if request.method == "GET":
        return JsonResponse(
            {"success": True, "result": _serialize_blacklist(entry)}
        )
    if request.method == "DELETE":
        entry.delete()
        _invalidate_detection_result()
        return JsonResponse(
            {"success": True, "message": "黑名单船舶删除成功"}
        )

    try:
        data = _validate_blacklist_data(
            _read_json(request),
            current=entry,
            partial=request.method == "PATCH",
        )
        for field, value in data.items():
            setattr(entry, field, value)
        entry.save()
    except ValueError as exc:
        return _json_error(str(exc))
    except IntegrityError:
        return _json_error("该 MMSI 已存在于黑名单", status=409)
    _invalidate_detection_result()
    return JsonResponse(
        {
            "success": True,
            "message": "黑名单船舶更新成功",
            "result": _serialize_blacklist(entry),
        }
    )


def detect_black_list(request):
    ship_list = getattr(request, "ais_ship_list", None)
    if ship_list is None:
        ship_list = cache.get("latest_ais_data_raw", [])
    if not isinstance(ship_list, list) or not ship_list:
        return JsonResponse(
            {
                "success": True,
                "type": "黑名单预警",
                "timestamp": None,
                "count": 0,
                "results": [],
                "message": "暂无 AIS 数据",
            }
        )

    entries = {
        entry.mmsi: entry
        for entry in BlackList.objects.filter(is_active=True)
    }
    results = []
    for ship_info in ship_list:
        if not isinstance(ship_info, dict):
            continue
        mmsi = str(ship_info.get("mmsi") or "").strip()
        entry = entries.get(mmsi)
        if entry is None:
            continue

        ais_name = normalise_ais_name(ship_info.get("name"))
        name = (
            entry.shipName
            if ais_name == UNKNOWN_AIS_TARGET_NAME and entry.shipName
            else ais_name
        )
        reason = entry.reason or "该船舶已列入黑名单"
        details = f"检测到黑名单船舶：{reason}。"
        results.append(
            {
                "mmsi": mmsi,
                "location": [
                    ship_info.get("lon"),
                    ship_info.get("lat"),
                ],
                "timestamp": ship_info.get("timestamp"),
                "name": name,
                "blacklist_id": entry.id,
                "ship_type": entry.shipType,
                "reason": reason,
                "details": details,
                "detail": details,
            }
        )

    return JsonResponse(
        {
            "success": True,
            "type": "黑名单预警",
            "timestamp": _latest_ship_timestamp(ship_list),
            "count": len(results),
            "results": results,
            "message": "检测成功",
        }
    )
