from django.shortcuts import render
import json
import math
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


def haversine(lon1, lat1, lon2, lat2):
    R = 6371000  # meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi/2)**2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@csrf_exempt
@require_http_methods(["POST"])
def detect_collision(request):
    """
    船舶碰撞风险检测
    """
    try:
        ships = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'message': 'Invalid JSON'}, status=400)

    results = []
    distance_threshold = 50      # 米
    speed_threshold = 8           # 节

    n = len(ships)
    for i in range(n):
        for j in range(i + 1, n):
            s1, s2 = ships[i], ships[j]

            d = haversine(
                s1['lon'], s1['lat'],
                s2['lon'], s2['lat']
            )

            if d < distance_threshold:
                if s1['speed'] > speed_threshold and s2['speed'] > speed_threshold:
                    results.append({
                        'pair': f"{s1['mmsi']} - {s2['mmsi']}",
                        'event': 'Collision',
                        'distance': round(d, 1),
                        'risk': '高风险'
                    })

    return JsonResponse({
        'success': True,
        'count': len(results),
        'results': results
    })
