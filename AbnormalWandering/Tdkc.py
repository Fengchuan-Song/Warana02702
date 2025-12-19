import numpy as np
import pandas as pd
from shapely.geometry import Point, LineString
from math import sqrt, cos, degrees, sin, asin


def GetSED(start_pt, end_pt, pt):
    """
          垂直同步距离
    """
    # 计算中间点的预测坐标
    R = 6371

    xm_prime = start_pt[0] + (pt[2] - start_pt[2]).total_seconds() / (end_pt[2] - start_pt[2]).total_seconds() * (
                end_pt[0] - start_pt[0])
    ym_prime = start_pt[1] + (pt[2] - start_pt[2]).total_seconds() / (end_pt[2] - start_pt[2]).total_seconds() * (
                end_pt[1] - start_pt[1])
    deta_x = pt[0] - xm_prime
    deta_y = pt[1] - ym_prime
    # 算同步欧式距离
    sed = 2 * R * asin(sqrt((sin(deta_x / 2) ** 2) + cos(pt[0]) * cos(xm_prime) * (sin(deta_y / 2) ** 2)))

    return sed


def GetSVD(start_pt, end_pt, pt):
    """
          同步速度差
    """
    st = pt[3]
    ct = pt[4]
    st_prime = start_pt[0] + (pt[2] - start_pt[2]).total_seconds() / (end_pt[2] - start_pt[2]).total_seconds() * (
                end_pt[3] - start_pt[3])
    ct_prime = start_pt[1] + (pt[2] - start_pt[2]).total_seconds() / (end_pt[2] - start_pt[2]).total_seconds() * (
                ((end_pt[4] - start_pt[4] + 180) % 360) - 180)
    vx_t = st * sin(ct)
    vy_t = st * cos(ct)
    vxp_t = st_prime * sin(ct_prime)
    vyp_t = st_prime * sin(ct_prime)

    svd = np.sqrt((vxp_t - vx_t) ** 2 + (vyp_t - vy_t) ** 2)

    return svd


def ZScoreNormalize(values):
    """
    正则化
    """
    mean_val = np.mean(values)
    std_dev = np.std(values)
    return [(x - mean_val) / std_dev for x in values]


def FindSplitPt(pts):
    if len(pts) <= 2:
        return None

    start_pt = pts[0]
    end_pt = pts[-1]

    sed_set = []
    svd_set = []

    for i in range(1, len(pts) - 1):
        pt = pts[i]
        sed = GetSED(start_pt, end_pt, pt)
        svd = GetSVD(start_pt, end_pt, pt)
        sed_set.append(sed)
        svd_set.append(svd)

    norm_sed_set = ZScoreNormalize(sed_set)
    norm_svd_set = ZScoreNormalize(svd_set)

    norm_aggregated = [norm_sed + norm_svd for norm_sed, norm_svd in zip(norm_sed_set, norm_svd_set)]

    max_index = norm_aggregated.index(max(norm_aggregated))

    split_pt = pts[max_index + 1]
    ms_vector = [sed_set[max_index], svd_set[max_index]]

    return split_pt, ms_vector


def ConstructCBT(pts):
    if len(pts) <= 2:
        return None

    start_pt = pts[0]
    end_pt = pts[-1]

    split_pt, ms_vector = FindSplitPt(pts)

    left_subtree = ConstructCBT(pts[:pts.index(split_pt) + 1])
    right_subtree = ConstructCBT(pts[pts.index(split_pt):])

    node = [split_pt, ms_vector, left_subtree, right_subtree]

    return node

def IdentifyKeyNode(node, threshold_sed, threshold_svd, key_nodes):
    if node is not None:
        if node[1][0] > threshold_sed or node[1][1] > threshold_svd:
            key_nodes.append(node)
            return True
        else:
            left_result = IdentifyKeyNode(node[2], threshold_sed, threshold_svd, key_nodes)
            right_result = IdentifyKeyNode(node[3], threshold_sed, threshold_svd, key_nodes)
            if left_result or right_result:
                key_nodes.append(node)
                return True
            else:
                return False
    else:
        return False


def GetMeasurement(node):
    """
    递归地遍历树，收集所有节点的 ms_vector。

    :param node: 当前处理的树节点。
    :return: 包含所有节点 ms_vector 的列表。
    """
    # 如果节点为空，返回空列表
    if node is None:
        return []

    # 解构当前节点为 split_pt, ms_vector, left_subtree, right_subtree
    _, ms_vector, left_subtree, right_subtree = node

    # 收集当前节点的 ms_vector
    measurements = [ms_vector]

    # 递归获取左子树的所有 ms_vector 并添加到结果中
    measurements.extend(GetMeasurement(left_subtree))

    # 递归获取右子树的所有 ms_vector 并添加到结果中
    measurements.extend(GetMeasurement(right_subtree))
    return measurements


def TDKC(traj):
    # Construct CBT
    cbt = ConstructCBT(traj)

    # Calculate thresholds
    measurements = GetMeasurement(cbt)
    sed_set = [row[0] for row in measurements if len(row) > 0]
    svd_set = [row[1] for row in measurements if len(row) > 0]

    threshold_sed = np.mean(sed_set)
    threshold_svd = np.mean(svd_set)

    # Identify key nodes
    key_nodes = []
    IdentifyKeyNode(cbt, threshold_sed, threshold_svd, key_nodes)

    # Preserve points
    preserved_pts = [traj[0]]
    for node in key_nodes:
        if node[0] not in preserved_pts:
            preserved_pts.append(node[0])
    preserved_pts.append(traj[-1])

    cp_traj = preserved_pts

    return cp_traj

# if __name__ == "__main__":
#     data = pd.read_csv("./custom_trajectory2.csv", skip_blank_lines=True, low_memory=False)
#     data['Timestamp'] = pd.to_datetime(data['Timestamp'])
#     points = data.loc[:, ['Latitude', 'Longitude', 'Timestamp', 'Speed', 'Heading']].values
#
#     if isinstance(points, np.ndarray):
#         points = [tuple(point) for point in points]
#
#     compress_points = TDKC(points)
