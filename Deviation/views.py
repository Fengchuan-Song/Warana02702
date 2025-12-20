# Deviation/views.py

import json
import numpy as np
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.cache import cache
from django.utils.dateparse import parse_datetime
from datetime import timedelta
from . import models
from .apps import KNOWLEDGE_BASE, K_NEIGHBORS, DEVIATION_THRESHOLD_DTW  # 导入知识库和参数

# 配置参数
SEGMENT_LENGTH = 10

# 导入算法核心依赖 (它们已经在 apps.py 中被导入，但为确保函数可用性，这里再次尝试导入)
try:
    from fastdtw import fastdtw
    from scipy.spatial.distance import euclidean
    # 只需要导入必要的库，pandas 已经在 apps.py 的 load_knowledge_base 中处理
except ImportError:
    # 备用：如果环境不完整，提供一个假的函数防止崩溃
    def fastdtw(a, b, dist):
        return 9999, None


    def euclidean(a, b):
        return 9999


# --- 辅助函数：航道偏离检测核心逻辑 (Adaptation of check_deviation) ---
def check_deviation_core(new_trajectory_points):
    """
    对一条轨迹段执行偏航检测的核心逻辑。
    :param new_trajectory_points: 格式为 [ [lon, lat], ... ] 的轨迹段
    :return: (is_deviated, dtw_score, status_message)
    """
    if KNOWLEDGE_BASE is None:
        return False, -1, "Knowledge base not loaded, cannot perform detection."

    df_history, tree, df_start_points = KNOWLEDGE_BASE

    # 1. 准备新轨迹 (Lon, Lat)
    try:
        new_traj_np = np.array(new_trajectory_points, dtype=float)
    except ValueError:
        return True, float('inf'), "Data conversion error."

    if new_traj_np.shape[0] == 0:
        return True, float('inf'), "Empty trajectory segment."

    start_point = new_traj_np[0]  # (Lon, Lat)

    # 2. 快速查询k个最近邻
    # BallTree 假设是用 [lat, lon] (弧度) 构建的
    query_point = np.deg2rad([start_point[::-1]])  # flip to [lat, lon]

    if tree is None:
        return False, -2, "Knowledge base tree not initialized."

    # 容错处理：当 k > 实际的轨迹数量时
    k_actual = min(K_NEIGHBORS, len(df_start_points))
    if k_actual == 0:
        return False, -3, "No historical trajectories available."

    dist, ind = tree.query(query_point, k=k_actual)
    candidate_traj_ids = df_start_points.iloc[ind[0]]['TrajectoryID'].tolist()

    # 3. 精确比较 (DTW)
    min_dtw_distance = float('inf')

    for traj_id in candidate_traj_ids:
        # 提取历史轨迹点 (Longitude, Latitude)
        hist_traj_df = df_history[df_history['TrajectoryID'] == traj_id]
        if hist_traj_df.empty:
            continue

        hist_traj_np = hist_traj_df[['Longitude', 'Latitude']].values

        # 计算DTW (使用 fastdtw, dist=euclidean)
        distance, _ = fastdtw(new_traj_np, hist_traj_np, dist=euclidean)

        if distance < min_dtw_distance:
            min_dtw_distance = distance

    # 4. 做出判定
    is_deviated = min_dtw_distance > DEVIATION_THRESHOLD_DTW

    status_message = "高风险: 航道偏离！" if is_deviated else "正常"

    return is_deviated, min_dtw_distance, status_message


# --- Django View 接口 ---
@csrf_exempt
def detect_deviation(request):
    # 从cache中拿数据
    ship_list = cache.get('latest_ais_data_raw', [])
    timestamp_now = parse_datetime(ship_list[0].get('timestamp'))

    if not ship_list:
            return JsonResponse({'success': True, 'count': 0, 'results': []})
    
    # 判定逻辑
    results = []

    for ship_info in ship_list:
        mmsi = str(ship_info.get('mmsi'))
        longitude = float(ship_info.get('lon'))
        latitude = float(ship_info.get('lat'))

        try:
            models.TrajectoryPoint.objects.create(
                mmsi=mmsi,
                longitude=longitude,
                latitude=latitude
            )
        except Exception as e:
            print(f'{e}')

        latest_points = models.TrajectoryPoint.objects.filter(mmsi=mmsi).order_by('-timestamp')[:SEGMENT_LENGTH]

        current_length = len(latest_points)
        if current_length < SEGMENT_LENGTH:
            continue

        trajectory_segment = [[p.longitude, p.latitude] for p in reversed(latest_points)]

        # is_deviated = True
        is_deviated, dtw_score, status_message = check_deviation_core(trajectory_segment)

        if (is_deviated):
            results.append({
                'mmsi': mmsi,
                'location': [longitude, latitude],
                'name': ship_info.get('name'),
                'details': f"检测为航道偏离船舶，航线与习惯航路不符。"
            })

    # 4. 清理过期数据 (清理过去30分钟的数据)
    cleanup_time = timestamp_now - timedelta(minutes=5)
    models.TrajectoryPoint.objects.filter(timestamp__lt=cleanup_time).delete()

    return JsonResponse({
        'success': True,
        'type': '航道偏离',
        'timestamp': ship_list[0].get('timestamp'),
        'count': len(results),
        'results': results,
        'message': '检测成功'
    })
