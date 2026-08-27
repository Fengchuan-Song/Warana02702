import math


def _orientation(first, second, third, epsilon=1e-12):
    value = (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )
    if abs(value) <= epsilon:
        return 0
    return 1 if value > 0 else -1


def _point_on_segment(point, start, end, epsilon=1e-10):
    if _orientation(start, end, point, epsilon) != 0:
        return False
    return (
        min(start[0], end[0]) - epsilon
        <= point[0]
        <= max(start[0], end[0]) + epsilon
        and min(start[1], end[1]) - epsilon
        <= point[1]
        <= max(start[1], end[1]) + epsilon
    )


def _segments_intersect(first_start, first_end, second_start, second_end):
    orientations = (
        _orientation(first_start, first_end, second_start),
        _orientation(first_start, first_end, second_end),
        _orientation(second_start, second_end, first_start),
        _orientation(second_start, second_end, first_end),
    )
    if orientations[0] != orientations[1] and orientations[2] != orientations[3]:
        return True
    return any(
        (
            orientations[0] == 0
            and _point_on_segment(second_start, first_start, first_end),
            orientations[1] == 0
            and _point_on_segment(second_end, first_start, first_end),
            orientations[2] == 0
            and _point_on_segment(first_start, second_start, second_end),
            orientations[3] == 0
            and _point_on_segment(first_end, second_start, second_end),
        )
    )


def _polygon_area(vertices):
    return abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(vertices, vertices[1:] + vertices[:1])
        )
    ) / 2


def _validate_simple_polygon(vertices, label):
    if _polygon_area(vertices) <= 1e-12:
        raise ValueError(f"{label}顶点不能共线或形成零面积区域")

    count = len(vertices)
    for index in range(count):
        first_start = vertices[index]
        first_end = vertices[(index + 1) % count]
        for other_index in range(index + 1, count):
            if (
                other_index == (index + 1) % count
                or index == (other_index + 1) % count
            ):
                continue
            second_start = vertices[other_index]
            second_end = vertices[(other_index + 1) % count]
            if _segments_intersect(
                first_start,
                first_end,
                second_start,
                second_end,
            ):
                raise ValueError(f"{label}边线不能自相交")


def normalise_polygon_vertices(value, label="监控区", max_vertices=500):
    if not isinstance(value, list):
        raise ValueError("顶点坐标必须是数组")
    if len(value) > max_vertices + 1:
        raise ValueError(f"{label}最多允许 {max_vertices} 个顶点")

    vertices = []
    for index, point in enumerate(value, start=1):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"第 {index} 个顶点必须是 [经度, 纬度]")
        try:
            longitude = float(point[0])
            latitude = float(point[1])
        except (TypeError, ValueError):
            raise ValueError(f"第 {index} 个顶点的经纬度必须是有限数字")
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise ValueError(f"第 {index} 个顶点的经纬度必须是有限数字")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError(f"第 {index} 个顶点超出合法经纬度范围")
        vertices.append([longitude, latitude])

    if len(vertices) > 1 and vertices[0] == vertices[-1]:
        vertices.pop()
    if len(vertices) < 3 or len({tuple(point) for point in vertices}) < 3:
        raise ValueError(f"{label}至少需要 3 个不同顶点")
    if any(
        first == second
        for first, second in zip(vertices, vertices[1:] + vertices[:1])
    ):
        raise ValueError(f"{label}不能包含相邻的重复顶点")
    if max(point[0] for point in vertices) - min(
        point[0] for point in vertices
    ) > 180:
        raise ValueError(f"暂不支持跨越国际日期变更线的{label}")

    _validate_simple_polygon(vertices, label)
    return vertices


def polygon_bounds(vertices):
    return {
        "min_lon": min(point[0] for point in vertices),
        "min_lat": min(point[1] for point in vertices),
        "max_lon": max(point[0] for point in vertices),
        "max_lat": max(point[1] for point in vertices),
    }


def rectangle_vertices(min_lon, min_lat, max_lon, max_lat):
    return [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
    ]
