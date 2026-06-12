"""Project statistics and conservative cleanup planning for SWOTFlow."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from project_database import (
    ProjectDatabase,
    database_path_from_config,
    dataset_for_path,
    read_project_rows,
)
from swot_metadata import SWOT_L2_HR_RASTER_PATTERN, swot_product_rank


COMPLETED_EXTRACT_STATUSES = {
    "written",
    "rewritten_invalid_existing",
    "skipped_existing",
    "skipped_manifest",
    "local_complete",
}
COMPLETED_MOSAIC_STATUSES = {
    "MOSAIC_CREATED",
    "MOSAIC_CREATED_WITH_EXCLUSIONS",
    "COPIED_SINGLETON",
    "SKIPPED_EXISTS",
    "SKIPPED_MANIFEST",
}
QA_EXCLUDED_MOSAIC_STATUSES = {
    "SKIPPED_INCOMPATIBLE",
}
UPLOAD_CLEANUP_STATUSES = {
    "COMPLETED",
    "SKIPPED_ALREADY_EXISTS",
    "EE_VERIFIED_EXISTS",
}
EE_VERIFIED_STATUS = "EE_VERIFIED_EXISTS"
UTM_TILE_TOKEN_RE = re.compile(r"^UTM(?P<zone>\d{1,2})(?P<band>[C-HJ-NP-X])$")
LINEAGE_SNAPSHOT_LIMIT = 1000


@lru_cache(maxsize=None)
def cached_swot_filename_fields(file_name: str) -> Dict[str, str] | None:
    """Parse filename fields without converting timestamps for statistics."""
    stem = Path(str(file_name or "")).stem
    match = SWOT_L2_HR_RASTER_PATTERN.match(stem)
    if match is None:
        return None
    fields = {key: str(value or "") for key, value in match.groupdict().items()}
    descriptor_parts = fields["descriptor"].split("_")
    fields["grid_resolution"] = descriptor_parts[0] if descriptor_parts else ""
    fields["coordinate_system"] = descriptor_parts[1] if len(descriptor_parts) > 1 else ""
    fields["granule_overlap"] = descriptor_parts[2] if len(descriptor_parts) > 2 else ""
    return fields


@dataclass
class CleanupCandidate:
    """One local intermediate file that can be deleted after downstream proof."""

    stage: str
    path: Path
    reason: str
    protected_by: str
    size_bytes: int = 0


@dataclass
class ProjectInsights:
    """Computed project statistics for GUI display."""

    metrics: "OrderedDict[str, str]"
    stage_status_counts: List[tuple[str, str, int]]
    tile_counts: List[tuple[str, int]]
    date_counts: List[tuple[str, int]]
    processing_level_counts: List[tuple[str, int, int, int, int, int, int]]
    processing_level_tile_counts: List[tuple[str, str, int, int, int, int]]
    mosaic_output_grid_counts: List[tuple[str, int]]
    mosaic_source_tile_counts: List[tuple[str, int]]
    upload_status_counts: List[tuple[str, int]]
    uploaded_tile_counts: List[tuple[str, int]]
    uploaded_date_counts: List[tuple[str, int]]
    uploaded_processing_level_counts: List[tuple[str, int]]
    uploaded_grid_counts: List[tuple[str, int]]
    upload_error_counts: List[tuple[str, str, int]]
    upload_qa_tile_rows: List[tuple[str, int, int, int, int, int]]
    ready_not_uploaded_rows: List[tuple[str, str, str, str]]
    mosaic_exclusion_rows: List[tuple[str, str, str, str, str]]
    mosaic_lineage_rows: List[Dict[str, str]]
    cleanup_candidates: List[CleanupCandidate]


def statistics_folder(config: Mapping[str, Any]) -> Path:
    """Return the project statistics output folder."""
    return processing_path(config, "logs") / "statistics"


def statistics_snapshot_path(config: Mapping[str, Any]) -> Path:
    """Return the JSON snapshot path for the latest project statistics."""
    return statistics_folder(config) / "project_statistics_snapshot.json"


def read_csv_rows(path: str | Path) -> List[Dict[str, str]]:
    """Read project rows from SQLite, falling back to unregistered CSV files."""
    csv_path = Path(path)
    if dataset_for_path(csv_path):
        return read_project_rows(csv_path)
    if not csv_path.exists() or not csv_path.is_file():
        return []
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def write_csv_rows(path: str | Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    """Write simple CSV rows, creating parent folders as needed."""
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def config_path(config: Mapping[str, Any], section: str, key: str, default: str = "") -> Path:
    """Return a path value from a config section."""
    data = config.get(section, {})
    if not isinstance(data, Mapping):
        data = {}
    return Path(str(data.get(key) or default))


def processing_path(config: Mapping[str, Any], key: str, default: str = "") -> Path:
    """Return a processing path from config."""
    data = config.get("processing", {})
    if not isinstance(data, Mapping):
        data = {}
    return Path(str(data.get(key) or default))


def file_count(folder: Path, patterns: Sequence[str], recursive: bool = False) -> int:
    """Count files matching one or more glob patterns."""
    if not folder.exists() or not folder.is_dir():
        return 0
    count = 0
    for pattern in patterns:
        iterator = folder.rglob(pattern) if recursive else folder.glob(pattern)
        count += sum(1 for path in iterator if path.is_file())
    return count


def folder_size(folder: Path, patterns: Sequence[str], recursive: bool = False) -> int:
    """Return total bytes for files matching glob patterns."""
    if not folder.exists() or not folder.is_dir():
        return 0
    total = 0
    for pattern in patterns:
        iterator = folder.rglob(pattern) if recursive else folder.glob(pattern)
        for path in iterator:
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    return total


def format_bytes(value: int) -> str:
    """Return a compact byte-size string."""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def compact_count_keys(counter: Counter[str], limit: int = 8) -> str:
    """Return a compact comma-separated summary of counted keys."""
    if not counter:
        return ""
    parts = [f"{key} ({count})" for key, count in counter.most_common(limit)]
    remaining = len(counter) - len(parts)
    if remaining > 0:
        parts.append(f"+{remaining} more")
    return ", ".join(parts)


def parse_json_list(value: str) -> List[str]:
    """Parse a JSON list stored in a CSV cell."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if item]


def parse_path_list(value: str) -> List[str]:
    """Parse a JSON list or conservative delimited path list from a CSV cell."""
    parsed = parse_json_list(value)
    if parsed:
        return parsed
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[;|]", text) if part.strip()]


def date_from_text(value: str) -> str:
    """Extract YYYY-MM-DD from ISO-like or SWOT timestamp text."""
    text = str(value or "")
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    compact = re.search(r"(\d{4})(\d{2})(\d{2})T?", text)
    if compact:
        return f"{compact.group(1)}-{compact.group(2)}-{compact.group(3)}"
    return ""


def normalize_utm_tile_token(value: str) -> str:
    """Normalize one UTM/MGRS tile token, returning blank when it is not a tile."""
    match = UTM_TILE_TOKEN_RE.match(str(value or "").strip().upper())
    if match is None:
        return ""
    return f"UTM{int(match.group('zone')):02d}{match.group('band')}"


def processing_level_label(crid: str, product_counter: str | int | None) -> str:
    """Return a future-proof CRID/product-counter processing-level label."""
    crid_text = str(crid or "").strip().upper()
    counter_text = str(product_counter or "").strip()
    if crid_text and counter_text:
        return f"{crid_text}_{counter_text}"
    if crid_text:
        return crid_text
    return "UNKNOWN"


def parsed_processing_level(file_name: str | Path) -> str:
    """Return the processing level parsed from a SWOT filename."""
    fields = cached_swot_filename_fields(str(file_name))
    if fields is None:
        return ""
    return processing_level_label(
        fields.get("crid", ""),
        fields.get("product_counter", ""),
    )


def row_processing_level(
    row: Mapping[str, str],
    *,
    file_keys: Sequence[str],
    crid_keys: Sequence[str] = ("crid", "preferred_crid", "dominant_crid"),
    counter_keys: Sequence[str] = ("counter", "product_counter"),
) -> str:
    """Return a processing-level label from explicit columns or filename parsing."""
    crid = next((str(row.get(key, "") or "").strip() for key in crid_keys if row.get(key)), "")
    counter = next((str(row.get(key, "") or "").strip() for key in counter_keys if row.get(key)), "")
    if crid:
        return processing_level_label(crid, counter)
    for key in file_keys:
        level = parsed_processing_level(row.get(key, ""))
        if level:
            return level
    return "UNKNOWN"


def file_utm_tile(file_name: str | Path) -> str:
    """Return the UTM tile parsed from a SWOT filename."""
    fields = cached_swot_filename_fields(str(file_name))
    if fields is None:
        return ""
    return normalize_utm_tile_token(fields.get("coordinate_system", ""))


def level_sort_key(level: str) -> tuple[tuple[int, int, int, int, int], str]:
    """Sort known CRID/product-counter levels by SWOT rank, unknowns last."""
    text = str(level or "")
    if "_" not in text:
        return ((-1, -1, -1, -1, -1), text)
    crid, counter = text.rsplit("_", 1)
    return (swot_product_rank(crid, counter), text)


