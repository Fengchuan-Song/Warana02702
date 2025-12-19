import numpy as np
import pandas as pd
from shapely.geometry import Point, Polygon
from .Tdkc import TDKC  # 确保 Tdkc.py 在同级目录下


# --- 以下函数直接来自 LoiteringBehaviour.py，保持不变 ---
FIXED_REGION_COORDS = [
    (113.6109833, 22.1700302),  # 左下角 (最小经度, 最小纬度)
    (113.7926567, 22.2080471)   # 右上角 (最大经度, 最大纬度)
]

def sed(pm, ps, pe):
    """
    垂直同步距离
    """
    # 计算中间点的预测坐标
    xm_prime = ps[0] + (pm[2] - ps[2]).total_seconds() / (pe[2] - ps[2]).total_seconds() * (pe[0] - ps[0])
    ym_prime = ps[1] + (pm[2] - ps[2]).total_seconds() / (pe[2] - ps[2]).total_seconds() * (pe[1] - ps[1])

    # 计算同步欧式距离
    sed = np.sqrt((pm[0] - xm_prime) ** 2 + (pm[1] - ym_prime) ** 2)
    return sed


def split_original_segments(original_points, compressed_points):
    """
    将原始轨迹按压缩点分割为多个子段
    """
    segments = []
    compressed_times = [t for (x, y, t, s, c) in compressed_points]

    for i in range(len(compressed_times) - 1):
        start_time = compressed_times[i]
        end_time = compressed_times[i + 1]

        segment = [
            p for p in original_points
            if start_time <= p[2] <= end_time
        ]
        segments.append(segment)

    return segments


def calculate_curvature(segment):
    """
    曲率计算
    """
    curvatures = []
    for i in range(len(segment)):
        if i == 0 or i == len(segment) - 1:
            curvatures.append(0.0)
            continue

        p_prev = np.array([segment[i - 1][0], segment[i - 1][1]])
        p_curr = np.array([segment[i][0], segment[i][1]])
        p_next = np.array([segment[i + 1][0], segment[i + 1][1]])

        if (np.array_equal(p_prev, p_curr) or
                np.array_equal(p_curr, p_next) or
                np.array_equal(p_prev, p_next)):
            curvatures.append(0.0)
            continue

        vec1 = p_curr - p_prev
        vec2 = p_next - p_curr

        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        cos_alpha = dot_product / (norm1 * norm2)
        alpha_i = np.arccos(cos_alpha)

        Q_i = np.linalg.norm(p_next - p_prev)
        curvature = abs(2 * np.sin(alpha_i) / Q_i)
        curvatures.append(curvature)

    return curvatures


def detect_segment_wave_points(segment, global_curvatures, global_trajectory):
    """
    检测单个子段中的波点
    """
    wave_points = []
    if not segment:
        return wave_points

    start_time = segment[0][2]
    end_time = segment[-1][2]

    start_idx = next(i for i, p in enumerate(global_trajectory) if p[2] == start_time)
    end_idx = next(i for i, p in enumerate(global_trajectory) if p[2] == end_time)

    sub_curvatures = global_curvatures[start_idx:end_idx + 1]

    for i in range(1, len(sub_curvatures) - 1):
        if sub_curvatures[i] > sub_curvatures[i - 1] and sub_curvatures[i] > sub_curvatures[i + 1]:
            wave_points.append(segment[i])

    return wave_points


def filter_global_wave_points(segment, angle_threshold=100):
    """
    全局波点筛选
    """
    filtered_points = []
    for i in range(1, len(segment) - 1):
        p_prev = segment[i - 1]
        p_curr = segment[i]
        p_next = segment[i + 1]

        vec_prev = np.array([p_prev[0] - p_curr[0], p_prev[1] - p_curr[1]])
        vec_next = np.array([p_next[0] - p_curr[0], p_next[1] - p_curr[1]])

        cos_angle = np.dot(vec_prev, vec_next) / (np.linalg.norm(vec_prev) * np.linalg.norm(vec_next))
        angle = np.degrees(np.arccos(np.clip(cos_angle, -1.0, 1.0)))

        if 0 < angle < angle_threshold:
            filtered_points.append(p_curr)

    return filtered_points


def extract_wandering_segments(compressed_points, wandering_points, threshold=2):
    """
    检测单个子段中的波点是否大于阈值
    """
    wandering_segments = []
    wandering_segment_info = []

    for i in range(len(compressed_points) - 1):
        start = compressed_points[i]
        end = compressed_points[i + 1]

        segment_wandering_points = [
            p for p in wandering_points
            if start[2] < p[2] < end[2]
        ]

        count = len(segment_wandering_points)
        if count >= threshold:
            wandering_segments.append((start, end))
            wandering_segment_info.append({
                'start': start,
                'end': end,
                'wandering_points': segment_wandering_points,
                'count': count,
                'segment_index': i
            })

    return wandering_segments, wandering_segment_info


