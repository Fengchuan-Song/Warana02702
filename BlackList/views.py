import json
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import random
from . import models


@csrf_exempt
@require_http_methods(["POST"])
def detect_black_list(request):
    """
    接收前端传来的实时 AIS 船舶数据，执行黑名单检测，并返回结果。
    """
    # 1. 接收并解析实时 AIS 数据
    try:
        # 从请求体中解析前端发送的 JSON 数据
        # 前端发送的 body: JSON.stringify(rawData) 
        # rawData 假设是船舶对象列表
        data_received = json.loads(request.body.decode('utf-8'))
        shipData = data_received
        
        # 容错处理：如果收到的不是列表，则返回错误
        if not isinstance(shipData, list):
             return JsonResponse({'success': False, 'message': 'Invalid data format: Expected a list of ships.'}, status=400)
             
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid JSON format in request body.'}, status=400)
    except Exception:
        # 捕获其他可能的错误，例如 request.body 为空
        return JsonResponse({'success': False, 'message': 'Failed to receive or process data.'}, status=400)
    
    # 如果没有收到任何船舶数据，则返回空结果
    if not shipData:
        return JsonResponse({'success': True, 'count': 0, 'results': []})
        
    # 2. 执行黑名单预警功能
    # 从数据库加载黑名单 MMSI 集合 (使用 set 提升查找效率)
    try:
        blackLists_set = set(models.BlackList.objects.values_list('mmsi', flat=True))
    except Exception:
        # 演示容错：如果数据库模型查询失败，使用硬编码的 MMSI 集合进行演示
        print("WARNING: Failed to load models.BlackList. Using hardcoded MMSI set for demo.")
    
    results = []
    for each in shipData:
        mmsi = each.get('mmsi')
        name = each.get('name', '未知船只')
        
        if mmsi and mmsi in blackLists_set:
            # 找到匹配的黑名单船只，标记为高风险
            risk_level = '高风险' 
            
            results.append({
                'mmsi': mmsi,
                'name': name,
                # 实际风险信息应从数据库获取，这里简化为高风险
                'risk': risk_level,
                'status': risk_level, 
            })
            
    # 3. 返回结果
    response_data = {
            'success': True,
            'count': len(results),
            'results': results,  # 包含详细的船舶列表
        }
    
    return JsonResponse(response_data)