import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# ======================================================
# 🚫 禁止驶入围栏（高优先级）
# ======================================================
FORBIDDEN_BOUNDARIES = [
    {
        "id": "forbidden_zone_01",
        "name": "生态保护区",
        "min_lon": 113.6950,
        "max_lon": 113.7050,
        "min_lat": 22.4050,
        "max_lat": 22.4150,
        "risk": "高风险"
    },
    {
        "id": "forbidden_zone_02",
        "name": "港池禁入区",
        "min_lon": 113.6800,
        "max_lon": 113.6900,
        "min_lat": 22.3950,
        "max_lat": 22.4050,
        "risk": "高风险"
    }
]

# ======================================================
# ✅ 允许航行围栏（驶出则告警）
# ======================================================
ALLOWED_BOUNDARIES = [
    {
        "id": "allowed_zone_01",
        "name": "正常航行水域",
        "min_lon": 113.6600,
        "max_lon": 113.7300,
        "min_lat": 22.3800,
        "max_lat": 22.4500,
        "risk": "中风险"
    }
]

# ======================================================
# 基础工具函数
# ======================================================
def point_in_rectangle(lon, lat, fence):
    """判断点是否在矩形围栏内"""
    return (
        fence["min_lon"] <= lon <= fence["max_lon"]
        and fence["min_lat"] <= lat <= fence["max_lat"]
    )

# ======================================================
# 主检测接口
# ======================================================
@csrf_exempt
@require_http_methods(["POST"])
def detect_crossing_boundary(request):
    """
    CrossingBoundary 围栏越界检测
    - 禁止驶入围栏（ForbiddenBoundaryIntrusion）
    - 普通围栏越界（CrossingBoundary）
    """
    # ---------- 1. 解析请求 ----------
    try:
        shipData = json.loads(request.body.decode("utf-8"))
        if not isinstance(shipData, list):
            return JsonResponse(
                {"success": False, "message": "Invalid data format"}, status=400
            )
    except Exception:
        return JsonResponse(
            {"success": False, "message": "Invalid JSON"}, status=400
        )

    if not shipData:
        return JsonResponse({"success": True, "count": 0, "results": []})

    results = []

    # ---------- 2. 逐船检测 ----------
    for ship in shipData:
        lon = ship.get("longitude")
        lat = ship.get("latitude")
        mmsi = ship.get("mmsi")
        name = ship.get("name", "未知船舶")

        if lon is None or lat is None or mmsi is None:
            continue

        # ==================================================
        # 🚫 1. 禁止驶入围栏（最高优先级）
        # ==================================================
        forbidden_hit = False
        for fence in FORBIDDEN_BOUNDARIES:
            if point_in_rectangle(lon, lat, fence):
                results.append({
                    "mmsi": mmsi,
                    "name": name,
                    "event": "ForbiddenBoundaryIntrusion",
                    "boundary_id": fence["id"],
                    "boundary_name": fence["name"],
                    "lon": lon,
                    "lat": lat,
                    "risk": fence["risk"],
                    "status": fence["risk"],
                    "desc": "船舶驶入禁止航行水域"
                })
                forbidden_hit = True
                break

        # 命中禁止区后，不再做普通越界判断
        if forbidden_hit:
            continue

        # ==================================================
        # ✅ 2. 允许航行围栏越界
        # ==================================================
        for fence in ALLOWED_BOUNDARIES:
            inside = point_in_rectangle(lon, lat, fence)
            if not inside:
                results.append({
                    "mmsi": mmsi,
                    "name": name,
                    "event": "CrossingBoundary",
                    "boundary_id": fence["id"],
                    "boundary_name": fence["name"],
                    "lon": lon,
                    "lat": lat,
                    "risk": fence["risk"],
                    "status": fence["risk"],
                    "desc": "船舶驶出允许航行水域"
                })

    # ---------- 3. 返回结果 ----------
    return JsonResponse({
        "success": True,
        "count": len(results),
        "results": results
    })
