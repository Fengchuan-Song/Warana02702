import json
import traceback
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from datetime import timedelta
from .models import ParkingBuffer
from .utils import run_parking_analysis

# 配置：分析窗口（例如分析过去 2 小时的轨迹，因为驻留可能长达数小时）
ANALYSIS_WINDOW_HOURS = 2
MIN_POINTS_THRESHOLD = 5


@csrf_exempt
def detectAbnormalParking(request):
    if request.method == 'POST':
        try:
            body = json.loads(request.body)

            # --- 修复点 1: 统一 List/Dict 输入格式 ---
            if isinstance(body, dict):
                ship_data = [body]
            elif isinstance(body, list):
                ship_data = body
            else:
                return JsonResponse({'success': False, 'message': 'Invalid data format'}, status=400)

            results = []

            for item in ship_data:
                mmsi = item.get('mmsi')
                if not mmsi: continue

                # 1. 存入缓冲区
                ParkingBuffer.objects.create(
                    mmsi=mmsi,
                    name=item.get('name', ''),
                    longitude=item.get('lon'),  # 确保前端字段名一致
                    latitude=item.get('lat'),
                    speed=item.get('speed', 0),
                    timestamp=item.get('timestamp', timezone.now())
                )

                # 2. 提取滑动窗口数据
                # 驻留判断通常需要较长时间窗口，这里设为 2 小时
                window_start = timezone.now() - timedelta(hours=ANALYSIS_WINDOW_HOURS)
                recent_points = ParkingBuffer.objects.filter(
                    mmsi=mmsi,
                    timestamp__gte=window_start
                ).order_by('timestamp')

                # 3. 触发分析
                if recent_points.count() >= MIN_POINTS_THRESHOLD:
                    analysis = run_parking_analysis(recent_points)

                    if analysis['is_abnormal']:
                        results.append({
                            'mmsi': mmsi,
                            'name': item.get('name', 'Unknown'),
                            'risk': '驻留异常',
                            'status': '长时间异常停留',
                            'detail': analysis['events']
                        })

            # 4. 清理过期数据 (例如保留 4 小时数据)
            # 放在循环外执行一次即可，减少数据库压力
            cleanup_time = timezone.now() - timedelta(hours=4)
            ParkingBuffer.objects.filter(timestamp__lt=cleanup_time).delete()

            return JsonResponse({
                'success': True,
                'count': len(results),
                'results': results
            })

        except Exception as e:
            traceback.print_exc()
            return JsonResponse({'success': False, 'message': str(e)}, status=500)

    return JsonResponse({'success': False, 'message': 'Only POST allowed'}, status=405)