def mosaic_source_tile_levels(row: Mapping[str, str]) -> List[tuple[str, str]]:
    """Return source UTM tile and processing-level pairs for one mosaic row."""
    pairs: set[tuple[str, str]] = set()
    for file_name in parse_path_list(row.get("input_files", "")):
        tile = file_utm_tile(file_name)
        level = parsed_processing_level(file_name)
        if tile or level:
            pairs.add((tile or "UNKNOWN", level or "UNKNOWN"))
    return sorted(pairs)


def mosaic_output_grid(row: Mapping[str, str]) -> str:
    """Return the output CRS/grid token recorded for one mosaic row."""
    grid = str(row.get("coordinate_system", "") or "").strip().upper()
    if grid:
        return grid
    fields = cached_swot_filename_fields(str(row.get("output_file", "")))
    if fields is None:
        return ""
    return str(fields.get("coordinate_system", "") or "").strip().upper()


def mosaic_source_tiles(row: Mapping[str, str]) -> List[str]:
    """Return unique source UTM tiles contributing to one mosaic row."""
    tiles: set[str] = set()
    for file_name in parse_path_list(row.get("input_files", "")):
        fields = cached_swot_filename_fields(str(file_name))
        if fields is None:
            continue
        tile = normalize_utm_tile_token(fields.get("coordinate_system", ""))
        if tile:
            tiles.add(tile)
    return sorted(tiles)


def mosaic_excluded_sources(row: Mapping[str, str]) -> List[tuple[str, str]]:
    """Return excluded mosaic source paths and reasons from one report row."""
    files = parse_path_list(row.get("excluded_input_files", ""))
    reasons = parse_json_list(row.get("excluded_reasons", ""))
    return [
        (file_name, reasons[index] if index < len(reasons) else "")
        for index, file_name in enumerate(files)
        if file_name
    ]


def lookup_mapping_value(
    lookup: Mapping[str, Mapping[str, str]],
    path_text: str | Path,
) -> Mapping[str, str]:
    """Look up one row by resolved path, basename, or raw text."""
    for key in path_lookup_keys(path_text):
        row = lookup.get(key)
        if row:
            return row
    return {}


def add_path_row_lookup(
    lookup: Dict[str, Mapping[str, str]],
    row: Mapping[str, str],
    *path_values: str | Path,
) -> None:
    """Index one CSV row by one or more path-like values."""
    for value in path_values:
        text = str(value or "").strip()
        if not text:
            continue
        for key in path_lookup_keys(text):
            lookup.setdefault(key, row)


def row_date_text(row: Mapping[str, str], *keys: str) -> str:
    """Return the first parseable date from a row and key list."""
    for key in keys:
        date_text = date_from_text(row.get(key, ""))
        if date_text:
            return date_text
    return ""


def row_utm_tile(row: Mapping[str, str], *file_keys: str) -> str:
    """Return a UTM tile from explicit columns or filename parsing."""
    explicit = normalize_utm_tile_token(row.get("utm_tile", ""))
    if explicit:
        return explicit
    zone = str(row.get("utm_zone", "") or "").strip()
    band = str(row.get("mgrs_band", "") or "").strip()
    if zone and band:
        token = normalize_utm_tile_token(f"UTM{zone}{band}")
        if token:
            return token
    for key in file_keys:
        tile = file_utm_tile(row.get(key, ""))
        if tile:
            return tile
    return ""


def lineage_output_exists(path_text: str) -> str:
    """Return yes/no for a lineage output path without raising on bad paths."""
    if not path_text:
        return ""
    try:
        return "yes" if Path(path_text).exists() else "no"
    except OSError:
        return "no"


def joined_unique(values: Iterable[str]) -> str:
    """Return a stable pipe-separated unique string."""
    seen: set[str] = set()
    output: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return " | ".join(output)


def build_mosaic_lineage_rows(
    download_rows: Sequence[Mapping[str, str]],
    extract_rows: Sequence[Mapping[str, str]],
    extract_error_rows: Sequence[Mapping[str, str]],
    mosaic_rows: Sequence[Mapping[str, str]],
) -> List[Dict[str, str]]:
    """Build a per-source audit table across download, extraction, and mosaic stages."""
    download_lookup: Dict[str, Mapping[str, str]] = {}
    for row in download_rows:
        add_path_row_lookup(
            download_lookup,
            row,
            row.get("local_path", ""),
            row.get("file_name", ""),
        )

    used_by_source: Dict[str, List[Mapping[str, str]]] = {}
    excluded_by_source: Dict[str, List[tuple[Mapping[str, str], str]]] = {}
    skipped_by_source: Dict[str, List[Mapping[str, str]]] = {}

    for row in mosaic_rows:
        status = str(row.get("status", "") or "")
        is_completed = status in COMPLETED_MOSAIC_STATUSES and row.get("stale", "false") != "true"
        target = used_by_source if is_completed else skipped_by_source
        for source in parse_path_list(row.get("input_files", "")):
            for key in path_lookup_keys(source):
                target.setdefault(key, []).append(row)
        if is_completed:
            for source, reason in mosaic_excluded_sources(row):
                for key in path_lookup_keys(source):
                    excluded_by_source.setdefault(key, []).append((row, reason))

    rows: List[Dict[str, str]] = []
    seen_raw_keys: set[str] = set()
    seen_extracted_keys: set[str] = set()

    def add_seen_raw(path_text: str) -> None:
        for key in path_lookup_keys(path_text):
            seen_raw_keys.add(key)

    def base_lineage_row(
        *,
        raw_file: str,
        extracted_file: str = "",
        download_row: Mapping[str, str] | None = None,
        extract_row: Mapping[str, str] | None = None,
    ) -> Dict[str, str]:
        download_row = download_row or {}
        extract_row = extract_row or {}
        return {
            "lineage_status": "",
            "utm_tile": (
                row_utm_tile(download_row, "file_name", "local_path")
                or row_utm_tile(extract_row, "input_nc", "output_tif")
            ),
            "date": (
                row_date_text(download_row, "start_time", "file_name", "local_path")
                or row_date_text(extract_row, "date", "start", "input_nc", "output_tif")
            ),
            "processing_level": (
                row_processing_level(download_row, file_keys=("file_name", "local_path"))
                if download_row
                else row_processing_level(extract_row, file_keys=("input_nc", "output_tif"))
            ),
            "raw_file": raw_file,
            "download_status": str(download_row.get("last_status", "") or ""),
            "downloaded": str(download_row.get("downloaded", "") or ""),
            "raw_exists": str(download_row.get("raw_exists", "") or ""),
            "extracted_file": extracted_file,
            "extract_status": str(extract_row.get("status", "") or ""),
            "extracted_exists": str(extract_row.get("output_exists", "") or lineage_output_exists(extracted_file)),
            "mosaic_outputs": "",
            "mosaic_statuses": "",
            "mosaic_output_exists": "",
            "mosaic_excluded": "no",
            "mosaic_exclusion_reason": "",
            "message": "",
        }

    for extract_row in extract_rows:
        raw_file = str(extract_row.get("input_nc", "") or "")
        extracted_file = str(extract_row.get("output_tif", "") or "")
        download_row = lookup_mapping_value(download_lookup, raw_file)
        if raw_file:
            add_seen_raw(raw_file)
        for key in path_lookup_keys(extracted_file):
            seen_extracted_keys.add(key)
        source_keys = path_lookup_keys(extracted_file)
        used_rows = [row for key in source_keys for row in used_by_source.get(key, [])]
        excluded_items = [item for key in source_keys for item in excluded_by_source.get(key, [])]
        skipped_rows = [row for key in source_keys for row in skipped_by_source.get(key, [])]
        row = base_lineage_row(
            raw_file=raw_file,
            extracted_file=extracted_file,
            download_row=download_row,
            extract_row=extract_row,
        )
        if used_rows:
            row["lineage_status"] = "used_in_mosaic"
            row["mosaic_outputs"] = joined_unique(item.get("output_file", "") for item in used_rows)
            row["mosaic_statuses"] = joined_unique(item.get("status", "") for item in used_rows)
            row["mosaic_output_exists"] = joined_unique(
                item.get("output_exists", "") or lineage_output_exists(item.get("output_file", ""))
                for item in used_rows
            )
            row["message"] = joined_unique(item.get("message", "") for item in used_rows)
        elif excluded_items:
            row["lineage_status"] = "excluded_from_partial_mosaic"
            row["mosaic_excluded"] = "yes"
            row["mosaic_outputs"] = joined_unique(item[0].get("output_file", "") for item in excluded_items)
            row["mosaic_statuses"] = joined_unique(item[0].get("status", "") for item in excluded_items)
            row["mosaic_output_exists"] = joined_unique(
                item[0].get("output_exists", "") or lineage_output_exists(item[0].get("output_file", ""))
                for item in excluded_items
            )
            row["mosaic_exclusion_reason"] = joined_unique(reason for _source, reason in excluded_items)
            row["message"] = joined_unique(item[0].get("message", "") for item in excluded_items)
        elif skipped_rows:
            row["lineage_status"] = "mosaic_group_skipped"
            row["mosaic_outputs"] = joined_unique(item.get("output_file", "") for item in skipped_rows)
            row["mosaic_statuses"] = joined_unique(item.get("status", "") for item in skipped_rows)
            row["mosaic_output_exists"] = joined_unique(
                item.get("output_exists", "") or lineage_output_exists(item.get("output_file", ""))
                for item in skipped_rows
            )
            row["message"] = joined_unique(item.get("message", "") for item in skipped_rows)
        elif str(extract_row.get("status", "") or "").lower() in COMPLETED_EXTRACT_STATUSES:
            row["lineage_status"] = "extracted_not_mosaicked"
        else:
            row["lineage_status"] = "extraction_not_complete"
        rows.append(row)

    for error_row in extract_error_rows:
        raw_file = str(error_row.get("input_nc", "") or "")
        if not raw_file:
            continue
        download_row = lookup_mapping_value(download_lookup, raw_file)
        add_seen_raw(raw_file)
        row = base_lineage_row(
            raw_file=raw_file,
            download_row=download_row,
            extract_row=error_row,
        )
        row["lineage_status"] = "extraction_failed"
        row["extract_status"] = "error"
        row["message"] = str(error_row.get("error", "") or "")
        rows.append(row)

    for mosaic_row in mosaic_rows:
        status = str(mosaic_row.get("status", "") or "")
        is_completed = status in COMPLETED_MOSAIC_STATUSES and mosaic_row.get("stale", "false") != "true"
        output_file = str(mosaic_row.get("output_file", "") or "")
        output_exists = str(mosaic_row.get("output_exists", "") or lineage_output_exists(output_file))
        output_grid = mosaic_output_grid(mosaic_row)
        date_text = row_date_text(mosaic_row, "start_date", "range_beginning", "output_file")
        for source in parse_path_list(mosaic_row.get("input_files", "")):
            if any(key in seen_extracted_keys for key in path_lookup_keys(source)):
                continue
            row = base_lineage_row(raw_file="", extracted_file=source)
            row["utm_tile"] = file_utm_tile(source) or normalize_utm_tile_token(output_grid)
            row["date"] = date_from_text(source) or date_text
            row["processing_level"] = parsed_processing_level(source) or row_processing_level(mosaic_row, file_keys=("output_file",))
            row["extract_status"] = "not_in_extract_manifest"
            row["extracted_exists"] = lineage_output_exists(source)
            row["mosaic_outputs"] = output_file
            row["mosaic_statuses"] = status
            row["mosaic_output_exists"] = output_exists
            row["message"] = str(mosaic_row.get("message", "") or "")
            row["lineage_status"] = "used_in_mosaic" if is_completed else "mosaic_group_skipped"
            rows.append(row)
            for key in path_lookup_keys(source):
                seen_extracted_keys.add(key)
        if is_completed:
            for source, reason in mosaic_excluded_sources(mosaic_row):
                if any(key in seen_extracted_keys for key in path_lookup_keys(source)):
                    continue
                row = base_lineage_row(raw_file="", extracted_file=source)
                row["utm_tile"] = file_utm_tile(source) or normalize_utm_tile_token(output_grid)
                row["date"] = date_from_text(source) or date_text
                row["processing_level"] = parsed_processing_level(source) or row_processing_level(mosaic_row, file_keys=("output_file",))
                row["extract_status"] = "not_in_extract_manifest"
                row["extracted_exists"] = lineage_output_exists(source)
                row["mosaic_outputs"] = output_file
                row["mosaic_statuses"] = status
                row["mosaic_output_exists"] = output_exists
                row["mosaic_excluded"] = "yes"
                row["mosaic_exclusion_reason"] = reason
                row["message"] = str(mosaic_row.get("message", "") or "")
                row["lineage_status"] = "excluded_from_partial_mosaic"
                rows.append(row)
                for key in path_lookup_keys(source):
                    seen_extracted_keys.add(key)

    for download_row in download_rows:
        raw_file = str(download_row.get("local_path", "") or download_row.get("file_name", "") or "")
        if not raw_file or any(key in seen_raw_keys for key in path_lookup_keys(raw_file)):
            continue
        row = base_lineage_row(raw_file=raw_file, download_row=download_row)
        selected = str(download_row.get("selected_for_download", "yes") or "yes").lower()
        status = str(download_row.get("last_status", "") or "").upper()
        if selected == "no" or status == "EXCLUDED_OLDER_VERSION":
            row["lineage_status"] = "remote_excluded_older_version"
        elif str(download_row.get("downloaded", "") or "").lower() == "yes":
            row["lineage_status"] = "downloaded_not_extracted"
        else:
            row["lineage_status"] = "download_not_accounted"
        row["message"] = str(download_row.get("duplicate_reason", "") or "")
        rows.append(row)

    rows.sort(
        key=lambda row: (
            row.get("utm_tile", ""),
            row.get("date", ""),
            row.get("raw_file", ""),
            row.get("extracted_file", ""),
        )
    )
    return rows


