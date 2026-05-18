"""Build lightweight UTM tile presets from GeoPackage sources.

The Tkinter launcher consumes the generated JSON only. This utility is an
offline helper so the GUI does not need geopandas, shapely, Fiona, or a map
stack at runtime.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import math
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from swot_download_tool import UTM_TILE_RE


Point = Tuple[float, float]
Ring = List[Point]
Polygon = List[Ring]
MultiPolygon = List[Polygon]
BBox = Tuple[float, float, float, float]


@dataclass
class GridFeature:
    """One UTM grid tile from the source GeoPackage."""

    fid: int
    token: str
    bbox: BBox
    geometry: MultiPolygon


@dataclass
class ContinentFeature:
    """One continent feature from the source GeoPackage."""

    name: str
    bbox: BBox
    geometry: MultiPolygon


def normalize_grid_token(zone: object, row: object) -> str | None:
    """Normalize GeoPackage ZONE/ROW attributes into a SWOT UTM token."""
    try:
        zone_int = int(float(str(zone)))
    except (TypeError, ValueError):
        return None
    row_text = str(row or "").strip().upper()
    if len(row_text) != 1:
        return None
    token = f"UTM{zone_int:02d}{row_text}"
    return token if UTM_TILE_RE.match(token) else None


def gpkg_wkb(blob: bytes) -> bytes:
    """Strip a GeoPackage geometry header and return plain WKB bytes."""
    if blob[:2] != b"GP":
        return blob
    flags = blob[3]
    envelope_code = (flags >> 1) & 0b111
    envelope_lengths = {
        0: 0,
        1: 32,
        2: 48,
        3: 48,
        4: 64,
    }
    if envelope_code not in envelope_lengths:
        raise ValueError(f"Unsupported GeoPackage envelope code: {envelope_code}")
    return blob[8 + envelope_lengths[envelope_code] :]


def read_uint(data: bytes, offset: int, endian: str) -> Tuple[int, int]:
    return struct.unpack_from(f"{endian}I", data, offset)[0], offset + 4


def read_point(data: bytes, offset: int, endian: str) -> Tuple[Point, int]:
    point = struct.unpack_from(f"{endian}dd", data, offset)
    return (point[0], point[1]), offset + 16


def read_polygon_wkb(data: bytes, offset: int = 0) -> Tuple[Polygon, int]:
    """Read a WKB Polygon and return rings plus the new offset."""
    byte_order = data[offset]
    endian = "<" if byte_order == 1 else ">"
    offset += 1
    geometry_type, offset = read_uint(data, offset, endian)
    if geometry_type % 1000 != 3:
        raise ValueError(f"Expected WKB Polygon, got geometry type {geometry_type}")
    ring_count, offset = read_uint(data, offset, endian)
    polygon: Polygon = []
    for _ in range(ring_count):
        point_count, offset = read_uint(data, offset, endian)
        ring: Ring = []
        for _ in range(point_count):
            point, offset = read_point(data, offset, endian)
            ring.append(point)
        polygon.append(ring)
    return polygon, offset


def read_gpkg_multipolygon(blob: bytes) -> MultiPolygon:
    """Read a GeoPackage Polygon or MultiPolygon geometry into rings."""
    data = gpkg_wkb(blob)
    byte_order = data[0]
    endian = "<" if byte_order == 1 else ">"
    offset = 1
    geometry_type, offset = read_uint(data, offset, endian)
    base_type = geometry_type % 1000
    if base_type == 3:
        polygon, _offset = read_polygon_wkb(data, 0)
        return [polygon]
    if base_type != 6:
        raise ValueError(f"Unsupported WKB geometry type {geometry_type}")
    polygon_count, offset = read_uint(data, offset, endian)
    multipolygon: MultiPolygon = []
    for _ in range(polygon_count):
        polygon, offset = read_polygon_wkb(data, offset)
        multipolygon.append(polygon)
    return multipolygon


def geometry_bbox(geometry: MultiPolygon) -> BBox:
    """Return minx, maxx, miny, maxy for a multipolygon."""
    xs: List[float] = []
    ys: List[float] = []
    for polygon in geometry:
        for ring in polygon:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    return min(xs), max(xs), min(ys), max(ys)


def bbox_overlaps(a: BBox, b: BBox) -> bool:
    return not (a[1] < b[0] or a[0] > b[1] or a[3] < b[2] or a[2] > b[3])


def point_in_ring(point: Point, ring: Ring) -> bool:
    """Return True when point is inside a closed ring."""
    x, y = point
    inside = False
    if len(ring) < 3:
        return False
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        if ((current_y > y) != (previous_y > y)) and (
            x
            < (previous_x - current_x) * (y - current_y) / (previous_y - current_y)
            + current_x
        ):
            inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Return True when point is inside a polygon and outside its holes."""
    if not polygon or not point_in_ring(point, polygon[0]):
        return False
    return not any(point_in_ring(point, hole) for hole in polygon[1:])


