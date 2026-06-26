"""Build compact SWOT PIXC reference tile presets from the official shapefiles.

The PIXC GUI consumes generated JSON only. This utility is an offline helper so
runtime code does not need GDAL, geopandas, Fiona, shapely, or pyproj.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import struct
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_TILES_OUTPUT = PROJECT_ROOT / "spatial_presets" / "swot_pixc_tiles_v1.json.gz"
DEFAULT_PASSES_OUTPUT = PROJECT_ROOT / "spatial_presets" / "swot_pixc_passes_v1.json.gz"
TILE_NAME_RE = re.compile(r"^(?P<pass>\d{3})_(?P<tile>\d{3})(?P<side>[LR])$")
SNIPPET_PAIR_RE = re.compile(r"(?P<key>[a-zA-Z_]+):(?P<value>[^,\s]+)")


Point = tuple[float, float]
BBox = tuple[float, float, float, float]


@dataclass
class ShpRecord:
    """One geometry record from an ESRI shapefile."""

    record_number: int
    shape_type: int
    bbox: BBox
    parts: list[list[Point]]


def rounded(value: float) -> float:
    """Round coordinates compactly while preserving sub-meter detail."""
    return round(float(value), 6)


def shp_records(data: bytes) -> Iterator[ShpRecord]:
    """Yield polyline/polygon records from SHP bytes."""
    position = 100
    while position + 8 <= len(data):
        record_number, content_words = struct.unpack(">2i", data[position : position + 8])
        position += 8
        content = data[position : position + content_words * 2]
        position += content_words * 2
        if len(content) < 44:
            continue
        shape_type = struct.unpack("<i", content[:4])[0]
        bbox = tuple(rounded(value) for value in struct.unpack("<4d", content[4:36]))
        part_count, point_count = struct.unpack("<2i", content[36:44])
        if shape_type not in {3, 5, 13, 15, 23, 25} or part_count <= 0 or point_count <= 0:
            continue
        part_offsets = list(struct.unpack(f"<{part_count}i", content[44 : 44 + 4 * part_count]))
        part_offsets.append(point_count)
        points_offset = 44 + 4 * part_count
        parts: list[list[Point]] = []
        for part_index in range(part_count):
            start = part_offsets[part_index]
            end = part_offsets[part_index + 1]
            part: list[Point] = []
            for point_index in range(start, end):
                raw_offset = points_offset + point_index * 16
                if raw_offset + 16 > len(content):
                    break
                x, y = struct.unpack("<2d", content[raw_offset : raw_offset + 16])
                part.append((rounded(x), rounded(y)))
            if part:
                parts.append(part)
        yield ShpRecord(
            record_number=record_number,
            shape_type=shape_type,
            bbox=bbox,  # type: ignore[arg-type]
            parts=parts,
        )


def dbf_fields(data: bytes) -> tuple[int, int, int, list[tuple[str, int, int]]]:
    """Return DBF record layout."""
    record_count = struct.unpack("<I", data[4:8])[0]
    header_length = struct.unpack("<H", data[8:10])[0]
    record_length = struct.unpack("<H", data[10:12])[0]
    fields: list[tuple[str, int, int]] = []
    offset = 32
    field_start = 1
    while offset < header_length and data[offset] != 0x0D:
        descriptor = data[offset : offset + 32]
        name = descriptor[:11].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        length = int(descriptor[16])
        fields.append((name, field_start, length))
        field_start += length
        offset += 32
    return record_count, header_length, record_length, fields


def dbf_rows(data: bytes, wanted_fields: Sequence[str], encoding: str = "utf-8") -> Iterator[dict[str, str]]:
    """Yield selected DBF fields as strings."""
    record_count, header_length, record_length, fields = dbf_fields(data)
    wanted = set(wanted_fields)
    selected = [(name, start, length) for name, start, length in fields if name in wanted]
    for index in range(record_count):
        record = data[header_length + index * record_length : header_length + (index + 1) * record_length]
        if not record or record[:1] == b"*":
            continue
        row: dict[str, str] = {}
        for name, start, length in selected:
            value = record[start : start + length].decode(encoding, errors="replace").strip()
            row[name] = value
        yield row


def parse_snippet(value: str) -> dict[str, str]:
    """Parse ArcGIS/KMZ snippet key-value text."""
    return {match.group("key"): match.group("value") for match in SNIPPET_PAIR_RE.finditer(value or "")}


def read_zip_text(zf: zipfile.ZipFile, name: str, fallback: str = "") -> str:
    """Read a short text member from a zip."""
    try:
        return zf.read(name).decode("utf-8", errors="replace")
    except KeyError:
        return fallback


def tile_document(zip_path: Path) -> dict[str, object]:
    """Build the compact PIXC reference tile document."""
    with zipfile.ZipFile(zip_path) as zf:
        rows = list(dbf_rows(zf.read("Tiles_poly.dbf"), ("Name", "Snippet")))
        geometries = list(shp_records(zf.read("Tiles_poly.shp")))
        prj = read_zip_text(zf, "Tiles_poly.prj")

    tiles: list[dict[str, object]] = []
    for row, geometry in zip(rows, geometries):
        name = row.get("Name", "")
        match = TILE_NAME_RE.match(name)
        if match is None or not geometry.parts:
            continue
        snippet = parse_snippet(row.get("Snippet", ""))
        ring = geometry.parts[0]
        tiles.append(
            {
                "n": name,
                "p": int(match.group("pass")),
                "t": int(match.group("tile")),
                "s": match.group("side"),
                "scene": snippet.get("scene_id", ""),
                "b": list(geometry.bbox),
                "g": [[x, y] for x, y in ring],
            }
        )

    return {
        "schema": "swot_pixc_tiles_v1",
        "source_zip": zip_path.name,
        "source_layer": "Tiles_poly",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "crs": "EPSG:4326",
        "prj": prj,
        "tile_count": len(tiles),
        "tiles": tiles,
    }


def pass_document(zip_path: Path) -> dict[str, object]:
    """Build a compact optional pass polyline document."""
    with zipfile.ZipFile(zip_path) as zf:
        rows = list(dbf_rows(zf.read("Passes_polL.dbf"), ("Name", "Snippet")))
        geometries = list(shp_records(zf.read("Passes_polL.shp")))
        prj = read_zip_text(zf, "Passes_polL.prj")

    passes: list[dict[str, object]] = []
    for row, geometry in zip(rows, geometries):
        name = row.get("Name", "")
        match = re.search(r"(\d{3})", name)
        if match is None or not geometry.parts:
            continue
        passes.append(
            {
                "n": name,
                "p": int(match.group(1)),
                "snippet": row.get("Snippet", ""),
                "b": list(geometry.bbox),
                "g": [[x, y] for x, y in geometry.parts[0]],
            }
        )

    return {
        "schema": "swot_pixc_passes_v1",
        "source_zip": zip_path.name,
        "source_layer": "Passes_polL",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "crs": "EPSG:4326",
        "prj": prj,
        "pass_count": len(passes),
        "passes": passes,
    }


def write_json_gz(path: Path, document: dict[str, object]) -> Path:
    """Write compact JSON as gzip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=9) as handle:
        handle.write(payload)
    return path


