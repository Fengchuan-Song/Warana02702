import json
import traceback
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from datetime import timedelta
from . import models
from .utils import run_loitering_analysis

# 配置：只分析最近多少分钟的数据（滑动窗口大小）
ANALYSIS_WINDOW_MINUTES = 30
# 配置：只有当缓冲区积累了至少多少个点才开始分析
MIN_POINTS_THRESHOLD = 10


@csrf_exempt
def receive_realtime_point(request):
    """
        仅使用异常徘徊算法识别风险船舶
        """
    if request.method == 'POST':
        try:
            body = json.loads(request.body)

            # 1. 统一输入格式 (处理单字典或列表)
            if isinstance(body, dict):
                shipData = [body]
            elif isinstance(body, list):
                shipData = body
            else:
                return JsonResponse({'success': False, 'message': '数据格式错误'}, status=400)

            results = []

            # 2. 遍历输入数据，存入缓冲区并进行算法检测
            for each in shipData:
                mmsi = each.get('mmsi')
                name = each.get('name', '未知船只')
                lon = each.get('lon')
                lat = each.get('lat')

                if not mmsi:
                    continue

                # --- A. 实时点存入缓冲区 ---
                models.TrajectoryBuffer.objects.create(
                    mmsi=mmsi,
                    name=name,
                    longitude=lon,
                    latitude=lat,
                    course=each.get('course', 0),
                    speed=each.get('speed', 0),
                    timestamp=each.get('timestamp', timezone.now())
                )

                # --- B. 提取滑动窗口数据触发算法 ---
                # 获取该船最近 30 分钟的所有轨迹点
                check_window = timezone.now() - timedelta(minutes=30)
                recent_points = models.TrajectoryBuffer.objects.filter(
                    mmsi=mmsi,
                    timestamp__gte=check_window
                ).order_by('timestamp')

                # 只有当缓冲区点数达到算法要求的阈值时才分析（建议至少5-10个点）
                if recent_points.count() >= 10:
                    # 调用你提供的 LoiteringBehaviour 逻辑封装的函数
                    analysis = run_loitering_analysis(recent_points)

                    if analysis.get('is_abnormal'):
                        # 算法识别出异常，按要求的格式加入结果集
                        results.append({
                            'mmsi': mmsi,
                            'name': name,
                            'risk': '徘徊高风险',
                            'status': '正在徘徊',
                            # 可选：加入具体的异常段信息
                            'detail': f"检测到 {analysis.get('abnormal_count', 0)} 处异常段"
                        })

            # 3. 按照要求的格式返回
            response_data = {
                'success': True,
                'count': len(results),
                'results': results,  # 仅包含被算法判定为“正在徘徊”的船舶
            }

            return JsonResponse(response_data)

        except Exception as e:
            traceback.print_exc()
            return JsonResponse({'success': False, 'message': str(e)}, status=500)

    return JsonResponse({'success': False, 'message': '仅支持POST方法'}, status=405)