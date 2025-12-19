# Deviation/views.py

import json
import numpy as np
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import models
from .apps import KNOWLEDGE_BASE, K_NEIGHBORS, DEVIATION_THRESHOLD_DTW  # 导入知识库和参数

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
@require_http_methods(["POST"])
def detect_deviation(request):
    """
    接收实时 AIS 轨迹点，积累后执行航道偏离检测，并返回结果。
    """
    # 每积累 N 个点进行一次检测 (N 可根据实时性要求配置)
    SEGMENT_LENGTH = 10

    # 1. 接收并解析实时 AIS 数据 (预期输入: 单个点)
    try:
        data = json.loads(request.body.decode('utf-8'))
        for each in data:
            mmsi = str(each.get('mmsi'))
            longitude = float(each.get('lon'))
            latitude = float(each.get('lat'))

            try:
                models.TrajectoryPoint.objects.create(
                    mmsi=mmsi,
                    longitude=longitude,
                    latitude=latitude
                )
            except Exception as e:
                print(f'{e}')

        if not all([mmsi, longitude, latitude]):
            return JsonResponse(
                {'success': False, 'message': 'Invalid input data: Missing mmsi, longitude, or latitude.'}, status=400)

    except (json.JSONDecodeError, ValueError) as e:
        return JsonResponse(
            {'success': False, 'message': f'Invalid JSON format or data type in request body. Error: {e}'}, status=400)
    except Exception:
        print(data)
        return JsonResponse({'success': False, 'message': 'Failed to receive or process data.'}, status=400)


    # 3. 提取最新的轨迹段进行检测
    # 查询该MMSI最新的 SEGMENT_LENGTH 个点，按时间倒序
    latest_points = models.TrajectoryPoint.objects.filter(mmsi=mmsi).order_by('-timestamp')[:SEGMENT_LENGTH]

    current_length = len(latest_points)

    # 如果点数不够，则继续积累
    if current_length < SEGMENT_LENGTH:
        return JsonResponse({
            'success': True,
            'mmsi': mmsi,
            'status': "积累中",
            'message': f"Trajectory accumulation in progress ({current_length}/{SEGMENT_LENGTH} points).",
            'is_deviated': False,
            'dtw_score': None
        })

    # 将查询集转换为算法所需的 [ [lon, lat], ... ] 格式
    # 注意：查询集是倒序的，需要反转为正序（从老到新）
    trajectory_segment = [[p.longitude, p.latitude] for p in reversed(latest_points)]

    # 4. 执行偏航检测核心逻辑
    is_deviated, dtw_score, status_message = check_deviation_core(trajectory_segment)

    # 5. 返回结果
    response_data = {
        'success': True,
        'mmsi': mmsi,
        # 'count': len(results),
        'is_deviated': is_deviated,
        'status': status_message,
        'dtw_score': round(dtw_score, 4) if dtw_score is not None and dtw_score >= 0 else None,
        'threshold': DEVIATION_THRESHOLD_DTW,
        'message': "Detection successful." if dtw_score >= 0 else status_message,
        'segment_length': current_length
    }

    return JsonResponse(response_data)


from django.shortcuts import render

# Create your views here.
