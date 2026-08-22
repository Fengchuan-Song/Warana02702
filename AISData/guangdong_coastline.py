"""Fast point-to-coast queries for the bundled Guangdong coastline."""

from __future__ import annotations

import gzip
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


EARTH_RADIUS_METRES = 6_371_000.0
GRID_SIZE_DEGREES = 0.1
COASTLINE_DATA_PATH = (
    Path(__file__).resolve().parent
    / "resources"
    / "guangdong_coastline.json.gz"
)


class GuangdongCoastlineDataError(RuntimeError):
    """Raised when the compact coastline resource cannot be loaded."""


@dataclass(frozen=True)
class CoastRelation:
    near: bool
    distance_metres: float


def _grid_cell(longitude, latitude):
    return (
        math.floor(longitude / GRID_SIZE_DEGREES),
        math.floor(latitude / GRID_SIZE_DEGREES),
    )


def _point_segment_distance_metres(longitude, latitude, start, end):
    latitude_radians = math.radians(latitude)

    def local(point):
        return (
            math.radians(point[0] - longitude)
            * EARTH_RADIUS_METRES
            * math.cos(latitude_radians),
            math.radians(point[1] - latitude) * EARTH_RADIUS_METRES,
        )

    start_x, start_y = local(start)
    end_x, end_y = local(end)
    segment_x = end_x - start_x
    segment_y = end_y - start_y
    length_squared = segment_x**2 + segment_y**2
    if length_squared == 0:
        return math.hypot(start_x, start_y)
    projection = max(
        0.0,
        min(
            1.0,
            -(start_x * segment_x + start_y * segment_y) / length_squared,
        ),
    )
    return math.hypot(
        start_x + projection * segment_x,
        start_y + projection * segment_y,
    )


@lru_cache(maxsize=1)
def load_guangdong_coastline():
    try:
        with gzip.open(COASTLINE_DATA_PATH, "rt", encoding="utf-8") as source:
            document = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuangdongCoastlineDataError("广东海岸线数据不可用") from exc

    if document.get("schema_version") != 1 or document.get("crs") != "EPSG:4326":
        raise GuangdongCoastlineDataError("广东海岸线数据格式无效")
    bounds = document.get("bounds")
    raw_lines = document.get("lines")
    if not isinstance(bounds, list) or len(bounds) != 4 or not isinstance(raw_lines, list):
        raise GuangdongCoastlineDataError("广东海岸线数据内容无效")

    segments = []
    grid = {}
    for raw_line in raw_lines:
        if not isinstance(raw_line, list) or len(raw_line) < 2:
            continue
        for start, end in zip(raw_line, raw_line[1:]):
            try:
                segment = (
                    (float(start[0]), float(start[1])),
                    (float(end[0]), float(end[1])),
                )
            except (IndexError, TypeError, ValueError) as exc:
                raise GuangdongCoastlineDataError(
                    "广东海岸线坐标无效"
                ) from exc
            segment_id = len(segments)
            segments.append(segment)
            min_cell = _grid_cell(
                min(segment[0][0], segment[1][0]),
                min(segment[0][1], segment[1][1]),
            )
            max_cell = _grid_cell(
                max(segment[0][0], segment[1][0]),
                max(segment[0][1], segment[1][1]),
            )
            for cell_x in range(min_cell[0], max_cell[0] + 1):
                for cell_y in range(min_cell[1], max_cell[1] + 1):
                    grid.setdefault((cell_x, cell_y), []).append(segment_id)
    if not segments:
        raise GuangdongCoastlineDataError("广东海岸线数据为空")
    return {
        "bounds": tuple(float(value) for value in bounds),
        "metadata": document.get("metadata", {}),
        "segments": tuple(segments),
        "grid": {cell: tuple(ids) for cell, ids in grid.items()},
    }


def distance_to_guangdong_coast_metres(longitude, latitude, search_metres):
    try:
        longitude = float(longitude)
        latitude = float(latitude)
        search_metres = max(0.0, float(search_metres))
    except (TypeError, ValueError):
        return math.inf
    if not all(math.isfinite(value) for value in (longitude, latitude, search_metres)):
        return math.inf

    data = load_guangdong_coastline()
    min_lon, min_lat, max_lon, max_lat = data["bounds"]
    latitude_margin = math.degrees(search_metres / EARTH_RADIUS_METRES)
    longitude_margin = latitude_margin / max(
        abs(math.cos(math.radians(latitude))), 1e-9
    )
    if not (
        min_lon - longitude_margin <= longitude <= max_lon + longitude_margin
        and min_lat - latitude_margin <= latitude <= max_lat + latitude_margin
    ):
        return math.inf

    min_cell = _grid_cell(
        longitude - longitude_margin,
        latitude - latitude_margin,
    )
    max_cell = _grid_cell(
        longitude + longitude_margin,
        latitude + latitude_margin,
    )
    candidate_ids = set()
    for cell_x in range(min_cell[0], max_cell[0] + 1):
        for cell_y in range(min_cell[1], max_cell[1] + 1):
            candidate_ids.update(data["grid"].get((cell_x, cell_y), ()))
    if not candidate_ids:
        return math.inf
    return min(
        _point_segment_distance_metres(
            longitude,
            latitude,
            *data["segments"][segment_id],
        )
        for segment_id in candidate_ids
    )


def guangdong_nearshore_relation(longitude, latitude, range_metres):
    distance = distance_to_guangdong_coast_metres(
        longitude,
        latitude,
        range_metres,
    )
    return CoastRelation(
        near=distance <= float(range_metres),
        distance_metres=distance,
    )
