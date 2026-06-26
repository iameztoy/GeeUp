"""Runtime helpers for compact SWOT PIXC reference tile presets."""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PIXC_REFERENCE_TILES_PATH = PROJECT_ROOT / "spatial_presets" / "swot_pixc_tiles_v1.json.gz"
TILE_NAME_RE = re.compile(r"^(?P<pass>\d{3})_(?P<tile>\d{3})(?P<side>[LR])$")

Point = tuple[float, float]
BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class PixcReferenceTile:
    """One SWOT PIXC reference tile polygon."""

    name: str
    pass_num: int
    tile_num: int
    tile_side: str
    scene_id: str
    bbox: BBox
    ring: tuple[Point, ...]

    @property
    def pixc_tile_id(self) -> str:
        """Return the PIXC filename tile token, such as 164L."""
        return f"{self.tile_num:03d}{self.tile_side}"

    @property
    def crosses_antimeridian(self) -> bool:
        """Return True when the tile ring crosses the antimeridian."""
        raw_lons = [point[0] for point in self.ring]
        has_jump = any(abs(raw_lons[index] - raw_lons[index - 1]) > 180.0 for index in range(1, len(raw_lons)))
        return has_jump or (self.bbox[2] - self.bbox[0]) > 180.0

    def geojson_feature(self, selected: bool = False) -> dict[str, object]:
        """Return this tile as a GeoJSON feature for the browser map."""
        return {
            "type": "Feature",
            "id": self.name,
            "properties": {
                "kind": "reference_tile",
                "name": self.name,
                "pass_num": f"{self.pass_num:03d}",
                "tile_num": f"{self.tile_num:03d}",
                "tile_side": self.tile_side,
                "scene_id": self.scene_id,
                "selected": bool(selected),
                "crosses_antimeridian": self.crosses_antimeridian,
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[list(point) for point in self.ring]],
            },
        }


@dataclass
class PixcReferenceIndex:
    """In-memory index for compact PIXC reference tiles."""

    tiles: list[PixcReferenceTile]
    by_name: dict[str, PixcReferenceTile]

    def get(self, name: str) -> Optional[PixcReferenceTile]:
        """Return one tile by canonical name."""
        return self.by_name.get(normalize_reference_tile_name(name))

    def require_tiles(self, names: Iterable[str]) -> list[PixcReferenceTile]:
        """Return canonical tile objects, raising for unknown names."""
        tiles: list[PixcReferenceTile] = []
        missing: list[str] = []
        seen: set[str] = set()
        for raw_name in names:
            name = normalize_reference_tile_name(raw_name)
            if not name or name in seen:
                continue
            seen.add(name)
            tile = self.by_name.get(name)
            if tile is None:
                missing.append(name)
            else:
                tiles.append(tile)
        if missing:
            raise ValueError(f"Unknown SWOT reference tile(s): {', '.join(missing[:8])}")
        return tiles

    def find_tiles_intersecting_bbox(self, bbox: Sequence[float], limit: Optional[int] = None) -> list[PixcReferenceTile]:
        """Return tiles whose polygons intersect a WGS84 bbox."""
        parsed_bbox = parse_bbox(bbox)
        query_ring = bbox_ring(parsed_bbox)
        matches: list[PixcReferenceTile] = []
        for tile in self.tiles:
            if tile_intersects_ring(tile, query_ring, parsed_bbox):
                matches.append(tile)
                if limit is not None and len(matches) >= limit:
                    break
        return matches

    def find_tiles_intersecting_polygon(
        self,
        points: Sequence[Sequence[float]],
        limit: Optional[int] = None,
    ) -> list[PixcReferenceTile]:
        """Return tiles whose polygons intersect a WGS84 polygon ring."""
        query_ring = parse_ring(points)
        parsed_bbox = ring_bbox(query_ring)
        matches: list[PixcReferenceTile] = []
        for tile in self.tiles:
            if tile_intersects_ring(tile, query_ring, parsed_bbox):
                matches.append(tile)
                if limit is not None and len(matches) >= limit:
                    break
        return matches

    def features_for_tiles(
        self,
        names: Iterable[str],
        selected_names: Iterable[str] = (),
    ) -> list[dict[str, object]]:
        """Return GeoJSON features for named tiles."""
        selected = {normalize_reference_tile_name(name) for name in selected_names}
        return [tile.geojson_feature(tile.name in selected) for tile in self.require_tiles(names)]


def normalize_reference_tile_name(value: str) -> str:
    """Normalize a SWOT reference tile name such as 001_164L."""
    text = str(value or "").strip().upper()
    match = TILE_NAME_RE.match(text)
    if not match:
        return ""
    return f"{int(match.group('pass')):03d}_{int(match.group('tile')):03d}{match.group('side')}"


