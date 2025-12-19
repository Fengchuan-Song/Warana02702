import json
import traceback
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.dateparse import parse_datetime
from django.utils.timezone import make_aware, is_naive
from django.utils import timezone
from . import models

# --- 1. 配置参数 ---
SPEED_THRESHOLD = 6.0
DURATION_THRESHOLD = 300


@csrf_exempt
def detect_low_speed(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': '仅支持 POST 请求'}, status=405)

    try:
        data_received = json.loads(request.body.decode('utf-8'))
        ship_list = data_received if isinstance(data_received, list) else [data_received]

        if not ship_list:
            return JsonResponse({'success': True, 'count': 0, 'results': []})

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
                    # 此时两边都是 aware datetime，可以正常相减了
                    duration = (timestamp - start_point.timestamp).total_seconds()

                    if duration > DURATION_THRESHOLD:
                        all_alerts.append({
                            'mmsi': mmsi,
                            'name': ship_info.get('name', f"船只-{mmsi}"),
                            'risk': '中风险',
                            'status': f"低速行驶 (已持续 {int(duration)}s)",
                            'details': {'speed': speed, 'duration': int(duration)}
                        })

        return JsonResponse({
            'success': True,
            'count': len(all_alerts),
            'results': all_alerts,
            'message': '检测成功'
        })

    except Exception as e:
        print("--- LowSpeed Detection Error ---")
        traceback.print_exc()
        return JsonResponse({'success': False, 'message': f"数据处理异常: {str(e)}"}, status=400)