def orientation(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def on_segment(a: Point, b: Point, c: Point) -> bool:
    return (
        min(a[0], c[0]) <= b[0] <= max(a[0], c[0])
        and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])
    )


def segments_intersect(a1: Point, a2: Point, b1: Point, b2: Point) -> bool:
    """Return True when two closed line segments intersect."""
    o1 = orientation(a1, a2, b1)
    o2 = orientation(a1, a2, b2)
    o3 = orientation(b1, b2, a1)
    o4 = orientation(b1, b2, a2)
    epsilon = 1e-9
    if abs(o1) < epsilon and on_segment(a1, b1, a2):
        return True
    if abs(o2) < epsilon and on_segment(a1, b2, a2):
        return True
    if abs(o3) < epsilon and on_segment(b1, a1, b2):
        return True
    if abs(o4) < epsilon and on_segment(b1, a2, b2):
        return True
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def ring_segments(ring: Ring) -> Iterable[Tuple[Point, Point]]:
    for index in range(len(ring)):
        yield ring[index - 1], ring[index]


def polygon_intersects(a: Polygon, b: Polygon) -> bool:
    """Return True when two polygons intersect."""
    if not a or not b:
        return False
    if any(point_in_polygon(point, b) for point in a[0]):
        return True
    if any(point_in_polygon(point, a) for point in b[0]):
        return True
    b_segments = [
        segment
        for ring_b in b
        for segment in ring_segments(ring_b)
    ]
    for ring_a in a:
        for segment_a in ring_segments(ring_a):
            for segment_b in b_segments:
                if segments_intersect(segment_a[0], segment_a[1], segment_b[0], segment_b[1]):
                    return True
    return False


def multipolygon_intersects(a: MultiPolygon, b: MultiPolygon) -> bool:
    """Return True when two multipolygons intersect."""
    a_polygons = [(polygon, geometry_bbox([polygon])) for polygon in a]
    b_polygons = [(polygon, geometry_bbox([polygon])) for polygon in b]
    for polygon_a, bbox_a in a_polygons:
        for polygon_b, bbox_b in b_polygons:
            if not bbox_overlaps(bbox_a, bbox_b):
                continue
            if polygon_intersects(polygon_a, polygon_b):
                return True
    return False


def read_grid_features(path: Path) -> List[GridFeature]:
    """Read valid SWOT UTM grid features from a GeoPackage."""
    features: List[GridFeature] = []
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            'select FID, ZONE, ROW_, SHAPE from "World_UTM_Grid"'
        ).fetchall()
    for fid, zone, row, shape in rows:
        token = normalize_grid_token(zone, row)
        if token is None:
            continue
        geometry = read_gpkg_multipolygon(shape)
        features.append(
            GridFeature(
                fid=int(fid),
                token=token,
                bbox=geometry_bbox(geometry),
                geometry=geometry,
            )
        )
    return features


def read_continent_features(path: Path) -> List[ContinentFeature]:
    """Read continent features from a GeoPackage."""
    features: List[ContinentFeature] = []
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            'select CONTINENT, SHAPE from "World_Continents"'
        ).fetchall()
    for name, shape in rows:
        geometry = read_gpkg_multipolygon(shape)
        features.append(
            ContinentFeature(
                name=str(name),
                bbox=geometry_bbox(geometry),
                geometry=geometry,
            )
        )
    return features


def build_continent_presets(
    utm_grid: Sequence[GridFeature],
    continents: Sequence[ContinentFeature],
) -> dict:
    """Intersect UTM grid features with continents and return JSON-ready presets."""
    presets = {}
    for continent in sorted(continents, key=lambda item: item.name):
        tiles = []
        for grid in utm_grid:
            if not bbox_overlaps(grid.bbox, continent.bbox):
                continue
            if multipolygon_intersects(grid.geometry, continent.geometry):
                tiles.append(grid.token)
        presets[continent.name] = {
            "name": continent.name,
            "description": f"UTM tiles intersecting {continent.name}.",
            "source": "continent",
            "tiles": sorted(set(tiles)),
        }
    return presets


def bbox_to_bounds(bbox: BBox) -> List[float]:
    """Convert internal minx, maxx, miny, maxy bbox to JSON bounds."""
    minx, maxx, miny, maxy = bbox
    return [round(minx, 2), round(miny, 2), round(maxx, 2), round(maxy, 2)]