def normalize_reference_tile_names(value: object) -> list[str]:
    """Normalize comma/semicolon/whitespace separated reference tile names."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = re.split(r"[\s,;]+", value.strip())
    else:
        raw_values = [str(item) for item in value]  # type: ignore[iteration-over-optional]
    names: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        name = normalize_reference_tile_name(raw_value)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def parse_bbox(value: Sequence[float]) -> BBox:
    """Parse west, south, east, north bbox text/numbers."""
    if len(value) != 4:
        raise ValueError("Reference tile bbox must contain west, south, east, north.")
    west, south, east, north = [float(item) for item in value]
    if not -180.0 <= west <= 180.0 or not -180.0 <= east <= 180.0:
        raise ValueError("Reference tile bbox longitudes must be between -180 and 180.")
    if not -90.0 <= south <= 90.0 or not -90.0 <= north <= 90.0:
        raise ValueError("Reference tile bbox latitudes must be between -90 and 90.")
    if west >= east:
        raise ValueError("Reference tile bbox west must be smaller than east.")
    if south >= north:
        raise ValueError("Reference tile bbox south must be smaller than north.")
    return west, south, east, north


def parse_ring(points: Sequence[Sequence[float]]) -> tuple[Point, ...]:
    """Parse a polygon ring and close it if needed."""
    ring = tuple((float(point[0]), float(point[1])) for point in points if len(point) >= 2)
    if len(ring) < 3:
        raise ValueError("Reference tile polygon must contain at least three points.")
    if ring[0] != ring[-1]:
        ring = (*ring, ring[0])
    return ring


def bbox_ring(bbox: BBox) -> tuple[Point, ...]:
    """Return a closed polygon ring for one bbox."""
    west, south, east, north = bbox
    return ((west, south), (east, south), (east, north), (west, north), (west, south))


def ring_bbox(ring: Sequence[Point]) -> BBox:
    """Return west, south, east, north for a ring in its current longitude domain."""
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return min(xs), min(ys), max(xs), max(ys)


def interval_overlaps(a_min: float, a_max: float, b_min: float, b_max: float) -> bool:
    """Return True when closed intervals overlap."""
    return not (a_max < b_min or a_min > b_max)


def unwrapped_ring(ring: Sequence[Point]) -> tuple[Point, ...]:
    """Return ring longitudes unwrapped to avoid antimeridian jumps."""
    if not ring:
        return ()
    points: list[Point] = [ring[0]]
    previous_lon = ring[0][0]
    for lon, lat in ring[1:]:
        adjusted_lon = lon
        while adjusted_lon - previous_lon > 180.0:
            adjusted_lon -= 360.0
        while previous_lon - adjusted_lon > 180.0:
            adjusted_lon += 360.0
        points.append((adjusted_lon, lat))
        previous_lon = adjusted_lon
    return tuple(points)


def unwrapped_lon_width(ring: Sequence[Point]) -> float:
    """Return the longitude width of an unwrapped ring."""
    unwrapped = unwrapped_ring(ring)
    if not unwrapped:
        return 0.0
    xs = [point[0] for point in unwrapped]
    return max(xs) - min(xs)


def shifted_rings_for_query(tile: PixcReferenceTile, query_bbox: BBox) -> list[tuple[Point, ...]]:
    """Return tile ring variants that may overlap a query longitude domain."""
    query_min, _south, query_max, _north = query_bbox
    base = unwrapped_ring(tile.ring)
    if not base:
        return []
    variants: list[tuple[Point, ...]] = []
    for shift in (-360.0, 0.0, 360.0):
        shifted = tuple((lon + shift, lat) for lon, lat in base)
        west, _south2, east, _north2 = ring_bbox(shifted)
        if interval_overlaps(west, east, query_min, query_max):
            variants.append(shifted)
    return variants


def point_in_ring(point: Point, ring: Sequence[Point]) -> bool:
    """Return True when point is inside a closed ring."""
    x, y = point
    inside = False
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


def orientation(a: Point, b: Point, c: Point) -> float:
    """Return cross-product orientation for three points."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def on_segment(a: Point, b: Point, c: Point) -> bool:
    """Return True when b lies on segment ac."""
    return min(a[0], c[0]) <= b[0] <= max(a[0], c[0]) and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])


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


def ring_segments(ring: Sequence[Point]) -> Iterable[tuple[Point, Point]]:
    """Yield line segments from a closed ring."""
    for index in range(1, len(ring)):
        yield ring[index - 1], ring[index]


