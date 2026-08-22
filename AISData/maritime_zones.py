"""Load and query the bundled Guangdong, Hong Kong and Macau maritime zones."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENCRYPTED_DATASET_PATH = (
    Path(__file__).resolve().parent
    / "resources"
    / "maritime_zones_gd_hk_mo.csv.aes"
)
DEFAULT_KEY_PATH = PROJECT_ROOT / ".runtime" / "secrets" / "maritime_zone_data.key"
KEY_ENV_VAR = "MARITIME_ZONE_DATA_KEY"
KEY_FILE_ENV_VAR = "MARITIME_ZONE_DATA_KEY_FILE"
ENCRYPTED_FILE_MAGIC = b"MZ01"
ENCRYPTED_FILE_NONCE_SIZE = 12
ENCRYPTED_FILE_AAD = b"WanAna02702:maritime_zones:v1"
POLYGON_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
SUPPORTED_ZONE_TYPES = frozenset(
    {"PRT", "ANC", "LIQ", "GCO", "DRY", "CTR", "GAS", "ROR", "PAX"}
)
PORT_ZONE_TYPES = frozenset({"PRT", "LIQ", "GCO", "DRY", "CTR", "GAS", "ROR", "PAX"})
ANCHORAGE_ZONE_TYPES = frozenset({"ANC"})


class MaritimeZoneDataError(RuntimeError):
    """Raised when encrypted maritime-zone data cannot be securely loaded."""


def _decode_encryption_key(value):
    try:
        padded_value = value.strip() + "=" * (-len(value.strip()) % 4)
        key = base64.urlsafe_b64decode(padded_value.encode("ascii"))
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        raise MaritimeZoneDataError("海事区域数据密钥格式无效") from exc
    if len(key) != 32:
        raise MaritimeZoneDataError("海事区域数据密钥必须为 256 位")
    return key


def _load_encryption_key():
    environment_key = os.environ.get(KEY_ENV_VAR)
    if environment_key:
        return _decode_encryption_key(environment_key)

    configured_path = os.environ.get(KEY_FILE_ENV_VAR)
    key_path = Path(configured_path) if configured_path else DEFAULT_KEY_PATH
    try:
        encoded_key = key_path.read_text(encoding="ascii")
    except OSError as exc:
        raise MaritimeZoneDataError("未配置海事区域数据解密密钥") from exc
    return _decode_encryption_key(encoded_key)


def _decrypt_dataset_text():
    try:
        payload = ENCRYPTED_DATASET_PATH.read_bytes()
    except OSError as exc:
        raise MaritimeZoneDataError("海事区域加密数据文件不可用") from exc

    minimum_size = len(ENCRYPTED_FILE_MAGIC) + ENCRYPTED_FILE_NONCE_SIZE + 16
    if len(payload) < minimum_size or not payload.startswith(ENCRYPTED_FILE_MAGIC):
        raise MaritimeZoneDataError("海事区域加密数据文件格式无效")

    nonce_start = len(ENCRYPTED_FILE_MAGIC)
    ciphertext_start = nonce_start + ENCRYPTED_FILE_NONCE_SIZE
    nonce = payload[nonce_start:ciphertext_start]
    ciphertext = payload[ciphertext_start:]
    try:
        plaintext = AESGCM(_load_encryption_key()).decrypt(
            nonce,
            ciphertext,
            ENCRYPTED_FILE_AAD,
        )
    except InvalidTag as exc:
        raise MaritimeZoneDataError("海事区域加密数据验证失败") from exc
    try:
        return plaintext.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MaritimeZoneDataError("海事区域加密数据内容无效") from exc


@dataclass(frozen=True)
class MaritimeZone:
    source_id: int
    name: str
    description: str
    locode: str
    zone_type: str
    points: tuple[tuple[float, float], ...]
    bounds: tuple[float, float, float, float]
    area: float

    @property
    def region(self):
        if self.locode.startswith("HK"):
            return "香港"
        if self.locode.startswith("MO"):
            return "澳门"
        return "广东"

    def as_geojson_feature(self):
        coordinates = list(self.points)
        if coordinates and coordinates[0] != coordinates[-1]:
            coordinates.append(coordinates[0])
        return {
            "type": "Feature",
            "id": self.source_id,
            "properties": {
                "id": self.source_id,
                "name": self.name,
                "description": self.description,
                "locode": self.locode,
                "zone_type": self.zone_type,
                "region": self.region,
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [coordinates],
            },
        }


def _parse_polygon(value):
    numbers = [float(item) for item in POLYGON_NUMBER_PATTERN.findall(value or "")]
    if len(numbers) < 6 or len(numbers) % 2:
        raise ValueError("polygon geometry must contain at least three coordinate pairs")
    points = tuple(zip(numbers[0::2], numbers[1::2]))
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        raise ValueError("polygon geometry must contain at least three distinct points")
    return points


def _polygon_area(points):
    return abs(
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
        )
    ) / 2


def _bounds(points):
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return (
        min(longitudes),
        min(latitudes),
        max(longitudes),
        max(latitudes),
    )


@lru_cache(maxsize=1)
def load_maritime_zones():
    """Return the validated bundled maritime zones as an immutable tuple."""
    zones = []
    with io.StringIO(_decrypt_dataset_text(), newline="") as source:
        reader = csv.DictReader(source)
        required = {
            "id",
            "AOI_Name",
            "AOI_Description",
            "locode",
            "type",
            "geometry",
        }
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(
                "maritime zone dataset is missing columns: "
                + ", ".join(sorted(missing))
            )

        seen_ids = set()
        for line_number, row in enumerate(reader, start=2):
            try:
                source_id = int(row["id"])
                zone_type = (row["type"] or "").strip().upper()
                locode = (row["locode"] or "").strip().upper()
                points = _parse_polygon(row["geometry"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid maritime zone at CSV line {line_number}: {exc}"
                ) from exc
            if source_id in seen_ids:
                raise ValueError(f"duplicate maritime zone id: {source_id}")
            if zone_type not in SUPPORTED_ZONE_TYPES:
                raise ValueError(f"unsupported maritime zone type: {zone_type}")
            if not locode.startswith(("CN", "HK", "MO")):
                raise ValueError(f"unsupported maritime zone locode: {locode}")
            seen_ids.add(source_id)
            zones.append(
                MaritimeZone(
                    source_id=source_id,
                    name=(row["AOI_Name"] or "").strip(),
                    description=(row["AOI_Description"] or "").strip(),
                    locode=locode,
                    zone_type=zone_type,
                    points=points,
                    bounds=_bounds(points),
                    area=_polygon_area(points),
                )
            )
    return tuple(zones)


def point_in_polygon(longitude, latitude, points):
    """Return whether a point is inside a polygon, including its boundary."""
    inside = False
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        cross = (longitude - x1) * (y2 - y1) - (latitude - y1) * (x2 - x1)
        if abs(cross) <= 1e-10 and (
            min(x1, x2) - 1e-10 <= longitude <= max(x1, x2) + 1e-10
            and min(y1, y2) - 1e-10 <= latitude <= max(y1, y2) + 1e-10
        ):
            return True
        if (y1 > latitude) != (y2 > latitude):
            intersection_x = x1 + (latitude - y1) * (x2 - x1) / (y2 - y1)
            if longitude < intersection_x:
                inside = not inside
    return inside


def get_maritime_zones(zone_types=None, locodes=None):
    zone_types = (
        {str(value).strip().upper() for value in zone_types}
        if zone_types
        else None
    )
    locodes = (
        {str(value).strip().upper() for value in locodes}
        if locodes
        else None
    )
    return tuple(
        zone
        for zone in load_maritime_zones()
        if (zone_types is None or zone.zone_type in zone_types)
        and (locodes is None or zone.locode in locodes)
    )


def zones_containing_point(longitude, latitude, zone_types=None):
    try:
        longitude = float(longitude)
        latitude = float(latitude)
    except (TypeError, ValueError):
        return ()
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        return ()
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        return ()

    matches = []
    for zone in get_maritime_zones(zone_types=zone_types):
        min_lon, min_lat, max_lon, max_lat = zone.bounds
        if not (
            min_lon <= longitude <= max_lon
            and min_lat <= latitude <= max_lat
        ):
            continue
        if point_in_polygon(longitude, latitude, zone.points):
            matches.append(zone)
    return tuple(sorted(matches, key=lambda zone: (zone.area, zone.source_id)))


def find_authorized_anchorage(longitude, latitude):
    try:
        matches = zones_containing_point(
            longitude,
            latitude,
            zone_types=ANCHORAGE_ZONE_TYPES,
        )
    except MaritimeZoneDataError:
        return None
    return matches[0] if matches else None


def maritime_zone_geojson(zone_types=None, locodes=None):
    zones = get_maritime_zones(zone_types=zone_types, locodes=locodes)
    return {
        "type": "FeatureCollection",
        "count": len(zones),
        "type_counts": dict(sorted(Counter(zone.zone_type for zone in zones).items())),
        "region_counts": dict(sorted(Counter(zone.region for zone in zones).items())),
        "features": [zone.as_geojson_feature() for zone in zones],
    }