def upload_row_source_tiles(
    row: Mapping[str, str],
    mosaic_lookup: Mapping[str, List[str]],
) -> List[str]:
    """Return source UTM tiles for one upload report row."""
    tiles = [normalize_utm_tile_token(tile) for tile in parse_json_list(row.get("source_utm_tiles", ""))]
    tiles = [tile for tile in tiles if tile]
    if tiles:
        return sorted(set(tiles))
    local_file = row.get("local_file", "")
    manifest_tiles = lookup_path_value(mosaic_lookup, local_file)
    if manifest_tiles:
        return sorted(set(manifest_tiles))
    tile = file_utm_tile(local_file)
    return [tile] if tile else []


def output_exists_for_upload(row: Mapping[str, str]) -> bool:
    """Return True when a mosaic row appears to represent a local upload-ready file."""
    output = Path(row.get("output_file", ""))
    if str(row.get("output_exists", "")).lower() == "yes":
        return True
    try:
        return output.exists() and output.is_file()
    except OSError:
        return False


def mosaic_level_lookup(rows: Iterable[Mapping[str, str]]) -> Dict[str, List[tuple[str, str]]]:
    """Return source tile/level pairs keyed by mosaic output path and file name."""
    lookup: Dict[str, List[tuple[str, str]]] = {}
    for row in rows:
        pairs = mosaic_source_tile_levels(row)
        output = str(row.get("output_file", "") or "").strip()
        if not output or not pairs:
            continue
        for key in path_lookup_keys(output):
            lookup[key] = pairs
    return lookup


def mosaic_source_tile_lookup(rows: Iterable[Mapping[str, str]]) -> Dict[str, List[str]]:
    """Return source UTM tiles keyed by mosaic output path and file name."""
    lookup: Dict[str, List[str]] = {}
    for row in rows:
        tiles = mosaic_source_tiles(row)
        output = str(row.get("output_file", "") or "").strip()
        if not output or not tiles:
            continue
        for key in path_lookup_keys(output):
            lookup[key] = tiles
    return lookup


def path_lookup_keys(path_text: str | Path) -> List[str]:
    """Return stable path lookup keys for local report rows."""
    path = Path(str(path_text))
    keys = {str(path).strip().lower(), path.name.lower()}
    if not path.is_absolute():
        keys.add(str((Path.cwd() / path)).lower())
    return [key for key in keys if key]


def lookup_path_value(
    lookup: Mapping[str, List[tuple[str, str]]],
    path_text: str | Path,
) -> List[tuple[str, str]]:
    """Look up path-associated values using resolved path and basename fallbacks."""
    for key in path_lookup_keys(path_text):
        values = lookup.get(key)
        if values:
            return values
    return []


def add_unique_swot_fields(
    rows: Iterable[Mapping[str, str]],
    *,
    file_key: str = "file_name",
) -> Dict[str, set[str]]:
    """Collect unique SWOT filename metadata from rows and explicit columns."""
    values: Dict[str, set[str]] = {
        "cycles": set(),
        "passes": set(),
        "scenes": set(),
        "crids": set(),
        "product_counters": set(),
    }
    for row in rows:
        explicit_pairs = {
            "cycles": row.get("cycle") or row.get("cycle_id"),
            "passes": row.get("pass") or row.get("pass_id"),
            "scenes": row.get("scene") or row.get("scene_id"),
            "crids": row.get("crid") or row.get("preferred_crid") or row.get("dominant_crid"),
            "product_counters": row.get("counter") or row.get("product_counter"),
        }
        for key, value in explicit_pairs.items():
            text = str(value or "").strip()
            if text:
                values[key].add(text)

        fields = cached_swot_filename_fields(str(row.get(file_key, "")))
        if fields is None:
            continue
        parsed_pairs = {
            "cycles": fields.get("cycle_id"),
            "passes": fields.get("pass_id"),
            "scenes": fields.get("scene_id"),
            "crids": fields.get("crid"),
            "product_counters": fields.get("product_counter"),
        }
        for key, value in parsed_pairs.items():
            text = str(value or "").strip()
            if text:
                values[key].add(text)
    return values


def merge_unique_fields(target: Dict[str, set[str]], source: Mapping[str, set[str]]) -> None:
    """Merge unique SWOT metadata sets."""
    for key, values in source.items():
        target.setdefault(key, set()).update(values)


def sum_float_column(rows: Iterable[Mapping[str, str]], key: str) -> float:
    """Sum a numeric CSV column, ignoring blank and invalid values."""
    total = 0.0
    for row in rows:
        try:
            total += float(str(row.get(key, "") or "0"))
        except ValueError:
            continue
    return total


