import json
import traceback
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.dateparse import parse_datetime
from django.utils.timezone import make_aware, is_naive
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta
from . import models

# --- 1. 配置参数 ---
SPEED_THRESHOLD = 1.5
DURATION_THRESHOLD = 300


@csrf_exempt
def detect_low_speed(request):
    # 从cache中拿数据
    ship_list = cache.get('latest_ais_data_raw', [])
    timestamp_now = parse_datetime(ship_list[0].get('timestamp'))

    if not ship_list:
            return JsonResponse({'success': True, 'count': 0, 'results': []})
    
    # 判断逻辑
    all_alerts = []

    for ship_info in ship_list:
        mmsi = str(ship_info.get('mmsi'))
        speed = float(ship_info.get('speed', 0))

        # --- 修正后的时间处理逻辑 ---
        ts_str = ship_info.get('timestamp')
        if ts_str:
            timestamp = parse_datetime(ts_str)
            if timestamp and is_naive(timestamp):
                timestamp = make_aware(timestamp)
        else:
            timestamp = timezone.now()

        # 存储到数据库
        models.LowSpeedPoint.objects.create(
            mmsi=mmsi,
            speed=speed,
            timestamp=timestamp
        )

        # 低速判定逻辑
        if speed < SPEED_THRESHOLD:
            last_normal_point = models.LowSpeedPoint.objects.filter(
                mmsi=mmsi,
                speed__gte=SPEED_THRESHOLD,
                timestamp__lt=timestamp
            ).order_by('-timestamp').first()

            if last_normal_point:
                start_point = models.LowSpeedPoint.objects.filter(
                    mmsi=mmsi,
                    timestamp__gt=last_normal_point.timestamp
                ).order_by('timestamp').first()
            else:
                start_point = models.LowSpeedPoint.objects.filter(mmsi=mmsi).order_by('timestamp').first()

            if start_point:
                duration = (timestamp - start_point.timestamp).total_seconds()

                if duration > DURATION_THRESHOLD:
                    all_alerts.append({
                        'mmsi': mmsi,
                        'location': [ship_info.get('lon'), ship_info.get('lat')],
                        'name': ship_info.get('name'),
                        'details': f"检测为低速船舶，当前速度为{speed:.2f}节,低于规定最小航速{SPEED_THRESHOLD:.2f}节，且已持续低速航行 {int(duration)}s。",
                    })

    # 4. 清理过期数据 (清理过去 DURATION_THRESHOLD / 60 分钟的数据)
    cleanup_time = timestamp_now - timedelta(minutes=DURATION_THRESHOLD / 60)
    models.LowSpeedPoint.objects.filter(timestamp__lt=cleanup_time).delete()

    return JsonResponse({
        'success': True,
        'type': '低速预警',
        'timestamp': ship_list[0].get('timestamp'),
        'count': len(all_alerts),
        'results': all_alerts,
        'message': '检测成功'
    })