def check_region_wandering(wandering_segment_info):
    """
    检查徘徊行为是否出现在固定区域内
    """
    # 直接使用顶部的常量
    lon_min, lat_min = FIXED_REGION_COORDS[0]
    lon_max, lat_max = FIXED_REGION_COORDS[1]

    region_polygon = Polygon([
        (lon_min, lat_min),
        (lon_max, lat_min),
        (lon_max, lat_max),
        (lon_min, lat_max),
        (lon_min, lat_min)
    ])

    abnormal_segments = []
    normal_segments = []

    for segment_info in wandering_segment_info:
        wandering_points = segment_info['wandering_points']
        is_abnormal = False

        # 1. 检查波点是否在区域内
        for point in wandering_points:
            # 注意：原数据结构通常为 (Lat, Lon, ...) 或 (Lon, Lat)，需根据你的实际数据确认索引
            # 假设 point[0]是Lat, point[1]是Lon (根据原始CSV读取习惯)
            # Shapely Point 接受 (Longitude, Latitude)
            lon, lat = point[1], point[0]
            point_geom = Point(lon, lat)

            if region_polygon.contains(point_geom):
                is_abnormal = True
                break

        # 2. 如果波点都不在区域内，检查整个段的首尾点
        if not is_abnormal:
            start_point = segment_info['start']
            end_point = segment_info['end']

            # 同样假设 point[1]是Lon, point[0]是Lat
            start_geom = Point(start_point[1], start_point[0])
            end_geom = Point(end_point[1], end_point[0])

            if region_polygon.contains(start_geom) or region_polygon.contains(end_geom):
                is_abnormal = True

        if is_abnormal:
            abnormal_segments.append(segment_info)
        else:
            normal_segments.append(segment_info)

    return abnormal_segments, normal_segments


# --- 核心入口函数 ---

def run_loitering_analysis(trajectory_queryset):
    """
    接收 Django QuerySet 或 字典列表，进行分析
    """
    # 1. 将 QuerySet 转换为 DataFrame
    # 这种方式兼容 Django Model 对象列表和普通字典列表
    if hasattr(trajectory_queryset, 'values'):
        # 如果是 Django QuerySet
        data = pd.DataFrame(list(trajectory_queryset.values(
            'latitude', 'longitude', 'timestamp', 'speed', 'course'
        )))
    else:
        # 如果是普通列表
        data = pd.DataFrame(trajectory_queryset)

    if data.empty:
        return {'is_abnormal': False, 'abnormal_count': 0, 'abnormal_segments': []}

    # 映射列名以匹配算法 (原算法用 Heading, 输入是 Course)
    data.rename(columns={
        'latitude': 'Latitude',
        'longitude': 'Longitude',
        'timestamp': 'Timestamp',
        'speed': 'Speed',
        'course': 'Heading'
    }, inplace=True)

    data['Timestamp'] = pd.to_datetime(data['Timestamp'])

    # 提取点位 (注意：算法可能要求 list of tuples)
    # 顺序：Lat, Lon, Time, Speed, Heading
    points = data.loc[:, ['Latitude', 'Longitude', 'Timestamp', 'Speed', 'Heading']].values
    if isinstance(points, np.ndarray):
        points = [tuple(point) for point in points]

    # --- 以下逻辑保持不变 ---
    # 2. 压缩
    compressed_points = TDKC(points)

    # 3. 分段
    segments = split_original_segments(points, compressed_points)

    # 4. 曲率计算
    curvatures = calculate_curvature(points)

    # 5. 波点检测
    all_wave_points = []
    for segment in segments:
        if len(segment) < 3: continue
        wave_points = detect_segment_wave_points(segment, curvatures, points)
        wandering_points = filter_global_wave_points(wave_points)
        all_wave_points.extend(wandering_points)

    # 6. 提取徘徊段
    wandering_segments, wandering_segment_info = extract_wandering_segments(
        compressed_points, all_wave_points, threshold=2
    )

    # 7. 区域判定
    abnormal_segments, normal_segments = check_region_wandering(wandering_segment_info)

    # 8. 格式化结果
    result = {
        'is_abnormal': len(abnormal_segments) > 0,
        'abnormal_count': len(abnormal_segments),
        'abnormal_segments': []
    }

    for seg in abnormal_segments:
        result['abnormal_segments'].append({
            'start_time': str(seg['start'][2]),
            'end_time': str(seg['end'][2]),
            'wave_point_count': seg['count']
        })

    return result