def update_stage_counts(
    counts: Counter[tuple[str, str]],
    stage: str,
    rows: Iterable[Mapping[str, str]],
    status_key: str,
) -> None:
    """Accumulate status counts for one stage."""
    for row in rows:
        status = str(row.get(status_key, "") or "unknown")
        counts[(stage, status)] += 1


def ee_inventory_image_rows(rows: Iterable[Mapping[str, str]]) -> List[Mapping[str, str]]:
    """Return EE inventory rows that represent image assets."""
    return [
        row
        for row in rows
        if str(row.get("asset_id", "") or "").strip()
        and str(row.get("asset_type", "") or "IMAGE").upper() in {"IMAGE", ""}
    ]


def asset_name_from_asset_id(asset_id: str) -> str:
    """Return the last path component of an Earth Engine asset id."""
    return str(asset_id or "").rstrip("/").rsplit("/", 1)[-1]


def inferred_source_tiles_from_text(text: str) -> List[str]:
    """Return source UTM tiles parsed from a file or asset name."""
    fields = cached_swot_filename_fields(str(text))
    if fields is None:
        return []
    tile = normalize_utm_tile_token(fields.get("coordinate_system", ""))
    return [tile] if tile else []


def inventory_verified_upload_row(row: Mapping[str, str]) -> Dict[str, str]:
    """Create a synthetic upload row from one EE inventory image row."""
    asset_id = str(row.get("asset_id", "") or "").strip()
    asset_name = str(row.get("asset_name", "") or "").strip() or asset_name_from_asset_id(asset_id)
    tiles = inferred_source_tiles_from_text(asset_name)
    fields = cached_swot_filename_fields(asset_name)
    output_grid = ""
    if fields is not None:
        output_grid = str(fields.get("coordinate_system", "") or "").upper()
    return {
        "local_file": asset_name,
        "asset_id": asset_id,
        "asset_name": asset_name,
        "final_status": EE_VERIFIED_STATUS,
        "output_grid": output_grid,
        "source_utm_tiles": json.dumps(tiles),
        "ee_asset_exists": "yes",
        "ee_verified_at": str(row.get("listed_at", "") or ""),
        "verification_source": "ee_asset_inventory.csv",
    }


def merge_upload_rows_with_ee_inventory(
    upload_rows: Iterable[Mapping[str, str]],
    inventory_rows: Iterable[Mapping[str, str]],
) -> List[Dict[str, str]]:
    """Return upload rows adjusted with EE inventory truth."""
    inventory_by_asset = {
        str(row.get("asset_id", "") or "").strip(): row
        for row in ee_inventory_image_rows(inventory_rows)
    }
    merged: List[Dict[str, str]] = []
    seen: set[str] = set()
    for raw_row in upload_rows:
        row = dict(raw_row)
        asset_id = str(row.get("asset_id", "") or "").strip()
        if asset_id:
            seen.add(asset_id)
        inventory_row = inventory_by_asset.get(asset_id)
        if inventory_row:
            row["final_status"] = EE_VERIFIED_STATUS
            row["ee_asset_exists"] = "yes"
            row["ee_verified_at"] = str(inventory_row.get("listed_at", "") or row.get("ee_verified_at", ""))
            row["verification_source"] = row.get("verification_source", "") or "ee_asset_inventory.csv"
            if not str(row.get("source_utm_tiles", "") or "").strip():
                asset_name = str(inventory_row.get("asset_name", "") or "").strip() or asset_name_from_asset_id(asset_id)
                row["source_utm_tiles"] = json.dumps(inferred_source_tiles_from_text(row.get("local_file", "") or asset_name))
            if not str(row.get("output_grid", "") or "").strip():
                tiles = [tile for tile in parse_json_list(row.get("source_utm_tiles", "")) if tile]
                row["output_grid"] = tiles[0] if len(tiles) == 1 else ""
        merged.append(row)

    for asset_id, inventory_row in inventory_by_asset.items():
        if asset_id not in seen:
            merged.append(inventory_verified_upload_row(inventory_row))
    return merged


def cleanup_path_size(path: Path) -> int:
    """Return file size or zero when unavailable."""
    try:
        return path.stat().st_size if path.exists() and path.is_file() else 0
    except OSError:
        return 0


def geotiff_sidecar_paths(path: Path) -> List[Path]:
    """Return expected GDAL sidecars for a GeoTIFF path."""
    if path.suffix.lower() not in {".tif", ".tiff"}:
        return []
    return [
        path.with_suffix(".tfw"),
        path.with_suffix(f"{path.suffix}.aux.xml"),
    ]


def orphan_temporary_mosaic_sidecars(mosaic_folder: Path) -> Iterable[Path]:
    """Yield temporary GDAL sidecars whose matching .part GeoTIFF is gone."""
    if not mosaic_folder.exists() or not mosaic_folder.is_dir():
        return []
    candidates: List[Path] = []
    for pattern in ("*.part.tif.aux.xml", "*.part.tiff.aux.xml"):
        for path in mosaic_folder.glob(pattern):
            temp_tif = Path(str(path)[: -len(".aux.xml")])
            if path.is_file() and not temp_tif.exists():
                candidates.append(path)
    return candidates


def add_cleanup_candidate(
    candidates: Dict[Path, CleanupCandidate],
    *,
    stage: str,
    path: Path,
    reason: str,
    protected_by: str,
) -> None:
    """Add one existing file to the cleanup candidate map."""
    if path.exists() and path.is_file():
        candidates[path] = CleanupCandidate(
            stage=stage,
            path=path,
            reason=reason,
            protected_by=protected_by,
            size_bytes=cleanup_path_size(path),
        )


def add_geotiff_candidate_with_sidecars(
    candidates: Dict[Path, CleanupCandidate],
    *,
    stage: str,
    path: Path,
    reason: str,
    sidecar_reason: str,
    protected_by: str,
) -> None:
    """Add a GeoTIFF cleanup candidate and any existing GDAL sidecars."""
    add_cleanup_candidate(
        candidates,
        stage=stage,
        path=path,
        reason=reason,
        protected_by=protected_by,
    )
    for sidecar in geotiff_sidecar_paths(path):
        add_cleanup_candidate(
            candidates,
            stage=stage,
            path=sidecar,
            reason=sidecar_reason,
            protected_by=protected_by,
        )


def completed_extract_rows(rows: Iterable[Mapping[str, str]]) -> Iterable[Mapping[str, str]]:
    """Yield extract manifest rows that prove extraction happened."""
    for row in rows:
        if str(row.get("status", "")).lower() in COMPLETED_EXTRACT_STATUSES:
            yield row


def completed_mosaic_rows(rows: Iterable[Mapping[str, str]]) -> Iterable[Mapping[str, str]]:
    """Yield mosaic manifest rows that prove mosaicking/copying happened."""
    for row in rows:
        if row.get("status", "") in COMPLETED_MOSAIC_STATUSES and row.get("stale", "false") != "true":
            yield row


def qa_excluded_mosaic_rows_without_raw_repair(
    rows: Iterable[Mapping[str, str]],
    raw_folder: Path,
) -> Iterable[Mapping[str, str]]:
    """Yield incompatible mosaic rows whose source raw files are no longer local."""
    for row in rows:
        if row.get("status", "") not in QA_EXCLUDED_MOSAIC_STATUSES:
            continue
        sources = parse_json_list(row.get("input_files", ""))
        if not sources:
            continue
        raw_paths = [raw_path_for_extracted_source(Path(source), raw_folder) for source in sources]
        if any(path.exists() for path in raw_paths):
            continue
        yield row


