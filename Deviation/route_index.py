import json
from math import cos, radians
from pathlib import Path
from threading import Lock

import numpy as np
from django.conf import settings
from sklearn.neighbors import BallTree


EARTH_RADIUS_METRES = 6_371_000
DEFAULT_INDEX_FILE = (
    Path(settings.BASE_DIR)
    / "Deviation"
    / "knowledge"
    / "QZHX_route_samples.npz"
)

_ROUTE_INDEX = None
_ROUTE_INDEX_ATTEMPTED = False
_ROUTE_INDEX_LOCK = Lock()


class RouteIndex:
    def __init__(self, latitudes, longitudes, axes, coherence, metadata):
        self.latitudes = np.asarray(latitudes, dtype=np.float64)
        self.longitudes = np.asarray(longitudes, dtype=np.float64)
        self.axes = np.asarray(axes, dtype=np.float64)
        self.coherence = np.asarray(coherence, dtype=np.float64)
        self.metadata = metadata
        if not (
            len(self.latitudes)
            == len(self.longitudes)
            == len(self.axes)
            == len(self.coherence)
        ):
            raise ValueError("Route-index arrays have different lengths")
        if len(self.latitudes) == 0:
            raise ValueError("Route index is empty")
        coordinates = np.deg2rad(
            np.column_stack((self.latitudes, self.longitudes))
        )
        self.tree = BallTree(coordinates, metric="haversine")
        self.min_lon = float(np.min(self.longitudes))
        self.max_lon = float(np.max(self.longitudes))
        self.min_lat = float(np.min(self.latitudes))
        self.max_lat = float(np.max(self.latitudes))

    def contains(self, lon, lat, margin_metres=0):
        latitude_margin = margin_metres / 111_320
        longitude_scale = max(0.1, cos(radians(float(lat))))
        longitude_margin = margin_metres / (111_320 * longitude_scale)
        return (
            self.min_lon - longitude_margin
            <= lon
            <= self.max_lon + longitude_margin
            and self.min_lat - latitude_margin
            <= lat
            <= self.max_lat + latitude_margin
        )

    def query(self, points):
        """Query ``[(lon, lat), ...]`` and return route attributes."""
        if not points:
            return {
                "distance_metres": np.asarray([], dtype=float),
                "axis_degrees": np.asarray([], dtype=float),
                "coherence": np.asarray([], dtype=float),
            }
        coordinates = np.asarray(
            [[point[1], point[0]] for point in points],
            dtype=np.float64,
        )
        distance, indices = self.tree.query(
            np.deg2rad(coordinates),
            k=1,
        )
        matched = indices[:, 0]
        return {
            "distance_metres": distance[:, 0] * EARTH_RADIUS_METRES,
            "axis_degrees": self.axes[matched],
            "coherence": self.coherence[matched],
        }


def load_route_index(path=None):
    path = Path(path or DEFAULT_INDEX_FILE)
    with np.load(path, allow_pickle=False) as payload:
        metadata_value = payload["metadata_json"]
        metadata_text = str(
            metadata_value.item()
            if metadata_value.ndim == 0
            else metadata_value[0]
        )
        metadata = json.loads(metadata_text)
        return RouteIndex(
            payload["latitudes"],
            payload["longitudes"],
            payload["axes"],
            payload["coherence"],
            metadata,
        )


def get_route_index():
    global _ROUTE_INDEX, _ROUTE_INDEX_ATTEMPTED
    if _ROUTE_INDEX_ATTEMPTED:
        return _ROUTE_INDEX
    with _ROUTE_INDEX_LOCK:
        if not _ROUTE_INDEX_ATTEMPTED:
            try:
                _ROUTE_INDEX = load_route_index()
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                _ROUTE_INDEX = None
            _ROUTE_INDEX_ATTEMPTED = True
    return _ROUTE_INDEX


def reset_route_index_cache():
    global _ROUTE_INDEX, _ROUTE_INDEX_ATTEMPTED
    with _ROUTE_INDEX_LOCK:
        _ROUTE_INDEX = None
        _ROUTE_INDEX_ATTEMPTED = False
