import json
import math
import re
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from .marine_weather_service import (
    MarineWeatherDataError,
    get_guangdong_marine_weather,
)
from .models import CameraConfiguration
from .typhoon_service import TyphoonDataError, get_current_typhoons


@never_cache
@ensure_csrf_cookie
def index(request):
    return render(request, 'Demo_v10.html')


@never_cache
@ensure_csrf_cookie
@require_GET
def model_parameter_page(request):
    """Render the full-page editor for runtime detection-model parameters."""
    return render(request, "model_parameters.html")


CAMERA_VIDEO_PATH = (
    Path(settings.BASE_DIR)
    / "Data"
    / "Video"
    / "a8105a2bbc46fb6c8418a742a56f7aa5.mp4"
)
RANGE_HEADER_PATTERN = re.compile(r"bytes=(\d*)-(\d*)$")
VIDEO_CHUNK_SIZE = 1024 * 1024
DEFAULT_CAMERA_KEY = "harbor-01"
DEFAULT_CAMERA_DATA = {
    "name": "港区摄像头 01",
    "longitude": 113.667,
    "latitude": 22.168,
    "video_url": "/api/cameras/harbor-01/video/",
    "is_visible": True,
}


def _video_chunks(file_path, start, length):
    with file_path.open("rb") as video_file:
        video_file.seek(start)
        remaining = length
        while remaining > 0:
            chunk = video_file.read(min(VIDEO_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@never_cache
@require_GET
def camera_video(request):
    """Stream the configured camera video, including HTTP range requests."""
    if not CAMERA_VIDEO_PATH.is_file():
        return HttpResponse("摄像头视频不存在", status=404)

    file_size = CAMERA_VIDEO_PATH.stat().st_size
    range_header = request.headers.get("Range", "").strip()
    match = RANGE_HEADER_PATTERN.fullmatch(range_header)

    if range_header and not match:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        return response

    if match:
        start_text, end_text = match.groups()
        if start_text:
            start = int(start_text)
            end = min(int(end_text), file_size - 1) if end_text else file_size - 1
        elif end_text:
            suffix_length = min(int(end_text), file_size)
            start = file_size - suffix_length
            end = file_size - 1
        else:
            start, end = 0, file_size - 1

        if start >= file_size or start > end:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            return response

        content_length = end - start + 1
        response = StreamingHttpResponse(
            _video_chunks(CAMERA_VIDEO_PATH, start, content_length),
            status=206,
            content_type="video/mp4",
        )
        response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        response["Content-Length"] = str(content_length)
    else:
        response = StreamingHttpResponse(
            _video_chunks(CAMERA_VIDEO_PATH, 0, file_size),
            content_type="video/mp4",
        )
        response["Content-Length"] = str(file_size)

    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = 'inline; filename="camera-harbor-01.mp4"'
    return response


def _camera_json_error(message, status=400):
    return JsonResponse(
        {"success": False, "message": message},
        status=status,
    )


def _serialize_camera(camera):
    return {
        "camera_key": camera.camera_key,
        "name": camera.name,
        "longitude": camera.longitude,
        "latitude": camera.latitude,
        "video_url": camera.video_url,
        "is_visible": camera.is_visible,
        "created_at": camera.created_at.isoformat(),
        "updated_at": camera.updated_at.isoformat(),
    }


def _read_camera_json(request):
    try:
        data = json.loads(request.body or b"{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ValueError("请求体必须是有效的 JSON")
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def _validate_camera_data(data):
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("摄像头名称不能为空")
    if len(name) > 100:
        raise ValueError("摄像头名称不能超过 100 个字符")

    try:
        longitude = float(data.get("longitude"))
        latitude = float(data.get("latitude"))
    except (TypeError, ValueError):
        raise ValueError("经纬度必须是数字")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("经度必须在 -180 到 180 之间")
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("纬度必须在 -90 到 90 之间")

    video_url = str(data.get("video_url", "")).strip()
    if not video_url:
        raise ValueError("视频地址不能为空")
    if len(video_url) > 2000:
        raise ValueError("视频地址不能超过 2000 个字符")

    is_visible = data.get("is_visible")
    if not isinstance(is_visible, bool):
        raise ValueError("is_visible 必须是布尔值")

    return {
        "name": name,
        "longitude": longitude,
        "latitude": latitude,
        "video_url": video_url,
        "is_visible": is_visible,
    }


def _validate_camera_key(value):
    camera_key = str(value or "").strip()
    if not camera_key:
        raise ValueError("摄像头标识不能为空")
    if len(camera_key) > 64:
        raise ValueError("摄像头标识不能超过 64 个字符")
    if not re.fullmatch(r"[-a-zA-Z0-9_]+", camera_key):
        raise ValueError("摄像头标识只能包含字母、数字、下划线和连字符")
    return camera_key


@never_cache
@require_http_methods(["GET", "POST"])
def camera_collection(request):
    if request.method == "GET":
        cameras = CameraConfiguration.objects.all()
        return JsonResponse(
            {
                "success": True,
                "count": cameras.count(),
                "results": [
                    _serialize_camera(camera) for camera in cameras
                ],
            }
        )

    try:
        data = _read_camera_json(request)
        camera_key = _validate_camera_key(data.get("camera_key"))
        cleaned = _validate_camera_data(data)
        camera = CameraConfiguration.objects.create(
            camera_key=camera_key,
            **cleaned,
        )
    except ValueError as exc:
        return _camera_json_error(str(exc))
    except IntegrityError:
        return _camera_json_error("该摄像头标识已存在", status=409)

    return JsonResponse(
        {
            "success": True,
            "created": True,
            "message": "摄像头已创建",
            "camera": _serialize_camera(camera),
        },
        status=201,
    )


@never_cache
@require_http_methods(["GET", "PUT", "DELETE"])
def camera_detail(request, camera_key):
    try:
        camera = CameraConfiguration.objects.get(camera_key=camera_key)
    except CameraConfiguration.DoesNotExist:
        return _camera_json_error("摄像头不存在", status=404)

    if request.method == "GET":
        return JsonResponse(
            {
                "success": True,
                "camera": _serialize_camera(camera),
            }
        )

    if request.method == "DELETE":
        camera.delete()
        return JsonResponse(
            {
                "success": True,
                "message": "摄像头已删除",
            }
        )

    try:
        cleaned = _validate_camera_data(_read_camera_json(request))
    except ValueError as exc:
        return _camera_json_error(str(exc))

    for field_name, value in cleaned.items():
        setattr(camera, field_name, value)
    camera.save()
    return JsonResponse(
        {
            "success": True,
            "created": False,
            "message": "摄像头配置已更新",
            "camera": _serialize_camera(camera),
        }
    )


@never_cache
@require_http_methods(["GET", "PUT"])
def camera_configuration(request):
    """Read or update the single map camera configuration."""
    if request.method == "GET":
        camera, _ = CameraConfiguration.objects.get_or_create(
            camera_key=DEFAULT_CAMERA_KEY,
            defaults=DEFAULT_CAMERA_DATA,
        )
        return JsonResponse(
            {
                "success": True,
                "camera": _serialize_camera(camera),
            }
        )

    try:
        cleaned = _validate_camera_data(_read_camera_json(request))
    except ValueError as exc:
        return _camera_json_error(str(exc))

    camera, created = CameraConfiguration.objects.update_or_create(
        camera_key=DEFAULT_CAMERA_KEY,
        defaults=cleaned,
    )
    return JsonResponse(
        {
            "success": True,
            "created": created,
            "message": "摄像头配置已保存",
            "camera": _serialize_camera(camera),
        }
    )


@never_cache
@require_GET
def current_typhoons(request):
    try:
        result = get_current_typhoons()
    except TyphoonDataError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
                "typhoons": [],
            },
            status=502,
        )

    return JsonResponse(
        {
            "success": True,
            **result,
        }
    )


@never_cache
@require_GET
def guangdong_marine_weather(request):
    try:
        result = get_guangdong_marine_weather()
    except MarineWeatherDataError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
                "points": [],
            },
            status=502,
        )

    return JsonResponse(
        {
            "success": True,
            **result,
        }
    )