def raw_path_for_extracted_source(source: Path, raw_folder: Path) -> Path:
    """Return the likely raw NetCDF path for one extracted GeoTIFF."""
    stem = source.stem
    for suffix in ("_africa_laea", "_wgs84"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return raw_folder / f"{stem}.nc"


def plan_cleanup_candidates(config: Mapping[str, Any]) -> List[CleanupCandidate]:
    """Return local intermediate files that downstream manifests prove safe to remove."""
    extract_manifest = config_path(config, "extract", "manifest_csv")
    mosaic_manifest = config_path(config, "mosaic", "manifest_csv")
    raw_folder = processing_path(config, "raw_downloads")
    artifacts = config.get("artifacts", {})
    upload_report = Path(str(artifacts.get("report_csv", ""))) if isinstance(artifacts, Mapping) else Path("")
    ee_inventory = Path(str(artifacts.get("ee_asset_inventory_csv", ""))) if isinstance(artifacts, Mapping) else Path("")

    candidates: Dict[Path, CleanupCandidate] = {}

    for row in completed_extract_rows(read_csv_rows(extract_manifest)):
        path = Path(row.get("input_nc", ""))
        add_cleanup_candidate(
            candidates,
            stage="raw",
            path=path,
            reason="Raw NetCDF has a completed extraction manifest row.",
            protected_by=str(extract_manifest),
        )

    for row in completed_mosaic_rows(read_csv_rows(mosaic_manifest)):
        for source in parse_json_list(row.get("input_files", "")):
            path = Path(source)
            add_geotiff_candidate_with_sidecars(
                candidates,
                stage="extracted",
                path=path,
                reason="Extracted GeoTIFF is recorded as a source of a completed mosaic group.",
                sidecar_reason="Extracted GeoTIFF sidecar belongs to a completed mosaic source.",
                protected_by=str(mosaic_manifest),
            )
        for source, _reason in mosaic_excluded_sources(row):
            path = Path(source)
            add_geotiff_candidate_with_sidecars(
                candidates,
                stage="qa",
                path=path,
                reason="Extracted GeoTIFF was excluded from a completed partial mosaic; mosaic_manifest.csv preserves the QA record.",
                sidecar_reason="Extracted GeoTIFF sidecar belongs to a source excluded from a completed partial mosaic.",
                protected_by=str(mosaic_manifest),
            )

    for row in qa_excluded_mosaic_rows_without_raw_repair(read_csv_rows(mosaic_manifest), raw_folder):
        reason = (
            "Extracted GeoTIFF belongs to an incompatible mosaic group with no local raw "
            "NetCDF left for repair; mosaic_manifest.csv preserves the QA record."
        )
        sidecar_reason = (
            "Extracted GeoTIFF sidecar belongs to an incompatible mosaic group with no "
            "local raw NetCDF left for repair."
        )
        for source in parse_json_list(row.get("input_files", "")):
            path = Path(source)
            add_geotiff_candidate_with_sidecars(
                candidates,
                stage="qa",
                path=path,
                reason=reason,
                sidecar_reason=sidecar_reason,
                protected_by=str(mosaic_manifest),
            )

    upload_rows = merge_upload_rows_with_ee_inventory(
        read_csv_rows(upload_report),
        read_csv_rows(ee_inventory),
    )
    for row in upload_rows:
        if row.get("final_status", "").upper() not in UPLOAD_CLEANUP_STATUSES:
            continue
        path = Path(row.get("local_file", ""))
        add_geotiff_candidate_with_sidecars(
            candidates,
            stage="mosaic",
            path=path,
            reason="Mosaic GeoTIFF is recorded as uploaded or already existing in Earth Engine.",
            sidecar_reason="Mosaic GeoTIFF sidecar belongs to an uploaded or EE-verified mosaic.",
            protected_by=str(upload_report),
        )

    mosaic_folder = processing_path(config, "mosaics")
    for path in orphan_temporary_mosaic_sidecars(mosaic_folder):
        add_cleanup_candidate(
            candidates,
            stage="temporary",
            path=path,
            reason="Orphaned temporary mosaic sidecar; matching .part GeoTIFF no longer exists.",
            protected_by=str(mosaic_folder),
        )

    return sorted(candidates.values(), key=lambda item: (item.stage, str(item.path).lower()))


def _collect_project_insights(config: Mapping[str, Any]) -> ProjectInsights:
    """Compute project-level metrics from manifests, reports, and local folders."""
    raw_folder = processing_path(config, "raw_downloads")
    extracted_folder = processing_path(config, "extracted_geotiffs")
    mosaic_folder = processing_path(config, "mosaics")
    logs_folder = processing_path(config, "logs")

    download_rows = read_csv_rows(config_path(config, "download", "manifest_csv"))
    extract_rows = read_csv_rows(config_path(config, "extract", "manifest_csv"))
    extract_error_rows = read_csv_rows(config_path(config, "extract", "errors_csv"))
    mosaic_rows = read_csv_rows(config_path(config, "mosaic", "manifest_csv"))
    artifacts = config.get("artifacts", {})
    upload_report_rows = read_csv_rows(artifacts.get("report_csv", "")) if isinstance(artifacts, Mapping) else []
    ee_inventory_rows = read_csv_rows(artifacts.get("ee_asset_inventory_csv", "")) if isinstance(artifacts, Mapping) else []
    ee_inventory_images = ee_inventory_image_rows(ee_inventory_rows)
    upload_rows = merge_upload_rows_with_ee_inventory(upload_report_rows, ee_inventory_rows)
    project_database = ProjectDatabase(database_path_from_config(config))
    workflow_row_count = project_database.dataset_count("workflow_manifest")

    stage_counts: Counter[tuple[str, str]] = Counter()
    update_stage_counts(stage_counts, "download", download_rows, "last_status")
    update_stage_counts(stage_counts, "extract", extract_rows, "status")
    update_stage_counts(stage_counts, "mosaic", mosaic_rows, "status")
    update_stage_counts(stage_counts, "upload", upload_rows, "final_status")
    for stage, status, count in project_database.workflow_stage_status_counts():
        stage_counts[(f"workflow:{stage}", status)] += count

    tile_counter: Counter[str] = Counter()
    date_counter: Counter[str] = Counter()
    for row in download_rows:
        tile = row.get("utm_tile", "")
        if tile:
            tile_counter[tile] += 1
        date_text = date_from_text(row.get("start_time", ""))
        if date_text:
            date_counter[date_text] += 1
    for row in extract_rows:
        tile = f"UTM{row.get('utm_zone', '')}{row.get('mgrs_band', '')}".strip()
        if tile.startswith("UTM") and len(tile) > 3:
            tile_counter[tile] += 1
        date_text = date_from_text(row.get("date", "") or row.get("start", ""))
        if date_text:
            date_counter[date_text] += 1
    for row in mosaic_rows:
        date_text = date_from_text(row.get("start_date", "") or row.get("range_beginning", ""))
        if date_text:
            date_counter[date_text] += 1

    unique_fields: Dict[str, set[str]] = {
        "cycles": set(),
        "passes": set(),
        "scenes": set(),
        "crids": set(),
        "product_counters": set(),
    }
    merge_unique_fields(unique_fields, add_unique_swot_fields(download_rows, file_key="file_name"))
    merge_unique_fields(unique_fields, add_unique_swot_fields(extract_rows, file_key="input_nc"))
    merge_unique_fields(unique_fields, add_unique_swot_fields(mosaic_rows, file_key="output_file"))

    moved_name = str(config.get("duplicates", {}).get("moved_folder_name", "moved")) if isinstance(config.get("duplicates", {}), Mapping) else "moved"
    moved_files = 0
    if raw_folder.exists():
        moved_files = sum(
            1
            for path in raw_folder.rglob("*")
            if path.is_file() and moved_name in path.relative_to(raw_folder).parts
        )

    cleanup_candidates = plan_cleanup_candidates(config)
    mosaic_lineage_rows = build_mosaic_lineage_rows(
        download_rows,
        extract_rows,
        extract_error_rows,
        mosaic_rows,
    )
    downloaded_rows = [row for row in download_rows if row.get("downloaded", "").lower() == "yes"]
    raw_existing_download_rows = [row for row in download_rows if row.get("raw_exists", "").lower() == "yes"]
    excluded_download_rows = [
        row
        for row in download_rows
        if row.get("last_status", "") == "EXCLUDED_OLDER_VERSION"
        or row.get("duplicate_filter_status", "") == "excluded_older_version"
    ]
    complete_extract_rows = list(completed_extract_rows(extract_rows))
    complete_mosaic_rows = list(completed_mosaic_rows(mosaic_rows))
    stale_mosaic_rows = [row for row in mosaic_rows if row.get("stale", "").lower() == "true"]
    uploaded_rows = [
        row for row in upload_rows if row.get("final_status", "").upper() in UPLOAD_CLEANUP_STATUSES
    ]
    ee_verified_upload_rows = [
        row for row in upload_rows if row.get("final_status", "").upper() == "EE_VERIFIED_EXISTS"
    ]
    submitted_upload_rows = [
        row for row in upload_rows if row.get("final_status", "").upper() == "SUBMITTED"
    ]
    filtered_upload_rows = [
        row
        for row in upload_rows
        if row.get("final_status", "").upper() == "FILTERED_UTM_TILE"
        or row.get("upload_selected", "").lower() == "no"
    ]
    upload_status_counter: Counter[str] = Counter(
        str(row.get("final_status", "") or "UNKNOWN").upper()
        for row in upload_rows
    )
    uploaded_tile_counter: Counter[str] = Counter()
    submitted_tile_counter: Counter[str] = Counter()
    uploaded_date_counter: Counter[str] = Counter()
    uploaded_level_counter: Counter[str] = Counter()
    uploaded_grid_counter: Counter[str] = Counter()
    upload_error_counter: Counter[tuple[str, str]] = Counter()
    upload_success_path_keys: set[str] = set()

    mosaic_tiles_by_output = mosaic_source_tile_lookup(complete_mosaic_rows)
    for row in upload_rows:
        status = str(row.get("final_status", "") or "UNKNOWN").upper()
        if status == "SUBMITTED":
            submitted_tiles = upload_row_source_tiles(row, mosaic_tiles_by_output)
            if not submitted_tiles:
                submitted_tiles = ["UNKNOWN"]
            for tile in submitted_tiles:
                submitted_tile_counter[tile] += 1
        if status not in UPLOAD_CLEANUP_STATUSES and status != "FILTERED_UTM_TILE":
            error_text = str(row.get("error_message", "") or "").strip()
            if not error_text:
                error_text = str(row.get("upload_filter_status", "") or "").strip()
            upload_error_counter[(status, error_text[:180])] += 1

    for row in uploaded_rows:
        local_file = row.get("local_file", "")
        if local_file:
            upload_success_path_keys.update(path_lookup_keys(local_file))
        upload_tiles = upload_row_source_tiles(row, mosaic_tiles_by_output)
        if not upload_tiles:
            upload_tiles = ["UNKNOWN"]
        for tile in upload_tiles:
            uploaded_tile_counter[tile] += 1
        date_text = date_from_text(row.get("metadata_start_time", "") or local_file)
        if date_text:
            uploaded_date_counter[date_text] += 1
        level = row_processing_level(row, file_keys=("local_file",))
        uploaded_level_counter[level] += 1
        grid = str(row.get("output_grid", "") or "").strip().upper()
        if not grid:
            fields = cached_swot_filename_fields(str(local_file))
            grid = str((fields.get("coordinate_system", "") if fields else "") or "").upper()
        uploaded_grid_counter[grid or "UNKNOWN"] += 1

    level_counter: Dict[str, Counter[str]] = {}
    tile_level_counter: Dict[tuple[str, str], Counter[str]] = {}
    downloaded_tile_stage_counter: Counter[str] = Counter()
    extracted_tile_stage_counter: Counter[str] = Counter()

    def add_level_count(level: str, key: str, count: int = 1) -> None:
        level_text = level or "UNKNOWN"
        level_counter.setdefault(level_text, Counter())[key] += count

    def add_tile_level_count(
        tile: str,
        level: str,
        key: str,
        count: int = 1,
    ) -> None:
        tile_text = normalize_utm_tile_token(tile) or "UNKNOWN"
        level_text = level or "UNKNOWN"
        tile_level_counter.setdefault((tile_text, level_text), Counter())[key] += count

    for row in download_rows:
        level = row_processing_level(row, file_keys=("file_name", "local_path"))
        tile = normalize_utm_tile_token(row.get("utm_tile", "")) or file_utm_tile(row.get("file_name", ""))
        add_level_count(level, "remote")
        add_tile_level_count(tile, level, "remote")
        if str(row.get("selected_for_download", "yes") or "yes").lower() != "no":
            add_level_count(level, "selected")
        if str(row.get("downloaded", "")).lower() == "yes":
            add_level_count(level, "downloaded")
            add_tile_level_count(tile, level, "downloaded")
            downloaded_tile_stage_counter[normalize_utm_tile_token(tile) or "UNKNOWN"] += 1

    for row in complete_extract_rows:
        level = row_processing_level(row, file_keys=("input_nc", "output_tif"))
        tile = normalize_utm_tile_token(f"UTM{row.get('utm_zone', '')}{row.get('mgrs_band', '')}") or file_utm_tile(row.get("input_nc", ""))
        add_level_count(level, "extracted")
        add_tile_level_count(tile, level, "extracted")
        extracted_tile_stage_counter[normalize_utm_tile_token(tile) or "UNKNOWN"] += 1

    mosaic_levels_by_output = mosaic_level_lookup(complete_mosaic_rows)
    for row in complete_mosaic_rows:
        source_pairs = mosaic_source_tile_levels(row)
        if not source_pairs:
            source_pairs = lookup_path_value(mosaic_levels_by_output, row.get("output_file", ""))
        if not source_pairs:
            fallback_tile = normalize_utm_tile_token(mosaic_output_grid(row))
            fallback_level = row_processing_level(row, file_keys=("output_file",))
            source_pairs = [(fallback_tile or "UNKNOWN", fallback_level)]
        for tile, level in source_pairs:
            add_level_count(level, "mosaic_sources")
            add_tile_level_count(tile, level, "mosaic_sources")

    for row in uploaded_rows:
        upload_pairs = lookup_path_value(mosaic_levels_by_output, row.get("local_file", ""))
        if not upload_pairs:
            upload_pairs = [(file_utm_tile(row.get("local_file", "")) or "UNKNOWN", parsed_processing_level(row.get("local_file", "")) or "UNKNOWN")]
        for _tile, level in upload_pairs:
            add_level_count(level, "uploaded")

    mosaic_output_grid_counter: Counter[str] = Counter()
    mosaic_source_tile_counter: Counter[str] = Counter()
    mosaic_exclusion_rows: List[tuple[str, str, str, str, str]] = []
    ready_not_uploaded: List[tuple[str, str, str, str]] = []
    for row in complete_mosaic_rows:
        output_grid = mosaic_output_grid(row)
        if output_grid:
            mosaic_output_grid_counter[output_grid] += 1
        source_tiles = mosaic_source_tiles(row)
        if not source_tiles:
            fallback_tile = normalize_utm_tile_token(output_grid)
            if fallback_tile:
                source_tiles = [fallback_tile]
        for tile in source_tiles:
            mosaic_source_tile_counter[tile] += 1
        if output_exists_for_upload(row):
            output_file = str(row.get("output_file", "") or "").strip()
            if output_file and not any(key in upload_success_path_keys for key in path_lookup_keys(output_file)):
                ready_not_uploaded.append(
                    (
                        output_file,
                        ",".join(source_tiles) if source_tiles else "",
                        date_from_text(row.get("start_date", "") or row.get("range_beginning", "")),
                        output_grid,
                    )
                )
        for excluded_file, reason in mosaic_excluded_sources(row):
            mosaic_exclusion_rows.append(
                (
                    str(row.get("output_file", "") or ""),
                    excluded_file,
                    reason,
                    date_from_text(row.get("start_date", "") or row.get("range_beginning", "")),
                    output_grid,
                )
            )
    common_crs_mosaic_count = sum(
        count
        for grid, count in mosaic_output_grid_counter.items()
        if not normalize_utm_tile_token(grid)
    )
    date_values = sorted(date_counter)
    known_size_mb = sum_float_column(download_rows, "size_mb")
    excluded_size_mb = sum_float_column(excluded_download_rows, "size_mb")
    raw_count = file_count(raw_folder, ["*.nc"], recursive=False)
    raw_tree_count = file_count(raw_folder, ["*.nc"], recursive=True)
    extracted_count = file_count(extracted_folder, ["*.tif", "*.tiff"], recursive=False)
    extracted_tree_count = file_count(extracted_folder, ["*.tif", "*.tiff"], recursive=True)
    mosaic_count = file_count(mosaic_folder, ["*.tif", "*.tiff"], recursive=False)
    mosaic_tree_count = file_count(mosaic_folder, ["*.tif", "*.tiff"], recursive=True)
    log_count = file_count(logs_folder, ["*.csv", "*.log", "*.txt", "*.html"], recursive=True)
    raw_size = folder_size(raw_folder, ["*.nc"], recursive=False)
    extracted_size = folder_size(extracted_folder, ["*.tif", "*.tiff"], recursive=False)
    mosaic_size = folder_size(mosaic_folder, ["*.tif", "*.tiff"], recursive=False)
    logs_size = folder_size(logs_folder, ["*"], recursive=True)
    total_project_files = raw_tree_count + extracted_tree_count + mosaic_tree_count + log_count
    total_project_size = (
        folder_size(raw_folder, ["*"], recursive=True)
        + folder_size(extracted_folder, ["*"], recursive=True)
        + folder_size(mosaic_folder, ["*"], recursive=True)
        + logs_size
    )

    metrics: "OrderedDict[str, str]" = OrderedDict()
    metrics["Total project files on disk"] = str(total_project_files)
    metrics["Total project folder size"] = format_bytes(total_project_size)
    metrics["Raw NetCDF files on disk"] = str(raw_count)
    metrics["Raw NetCDF files in raw tree"] = str(raw_tree_count)
    metrics["Extracted GeoTIFFs on disk"] = str(extracted_count)
    metrics["Extracted GeoTIFFs in extracted tree"] = str(extracted_tree_count)
    metrics["Mosaic GeoTIFFs on disk"] = str(mosaic_count)
    metrics["Mosaic GeoTIFFs in mosaic tree"] = str(mosaic_tree_count)
    metrics["Log/report files on disk"] = str(log_count)
    metrics["Duplicate files moved"] = str(moved_files)
    metrics["Download manifest rows"] = str(len(download_rows))
    metrics["Downloaded granules recorded"] = str(len(downloaded_rows))
    metrics["Remote matches excluded as older versions"] = str(len(excluded_download_rows))
    metrics["Known size excluded by version filter"] = format_bytes(int(excluded_size_mb * 1024 * 1024))
    metrics["Downloaded raw files still present"] = str(len(raw_existing_download_rows))
    metrics["Downloaded raw files no longer present"] = str(max(0, len(downloaded_rows) - len(raw_existing_download_rows)))
    metrics["Known cumulative download size"] = format_bytes(int(known_size_mb * 1024 * 1024))
    metrics["Extraction manifest rows"] = str(len(extract_rows))
    metrics["Completed extractions recorded"] = str(len(complete_extract_rows))
    metrics["Mosaic manifest rows"] = str(len(mosaic_rows))
    metrics["Completed mosaics recorded"] = str(len(complete_mosaic_rows))
    metrics["Stale mosaic rows"] = str(len(stale_mosaic_rows))
    metrics["Unique mosaic output grids observed"] = str(len(mosaic_output_grid_counter))
    metrics["Completed common-CRS/non-UTM mosaics"] = str(common_crs_mosaic_count)
    metrics["Unique mosaic source UTM tiles observed"] = str(len(mosaic_source_tile_counter))
    metrics["Mosaic source files excluded"] = str(len(mosaic_exclusion_rows))
    metrics["Mosaic lineage rows"] = str(len(mosaic_lineage_rows))
    lineage_status_counter = Counter(row.get("lineage_status", "unknown") for row in mosaic_lineage_rows)
    metrics["Lineage used in mosaic"] = str(lineage_status_counter.get("used_in_mosaic", 0))
    metrics["Lineage extracted not mosaicked"] = str(lineage_status_counter.get("extracted_not_mosaicked", 0))
    metrics["Lineage extraction failed"] = str(lineage_status_counter.get("extraction_failed", 0))
    metrics["Lineage mosaic group skipped"] = str(lineage_status_counter.get("mosaic_group_skipped", 0))
    metrics["Lineage excluded from partial mosaic"] = str(lineage_status_counter.get("excluded_from_partial_mosaic", 0))
    metrics["Lineage downloaded not extracted"] = str(lineage_status_counter.get("downloaded_not_extracted", 0))
    metrics["Upload report rows"] = str(len(upload_report_rows))
    metrics["EE inventory image assets"] = str(len(ee_inventory_images))
    metrics["Effective upload rows after EE inventory merge"] = str(len(upload_rows))
    metrics["Uploaded/already-existing assets recorded"] = str(len(uploaded_rows))
    metrics["EE-verified existing assets recorded"] = str(len(ee_verified_upload_rows))
    metrics["Submitted uploads awaiting EE verification"] = str(len(submitted_upload_rows))
    metrics["Unique submitted source UTM tiles awaiting verification"] = str(len([tile for tile in submitted_tile_counter if tile != "UNKNOWN"]))
    metrics["Submitted source UTM tiles awaiting verification"] = compact_count_keys(submitted_tile_counter)
    metrics["Upload rows filtered by UTM selection"] = str(len(filtered_upload_rows))
    metrics["Unique uploaded source UTM tiles"] = str(len([tile for tile in uploaded_tile_counter if tile != "UNKNOWN"]))
    metrics["Unique uploaded dates"] = str(len(uploaded_date_counter))
    metrics["Unique uploaded processing levels"] = str(len([level for level in uploaded_level_counter if level != "UNKNOWN"]))
    metrics["Upload failures/errors recorded"] = str(sum(upload_error_counter.values()))
    metrics["Upload-ready mosaics not uploaded/verified"] = str(len(ready_not_uploaded))
    metrics["Workflow manifest rows"] = str(workflow_row_count)
    metrics["Unique UTM tiles observed"] = str(len(tile_counter))
    metrics["Unique dates observed"] = str(len(date_counter))
    metrics["Unique SWOT cycles observed"] = str(len(unique_fields["cycles"]))
    metrics["Unique SWOT passes observed"] = str(len(unique_fields["passes"]))
    metrics["Unique SWOT scenes observed"] = str(len(unique_fields["scenes"]))
    metrics["Unique CRIDs observed"] = str(len(unique_fields["crids"]))
    metrics["Unique product counters observed"] = str(len(unique_fields["product_counters"]))
    metrics["Unique processing levels observed"] = str(len([level for level in level_counter if level != "UNKNOWN"]))
    metrics["Date coverage"] = f"{date_values[0]} to {date_values[-1]}" if date_values else ""
    metrics["Raw folder size"] = format_bytes(raw_size)
    metrics["Extracted folder size"] = format_bytes(extracted_size)
    metrics["Mosaic folder size"] = format_bytes(mosaic_size)
    metrics["Logs folder size"] = format_bytes(logs_size)
    metrics["Cleanup candidates"] = str(len(cleanup_candidates))
    metrics["Cleanup candidate size"] = format_bytes(sum(item.size_bytes for item in cleanup_candidates))

    level_rows = [
        (
            level,
            counts.get("remote", 0),
            counts.get("selected", 0),
            counts.get("downloaded", 0),
            counts.get("extracted", 0),
            counts.get("mosaic_sources", 0),
            counts.get("uploaded", 0),
        )
        for level, counts in level_counter.items()
    ]
    level_rows.sort(key=lambda item: level_sort_key(item[0]), reverse=True)
    tile_level_rows = [
        (
            tile,
            level,
            counts.get("remote", 0),
            counts.get("downloaded", 0),
            counts.get("extracted", 0),
            counts.get("mosaic_sources", 0),
        )
        for (tile, level), counts in tile_level_counter.items()
    ]
    tile_level_rows.sort(key=lambda item: (item[0], level_sort_key(item[1])), reverse=False)
    all_qa_tiles = sorted(
        set(downloaded_tile_stage_counter)
        | set(extracted_tile_stage_counter)
        | set(mosaic_source_tile_counter)
        | set(uploaded_tile_counter)
    )
    upload_qa_rows = [
        (
            tile,
            downloaded_tile_stage_counter.get(tile, 0),
            extracted_tile_stage_counter.get(tile, 0),
            mosaic_source_tile_counter.get(tile, 0),
            uploaded_tile_counter.get(tile, 0),
            max(0, mosaic_source_tile_counter.get(tile, 0) - uploaded_tile_counter.get(tile, 0)),
        )
        for tile in all_qa_tiles
    ]

    return ProjectInsights(
        metrics=metrics,
        stage_status_counts=[
            (stage, status, count)
            for (stage, status), count in sorted(stage_counts.items())
        ],
        tile_counts=tile_counter.most_common(50),
        date_counts=sorted(date_counter.items()),
        processing_level_counts=level_rows,
        processing_level_tile_counts=tile_level_rows,
        mosaic_output_grid_counts=mosaic_output_grid_counter.most_common(50),
        mosaic_source_tile_counts=mosaic_source_tile_counter.most_common(50),
        upload_status_counts=upload_status_counter.most_common(),
        uploaded_tile_counts=uploaded_tile_counter.most_common(100),
        uploaded_date_counts=sorted(uploaded_date_counter.items()),
        uploaded_processing_level_counts=uploaded_level_counter.most_common(50),
        uploaded_grid_counts=uploaded_grid_counter.most_common(50),
        upload_error_counts=[
            (status, message, count)
            for (status, message), count in upload_error_counter.most_common(100)
        ],
        upload_qa_tile_rows=upload_qa_rows,
        ready_not_uploaded_rows=ready_not_uploaded,
        mosaic_exclusion_rows=mosaic_exclusion_rows,
        mosaic_lineage_rows=mosaic_lineage_rows,
        cleanup_candidates=cleanup_candidates,
    )


def collect_project_insights(config: Mapping[str, Any]) -> ProjectInsights:
    """Compute project insights with a bounded per-refresh filename cache."""
    cached_swot_filename_fields.cache_clear()
    try:
        return _collect_project_insights(config)
    finally:
        cached_swot_filename_fields.cache_clear()


def delete_cleanup_candidates(candidates: Iterable[CleanupCandidate]) -> tuple[int, int, List[str]]:
    """Delete candidate files and return deleted count, bytes, and errors."""
    deleted = 0
    bytes_deleted = 0
    errors: List[str] = []
    for candidate in candidates:
        try:
            size = cleanup_path_size(candidate.path)
            candidate.path.unlink()
            deleted += 1
            bytes_deleted += size
        except OSError as exc:
            errors.append(f"{candidate.path}: {exc}")
    return deleted, bytes_deleted, errors


def project_insights_snapshot(insights: ProjectInsights) -> Dict[str, Any]:
    """Return a JSON-serializable statistics snapshot."""
    generated_at = datetime.now().replace(microsecond=0).isoformat()
    return {
        "generated_at": generated_at,
        "metrics": [
            {"metric": metric, "value": value}
            for metric, value in insights.metrics.items()
        ],
        "stage_status_counts": [
            {"stage": stage, "status": status, "count": count}
            for stage, status, count in insights.stage_status_counts
        ],
        "tile_counts": [
            {"tile": tile, "count": count}
            for tile, count in insights.tile_counts
        ],
        "date_counts": [
            {"date": date_text, "count": count}
            for date_text, count in insights.date_counts
        ],
        "processing_level_counts": [
            {
                "level": level,
                "remote": remote,
                "selected": selected,
                "downloaded": downloaded,
                "extracted": extracted,
                "mosaic_sources": mosaic_sources,
                "uploaded": uploaded,
            }
            for (
                level,
                remote,
                selected,
                downloaded,
                extracted,
                mosaic_sources,
                uploaded,
            ) in insights.processing_level_counts
        ],
        "processing_level_tile_counts": [
            {
                "tile": tile,
                "level": level,
                "remote": remote,
                "downloaded": downloaded,
                "extracted": extracted,
                "mosaic_sources": mosaic_sources,
            }
            for tile, level, remote, downloaded, extracted, mosaic_sources in insights.processing_level_tile_counts
        ],
        "mosaic_output_grid_counts": [
            {"grid": grid, "count": count}
            for grid, count in insights.mosaic_output_grid_counts
        ],
        "mosaic_source_tile_counts": [
            {"tile": tile, "count": count}
            for tile, count in insights.mosaic_source_tile_counts
        ],
        "upload_status_counts": [
            {"status": status, "count": count}
            for status, count in insights.upload_status_counts
        ],
        "uploaded_tile_counts": [
            {"tile": tile, "count": count}
            for tile, count in insights.uploaded_tile_counts
        ],
        "uploaded_date_counts": [
            {"date": date_text, "count": count}
            for date_text, count in insights.uploaded_date_counts
        ],
        "uploaded_processing_level_counts": [
            {"level": level, "count": count}
            for level, count in insights.uploaded_processing_level_counts
        ],
        "uploaded_grid_counts": [
            {"grid": grid, "count": count}
            for grid, count in insights.uploaded_grid_counts
        ],
        "upload_error_counts": [
            {"status": status, "message": message, "count": count}
            for status, message, count in insights.upload_error_counts
        ],
        "upload_qa_tile_rows": [
            {
                "tile": tile,
                "downloaded": downloaded,
                "extracted": extracted,
                "mosaic_sources": mosaic_sources,
                "uploaded": uploaded,
                "missing_upload": missing_upload,
            }
            for tile, downloaded, extracted, mosaic_sources, uploaded, missing_upload in insights.upload_qa_tile_rows
        ],
        "ready_not_uploaded_rows": [
            {
                "output_file": output_file,
                "source_tiles": source_tiles,
                "date": date_text,
                "grid": grid,
            }
            for output_file, source_tiles, date_text, grid in insights.ready_not_uploaded_rows
        ],
        "mosaic_exclusion_rows": [
            {
                "output_file": output_file,
                "excluded_file": excluded_file,
                "reason": reason,
                "date": date_text,
                "grid": grid,
            }
            for output_file, excluded_file, reason, date_text, grid in insights.mosaic_exclusion_rows
        ],
        "mosaic_lineage_rows": insights.mosaic_lineage_rows[:LINEAGE_SNAPSHOT_LIMIT],
        "mosaic_lineage_row_count": len(insights.mosaic_lineage_rows),
    }


def write_project_insights_snapshot(config: Mapping[str, Any], insights: ProjectInsights) -> Path:
    """Write the latest project statistics snapshot as JSON plus CSV tables."""
    folder = statistics_folder(config)
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = project_insights_snapshot(insights)
    snapshot_path = statistics_snapshot_path(config)
    with snapshot_path.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2)

    write_csv_rows(
        folder / "project_statistics_metrics.csv",
        ["metric", "value"],
        snapshot["metrics"],
    )
    write_csv_rows(
        folder / "project_statistics_stage_status.csv",
        ["stage", "status", "count"],
        snapshot["stage_status_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_tiles.csv",
        ["tile", "count"],
        snapshot["tile_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_dates.csv",
        ["date", "count"],
        snapshot["date_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_processing_levels.csv",
        [
            "level",
            "remote",
            "selected",
            "downloaded",
            "extracted",
            "mosaic_sources",
            "uploaded",
        ],
        snapshot["processing_level_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_processing_levels_by_tile.csv",
        ["tile", "level", "remote", "downloaded", "extracted", "mosaic_sources"],
        snapshot["processing_level_tile_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_mosaic_output_grids.csv",
        ["grid", "count"],
        snapshot["mosaic_output_grid_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_mosaic_source_tiles.csv",
        ["tile", "count"],
        snapshot["mosaic_source_tile_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_upload_status.csv",
        ["status", "count"],
        snapshot["upload_status_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_uploaded_tiles.csv",
        ["tile", "count"],
        snapshot["uploaded_tile_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_uploaded_dates.csv",
        ["date", "count"],
        snapshot["uploaded_date_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_uploaded_processing_levels.csv",
        ["level", "count"],
        snapshot["uploaded_processing_level_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_uploaded_grids.csv",
        ["grid", "count"],
        snapshot["uploaded_grid_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_upload_errors.csv",
        ["status", "message", "count"],
        snapshot["upload_error_counts"],
    )
    write_csv_rows(
        folder / "project_statistics_upload_qa_by_tile.csv",
        ["tile", "downloaded", "extracted", "mosaic_sources", "uploaded", "missing_upload"],
        snapshot["upload_qa_tile_rows"],
    )
    write_csv_rows(
        folder / "project_statistics_ready_not_uploaded.csv",
        ["output_file", "source_tiles", "date", "grid"],
        snapshot["ready_not_uploaded_rows"],
    )
    write_csv_rows(
        folder / "project_statistics_mosaic_exclusions.csv",
        ["output_file", "excluded_file", "reason", "date", "grid"],
        snapshot["mosaic_exclusion_rows"],
    )
    write_csv_rows(
        folder / "project_statistics_mosaic_lineage.csv",
        [
            "lineage_status",
            "utm_tile",
            "date",
            "processing_level",
            "raw_file",
            "download_status",
            "downloaded",
            "raw_exists",
            "extracted_file",
            "extract_status",
            "extracted_exists",
            "mosaic_outputs",
            "mosaic_statuses",
            "mosaic_output_exists",
            "mosaic_excluded",
            "mosaic_exclusion_reason",
            "message",
        ],
        insights.mosaic_lineage_rows,
    )
    return snapshot_path


def _int_value(value: Any) -> int:
    """Return an integer value for snapshot counters."""
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def load_project_insights_snapshot(
    config: Mapping[str, Any],
) -> tuple[ProjectInsights, str] | None:
    """Load the latest saved project statistics snapshot."""
    path = statistics_snapshot_path(config)
    if not path.exists() or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    metrics = OrderedDict(
        (str(row.get("metric", "")), str(row.get("value", "")))
        for row in snapshot.get("metrics", [])
        if row.get("metric", "") != ""
    )
    insights = ProjectInsights(
        metrics=metrics,
        stage_status_counts=[
            (str(row.get("stage", "")), str(row.get("status", "")), _int_value(row.get("count")))
            for row in snapshot.get("stage_status_counts", [])
        ],
        tile_counts=[
            (str(row.get("tile", "")), _int_value(row.get("count")))
            for row in snapshot.get("tile_counts", [])
        ],
        date_counts=[
            (str(row.get("date", "")), _int_value(row.get("count")))
            for row in snapshot.get("date_counts", [])
        ],
        processing_level_counts=[
            (
                str(row.get("level", "")),
                _int_value(row.get("remote")),
                _int_value(row.get("selected")),
                _int_value(row.get("downloaded")),
                _int_value(row.get("extracted")),
                _int_value(row.get("mosaic_sources")),
                _int_value(row.get("uploaded")),
            )
            for row in snapshot.get("processing_level_counts", [])
        ],
        processing_level_tile_counts=[
            (
                str(row.get("tile", "")),
                str(row.get("level", "")),
                _int_value(row.get("remote")),
                _int_value(row.get("downloaded")),
                _int_value(row.get("extracted")),
                _int_value(row.get("mosaic_sources")),
            )
            for row in snapshot.get("processing_level_tile_counts", [])
        ],
        mosaic_output_grid_counts=[
            (str(row.get("grid", "")), _int_value(row.get("count")))
            for row in snapshot.get("mosaic_output_grid_counts", [])
        ],
        mosaic_source_tile_counts=[
            (str(row.get("tile", "")), _int_value(row.get("count")))
            for row in snapshot.get("mosaic_source_tile_counts", [])
        ],
        upload_status_counts=[
            (str(row.get("status", "")), _int_value(row.get("count")))
            for row in snapshot.get("upload_status_counts", [])
        ],
        uploaded_tile_counts=[
            (str(row.get("tile", "")), _int_value(row.get("count")))
            for row in snapshot.get("uploaded_tile_counts", [])
        ],
        uploaded_date_counts=[
            (str(row.get("date", "")), _int_value(row.get("count")))
            for row in snapshot.get("uploaded_date_counts", [])
        ],
        uploaded_processing_level_counts=[
            (str(row.get("level", "")), _int_value(row.get("count")))
            for row in snapshot.get("uploaded_processing_level_counts", [])
        ],
        uploaded_grid_counts=[
            (str(row.get("grid", "")), _int_value(row.get("count")))
            for row in snapshot.get("uploaded_grid_counts", [])
        ],
        upload_error_counts=[
            (str(row.get("status", "")), str(row.get("message", "")), _int_value(row.get("count")))
            for row in snapshot.get("upload_error_counts", [])
        ],
        upload_qa_tile_rows=[
            (
                str(row.get("tile", "")),
                _int_value(row.get("downloaded")),
                _int_value(row.get("extracted")),
                _int_value(row.get("mosaic_sources")),
                _int_value(row.get("uploaded")),
                _int_value(row.get("missing_upload")),
            )
            for row in snapshot.get("upload_qa_tile_rows", [])
        ],
        ready_not_uploaded_rows=[
            (
                str(row.get("output_file", "")),
                str(row.get("source_tiles", "")),
                str(row.get("date", "")),
                str(row.get("grid", "")),
            )
            for row in snapshot.get("ready_not_uploaded_rows", [])
        ],
        mosaic_exclusion_rows=[
            (
                str(row.get("output_file", "")),
                str(row.get("excluded_file", "")),
                str(row.get("reason", "")),
                str(row.get("date", "")),
                str(row.get("grid", "")),
            )
            for row in snapshot.get("mosaic_exclusion_rows", [])
        ],
        mosaic_lineage_rows=[
            {str(key): str(value) for key, value in row.items()}
            for row in snapshot.get("mosaic_lineage_rows", [])
            if isinstance(row, Mapping)
        ],
        cleanup_candidates=[],
    )
    return insights, str(snapshot.get("generated_at", ""))
