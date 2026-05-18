"""Project statistics and conservative cleanup planning for GeeUp."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from swot_metadata import parse_swot_l2_hr_raster_metadata


COMPLETED_EXTRACT_STATUSES = {
    "written",
    "skipped_existing",
    "skipped_manifest",
    "local_complete",
}
COMPLETED_MOSAIC_STATUSES = {
    "MOSAIC_CREATED",
    "COPIED_SINGLETON",
    "SKIPPED_EXISTS",
    "SKIPPED_MANIFEST",
}
UPLOAD_CLEANUP_STATUSES = {
    "COMPLETED",
    "SKIPPED_ALREADY_EXISTS",
    "EE_VERIFIED_EXISTS",
}
UTM_TILE_TOKEN_RE = re.compile(r"^UTM(?P<zone>\d{1,2})(?P<band>[C-HJ-NP-X])$")


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
    mosaic_output_grid_counts: List[tuple[str, int]]
    mosaic_source_tile_counts: List[tuple[str, int]]
    cleanup_candidates: List[CleanupCandidate]


def read_csv_rows(path: str | Path) -> List[Dict[str, str]]:
    """Read CSV rows, returning an empty list for missing files."""
    csv_path = Path(path)
    if not csv_path.exists() or not csv_path.is_file():
        return []
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


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


def mosaic_output_grid(row: Mapping[str, str]) -> str:
    """Return the output CRS/grid token recorded for one mosaic row."""
    grid = str(row.get("coordinate_system", "") or "").strip().upper()
    if grid:
        return grid
    parsed = parse_swot_l2_hr_raster_metadata(row.get("output_file", ""))
    if parsed is None:
        return ""
    return str(parsed.fields.get("coordinate_system", "") or "").strip().upper()


def mosaic_source_tiles(row: Mapping[str, str]) -> List[str]:
    """Return unique source UTM tiles contributing to one mosaic row."""
    tiles: set[str] = set()
    for file_name in parse_path_list(row.get("input_files", "")):
        parsed = parse_swot_l2_hr_raster_metadata(file_name)
        if parsed is None:
            continue
        tile = normalize_utm_tile_token(parsed.fields.get("coordinate_system", ""))
        if tile:
            tiles.add(tile)
    return sorted(tiles)


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

        parsed = parse_swot_l2_hr_raster_metadata(row.get(file_key, ""))
        if parsed is None:
            continue
        fields = parsed.fields
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


def cleanup_path_size(path: Path) -> int:
    """Return file size or zero when unavailable."""
    try:
        return path.stat().st_size if path.exists() and path.is_file() else 0
    except OSError:
        return 0


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


def plan_cleanup_candidates(config: Mapping[str, Any]) -> List[CleanupCandidate]:
    """Return local intermediate files that downstream manifests prove safe to remove."""
    extract_manifest = config_path(config, "extract", "manifest_csv")
    mosaic_manifest = config_path(config, "mosaic", "manifest_csv")
    artifacts = config.get("artifacts", {})
    upload_report = Path(str(artifacts.get("report_csv", ""))) if isinstance(artifacts, Mapping) else Path("")

    candidates: Dict[Path, CleanupCandidate] = {}

    for row in completed_extract_rows(read_csv_rows(extract_manifest)):
        path = Path(row.get("input_nc", ""))
        if path.exists() and path.is_file():
            candidates[path] = CleanupCandidate(
                stage="raw",
                path=path,
                reason="Raw NetCDF has a completed extraction manifest row.",
                protected_by=str(extract_manifest),
                size_bytes=cleanup_path_size(path),
            )

    for row in completed_mosaic_rows(read_csv_rows(mosaic_manifest)):
        for source in parse_json_list(row.get("input_files", "")):
            path = Path(source)
            if path.exists() and path.is_file():
                candidates[path] = CleanupCandidate(
                    stage="extracted",
                    path=path,
                    reason="Extracted GeoTIFF is recorded as a source of a completed mosaic group.",
                    protected_by=str(mosaic_manifest),
                    size_bytes=cleanup_path_size(path),
                )

    for row in read_csv_rows(upload_report):
        if row.get("final_status", "").upper() not in UPLOAD_CLEANUP_STATUSES:
            continue
        path = Path(row.get("local_file", ""))
        if path.exists() and path.is_file():
            candidates[path] = CleanupCandidate(
                stage="mosaic",
                path=path,
                reason="Mosaic GeoTIFF is recorded as uploaded or already existing in Earth Engine.",
                protected_by=str(upload_report),
                size_bytes=cleanup_path_size(path),
            )

    return sorted(candidates.values(), key=lambda item: (item.stage, str(item.path).lower()))


def collect_project_insights(config: Mapping[str, Any]) -> ProjectInsights:
    """Compute project-level metrics from manifests, reports, and local folders."""
    raw_folder = processing_path(config, "raw_downloads")
    extracted_folder = processing_path(config, "extracted_geotiffs")
    mosaic_folder = processing_path(config, "mosaics")
    logs_folder = processing_path(config, "logs")

    download_rows = read_csv_rows(config_path(config, "download", "manifest_csv"))
    extract_rows = read_csv_rows(config_path(config, "extract", "manifest_csv"))
    mosaic_rows = read_csv_rows(config_path(config, "mosaic", "manifest_csv"))
    artifacts = config.get("artifacts", {})
    upload_rows = read_csv_rows(artifacts.get("report_csv", "")) if isinstance(artifacts, Mapping) else []
    workflow_rows = read_csv_rows(logs_folder / "workflow_manifest.csv")

    stage_counts: Counter[tuple[str, str]] = Counter()
    update_stage_counts(stage_counts, "download", download_rows, "last_status")
    update_stage_counts(stage_counts, "extract", extract_rows, "status")
    update_stage_counts(stage_counts, "mosaic", mosaic_rows, "status")
    update_stage_counts(stage_counts, "upload", upload_rows, "final_status")
    update_stage_counts(stage_counts, "workflow", workflow_rows, "status")

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
    filtered_upload_rows = [
        row
        for row in upload_rows
        if row.get("final_status", "").upper() == "FILTERED_UTM_TILE"
        or row.get("upload_selected", "").lower() == "no"
    ]
    mosaic_output_grid_counter: Counter[str] = Counter()
    mosaic_source_tile_counter: Counter[str] = Counter()
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
    metrics["Upload report rows"] = str(len(upload_rows))
    metrics["Uploaded/already-existing assets recorded"] = str(len(uploaded_rows))
    metrics["EE-verified existing assets recorded"] = str(len(ee_verified_upload_rows))
    metrics["Upload rows filtered by UTM selection"] = str(len(filtered_upload_rows))
    metrics["Workflow manifest rows"] = str(len(workflow_rows))
    metrics["Unique UTM tiles observed"] = str(len(tile_counter))
    metrics["Unique dates observed"] = str(len(date_counter))
    metrics["Unique SWOT cycles observed"] = str(len(unique_fields["cycles"]))
    metrics["Unique SWOT passes observed"] = str(len(unique_fields["passes"]))
    metrics["Unique SWOT scenes observed"] = str(len(unique_fields["scenes"]))
    metrics["Unique CRIDs observed"] = str(len(unique_fields["crids"]))
    metrics["Unique product counters observed"] = str(len(unique_fields["product_counters"]))
    metrics["Date coverage"] = f"{date_values[0]} to {date_values[-1]}" if date_values else ""
    metrics["Raw folder size"] = format_bytes(raw_size)
    metrics["Extracted folder size"] = format_bytes(extracted_size)
    metrics["Mosaic folder size"] = format_bytes(mosaic_size)
    metrics["Logs folder size"] = format_bytes(logs_size)
    metrics["Cleanup candidates"] = str(len(cleanup_candidates))
    metrics["Cleanup candidate size"] = format_bytes(sum(item.size_bytes for item in cleanup_candidates))

    return ProjectInsights(
        metrics=metrics,
        stage_status_counts=[
            (stage, status, count)
            for (stage, status), count in sorted(stage_counts.items())
        ],
        tile_counts=tile_counter.most_common(50),
        date_counts=sorted(date_counter.items()),
        mosaic_output_grid_counts=mosaic_output_grid_counter.most_common(50),
        mosaic_source_tile_counts=mosaic_source_tile_counter.most_common(50),
        cleanup_candidates=cleanup_candidates,
    )


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
