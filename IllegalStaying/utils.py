import pandas as pd
import numpy as np
from math import radians, sin, cos, sqrt, asin

# ================= 监控区域配置 =================
# 113.6279434°E-113.7935190°E, 22.1376273°N-22.2008193°N
LON_MIN, LON_MAX = 113.6279434, 113.7935190
LAT_MIN, LAT_MAX = 22.1376273, 22.2008193


# ===============================================

def haversine(p1, p2):
    """
    计算两点之间的地理距离（Haversine公式）
    :param p1: (lon1, lat1)
    :param p2: (lon2, lat2)
    :return: 距离（单位：公里）
    """
    # 确保输入是 float 类型
    lon1, lat1 = float(p1[0]), float(p1[1])
    lon2, lat2 = float(p2[0]), float(p2[1])

    lat1, lon1 = radians(lat1), radians(lon1)
    lat2, lon2 = radians(lat2), radians(lon2)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371 * asin(sqrt(a))


def is_in_monitored_area(lat, lon):
    """
    判断点是否在监控区域内
    """
    return (LON_MIN <= lon <= LON_MAX) and (LAT_MIN <= lat <= LAT_MAX)


def detect_parking_events(points_list, distance_threshold=0.01, time_threshold_minutes=30, min_points=3):
    """
    核心检测逻辑
    :param points_list: 列表 [(lat, lon, timestamp), ...]
    """
    if len(points_list) < 2:
        return []

    # 按时间排序
    points_sorted = sorted(points_list, key=lambda x: x[2])

    A = []
    As = []
    Ps = points_sorted[0]
    A.append(Ps)

    for k in range(1, len(points_sorted)):
        Pk = points_sorted[k]
        # 注意：Ps[:2] 取 lat, lon。haversine 需要 (lon, lat) 顺序，需要反转一下
        # 假设 points_list 结构是 (lat, lon, time)
        # Ps[1] is lon, Ps[0] is lat
        d_k = haversine((Ps[1], Ps[0]), (Pk[1], Pk[0]))

        if d_k < distance_threshold:
            A.append(Pk)
        else:
            Pe = A[-1]
            start_time = Ps[2]
            end_time = Pe[2]
            time_diff = (end_time - start_time).total_seconds() / 60

            if len(A) > min_points and time_diff > time_threshold_minutes:
                As.append(A)

            A = [Pk]
            Ps = Pk

    # 处理最后一段
    if len(A) > min_points:
        start_time = A[0][2]
        end_time = A[-1][2]
        time_diff = (end_time - start_time).total_seconds() / 60
        if time_diff > time_threshold_minutes:
            As.append(A)

    return As


def run_parking_analysis(queryset):
    """
    业务入口：接收 Django QuerySet，进行数据清洗和分析
    """
    # 1. 转为 DataFrame 进行预处理
    if not queryset.exists():
        return {'is_abnormal': False, 'events': []}

    df = pd.DataFrame(list(queryset.values('latitude', 'longitude', 'timestamp', 'speed')))

    # 2. 关键过滤：速度筛选 (speed <= 6)
    # 过滤掉运动中的点，减少计算量并提高准确性
    df_filtered = df[df['speed'] <= 6]

    if df_filtered.empty:
        return {'is_abnormal': False, 'events': []}

    # 3. 提取点位 [(lat, lon, timestamp), ...]
    points = df_filtered.loc[:, ['latitude', 'longitude', 'timestamp']].values.tolist()

    # 4. 执行检测
    raw_events = detect_parking_events(
        points,
        distance_threshold=0.01,  # 10米范围
        time_threshold_minutes=30,  # 停留30分钟
        min_points=3
    )

    # 5. 分析结果是否在区域内
    abnormal_events = []
    for event in raw_events:
        # 取第一个点判断位置
        center_lat, center_lon = event[0][0], event[0][1]

        if is_in_monitored_area(center_lat, center_lon):
            start_t = event[0][2]
            end_t = event[-1][2]
            duration = (end_t - start_t).total_seconds() / 60

            abnormal_events.append({
                'start_time': str(start_t),
                'end_time': str(end_t),
                'duration': round(duration, 2)
            })

    return {
        'is_abnormal': len(abnormal_events) > 0,
        'events': abnormal_events
    }