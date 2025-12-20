import json
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.cache import cache
from django.utils.dateparse import parse_datetime
import random
from . import models


@csrf_exempt
def detect_black_list(request):
    # 从cache中拿数据
    ship_list = cache.get('latest_ais_data_raw', [])
    timestamp_now = parse_datetime(ship_list[0].get('timestamp'))

    if not ship_list:
            return JsonResponse({'success': True, 'count': 0, 'results': []})
    
    # 获取黑名单列表
    blackLists_set = set(models.BlackList.objects.values_list('mmsi', flat=True))

    # 判断逻辑
    results = []

    for ship_info in ship_list:
        mmsi = ship_info.get('mmsi')
        name = ship_info.get('name', '未知船只')
        
        if mmsi and mmsi in blackLists_set:
            results.append({
                'mmsi': mmsi,
                'location': [ship_info.get('lon'), ship_info.get('lat')],
                'name': name,
                'details': f"检测到黑名单船舶入侵。", 
            })
    
    return JsonResponse({
        'success': True,
        'type': '黑名单预警',
        'timestamp': ship_list[0].get('timestamp'),
        'count': len(results),
        'results': results,
        'message': '检测成功'
    })
