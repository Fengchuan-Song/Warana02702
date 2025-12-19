import json
import pandas as pd
import numpy as np
import math
import traceback
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from scipy.stats import pearsonr

from . import models


# --- 1. 辅助函数：计算距离 (来自您的算法 v4_right.py) ---
def haversine(coord1, coord2):
    """计算两个经纬度点之间的距离（单位：米）"""
    R = 6371000  # 地球半径
    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# --- 2. 核心特征提取 (适配您的算法指标) ---
def extract_pair_features(pair_df):
    """提取两船之间的行为特征"""
    if len(pair_df) < 3:
        return {'speed_corr': 0, 'avg_distance': 99999, 'distance_std': 999, 'duration': 0}

    # (1) 速度相关性 (Pearson Correlation)
    sog_x = pair_df['sog_x'].values
    sog_y = pair_df['sog_y'].values
    if len(set(sog_x)) > 1 and len(set(sog_y)) > 1:
        corr, _ = pearsonr(sog_x, sog_y)
    else:
        corr = 0.0

    # (2) 距离计算
    distances = [
        haversine((row.lat_x, row.lng_x), (row.lat_y, row.lng_y))
        for row in pair_df.itertuples()
    ]

    # (3) 时间跨度
    time_diff = pair_df['timestamp'].max() - pair_df['timestamp'].min()

    return {
        'speed_corr': corr if not np.isnan(corr) else 0.0,
        'avg_distance': np.mean(distances),
        'distance_std': np.std(distances, ddof=1) if len(distances) > 1 else 0.0,
        'duration': time_diff.total_seconds()
    }


# --- 3. 视图函数 ---
@csrf_exempt
def detect_double_dragging(request):
    """接收实时 AIS 列表，执行双拖行为检测"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': '仅支持 POST 请求'}, status=405)

    try:
        # 解析前端发送的 JSON 数据
        data_received = json.loads(request.body.decode('utf-8'))

        # 兼容处理：前端 Demo_v9 发送的是数组
        ship_list = data_received if isinstance(data_received, list) else [data_received]

        if not ship_list:
            return JsonResponse({'success': True, 'count': 0, 'results': []})

        # 遍历接收到的所有船舶点并存入数据库
        for ship_info in ship_list:
            mmsi = str(ship_info.get('mmsi'))
            # 字段对齐：根据您的打印结果，前端字段为 lon, speed, course
            lat = float(ship_info.get('lat', 0))
            lng = float(ship_info.get('lon', 0))  # 前端 lon -> 后端 lng
            sog = float(ship_info.get('speed', 0))  # 前端 speed -> 后端 sog
            cog = float(ship_info.get('course', 0))  # 前端 course -> 后端 cog

            # 时间戳解析
            ts_str = ship_info.get('timestamp')
            timestamp = parse_datetime(ts_str) if ts_str else timezone.now()

            # 存储到数据库
            models.DoubleDraggingPoint.objects.create(
                mmsi=mmsi, lat=lat, lng=lng, sog=sog, cog=cog, timestamp=timestamp
            )

        # 执行检测逻辑（以列表中第一艘船为例进行演示，实际可循环所有船）
        # 这里选取列表中变动最活跃的一艘船或指定船只
        test_mmsi = str(ship_list[0].get('mmsi'))

        # 提取船 A 最近的 12 个点
        ship_a_qs = models.DoubleDraggingPoint.objects.filter(mmsi=test_mmsi).order_by('-timestamp')[:12]

        # 如果点数不够，返回“积累中”但 success 必须为 True，防止前端显示“失败”
        if ship_a_qs.count() < 12:
            return JsonResponse({
                'success': True,
                'count': 0,
                'results': [],
                'status': 'accumulating',
                'message': f'正在积累数据 ({ship_a_qs.count()}/12)'
            })

        # 将 QuerySet 转为 DataFrame 方便算法处理
        df_a = pd.DataFrame(list(ship_a_qs.values('timestamp', 'lat', 'lng', 'sog', 'cog')))

        # 获取其他候选船舶
        other_ships = models.DoubleDraggingPoint.objects.exclude(mmsi=test_mmsi).values_list('mmsi',
                                                                                             flat=True).distinct()

        detections = []
        for other_mmsi in other_ships:
            ship_b_qs = models.DoubleDraggingPoint.objects.filter(mmsi=other_mmsi).order_by('-timestamp')[:20]
            if ship_b_qs.count() < 12:
                continue

            df_b = pd.DataFrame(list(ship_b_qs.values('timestamp', 'lat', 'lng', 'sog', 'cog')))

            # 时间对齐：使用 pandas 的 merge_asof
            merged = pd.merge_asof(
                df_a.sort_values('timestamp'),
                df_b.sort_values('timestamp'),
                on='timestamp',
                direction='nearest',
                tolerance=pd.Timedelta(minutes=15)
            ).dropna(subset=['lat_x', 'lat_y'])

            if len(merged) >= 12:
                features = extract_pair_features(merged)

                # 判定逻辑：计分制 (来自您的 v4_right.py)
                score = 0
                if features['speed_corr'] > 0.6: score += 1
                if 100 <= features['avg_distance'] <= 2000: score += 1
                if features['duration'] > 1800: score += 1
                if features['distance_std'] < 500: score += 1

                if score >= 3:
                    detections.append({
                        'mmsi': test_mmsi,
                        'name': f"疑似双拖船对 ({test_mmsi}-{other_mmsi})",
                        'pair_mmsi': other_mmsi,
                        'risk': '高风险',
                        'status': '双拖作业中',
                        'details': features
                    })

        # 返回最终检测结果
        return JsonResponse({
            'success': True,
            'count': len(detections),
            'results': detections
        })

    except Exception as e:
        # 打印详细错误堆栈到终端
        print("--- DoubleDragging Error Details ---")
        traceback.print_exc()
        return JsonResponse({'success': False, 'message': f"服务器内部错误: {str(e)}"}, status=400)