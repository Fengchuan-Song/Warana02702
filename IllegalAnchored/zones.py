"""Published Guangdong anchorage geometry used by illegal-anchor detection.

Coordinates use CGCS2000/WGS84-compatible longitude/latitude degrees.  The
geometry is deliberately kept in Python data rather than hidden in detector
code so it can be reviewed and replaced when the maritime authority publishes
an adjustment.

The local source documents supplied with this project provide the anchorage
names and the 2021 Shenzhen restricted-water coordinates.  Geometry below is
supplemented by official MSA notices:

* Shenzhen MSA, adjusted yacht navigation restriction waters (2025).
* Shenzhen Special Economic Zone Maritime Traffic Safety Regulation (2025).
* China MSA chart correction notice 2025-16.
* Guangzhou MSA/chart correction information for 45SJA and 70DHA/B (2025).

An AIS alert is only a screening result.  Actual anchoring permission,
emergency reports and temporary traffic controls must still be checked with
the competent maritime authority.
"""

from math import asin, cos, radians, sin, sqrt

from AISData.maritime_zones import find_authorized_anchorage


SOURCE_METADATA = {
    "regulation": {
        "name": "深圳经济特区海上交通安全条例",
        "url": "https://www.sz.msa.gov.cn/flfg/2198726.jhtml",
        "effective_date": "2025-07-20",
    },
    "shenzhen_geometry": {
        "name": "深圳海上游艇航行限制水域及海图改正资料",
        "url": (
            "https://www.msa.gov.cn/public/documents/document/"
            "mdkx/njm4/~edisp/20250421091638111.pdf"
        ),
        "geometry_date": "2025",
        "note": "用于锚地边界几何；临时/游艇限制条款不作为当前执法依据",
    },
    "guangzhou_geometry": {
        "name": "广州港锚地调整海图改正资料",
        "url": (
            "https://pnp.chart.msa.gov.cn/CorrectionPdf/Download/"
            "7149f8aa-aabb-4b12-b44f-24c1825707d9?y=2025"
        ),
        "geometry_date": "2025",
        "note": "仅补充已取得精确坐标的锚地，不代表广州港锚地全集",
    },
}


def dms(degrees, minutes=0, seconds=0):
    """Convert a positive DMS coordinate used in Guangdong to decimal degrees."""
    return degrees + minutes / 60 + seconds / 3600


def ll(lon_d, lon_m, lon_s, lat_d, lat_m, lat_s):
    """Return a (longitude, latitude) pair from DMS components."""
    return (dms(lon_d, lon_m, lon_s), dms(lat_d, lat_m, lat_s))