def rings_intersect(a: Sequence[Point], b: Sequence[Point]) -> bool:
    """Return True when two simple polygon exterior rings intersect."""
    if not a or not b:
        return False
    if any(point_in_ring(point, b) for point in a):
        return True
    if any(point_in_ring(point, a) for point in b):
        return True
    b_segments = list(ring_segments(b))
    for a1, a2 in ring_segments(a):
        for b1, b2 in b_segments:
            if segments_intersect(a1, a2, b1, b2):
                return True
    return False


def tile_intersects_ring(tile: PixcReferenceTile, query_ring: Sequence[Point], query_bbox: BBox) -> bool:
    """Return True when one tile polygon intersects a query ring."""
    _west, south, _east, north = query_bbox
    if not interval_overlaps(tile.bbox[1], tile.bbox[3], south, north):
        return False
    for shifted_ring in shifted_rings_for_query(tile, query_bbox):
        shifted_bbox = ring_bbox(shifted_ring)
        if not interval_overlaps(shifted_bbox[1], shifted_bbox[3], south, north):
            continue
        if rings_intersect(shifted_ring, query_ring):
            return True
    return False


def raw_tile_to_dataclass(raw: Mapping[str, object]) -> PixcReferenceTile:
    """Parse one compact JSON tile record."""
    name = normalize_reference_tile_name(str(raw.get("n") or raw.get("name") or ""))
    if not name:
        raise ValueError("Invalid PIXC reference tile name.")
    ring_value = raw.get("g") or raw.get("ring") or []
    ring = parse_ring(ring_value)  # type: ignore[arg-type]
    bbox_value = raw.get("b") or raw.get("bbox") or ring_bbox(ring)
    bbox = tuple(float(item) for item in bbox_value)  # type: ignore[iteration-over-optional]
    if len(bbox) != 4:
        raise ValueError(f"Invalid bbox for PIXC reference tile {name}.")
    return PixcReferenceTile(
        name=name,
        pass_num=int(raw.get("p") or raw.get("pass_num") or name[:3]),
        tile_num=int(raw.get("t") or raw.get("tile_num") or name[4:7]),
        tile_side=str(raw.get("s") or raw.get("tile_side") or name[-1]).upper(),
        scene_id=str(raw.get("scene") or raw.get("scene_id") or ""),
        bbox=bbox,  # type: ignore[arg-type]
        ring=ring,
    )


@lru_cache(maxsize=4)
def load_pixc_reference_tiles(path: str | Path = DEFAULT_PIXC_REFERENCE_TILES_PATH) -> PixcReferenceIndex:
    """Load the compact PIXC reference tile cache."""
    cache_path = Path(path)
    if not cache_path.exists():
        raise FileNotFoundError(
            f"Missing PIXC reference tile cache: {cache_path}. "
            "Generate it with build_pixc_reference_tiles.py."
        )
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    if document.get("schema") != "swot_pixc_tiles_v1":
        raise ValueError(f"Unsupported PIXC reference cache schema: {document.get('schema')}")
    tiles = [raw_tile_to_dataclass(raw_tile) for raw_tile in document.get("tiles", [])]
    by_name = {tile.name: tile for tile in tiles}
    return PixcReferenceIndex(tiles=tiles, by_name=by_name)


def find_tiles_intersecting_bbox(
    bbox: Sequence[float],
    *,
    limit: Optional[int] = None,
    path: str | Path = DEFAULT_PIXC_REFERENCE_TILES_PATH,
) -> list[PixcReferenceTile]:
    """Return reference tiles intersecting a WGS84 bbox."""
    return load_pixc_reference_tiles(path).find_tiles_intersecting_bbox(bbox, limit=limit)


def find_tiles_intersecting_polygon(
    points: Sequence[Sequence[float]],
    *,
    limit: Optional[int] = None,
    path: str | Path = DEFAULT_PIXC_REFERENCE_TILES_PATH,
) -> list[PixcReferenceTile]:
    """Return reference tiles intersecting a WGS84 polygon."""
    return load_pixc_reference_tiles(path).find_tiles_intersecting_polygon(points, limit=limit)


def selected_reference_tiles_to_track_filters(tile_names: Iterable[str], cycle: int | str | None = None) -> list[object]:
    """Convert selected reference tiles into grouped PIXC track filters."""
    if cycle in (None, ""):
        return []
    cycle_id = int(cycle)
    from .download import PixcTileId, PixcTrackFilter

    index = load_pixc_reference_tiles()
    grouped: dict[int, list[PixcTileId]] = {}
    for tile in index.require_tiles(tile_names):
        grouped.setdefault(tile.pass_num, []).append(PixcTileId(tile.tile_num, tile.tile_side))
    return [
        PixcTrackFilter(cycle_id=cycle_id, pass_id=pass_num, tile_ids=tuple(tile_ids))
        for pass_num, tile_ids in sorted(grouped.items())
    ]
