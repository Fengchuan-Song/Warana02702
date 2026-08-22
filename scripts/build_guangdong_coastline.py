"""Build the compact Guangdong coastline resource used by smuggling detection.

The source coastline is the WGS84 OpenStreetMap coastline Shapefile from
https://osmdata.openstreetmap.de/data/coastlines.html.  The Guangdong boundary
is OpenStreetMap relation 911844, exported as GeoJSON.  This build-time script
requires Fiona, PyProj and Shapely; the Django application does not.
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import fiona
from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import LineString, shape
from shapely.ops import transform, unary_union


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COASTLINE_PATH = (
    PROJECT_ROOT
    / "Data"
    / "Coastline"
    / "OSM"
    / "coastlines-split-4326"
    / "lines.shp"
)
DEFAULT_BOUNDARY_PATH = (
    PROJECT_ROOT
    / "Data"
    / "Coastline"
    / "OSM"
    / "guangdong-boundary-osm.geojson"
)
DEFAULT_OUTPUT_PATH = (
    PROJECT_ROOT / "AISData" / "resources" / "guangdong_coastline.json.gz"
)


def _line_parts(geometry):
    if geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
        return
    if hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _line_parts(part)


def _coordinate_line(line, inverse_transformer, simplify_metres):
    simplified = line.simplify(simplify_metres, preserve_topology=False)
    wgs84_line = transform(inverse_transformer.transform, simplified)
    coordinates = [
        [round(float(longitude), 6), round(float(latitude), 6)]
        for longitude, latitude in wgs84_line.coords
    ]
    return coordinates if len(coordinates) >= 2 else None


def build_resource(
    coastline_path,
    boundary_path,
    output_path,
    selection_buffer_metres=250.0,
    simplify_metres=25.0,
):
    boundary_document = json.loads(boundary_path.read_text(encoding="utf-8"))
    guangdong = make_valid(shape(boundary_document))
    if guangdong.is_empty:
        raise ValueError("Guangdong boundary is empty")

    # A local azimuthal-equidistant projection keeps coastal distance errors
    # small across Guangdong while allowing metre-based buffer/simplification.
    local_crs = (
        "+proj=aeqd +lat_0=23 +lon_0=113.5 +datum=WGS84 +units=m +no_defs"
    )
    forward = Transformer.from_crs("EPSG:4326", local_crs, always_xy=True)
    inverse = Transformer.from_crs(local_crs, "EPSG:4326", always_xy=True)
    guangdong_local = transform(forward.transform, guangdong)
    selection_area = guangdong_local.buffer(selection_buffer_metres)

    min_lon, min_lat, max_lon, max_lat = guangdong.bounds
    margin_degrees = selection_buffer_metres / 100_000.0
    source_bbox = (
        min_lon - margin_degrees,
        min_lat - margin_degrees,
        max_lon + margin_degrees,
        max_lat + margin_degrees,
    )

    selected_lines = []
    with fiona.open(coastline_path) as source:
        for feature in source.filter(bbox=source_bbox):
            source_line = shape(feature["geometry"])
            clipped = transform(forward.transform, source_line).intersection(
                selection_area
            )
            for line in _line_parts(clipped):
                coordinates = _coordinate_line(line, inverse, simplify_metres)
                if coordinates:
                    selected_lines.append(coordinates)

    if not selected_lines:
        raise ValueError("No Guangdong coastline segments were selected")

    merged = unary_union([LineString(line) for line in selected_lines])
    bounds = [round(float(value), 6) for value in merged.bounds]
    vertex_count = sum(len(line) for line in selected_lines)
    document = {
        "schema_version": 1,
        "name": "广东省海岸线（OpenStreetMap）",
        "crs": "EPSG:4326",
        "bounds": bounds,
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "coastline_source": (
                "https://osmdata.openstreetmap.de/data/coastlines.html"
            ),
            "coastline_package": "coastlines-split-4326",
            "boundary_source": (
                "https://www.openstreetmap.org/relation/911844"
            ),
            "boundary_relation_id": 911844,
            "selection_buffer_metres": selection_buffer_metres,
            "simplify_metres": simplify_metres,
            "license": "ODbL; © OpenStreetMap contributors",
            "line_count": len(selected_lines),
            "vertex_count": vertex_count,
        },
        "lines": selected_lines,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_path, "wt", encoding="utf-8", compresslevel=9) as target:
        json.dump(document, target, ensure_ascii=False, separators=(",", ":"))
        target.write("\n")
    return document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coastline", type=Path, default=DEFAULT_COASTLINE_PATH)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--selection-buffer-metres", type=float, default=250.0)
    parser.add_argument("--simplify-metres", type=float, default=25.0)
    arguments = parser.parse_args()
    document = build_resource(
        arguments.coastline,
        arguments.boundary,
        arguments.output,
        arguments.selection_buffer_metres,
        arguments.simplify_metres,
    )
    print(
        json.dumps(
            {
                "output": str(arguments.output.resolve()),
                "bounds": document["bounds"],
                **document["metadata"],
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