def build_reference_files(
    zip_path: Path,
    tiles_output: Path = DEFAULT_TILES_OUTPUT,
    passes_output: Path | None = DEFAULT_PASSES_OUTPUT,
) -> tuple[Path, Path | None]:
    """Build compact reference files and return their paths."""
    tiles_path = write_json_gz(tiles_output, tile_document(zip_path))
    passes_path = write_json_gz(passes_output, pass_document(zip_path)) if passes_output is not None else None
    return tiles_path, passes_path


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("refs_zip", type=Path, help="Path to refs.zip containing Tiles_poly and Passes_polL shapefiles.")
    parser.add_argument("--tiles-output", type=Path, default=DEFAULT_TILES_OUTPUT)
    parser.add_argument("--passes-output", type=Path, default=DEFAULT_PASSES_OUTPUT)
    parser.add_argument("--skip-passes", action="store_true", help="Only generate the tile polygon cache.")
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint."""
    args = parse_args()
    passes_output = None if args.skip_passes else args.passes_output
    tiles_path, passes_path = build_reference_files(args.refs_zip, args.tiles_output, passes_output)
    print(f"Wrote PIXC tile reference cache: {tiles_path}")
    if passes_path is not None:
        print(f"Wrote PIXC pass reference cache: {passes_path}")


if __name__ == "__main__":
    main()
