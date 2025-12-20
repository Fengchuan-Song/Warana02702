import json
import traceback
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.core.cache import cache
from django.utils.dateparse import parse_datetime
from datetime import timedelta
from . import models
from .utils import run_loitering_analysis

# 配置：只分析最近多少分钟的数据（滑动窗口大小）
ANALYSIS_WINDOW_MINUTES = 30
# 配置：只有当缓冲区积累了至少多少个点才开始分析
MIN_POINTS_THRESHOLD = 10


@csrf_exempt
def receive_realtime_point(request):
    # 从cache中拿数据
    ship_list = cache.get('latest_ais_data_raw', [])
    timestamp_now = parse_datetime(ship_list[0].get('timestamp'))

    if not ship_list:
            return JsonResponse({'success': True, 'count': 0, 'results': []})

    # 判定逻辑
    results = []

    # 2. 遍历输入数据，存入缓冲区并进行算法检测
    for ship_info in ship_list:
        mmsi = ship_info.get('mmsi')
        name = ship_info.get('name', '未知船只')
        lon = ship_info.get('lon')
        lat = ship_info.get('lat')

        if not mmsi:
            continue

        # --- A. 实时点存入缓冲区 ---
        models.TrajectoryBuffer.objects.create(
            mmsi=mmsi,
            name=name,
            longitude=lon,
            latitude=lat,
            course=ship_info.get('course', 0),
            speed=ship_info.get('speed', 0),
            timestamp=ship_info.get('timestamp', timezone.now())
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
                    'location': [ship_info.get('lon'), ship_info.get('lat')],
                    'name': name,
                    'details': f"检测为异常徘徊船舶，判定逻辑待补充。"
                })
    
    # 4. 清理过期数据 (清理过去 DURATION_THRESHOLD / 60 分钟的数据)
    cleanup_time = timestamp_now - timedelta(minutes=ANALYSIS_WINDOW_MINUTES)
    models.TrajectoryBuffer.objects.filter(timestamp__lt=cleanup_time).delete()  
    
    # 3. 按照要求的格式返回
    return JsonResponse({
        'success': True,
        'type': '异常徘徊',
        'timestamp': ship_list[0].get('timestamp'),
        'count': len(results),
        'results': results,
        'message': '检测成功'
    })