def combined_bounds(features: Sequence[GridFeature] | Sequence[ContinentFeature]) -> List[float]:
    """Return JSON bounds covering all features."""
    minx = min(feature.bbox[0] for feature in features)
    maxx = max(feature.bbox[1] for feature in features)
    miny = min(feature.bbox[2] for feature in features)
    maxy = max(feature.bbox[3] for feature in features)
    return [round(minx, 2), round(miny, 2), round(maxx, 2), round(maxy, 2)]


def thin_ring(ring: Ring, max_points: int) -> List[List[float]]:
    """Return a display-friendly exterior ring with at most max_points points."""
    if not ring:
        return []
    points = ring
    if len(points) > max_points:
        step = max(1, math.ceil((len(points) - 1) / (max_points - 1)))
        points = points[::step]
        if points[-1] != ring[-1]:
            points.append(ring[-1])
    if len(points) > 1 and points[0] != points[-1]:
        points.append(points[0])
    return [[round(x, 2), round(y, 2)] for x, y in points]


def display_polygons(geometry: MultiPolygon, max_ring_points: int) -> List[List[List[float]]]:
    """Return simplified exterior rings for display and hit-testing."""
    polygons = []
    for polygon in geometry:
        if not polygon:
            continue
        ring = thin_ring(polygon[0], max_ring_points)
        if len(ring) >= 4:
            polygons.append(ring)
    return polygons


def bbox_display_polygon(bbox: BBox) -> List[List[List[float]]]:
    """Return a rectangular display polygon for a feature bbox."""
    minx, maxx, miny, maxy = bbox
    return [
        [
            [round(minx, 2), round(miny, 2)],
            [round(maxx, 2), round(miny, 2)],
            [round(maxx, 2), round(maxy, 2)],
            [round(minx, 2), round(maxy, 2)],
            [round(minx, 2), round(miny, 2)],
        ]
    ]


def build_display_geometries(
    utm_grid: Sequence[GridFeature],
    continents: Sequence[ContinentFeature],
) -> dict:
    """Return lightweight JSON geometry for the Tkinter UTM selector."""
    return {
        "schema_version": 1,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "coordinate_system": "EPSG:3857",
        "bounds": combined_bounds(utm_grid),
        "tiles": {
            grid.token: {
                "bounds": bbox_to_bounds(grid.bbox),
                "polygons": bbox_display_polygon(grid.bbox),
            }
            for grid in sorted(utm_grid, key=lambda feature: feature.token)
        },
        "continents": [
            {
                "name": continent.name,
                "bounds": bbox_to_bounds(continent.bbox),
                "polygons": display_polygons(continent.geometry, max_ring_points=450),
            }
            for continent in sorted(continents, key=lambda feature: feature.name)
        ],
    }


def write_presets(
    utm_grid_path: Path,
    continents_path: Path,
    output_path: Path,
) -> Path:
    """Build and write continent UTM presets."""
    utm_grid = read_grid_features(utm_grid_path)
    continents = read_continent_features(continents_path)
    document = {
        "schema_version": 1,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "method": "GeoPackage WKB polygon intersection using Python standard library.",
        "source": {
            "utm_grid": utm_grid_path.name,
            "continents": continents_path.name,
            "utm_table": "World_UTM_Grid",
            "continent_table": "World_Continents",
        },
        "presets": build_continent_presets(utm_grid, continents),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    return output_path


def write_display_geometries(
    utm_grid_path: Path,
    continents_path: Path,
    output_path: Path,
) -> Path:
    """Build and write lightweight display geometry JSON."""
    utm_grid = read_grid_features(utm_grid_path)
    continents = read_continent_features(continents_path)
    document = build_display_geometries(utm_grid, continents)
    document["source"] = {
        "utm_grid": utm_grid_path.name,
        "continents": continents_path.name,
        "utm_table": "World_UTM_Grid",
        "continent_table": "World_Continents",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, separators=(",", ":"))
        handle.write("\n")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build continent-to-UTM tile presets from GeoPackage sources."
    )
    parser.add_argument("--utm-grid", required=True, type=Path)
    parser.add_argument("--continents", required=True, type=Path)
    parser.add_argument(
        "--output",
        default=Path("spatial_presets") / "continent_utm_tiles.json",
        type=Path,
    )
    parser.add_argument(
        "--display-output",
        default=Path("spatial_presets") / "utm_display_geometries.json",
        type=Path,
    )
    args = parser.parse_args(argv)
    preset_path = write_presets(args.utm_grid, args.continents, args.output)
    display_path = write_display_geometries(
        args.utm_grid,
        args.continents,
        args.display_output,
    )
    print(f"Wrote {preset_path}")
    print(f"Wrote {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
