"""Merge built-in areas and database edits for UI and detection workers."""

import math
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

from .maritime_zones import MaritimeZone, _bounds, _polygon_area, load_maritime_zones


CUSTOM_ID_OFFSET = 1_000_000_000
_snapshot_context = ContextVar("maritime_zone_snapshot", default=None)


def make_zone(source_id, name, description, locode, zone_type, vertices,
              is_active=True, source="custom", circle_center=None, circle_radius_m=None):
    points = tuple(tuple(point) for point in vertices)
    return MaritimeZone(
        source_id=source_id, name=name, description=description, locode=locode,
        zone_type=zone_type, points=points, bounds=_bounds(points),
        area=_polygon_area(points), is_active=is_active, source=source,
        circle_center=circle_center, circle_radius_m=circle_radius_m,
    )


@lru_cache(maxsize=1)
def built_in_zones():
    from IllegalAnchored.zones import AUTHORIZED_ANCHORAGES

    zones = list(load_maritime_zones())
    # Negative identifiers are reserved for the existing published list. Keep
    # its order stable; added areas should be created through management.
    for index, definition in enumerate(AUTHORIZED_ANCHORAGES, start=1):
        center = definition.get("center")
        radius = definition.get("radius_m")
        if center:
            # A polygon for display; detection keeps the exact geodesic circle.
            lon, lat = map(math.radians, center)
            angular = radius / 6_371_000
            vertices = []
            for step in range(72):
                bearing = step * math.tau / 72
                y = math.asin(math.sin(lat) * math.cos(angular)
                              + math.cos(lat) * math.sin(angular) * math.cos(bearing))
                x = lon + math.atan2(math.sin(bearing) * math.sin(angular) * math.cos(lat),
                                     math.cos(angular) - math.sin(lat) * math.sin(y))
                vertices.append([math.degrees(x), math.degrees(y)])
        else:
            vertices = definition["points"]
        zones.append(make_zone(
            -index, definition["name"], "已公布锚地资料",
            "CNSZX" if definition["city"] == "深圳" else "CNCAN", "ANC", vertices,
            source="published", circle_center=center, circle_radius_m=radius,
        ))
    return tuple(zones)


def merge_zones(records):
    zones = {zone.source_id: zone for zone in built_in_zones()}
    for record in records:
        identifier = record.source_id if record.source_id is not None else CUSTOM_ID_OFFSET + record.pk
        base = zones.get(identifier)
        # An enable/disable or metadata edit keeps an unchanged circle exact.
        same_geometry = base is not None and record.vertices == [list(p) for p in base.points]
        zones[identifier] = make_zone(
            identifier, record.name, record.description, record.locode,
            record.zone_type, record.vertices, record.is_active,
            source="edited" if record.source_id is not None else "custom",
            circle_center=base.circle_center if same_geometry else None,
            circle_radius_m=base.circle_radius_m if same_geometry else None,
        )
    return tuple(zones.values())


def read_zone_snapshot():
    from .models import MaritimeZoneOverride, MaritimeZoneRevision

    # Detect a commit between the version and row reads without locking readers.
    for _ in range(3):
        revision = MaritimeZoneRevision.objects.filter(pk=1).values_list("version", flat=True).first() or 0
        records = list(MaritimeZoneOverride.objects.all())
        if (MaritimeZoneRevision.objects.filter(pk=1).values_list("version", flat=True).first() or 0) == revision:
            return {"revision": revision, "zones": merge_zones(records)}
    from .maritime_zones import MaritimeZoneDataError
    raise MaritimeZoneDataError("海事区域正在更新，请重试")


def get_zone_snapshot():
    context = _snapshot_context.get()
    if context is None:
        return read_zone_snapshot()
    if "snapshot" not in context:
        context["snapshot"] = read_zone_snapshot()
    return context["snapshot"]


def invalidate_zone_snapshot():
    context = _snapshot_context.get()
    if context is not None:
        context.clear()


@contextmanager
def maritime_zone_snapshot():
    """One lazy, consistent snapshot per request or detector invocation."""
    if _snapshot_context.get() is not None:
        yield
        return
    token = _snapshot_context.set({})
    try:
        yield
    finally:
        _snapshot_context.reset(token)


class MaritimeZoneSnapshotMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with maritime_zone_snapshot():
            return self.get_response(request)