# Official Shenzhen anchorages whose complete boundary coordinates are
# available.  Temporary Huangtian cargo anchorage is intentionally excluded.
AUTHORIZED_ANCHORAGES = [
    {
        "name": "大亚湾1号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(114, 37, 13, 22, 32, 37),
            ll(114, 33, 3, 22, 34, 28),
            ll(114, 36, 55, 22, 34, 2),
        ],
    },
    {
        "name": "大亚湾2号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(114, 36, 49, 22, 34, 29),
            ll(114, 33, 8, 22, 34, 54),
            ll(114, 36, 0, 22, 38, 12),
        ],
    },
    {
        "name": "大鹏湾1号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(114, 37, 44, 22, 23, 15),
            ll(114, 34, 14, 22, 21, 36),
            ll(114, 31, 50, 22, 25, 54),
            ll(114, 35, 14, 22, 27, 30),
        ],
    },
    {
        "name": "大鹏湾LNG专用锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(114, 27, 33, 22, 30, 30),
            ll(114, 28, 15, 22, 30, 30),
            ll(114, 28, 15, 22, 30, 3),
            ll(114, 27, 33, 22, 30, 3),
        ],
    },
    {
        "name": "大鹏湾2号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(114, 26, 14, 22, 33, 50),
            ll(114, 26, 22, 22, 34, 0),
            ll(114, 28, 0, 22, 32, 42),
            ll(114, 28, 15, 22, 31, 12),
            ll(114, 28, 15, 22, 31, 6),
            ll(114, 27, 31, 22, 31, 6),
            ll(114, 27, 31, 22, 32, 47),
        ],
    },
    {
        "name": "大鹏湾3号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(114, 24, 0, 22, 36, 0),
            ll(114, 25, 4, 22, 35, 42),
            ll(114, 25, 4, 22, 34, 49),
            ll(114, 24, 40, 22, 34, 4),
            ll(114, 24, 0, 22, 34, 4),
        ],
    },
    {
        "name": "大鹏湾4号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(114, 20, 18, 22, 35, 24),
            ll(114, 21, 48, 22, 36, 0),
            ll(114, 22, 31, 22, 36, 0),
            ll(114, 23, 14, 22, 35, 0),
            ll(114, 23, 14, 22, 34, 6),
            ll(114, 20, 18, 22, 34, 17),
        ],
    },
    {
        "name": "大鹏湾5号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(114, 18, 54, 22, 35, 24),
            ll(114, 20, 0, 22, 35, 24),
            ll(114, 20, 0, 22, 34, 9),
            ll(114, 18, 12, 22, 34, 9),
            ll(114, 18, 12, 22, 34, 48),
        ],
    },
    {
        "name": "大屿山1号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(113, 49, 20, 22, 16, 15),
            ll(113, 50, 9, 22, 16, 15),
            ll(113, 50, 22, 22, 16, 11),
            ll(113, 49, 44, 22, 14, 47),
        ],
    },
    {
        "name": "大屿山2号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(113, 50, 50, 22, 19, 47),
            ll(113, 52, 0, 22, 19, 47),
            ll(113, 51, 19, 22, 18, 16),
            ll(113, 49, 56, 22, 18, 16),
        ],
    },
    {
        "name": "东角头锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(113, 56, 14, 22, 28, 28),
            ll(113, 56, 37, 22, 28, 30),
            ll(113, 56, 44, 22, 28, 18),
            ll(113, 56, 12, 22, 28, 0),
        ],
    },
    {
        "name": "孖洲西危险品锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(113, 49, 2, 22, 29, 43),
            ll(113, 49, 37, 22, 29, 43),
            ll(113, 50, 49, 22, 28, 21),
            ll(113, 49, 2, 22, 28, 21),
        ],
    },
    {
        "name": "矾石小型船舶锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(113, 46, 12, 22, 32, 55),
            ll(113, 47, 3, 22, 33, 8),
            ll(113, 47, 57, 22, 31, 56),
            ll(113, 47, 44, 22, 31, 28),
            ll(113, 46, 9, 22, 31, 30),
        ],
    },
    {
        "name": "黄田3号锚地",
        "city": "深圳",
        "shape": "polygon",
        "points": [
            ll(113, 45, 13, 22, 39, 2),
            ll(113, 44, 39, 22, 39, 2),
            ll(113, 44, 26, 22, 40, 0),
            ll(113, 45, 0, 22, 40, 0),
        ],
    },
    # Additional Guangzhou anchorages with exact 2025 official coordinates.
    {
        "name": "45SJA锚地",
        "city": "广州",
        "shape": "circle",
        "center": ll(113, 38, 24, 22, 44, 35),
        "radius_m": 435,
    },
    {
        "name": "70DHA锚地",
        "city": "广州",
        "shape": "polygon",
        "points": [
            ll(113, 33, 17, 22, 51, 29.5),
            ll(113, 33, 34, 22, 51, 36),
            ll(113, 33, 47, 22, 51, 15),
            ll(113, 33, 29, 22, 51, 10),
        ],
    },
    {
        "name": "70DHB锚地",
        "city": "广州",
        "shape": "polygon",
        "points": [
            ll(113, 33, 33, 22, 51, 4),
            ll(113, 33, 50, 22, 51, 9),
            ll(113, 33, 54, 22, 51, 3),
            ll(113, 33, 45, 22, 50, 45),
        ],
    },
]


