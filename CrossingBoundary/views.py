import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.cache import cache
from django.utils.dateparse import parse_datetime

FORBIDDEN_BOUNDARIES = [
    {
        'id': "forbidden_zone_01",
        'name': "生态保护区",
        'min_lon': 113.6950,
        'max_lon': 113.7050,
        'min_lat': 22.4050,
        'max_lat': 22.4150
    },
    {
        'id': "forbidden_zone_02",
        'name': "港池禁入区",
        'min_lon': 113.6800,
        'max_lon': 113.6900,
        'min_lat': 22.3950,
        'max_lat': 22.4050
    }
]

def point_in_rectangle(lon, lat, fence):
    """判断点是否在矩形围栏内"""
    return (
        fence["min_lon"] <= lon <= fence["max_lon"]
        and fence["min_lat"] <= lat <= fence["max_lat"]
    )


@csrf_exempt
def detect_crossing_boundary(request):
    # 从cache中拿数据
    ship_list = cache.get('latest_ais_data_raw', [])
    timestamp_now = parse_datetime(ship_list[0].get('timestamp'))

    if not ship_list:
            return JsonResponse({'success': True, 'count': 0, 'results': []})
    
    # 检测逻辑
    results = []

    # ---------- 2. 逐船检测 ----------
    for ship_info in ship_list:
        lon = ship_info.get("longitude")
        lat = ship_info.get("latitude")
        mmsi = ship_info.get("mmsi")
        name = ship_info.get("name", "未知船舶")

        if lon is None or lat is None or mmsi is None:
            continue

        forbidden_hit = False
        for fence in FORBIDDEN_BOUNDARIES:
            if point_in_rectangle(lon, lat, fence):
                results.append({
                    'mmsi': mmsi,
                    'name': name,
                    'location': [lon, lat],
                    'details': f"检测为围栏越界船舶，船舶驶入{fence['id']}: {fence['name']}。",
                })
                forbidden_hit = True

    # ---------- 3. 返回结果 ----------
    return JsonResponse({
        'success': True,
        'type': "海上围栏越界",
        'timestamp': ship_list[0].get('timestamp'),
        'count': len(results),
        'results': results,
        'message': '检测成功'
    })