# Explicit no-anchor/traffic-warning geometry in the current Shenzhen
# regulation.  These take precedence over ordinary anchorage classification.
PROHIBITED_ANCHOR_ZONES = [
    {
        "name": "深圳西部公共航路",
        "reason": "航路内禁止锚泊",
        "points": [
            ll(113, 51, 42.76, 22, 29, 26.08),  # H1
            ll(113, 52, 8.97, 22, 28, 23.30),   # H2
            ll(113, 52, 39.28, 22, 27, 23.43),  # H3
            ll(113, 52, 57.05, 22, 26, 58.66),  # H4
            ll(113, 52, 10.45, 22, 26, 40.60),  # H8
            ll(113, 52, 10.34, 22, 27, 9.90),   # H7
            ll(113, 52, 2.17, 22, 27, 40.78),   # H6
            ll(113, 51, 21.80, 22, 29, 17.26),  # H5
        ],
    },
    {
        "name": "妈湾警戒区",
        "reason": "警戒区内禁止锚泊",
        "points": [
            ll(113, 51, 8.17, 22, 29, 42.18),
            ll(113, 51, 33.40, 22, 29, 51.70),
            ll(113, 51, 44.74, 22, 29, 26.86),
            ll(113, 51, 19.87, 22, 29, 16.40),
        ],
    },
    {
        "name": "蛇口警戒区",
        "reason": "警戒区内禁止锚泊",
        "points": [
            ll(113, 52, 10.60, 22, 26, 40.50),
            ll(113, 53, 2.00, 22, 27, 0.50),
            ll(113, 53, 12.10, 22, 26, 19.00),
            ll(113, 52, 43.10, 22, 26, 3.00),
        ],
    },
]


# Conservative screening envelopes.  "Outside an anchorage" is only evaluated
# inside Shenzhen waters for which this project has reasonably complete
# anchorage geometry.  Guangzhou additions are legal-zone supplements only.
DETECTION_AREAS = [
    {
        "name": "深圳西部锚泊规则覆盖区",
        "bounds": (113.72, 22.24, 114.02, 22.72),
    },
    {
        "name": "深圳东部锚泊规则覆盖区",
        "bounds": (114.15, 22.32, 114.70, 22.70),
    },
]


def point_in_polygon(lon, lat, points):
    """Ray-casting point-in-polygon test with boundary-inclusive behaviour."""
    inside = False
    count = len(points)
    if count < 3:
        return False

    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]

        cross = (lon - x1) * (y2 - y1) - (lat - y1) * (x2 - x1)
        if abs(cross) <= 1e-10:
            if (
                min(x1, x2) - 1e-10 <= lon <= max(x1, x2) + 1e-10
                and min(y1, y2) - 1e-10 <= lat <= max(y1, y2) + 1e-10
            ):
                return True

        if (y1 > lat) != (y2 > lat):
            intersection_x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < intersection_x:
                inside = not inside
    return inside


def distance_m(lon1, lat1, lon2, lat2):
    """Great-circle distance in metres."""
    earth_radius_m = 6_371_000
    lon_delta = radians(lon2 - lon1)
    lat_delta = radians(lat2 - lat1)
    lat1_rad = radians(lat1)
    lat2_rad = radians(lat2)
    value = (
        sin(lat_delta / 2) ** 2
        + cos(lat1_rad) * cos(lat2_rad) * sin(lon_delta / 2) ** 2
    )
    return 2 * earth_radius_m * asin(sqrt(min(1, value)))


def _inside_zone(lon, lat, zone):
    if zone["shape"] == "circle":
        center_lon, center_lat = zone["center"]
        return distance_m(lon, lat, center_lon, center_lat) <= zone["radius_m"]
    return point_in_polygon(lon, lat, zone["points"])


def classify_location(lon, lat):
    """Classify one position for anchoring-rule screening."""
    for zone in PROHIBITED_ANCHOR_ZONES:
        if point_in_polygon(lon, lat, zone["points"]):
            return {
                "state": "prohibited",
                "zone_name": zone["name"],
                "reason": zone["reason"],
            }

    for zone in AUTHORIZED_ANCHORAGES:
        if _inside_zone(lon, lat, zone):
            return {
                "state": "authorized",
                "zone_name": zone["name"],
                "reason": "位于已公布锚地范围内",
            }

    dataset_anchorage = find_authorized_anchorage(lon, lat)
    if dataset_anchorage is not None:
        return {
            "state": "authorized",
            "zone_name": dataset_anchorage.name,
            "reason": (
                "位于系统海事区域数据集锚地范围内"
                f"（{dataset_anchorage.locode}）"
            ),
        }

    for area in DETECTION_AREAS:
        min_lon, min_lat, max_lon, max_lat = area["bounds"]
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
            return {
                "state": "unassigned",
                "zone_name": area["name"],
                "reason": "未落入系统已收录的授权锚地",
            }

    return {
        "state": "outside_coverage",
        "zone_name": None,
        "reason": "超出当前锚泊规则数据覆盖范围",
    }
