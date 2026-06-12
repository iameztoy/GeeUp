"""Create SWOT GeoTIFF mosaics before Earth Engine upload."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import csv
import json
import math
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

from gdal_runtime import REQUIRED_GDAL_DRIVERS, current_process_gdal_check
from project_database import dataset_for_path, read_project_rows, upsert_project_rows
from swot_download_tool import normalize_utm_tiles
from swot_metadata import ParsedMetadata, parse_swot_l2_hr_raster_metadata, swot_product_rank
from workflow_manifest import (
    source_signature,
    timestamp_text,
    upsert_workflow_manifest,
    workflow_manifest_path,
)


PROGRESS_PREFIX = "GEEUP_PROGRESS"
ProgressCallback = Callable[[int, int, str], None]
DEFAULT_PROCESSING_ROOT = "./SWOT_Processing"
DEFAULT_PROCESSING_PATHS = {
    "root": DEFAULT_PROCESSING_ROOT,
    "raw_downloads": f"{DEFAULT_PROCESSING_ROOT}/01_raw_downloads",
    "extracted_geotiffs": f"{DEFAULT_PROCESSING_ROOT}/02_extracted_geotiffs",
    "mosaics": f"{DEFAULT_PROCESSING_ROOT}/03_mosaics",
    "logs": f"{DEFAULT_PROCESSING_ROOT}/00_logs",
}
GROUPING_MODE_UTM_ZONE = "utm_zone"
GROUPING_MODE_UTM_ZONE_HEMISPHERE = "utm_zone_hemisphere"
GROUPING_MODE_PASS_DATE_COMMON_CRS = "pass_date_common_crs"
VALID_GROUPING_MODES = {
    GROUPING_MODE_UTM_ZONE,
    GROUPING_MODE_UTM_ZONE_HEMISPHERE,
    GROUPING_MODE_PASS_DATE_COMMON_CRS,
}
UTM_TOKEN_RE = re.compile(r"^UTM(?P<zone>\d{1,2})(?P<band>[C-HJ-NP-X])$")
DEFAULT_COMMON_CRS_LABEL = "COMMON"
DEFAULT_CONFIG: Dict[str, Any] = {
    "processing": DEFAULT_PROCESSING_PATHS,
    "mosaic": {
        "input_folder": DEFAULT_PROCESSING_PATHS["extracted_geotiffs"],
        "output_folder": DEFAULT_PROCESSING_PATHS["mosaics"],
        "grouping_mode": GROUPING_MODE_UTM_ZONE,
        "target_crs_label": "",
        "recursive": False,
        "overwrite": False,
        "write_world_file": True,
        "extensions": [".tif", ".tiff"],
        "report_csv": f"{DEFAULT_PROCESSING_PATHS['logs']}/mosaic_report.csv",
        "manifest_csv": f"{DEFAULT_PROCESSING_PATHS['logs']}/mosaic_manifest.csv",
        "skip_manifest_existing": True,
        "mixed_crid_report_csv": "",
        "workers": 1,
        "utm_tiles": [],
    },
}


REPORT_COLUMNS = [
    "status",
    "output_file",
    "input_count",
    "original_input_count",
    "excluded_input_count",
    "cycle_id",
    "pass_id",
    "start_date",
    "coordinate_system",
    "descriptor",
    "range_beginning",
    "range_ending",
    "crid",
    "product_counter",
    "mixed_crid",
    "source_crids",
    "source_product_counters",
    "dominant_crid",
    "preferred_crid",
    "source_signature",
    "output_exists",
    "known_from_manifest",
    "stale",
    "updated_at",
    "message",
    "excluded_input_files",
    "excluded_reasons",
    "input_files",
]
ERROR_DISK_SPACE_STATUS = "ERROR_DISK_SPACE"
DISK_SPACE_ERROR_PATTERNS = (
    "no space left",
    "not enough space",
    "disk full",
    "insufficient disk",
    "tiffappendtostrip:write error",
    "write error at scanline",
)


@dataclass(frozen=True)
class MosaicGroupKey:
    """SWOT metadata fields that define one mosaic group."""

    descriptor: str
    cycle_id: str
    pass_id: str
    start_date: str
    coordinate_system: str


@dataclass
class MosaicConfig:
    """Runtime configuration for the mosaic tool."""

    input_folder: Path
    output_folder: Path
    grouping_mode: str = GROUPING_MODE_UTM_ZONE
    target_crs_label: str = ""
    recursive: bool = False
    overwrite: bool = False
    write_world_file: bool = True
    extensions: List[str] = field(default_factory=lambda: [".tif", ".tiff"])
    report_csv: Path = Path("./reports/mosaic_report.csv")
    manifest_csv: Path = Path(f"{DEFAULT_PROCESSING_PATHS['logs']}/mosaic_manifest.csv")
    skip_manifest_existing: bool = True
    mixed_crid_report_csv: Optional[Path] = None
    workers: int = 1
    utm_tiles: List[str] = field(default_factory=list)
    base_dir: Path = Path.cwd()


@dataclass
class MosaicSource:
    """One source file and its parsed SWOT metadata."""

    path: Path
    metadata: ParsedMetadata


@dataclass
class MosaicGroup:
    """One planned mosaic group."""

    key: MosaicGroupKey
    sources: List[MosaicSource]
    output_file: Path


@dataclass
class SourceExclusion:
    """One mosaic input that was excluded from a partial mosaic."""

    source: MosaicSource
    reason: str


@dataclass
class MosaicPlan:
    """Full scan result before execution."""

    groups: List[MosaicGroup] = field(default_factory=list)
    report_rows: List[Dict[str, str]] = field(default_factory=list)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge two dictionaries."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_path(value: str | Path | None, base_dir: Path) -> Path:
    """Resolve a path against the config file directory."""
    if value in (None, ""):
        raise ValueError("A required mosaic path value was empty.")
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def resolve_optional_path(value: str | Path | None, base_dir: Path) -> Optional[Path]:
    """Resolve an optional path against the config file directory."""
    if value in (None, ""):
        return None
    return resolve_path(value, base_dir)


def load_config_file(config_path: Path) -> MosaicConfig:
    """Load mosaic settings from YAML or JSON-style YAML config."""
    with config_path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    merged = deep_merge(DEFAULT_CONFIG, user_config)
    return parse_config(merged, config_path.parent.resolve())


def parse_config(data: Dict[str, Any], base_dir: Path) -> MosaicConfig:
    """Convert raw config data into a validated MosaicConfig."""
    mosaic_data = data.get("mosaic", {})
    input_folder = mosaic_data.get("input_folder") or data.get("input_folder", "")
    config = MosaicConfig(
        input_folder=resolve_path(input_folder, base_dir),
        output_folder=resolve_path(mosaic_data.get("output_folder", "./mosaics"), base_dir),
        grouping_mode=str(
            mosaic_data.get("grouping_mode", GROUPING_MODE_UTM_ZONE)
        ).strip(),
        target_crs_label=normalize_crs_label(mosaic_data.get("target_crs_label", "")),
        recursive=bool(mosaic_data.get("recursive", False)),
        overwrite=bool(mosaic_data.get("overwrite", False)),
        write_world_file=bool(mosaic_data.get("write_world_file", True)),
        extensions=[
            str(value).lower()
            for value in mosaic_data.get("extensions", [".tif", ".tiff"])
        ],
        report_csv=resolve_path(mosaic_data.get("report_csv", "./reports/mosaic_report.csv"), base_dir),
        manifest_csv=resolve_path(
            mosaic_data.get(
                "manifest_csv",
                f"{data.get('processing', {}).get('logs', DEFAULT_PROCESSING_PATHS['logs'])}/mosaic_manifest.csv",
            ),
            base_dir,
        ),
        skip_manifest_existing=bool(mosaic_data.get("skip_manifest_existing", True)),
        mixed_crid_report_csv=resolve_optional_path(
            mosaic_data.get("mixed_crid_report_csv", ""),
            base_dir,
        ),
        workers=normalize_workers(mosaic_data.get("workers", 1)),
        utm_tiles=normalize_utm_tiles(mosaic_data.get("utm_tiles", [])),
        base_dir=base_dir,
    )
    validate_config(config)
    return config


def validate_config(config: MosaicConfig) -> None:
    """Raise ValueError when the mosaic configuration is invalid."""
    if config.grouping_mode not in VALID_GROUPING_MODES:
        raise ValueError(
            "mosaic.grouping_mode must be one of: "
            f"{', '.join(sorted(VALID_GROUPING_MODES))}."
        )
    if not config.input_folder.exists():
        raise ValueError(f"Mosaic input folder does not exist: {config.input_folder}")
    if not config.input_folder.is_dir():
        raise ValueError(f"Mosaic input path is not a directory: {config.input_folder}")
    if config.output_folder.resolve() == config.input_folder.resolve():
        raise ValueError("Mosaic output folder must be different from the input folder.")
    if not config.extensions:
        raise ValueError("mosaic.extensions must include at least one extension.")
    if config.target_crs_label and not config.target_crs_label.replace("_", "").isalnum():
        raise ValueError(
            "mosaic.target_crs_label may contain only letters, numbers, and underscores."
        )
    if config.workers < 1:
        raise ValueError("mosaic.workers must be at least 1.")


def normalize_crs_label(value: Any) -> str:
    """Normalize the optional common-CRS label used in output filenames."""
    return str(value or "").strip().upper()


def normalize_workers(value: Any) -> int:
    """Normalize optional worker count; 1 preserves sequential processing."""
    if value in (None, ""):
        return 1
    return max(1, int(value))


def collect_input_files(config: MosaicConfig) -> List[Path]:
    """Return sorted GeoTIFF inputs, excluding the configured output folder."""
    extensions = {ext.lower() for ext in config.extensions}
    globber = config.input_folder.rglob if config.recursive else config.input_folder.glob
    output_folder = config.output_folder.resolve()
    files: List[Path] = []
    for path in globber("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        resolved = path.resolve()
        if is_relative_to(resolved, output_folder):
            continue
        files.append(resolved)
    return sorted(files)


def is_relative_to(path: Path, parent: Path) -> bool:
    """Return True when path is inside parent, compatible with older Python APIs."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def group_key(metadata: ParsedMetadata, config: MosaicConfig) -> MosaicGroupKey:
    """Return the mosaic grouping key for parsed SWOT metadata."""
    fields = metadata.fields
    if config.grouping_mode == GROUPING_MODE_PASS_DATE_COMMON_CRS:
        coordinate_system = normalize_crs_label(config.target_crs_label) or DEFAULT_COMMON_CRS_LABEL
        descriptor = descriptor_for_common_crs(fields, coordinate_system)
    elif config.grouping_mode == GROUPING_MODE_UTM_ZONE_HEMISPHERE:
        coordinate_system = utm_zone_hemisphere_token(fields["coordinate_system"])
        descriptor = descriptor_for_coordinate_system(fields, coordinate_system)
    else:
        coordinate_system = fields["coordinate_system"]
        descriptor = fields["descriptor"]
    return MosaicGroupKey(
        descriptor=descriptor,
        cycle_id=fields["cycle_id"],
        pass_id=fields["pass_id"],
        start_date=fields["range_beginning"][:8],
        coordinate_system=coordinate_system,
    )


def utm_zone_hemisphere_token(coordinate_system: str) -> str:
    """Collapse MGRS latitude bands to UTM zone plus hemisphere."""
    match = UTM_TOKEN_RE.match(coordinate_system)
    if match is None:
        return coordinate_system
    zone = int(match.group("zone"))
    band = match.group("band")
    hemisphere = "N" if band >= "N" else "S"
    return f"UTM{zone:02d}{hemisphere}"


def descriptor_for_coordinate_system(fields: Dict[str, str], coordinate_system: str) -> str:
    """Return a SWOT-compatible descriptor with a replacement coordinate token."""
    descriptor_parts = fields["descriptor"].split("_")
    if len(descriptor_parts) < 2:
        return f"{fields.get('grid_resolution', 'unknown')}_{coordinate_system}"
    return "_".join(
        [descriptor_parts[0], coordinate_system, *descriptor_parts[2:]]
    )


def descriptor_for_common_crs(fields: Dict[str, str], coordinate_system: str) -> str:
    """Return a SWOT-compatible descriptor with a common CRS token."""
    return descriptor_for_coordinate_system(fields, coordinate_system)


def build_mosaic_plan(config: MosaicConfig) -> MosaicPlan:
    """Scan inputs, parse SWOT names, and create grouped mosaic outputs."""
    grouped: Dict[MosaicGroupKey, List[MosaicSource]] = {}
    report_rows: List[Dict[str, str]] = []
    selected_tiles = set(config.utm_tiles)

    for file_path in collect_input_files(config):
        try:
            metadata = parse_swot_l2_hr_raster_metadata(file_path)
        except ValueError as exc:
            report_rows.append(
                report_row(
                    status="INVALID_FILENAME",
                    message=f"Invalid SWOT timestamp: {exc}",
                    input_files=[file_path],
                )
            )
            continue
        if metadata is None:
            report_rows.append(
                report_row(
                    status="INVALID_FILENAME",
                    message="Filename does not match SWOT L2 HR Raster pattern.",
                    input_files=[file_path],
                )
            )
            continue
        source_tile = str(metadata.fields.get("coordinate_system", "") or "").upper()
        if selected_tiles and source_tile not in selected_tiles:
            continue
        grouped.setdefault(group_key(metadata, config), []).append(
            MosaicSource(path=file_path, metadata=metadata)
        )

    groups = []
    for key, sources in sorted(grouped.items(), key=lambda item: group_sort_key(item[0])):
        sorted_sources = sorted(sources, key=lambda source: source.path.name.lower())
        groups.append(
            MosaicGroup(
                key=key,
                sources=sorted_sources,
                output_file=config.output_folder / f"{build_output_stem(sorted_sources, key)}.tif",
            )
        )
    return MosaicPlan(groups=groups, report_rows=report_rows)


def mosaic_record_id(group: MosaicGroup) -> str:
    """Return the stable manifest key for one planned mosaic output."""
    return group.output_file.name


def group_source_signature(group: MosaicGroup) -> str:
    """Return a signature for the current source membership of a mosaic group."""
    return source_signature([source.path for source in group.sources])


def read_mosaic_manifest(path: Path) -> Dict[str, Dict[str, str]]:
    """Load the cumulative mosaic manifest keyed by output file name."""
    rows: Dict[str, Dict[str, str]] = {}
    for row in read_project_rows(path, "mosaic_manifest"):
        key = Path(row.get("output_file", "")).name
        if key:
            rows[key] = {column: row.get(column, "") for column in REPORT_COLUMNS}
    return rows


def mosaic_manifest_completed(row: Dict[str, str] | None) -> bool:
    """Return True when a mosaic manifest row records completed work."""
    if not row:
        return False
    return row.get("status", "") in {
        "MOSAIC_CREATED",
        "MOSAIC_CREATED_WITH_EXCLUSIONS",
        "COPIED_SINGLETON",
        "SKIPPED_EXISTS",
        "SKIPPED_MANIFEST",
    }


def add_mosaic_tracking_fields(
    row: Dict[str, str],
    group: MosaicGroup,
    signature: str,
    *,
    known_from_manifest: bool = False,
    stale: bool = False,
) -> Dict[str, str]:
    """Attach manifest/ledger tracking fields to one mosaic row."""
    row["source_signature"] = signature
    row["output_exists"] = "yes" if group.output_file.exists() else "no"
    row["known_from_manifest"] = "yes" if known_from_manifest else "no"
    row["stale"] = "true" if stale else "false"
    row["updated_at"] = row.get("updated_at") or timestamp_text()
    return row


def write_mosaic_manifest(config: MosaicConfig, rows: Iterable[Dict[str, str]]) -> None:
    """Merge mosaic run rows into the cumulative mosaic manifest."""
    existing = read_mosaic_manifest(config.manifest_csv)
    for row in rows:
        key = Path(row.get("output_file", "")).name
        if not key:
            continue
        previous = existing.get(key, {column: "" for column in REPORT_COLUMNS})
        previous.update(row)
        previous["updated_at"] = timestamp_text()
        existing[key] = previous
    upsert_project_rows(
        config.manifest_csv,
        existing.values(),
        dataset="mosaic_manifest",
        export_csv=True,
        fieldnames=REPORT_COLUMNS,
    )


def write_mosaic_workflow_manifest(config: MosaicConfig, rows: Iterable[Dict[str, str]]) -> None:
    """Update the shared workflow manifest with mosaic rows."""
    workflow_rows: List[Dict[str, Any]] = []
    for row in rows:
        output_path = Path(row.get("output_file", ""))
        workflow_rows.append(
            {
                "stage": "mosaic",
                "record_id": output_path.name,
                "record_type": "swot_mosaic",
                "status": row.get("status", ""),
                "source_path": row.get("input_files", ""),
                "output_path": row.get("output_file", ""),
                "date": row.get("start_date", ""),
                "start_time": row.get("range_beginning", ""),
                "end_time": row.get("range_ending", ""),
                "cycle_id": row.get("cycle_id", ""),
                "pass_id": row.get("pass_id", ""),
                "scene_id": "MOSA",
                "coordinate_system": row.get("coordinate_system", ""),
                "grouping_mode": config.grouping_mode,
                "source_signature": row.get("source_signature", ""),
                "input_count": row.get("input_count", ""),
                "output_exists": row.get("output_exists", ""),
                "known_from_stage_manifest": row.get("known_from_manifest", "no"),
                "message": row.get("message", ""),
            }
        )
    upsert_workflow_manifest(workflow_manifest_path(config.report_csv), workflow_rows)


def group_sort_key(key: MosaicGroupKey) -> Tuple[str, str, str, str, str]:
    """Return a stable sortable tuple for group keys."""
    return (
        key.descriptor,
        key.cycle_id,
        key.pass_id,
        key.start_date,
        key.coordinate_system,
    )


def build_output_stem(sources: Sequence[MosaicSource], key: MosaicGroupKey) -> str:
    """Build a SWOT-compatible output stem for a mosaic group."""
    if not sources:
        raise ValueError("Cannot build an output name for an empty mosaic group.")
    fields = [source.metadata.fields for source in sources]
    first = fields[0]
    range_beginning = min(field["range_beginning"] for field in fields)
    range_ending = max(field["range_ending"] for field in fields)
    crids = {field["crid"] for field in fields}
    crid = next(iter(crids)) if len(crids) == 1 else "MIXD"
    return (
        "SWOT_L2_HR_Raster_"
        f"{key.descriptor}_"
        f"{key.cycle_id}_"
        f"{key.pass_id}_"
        f"MOSA_"
        f"{range_beginning}_"
        f"{range_ending}_"
        f"{crid}_"
        "01"
    )


def preflight_mosaic_group(
    group: MosaicGroup,
    config: MosaicConfig,
    existing_manifest: Dict[str, Dict[str, str]],
    dry_run: bool,
) -> Tuple[Optional[Dict[str, str]], str]:
    """Return a non-writing report row for dry-run/skipped groups, or None."""
    signature = group_source_signature(group)
    manifest_row = existing_manifest.get(mosaic_record_id(group))
    previous_signature = (manifest_row or {}).get("source_signature", "")
    if dry_run:
        status = "PLANNED_COPY" if len(group.sources) == 1 else "PLANNED_MOSAIC"
        return (
            add_mosaic_tracking_fields(
                report_row_for_group(
                    group,
                    status,
                    "Dry run only. No output written.",
                ),
                group,
                signature,
            ),
            signature,
        )

    if group.output_file.exists() and not config.overwrite:
        stale = bool(previous_signature and previous_signature != signature)
        status = "STALE_EXISTS" if stale else "SKIPPED_EXISTS"
        message = (
            "Output exists, but the current source set differs from the recorded mosaic manifest. "
            "Enable overwrite or remove the output to rebuild."
            if stale
            else "Output exists and overwrite is disabled."
        )
        return (
            add_mosaic_tracking_fields(
                report_row_for_group(group, status, message),
                group,
                signature,
                stale=stale,
            ),
            signature,
        )

    if (
        config.skip_manifest_existing
        and mosaic_manifest_completed(manifest_row)
        and previous_signature == signature
    ):
        return (
            add_mosaic_tracking_fields(
                report_row_for_group(
                    group,
                    "SKIPPED_MANIFEST",
                    "Mosaic output is already recorded in the cumulative mosaic manifest.",
                ),
                group,
                signature,
                known_from_manifest=True,
            ),
            signature,
        )

    return None, signature


def execute_mosaic_group(group: MosaicGroup, config: MosaicConfig, signature: str) -> Dict[str, str]:
    """Write one mosaic output and return its report row."""
    try:
        selected_sources, exclusions = select_compatible_sources(group.sources)
        if not selected_sources:
            return add_mosaic_tracking_fields(
                report_row_for_group(
                    group,
                    "SKIPPED_INCOMPATIBLE",
                    exclusion_message(exclusions, len(group.sources)),
                    exclusions=exclusions,
                    original_input_count=len(group.sources),
                ),
                group,
                signature,
            )

        effective_group = group
        effective_signature = signature
        if exclusions:
            effective_group = MosaicGroup(
                key=group.key,
                sources=selected_sources,
                output_file=group.output_file.with_name(
                    f"{build_output_stem(selected_sources, group.key)}.tif"
                ),
            )
            effective_signature = group_source_signature(effective_group)

        if len(effective_group.sources) == 1:
            copy_singleton(effective_group, config)
            status = "MOSAIC_CREATED_WITH_EXCLUSIONS" if exclusions else "COPIED_SINGLETON"
            message = (
                partial_mosaic_message(exclusions, len(group.sources))
                if exclusions
                else "Single-file group written to mosaic output folder."
            )
            return add_mosaic_tracking_fields(
                report_row_for_group(
                    effective_group,
                    status,
                    message,
                    exclusions=exclusions,
                    original_input_count=len(group.sources),
                ),
                effective_group,
                effective_signature,
            )
        merge_group(effective_group, config)
        status = "MOSAIC_CREATED_WITH_EXCLUSIONS" if exclusions else "MOSAIC_CREATED"
        message = (
            partial_mosaic_message(exclusions, len(group.sources))
            if exclusions
            else "Merged with current wse priority and wse_qual fallback over class 3."
        )
        return add_mosaic_tracking_fields(
            report_row_for_group(
                effective_group,
                status,
                message,
                exclusions=exclusions,
                original_input_count=len(group.sources),
            ),
            effective_group,
            effective_signature,
        )
    except IncompatibleRasterGroupError as exc:
        return add_mosaic_tracking_fields(
            report_row_for_group(group, "SKIPPED_INCOMPATIBLE", str(exc)),
            group,
            signature,
        )
    except Exception as exc:  # noqa: BLE001
        status = ERROR_DISK_SPACE_STATUS if is_disk_space_error(exc) else "ERROR"
        message = str(exc)
        if status == ERROR_DISK_SPACE_STATUS:
            message = (
                f"{message} Mosaic run stopped early because this looks like a disk-space "
                "or low-level GeoTIFF write failure. Free space, then rerun Mosaic with "
                "overwrite off and manifest skipping on."
            )
        cleanup_temporary_output(group.output_file)
        return add_mosaic_tracking_fields(
            report_row_for_group(group, status, message),
            group,
            signature,
        )


def execute_mosaic_group_worker(
    index: int,
    group: MosaicGroup,
    config: MosaicConfig,
    signature: str,
) -> Tuple[int, Dict[str, str]]:
    """Write one mosaic output in a child process."""
    return index, execute_mosaic_group(group, config, signature)


def run_mosaic(
    config: MosaicConfig,
    dry_run: bool = False,
    progress_callback: Optional[ProgressCallback] = None,
) -> Tuple[int, List[Dict[str, str]]]:
    """Plan and optionally write all mosaic outputs."""
    plan = build_mosaic_plan(config)
    rows = list(plan.report_rows)
    group_rows: List[Tuple[int, Dict[str, str]]] = []
    existing_manifest = read_mosaic_manifest(config.manifest_csv)
    total = len(plan.groups)
    if progress_callback is not None:
        if dry_run:
            message = "Starting mosaic planning"
        elif config.workers <= 1:
            message = "Starting mosaic run (sequential)"
        else:
            message = f"Starting mosaic run ({config.workers} workers)"
        progress_callback(0, total, message)

    completed = 0
    work_items: List[Tuple[int, MosaicGroup, str]] = []
    stopped_for_disk = False
    for index, group in enumerate(plan.groups, start=1):
        if progress_callback is not None:
            progress_callback(completed, total, f"Planning {group.output_file.name}")
        row, signature = preflight_mosaic_group(group, config, existing_manifest, dry_run)
        if row is not None:
            group_rows.append((index, row))
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total, f"{row['status']}: {group.output_file.name}")
            continue
        work_items.append((index, group, signature))

    if not dry_run and work_items:
        if config.workers <= 1:
            for index, group, signature in work_items:
                if progress_callback is not None:
                    progress_callback(completed, total, f"Processing {group.output_file.name}")
                row = execute_mosaic_group(group, config, signature)
                group_rows.append((index, row))
                completed += 1
                if progress_callback is not None:
                    progress_callback(completed, total, f"{row['status']}: {group.output_file.name}")
                if row["status"] == ERROR_DISK_SPACE_STATUS:
                    stopped_for_disk = True
                    break
        else:
            workers = min(config.workers, len(work_items))
            next_item = 0
            active: Dict[Any, Tuple[int, MosaicGroup]] = {}
            with ProcessPoolExecutor(max_workers=workers) as executor:
                while next_item < len(work_items) and len(active) < workers:
                    index, group, signature = work_items[next_item]
                    future = executor.submit(execute_mosaic_group_worker, index, group, config, signature)
                    active[future] = (index, group)
                    next_item += 1

                while active:
                    done, _pending = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        index, group = active.pop(future)
                        try:
                            result_index, row = future.result()
                        except Exception as exc:  # noqa: BLE001
                            row = add_mosaic_tracking_fields(
                                report_row_for_group(group, "ERROR", f"{type(exc).__name__}: {exc}"),
                                group,
                                group_source_signature(group),
                            )
                            result_index = index
                            cleanup_temporary_output(group.output_file)
                        group_rows.append((result_index, row))
                        completed += 1
                        if progress_callback is not None:
                            progress_callback(completed, total, f"{row['status']}: {group.output_file.name}")
                        if row["status"] == ERROR_DISK_SPACE_STATUS:
                            stopped_for_disk = True

                    while not stopped_for_disk and next_item < len(work_items) and len(active) < workers:
                        index, group, signature = work_items[next_item]
                        future = executor.submit(execute_mosaic_group_worker, index, group, config, signature)
                        active[future] = (index, group)
                        next_item += 1

    rows.extend(row for _index, row in sorted(group_rows, key=lambda item: item[0]))
    write_report(config.report_csv, rows)
    write_mixed_crid_report(config.mixed_crid_report_csv, rows)
    if not dry_run:
        write_mosaic_manifest(config, rows)
        write_mosaic_workflow_manifest(config, rows)
    return summarize_exit_code(rows), rows


class IncompatibleRasterGroupError(Exception):
    """Raised when files in one metadata group cannot be safely merged."""


def raster_signature(source: MosaicSource) -> Tuple[Dict[str, Any], str]:
    """Return a compatibility signature for one raster source."""
    gdal = require_gdal()
    dataset = gdal.Open(str(source.path))
    if dataset is None:
        raise IncompatibleRasterGroupError(f"{source.path.name} could not be opened by GDAL.")

    try:
        transform = dataset.GetGeoTransform(can_return_null=True)
        projection = dataset.GetProjectionRef()
        if transform is None:
            raise IncompatibleRasterGroupError(f"{source.path.name} has no geotransform.")
        if not projection:
            raise IncompatibleRasterGroupError(f"{source.path.name} has no projection.")
        try:
            transform_values = tuple(float(value) for value in transform)
        except (TypeError, ValueError) as exc:
            raise IncompatibleRasterGroupError(
                f"{source.path.name} has invalid geotransform values: {transform}."
            ) from exc
        if len(transform_values) != 6 or not all(math.isfinite(value) for value in transform_values):
            raise IncompatibleRasterGroupError(
                f"{source.path.name} has invalid geotransform values: {transform}."
            )
        if (
            transform_values[2] != 0
            or transform_values[4] != 0
            or transform_values[1] <= 0
            or transform_values[5] >= 0
        ):
            raise IncompatibleRasterGroupError(
                f"{source.path.name} is not a north-up raster with standard orientation."
            )

        pixel_width = abs(transform_values[1])
        pixel_height = abs(transform_values[5])
        if pixel_width < 1e-12 or pixel_height < 1e-12:
            raise IncompatibleRasterGroupError(
                f"{source.path.name} has invalid pixel size in geotransform: {transform}."
            )
        if pixel_width > 1_000_000 or pixel_height > 1_000_000:
            raise IncompatibleRasterGroupError(
                f"{source.path.name} has unrealistic pixel size in geotransform: {transform}."
            )
        if abs(transform_values[0]) > 1e15 or abs(transform_values[3]) > 1e15:
            raise IncompatibleRasterGroupError(
                f"{source.path.name} has unrealistic origin in geotransform: {transform}."
            )

        signature = {
            "count": dataset.RasterCount,
            "dtypes": tuple(
                dataset.GetRasterBand(index).DataType
                for index in range(1, dataset.RasterCount + 1)
            ),
            "projection": projection,
            "res": (round(pixel_width, 12), round(pixel_height, 12)),
            "nodatavals": tuple(
                dataset.GetRasterBand(index).GetNoDataValue()
                for index in range(1, dataset.RasterCount + 1)
            ),
        }
    finally:
        dataset = None
    return signature, signature_key(signature)


def signature_key(signature: Mapping[str, Any]) -> str:
    """Return a stable key for grouping compatible raster signatures."""
    return json.dumps(
        {
            "count": signature["count"],
            "dtypes": list(signature["dtypes"]),
            "projection": signature["projection"],
            "res": list(signature["res"]),
            "nodatavals": list(signature["nodatavals"]),
        },
        sort_keys=True,
        default=str,
    )


def signature_mismatches(signature: Mapping[str, Any], baseline: Mapping[str, Any]) -> List[str]:
    """Return signature fields that differ from the selected compatible group."""
    return [key for key, value in signature.items() if value != baseline[key]]


def select_compatible_sources(
    sources: Sequence[MosaicSource],
) -> Tuple[List[MosaicSource], List[SourceExclusion]]:
    """Keep the largest compatible source cluster and report excluded inputs."""
    groups: Dict[str, List[Tuple[MosaicSource, Dict[str, Any]]]] = {}
    order: List[str] = []
    exclusions: List[SourceExclusion] = []
    for source in sources:
        try:
            signature, key = raster_signature(source)
        except IncompatibleRasterGroupError as exc:
            exclusions.append(SourceExclusion(source=source, reason=str(exc)))
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((source, signature))

    if not groups:
        return [], exclusions

    largest_group_size = max(len(group) for group in groups.values())
    largest_keys = [key for key in order if len(groups[key]) == largest_group_size]
    if len(largest_keys) > 1:
        for key in largest_keys:
            for source, _signature in groups[key]:
                exclusions.append(
                    SourceExclusion(
                        source=source,
                        reason=(
                            f"{source.path.name} belongs to a tied incompatible source group; "
                            "no dominant compatible group could be selected."
                        ),
                    )
                )
        return [], exclusions

    selected_key = largest_keys[0]
    selected_group = groups[selected_key]
    selected_signature = selected_group[0][1]
    selected_sources = [source for source, _signature in selected_group]
    for key in order:
        if key == selected_key:
            continue
        for source, signature in groups[key]:
            mismatches = ", ".join(signature_mismatches(signature, selected_signature))
            exclusions.append(
                SourceExclusion(
                    source=source,
                    reason=f"{source.path.name} differs from selected compatible sources in: {mismatches}.",
                )
            )
    return selected_sources, exclusions


def exclusion_message(exclusions: Sequence[SourceExclusion], original_count: int) -> str:
    """Return a compact message for a fully incompatible group."""
    reasons = [f"{item.source.path.name}: {item.reason}" for item in exclusions[:5]]
    extra = "" if len(exclusions) <= 5 else f" +{len(exclusions) - 5} more."
    return (
        f"No compatible mosaic inputs remained from {original_count} source file(s). "
        f"Excluded sources: {' | '.join(reasons)}{extra}"
    )


def partial_mosaic_message(exclusions: Sequence[SourceExclusion], original_count: int) -> str:
    """Return a compact message for a mosaic created after excluding bad sources."""
    kept = original_count - len(exclusions)
    names = ", ".join(item.source.path.name for item in exclusions[:5])
    extra = "" if len(exclusions) <= 5 else f", +{len(exclusions) - 5} more"
    return (
        f"Created from {kept} of {original_count} compatible source file(s); "
        f"excluded {len(exclusions)} invalid/incompatible source(s): {names}{extra}."
    )


def require_gdal() -> Any:
    """Import GDAL lazily so dry-run planning can run outside the GDAL runtime."""
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError(
            "GDAL is required to write mosaics. Run this tool with the configured "
            "conda GDAL Python, for example D:\\SWOT\\conda_envs\\swot_gdal\\python.exe."
        ) from exc
    gdal.UseExceptions()
    return gdal


def validate_raster_group(sources: Sequence[MosaicSource]) -> None:
    """Ensure a group is compatible before GDAL VRT mosaicking is called."""
    baseline: Optional[Dict[str, Any]] = None
    for source in sources:
        signature, _key = raster_signature(source)

        if baseline is None:
            baseline = signature
            continue
        mismatches = [
            key
            for key, value in signature.items()
            if value != baseline[key]
        ]
        if mismatches:
            raise IncompatibleRasterGroupError(
                f"{source.path.name} differs from the first file in: {', '.join(mismatches)}."
            )


def geotiff_creation_options(config: MosaicConfig) -> List[str]:
    """Return GDAL GeoTIFF creation options for mosaic outputs."""
    options = ["BIGTIFF=IF_SAFER"]
    if config.write_world_file:
        options.append("TFW=YES")
    return options


def temporary_mosaic_path(output_file: Path) -> Path:
    """Return a same-folder temporary GeoTIFF path for one final mosaic."""
    return output_file.with_name(f"{output_file.stem}.part{output_file.suffix}")


def world_file_path(path: Path) -> Path:
    """Return the standard world-file sidecar path for a GeoTIFF."""
    return path.with_suffix(".tfw")


def cleanup_temporary_output(output_file: Path) -> None:
    """Remove temporary mosaic files left after a failed write."""
    temp_file = temporary_mosaic_path(output_file)
    for path in (
        temp_file,
        world_file_path(temp_file),
        temp_file.with_suffix(f"{temp_file.suffix}.aux.xml"),
    ):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def promote_temporary_output(temp_file: Path, output_file: Path, config: MosaicConfig) -> None:
    """Atomically promote a completed temporary GeoTIFF to the final output name."""
    temp_world = world_file_path(temp_file)
    final_world = world_file_path(output_file)
    temp_aux = temp_file.with_suffix(f"{temp_file.suffix}.aux.xml")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file.replace(output_file)
    if temp_world.exists():
        temp_world.replace(final_world)
    elif config.write_world_file:
        try:
            if final_world.exists():
                final_world.unlink()
        except OSError:
            pass
    try:
        if temp_aux.exists():
            temp_aux.unlink()
    except OSError:
        pass


def is_disk_space_error(exc: BaseException) -> bool:
    """Return True when an exception looks like disk exhaustion or write failure."""
    message = str(exc).lower()
    return any(pattern in message for pattern in DISK_SPACE_ERROR_PATTERNS)


def apply_output_band_metadata(dataset: Any) -> None:
    """Restore expected band descriptions on extraction-style mosaic outputs."""
    if dataset.RasterCount >= 1:
        dataset.GetRasterBand(1).SetDescription("wse")
    if dataset.RasterCount >= 2:
        dataset.GetRasterBand(2).SetDescription("wse_qual")


def copy_singleton(group: MosaicGroup, config: MosaicConfig) -> None:
    """Write a single-file group to the output folder using the mosaic output name."""
    gdal = require_gdal()

    config.output_folder.mkdir(parents=True, exist_ok=True)
    temp_output = temporary_mosaic_path(group.output_file)
    cleanup_temporary_output(group.output_file)
    source_ds = gdal.Open(str(group.sources[0].path))
    if source_ds is None:
        raise RuntimeError(f"Could not open singleton input: {group.sources[0].path.name}")
    out_ds = None
    promoted = False
    try:
        translate_options = gdal.TranslateOptions(
            format="GTiff",
            creationOptions=geotiff_creation_options(config),
        )
        out_ds = gdal.Translate(str(temp_output), source_ds, options=translate_options)
        source_ds = None
        if out_ds is None:
            raise RuntimeError(f"Could not write singleton output: {group.output_file}")
        apply_output_band_metadata(out_ds)
        out_ds.FlushCache()
        out_ds = None
        promote_temporary_output(temp_output, group.output_file, config)
        promoted = True
    finally:
        source_ds = None
        if out_ds is not None:
            out_ds = None
        if not promoted:
            cleanup_temporary_output(group.output_file)


def require_numpy() -> tuple[Any, Any]:
    """Import NumPy and GDAL array helpers lazily from the GDAL runtime."""
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required for wse_qual overlap handling in mosaics. "
            "Install it in the GDAL conda environment."
        ) from exc
    try:
        from osgeo import gdal_array
    except ImportError as exc:
        raise RuntimeError(
            "osgeo.gdal_array is required for wse_qual overlap handling in mosaics."
        ) from exc
    return np, gdal_array


def dataset_bounds(dataset: Any) -> Tuple[float, float, float, float]:
    """Return north-up dataset bounds as minx, miny, maxx, maxy."""
    transform = dataset.GetGeoTransform()
    min_x = transform[0]
    max_y = transform[3]
    max_x = min_x + dataset.RasterXSize * transform[1]
    min_y = max_y + dataset.RasterYSize * transform[5]
    return min_x, min_y, max_x, max_y


def build_aligned_source_vrts(
    group: MosaicGroup,
    output_dataset: Any,
    gdal: Any,
) -> Tuple[List[Any], List[str]]:
    """Align each source to the final mosaic grid so band 2 can be selected per pixel."""
    min_x, min_y, max_x, max_y = dataset_bounds(output_dataset)
    aligned_datasets: List[Any] = []
    vrt_paths: List[str] = []
    for index, source in enumerate(group.sources):
        vrt_path = f"/vsimem/{group.output_file.stem}_aligned_{index}.vrt"
        options = gdal.WarpOptions(
            format="VRT",
            outputBounds=[min_x, min_y, max_x, max_y],
            width=output_dataset.RasterXSize,
            height=output_dataset.RasterYSize,
            resampleAlg="near",
        )
        dataset = gdal.Warp(vrt_path, str(source.path), options=options)
        if dataset is None:
            raise RuntimeError(f"Could not align source for band-2 merge: {source.path.name}")
        aligned_datasets.append(dataset)
        vrt_paths.append(vrt_path)
    return aligned_datasets, vrt_paths


def valid_data_mask(array: Any, nodata: Any, np: Any) -> Any:
    """Return a boolean mask of valid values for one raster block."""
    if nodata is None:
        return np.ones(array.shape, dtype=bool)
    if isinstance(nodata, float) and np.isnan(nodata):
        return ~np.isnan(array)
    return array != nodata


def rewrite_quality_band(
    output_dataset: Any,
    aligned_sources: Sequence[Any],
    band1_nodata: Any,
    band2_nodata: Any,
) -> None:
    """Overwrite output band 2 using wse-aware overlap rules."""
    np, gdal_array = require_numpy()

    output_band = output_dataset.GetRasterBand(2)
    block_cols, block_rows = output_band.GetBlockSize()
    if block_cols <= 0:
        block_cols = min(512, output_dataset.RasterXSize)
    if block_rows <= 0:
        block_rows = min(512, output_dataset.RasterYSize)

    quality_dtype = gdal_array.GDALTypeCodeToNumericTypeCode(output_band.DataType)
    if quality_dtype is None:
        raise RuntimeError("Could not map output band 2 GDAL data type to NumPy.")

    for y_off in range(0, output_dataset.RasterYSize, block_rows):
        rows = min(block_rows, output_dataset.RasterYSize - y_off)
        for x_off in range(0, output_dataset.RasterXSize, block_cols):
            cols = min(block_cols, output_dataset.RasterXSize - x_off)
            quality_block = np.full((rows, cols), band2_nodata, dtype=quality_dtype)
            assigned = np.zeros((rows, cols), dtype=bool)

            for source_dataset in aligned_sources:
                source_wse = source_dataset.GetRasterBand(1).ReadAsArray(x_off, y_off, cols, rows)
                source_quality = source_dataset.GetRasterBand(2).ReadAsArray(
                    x_off,
                    y_off,
                    cols,
                    rows,
                )
                if source_wse is None or source_quality is None:
                    raise RuntimeError("Could not read aligned source block for quality merge.")
                mask = (
                    (~assigned)
                    & valid_data_mask(source_wse, band1_nodata, np)
                    & np.isin(source_quality, (0, 1, 2))
                )
                if np.any(mask):
                    quality_block[mask] = source_quality[mask]
                    assigned[mask] = True

            if not np.all(assigned):
                for source_dataset in aligned_sources:
                    source_wse = source_dataset.GetRasterBand(1).ReadAsArray(
                        x_off,
                        y_off,
                        cols,
                        rows,
                    )
                    source_quality = source_dataset.GetRasterBand(2).ReadAsArray(
                        x_off,
                        y_off,
                        cols,
                        rows,
                    )
                    if source_wse is None or source_quality is None:
                        raise RuntimeError("Could not read aligned source block for quality merge.")
                    mask = (
                        (~assigned)
                        & valid_data_mask(source_wse, band1_nodata, np)
                        & (source_quality == 3)
                    )
                    if np.any(mask):
                        quality_block[mask] = source_quality[mask]
                        assigned[mask] = True

            output_band.WriteArray(quality_block, x_off, y_off)

    apply_output_band_metadata(output_dataset)
    output_band.FlushCache()


def merge_group(group: MosaicGroup, config: MosaicConfig) -> None:
    """Merge a compatible multi-file group into one GeoTIFF."""
    gdal = require_gdal()

    config.output_folder.mkdir(parents=True, exist_ok=True)
    temp_output = temporary_mosaic_path(group.output_file)
    cleanup_temporary_output(group.output_file)
    vrt_path = f"/vsimem/{group.output_file.stem}.vrt"
    source_paths = [str(source.path) for source in reversed(group.sources)]
    vrt_ds = None
    output_dataset = None
    aligned_sources: List[Any] = []
    aligned_vrt_paths: List[str] = []
    promoted = False
    try:
        vrt_ds = gdal.BuildVRT(vrt_path, source_paths)
        if vrt_ds is None:
            raise RuntimeError(f"Could not build VRT for {group.output_file.name}")
        translate_options = gdal.TranslateOptions(
            format="GTiff",
            creationOptions=geotiff_creation_options(config),
        )
        output_dataset = gdal.Translate(str(temp_output), vrt_ds, options=translate_options)
        if output_dataset is None:
            raise RuntimeError(f"Could not write mosaic: {group.output_file}")
        apply_output_band_metadata(output_dataset)
        output_dataset.FlushCache()
        output_dataset = None

        output_dataset = gdal.Open(str(temp_output), gdal.GA_Update)
        if output_dataset is None:
            raise RuntimeError(f"Could not reopen mosaic for quality-band rewrite: {group.output_file}")
        if output_dataset.RasterCount < 2:
            apply_output_band_metadata(output_dataset)
            output_dataset.FlushCache()
            output_dataset = None
            promote_temporary_output(temp_output, group.output_file, config)
            promoted = True
            return

        band1 = output_dataset.GetRasterBand(1)
        band2 = output_dataset.GetRasterBand(2)
        band1_nodata = band1.GetNoDataValue()
        band2_nodata = band2.GetNoDataValue()
        if band2_nodata is None:
            band2_nodata = 255
            band2.SetNoDataValue(band2_nodata)

        aligned_sources, aligned_vrt_paths = build_aligned_source_vrts(
            group,
            output_dataset,
            gdal,
        )
        rewrite_quality_band(output_dataset, aligned_sources, band1_nodata, band2_nodata)
        output_dataset.FlushCache()
        output_dataset = None
        promote_temporary_output(temp_output, group.output_file, config)
        promoted = True
    finally:
        for index in range(len(aligned_sources)):
            aligned_sources[index] = None
        if output_dataset is not None:
            output_dataset = None
        vrt_ds = None
        try:
            gdal.Unlink(vrt_path)
        except Exception:
            pass
        for path in aligned_vrt_paths:
            try:
                gdal.Unlink(path)
            except Exception:
                pass
        if not promoted:
            cleanup_temporary_output(group.output_file)


def report_row_for_group(
    group: MosaicGroup,
    status: str,
    message: str,
    *,
    exclusions: Sequence[SourceExclusion] = (),
    original_input_count: Optional[int] = None,
) -> Dict[str, str]:
    """Build one report row for a grouped output."""
    fields = [source.metadata.fields for source in group.sources]
    diagnostics = crid_diagnostics(fields)
    return report_row(
        status=status,
        message=message,
        input_files=[source.path for source in group.sources],
        output_file=group.output_file,
        input_count=len(group.sources),
        original_input_count=original_input_count,
        excluded_input_files=[item.source.path for item in exclusions],
        excluded_reasons=[item.reason for item in exclusions],
        excluded_input_count=len(exclusions),
        cycle_id=group.key.cycle_id,
        pass_id=group.key.pass_id,
        start_date=group.key.start_date,
        coordinate_system=group.key.coordinate_system,
        descriptor=group.key.descriptor,
        range_beginning=min(field["range_beginning"] for field in fields),
        range_ending=max(field["range_ending"] for field in fields),
        crid=diagnostics["crid"],
        product_counter="01",
        mixed_crid=diagnostics["mixed_crid"],
        source_crids=diagnostics["source_crids"],
        source_product_counters=diagnostics["source_product_counters"],
        dominant_crid=diagnostics["dominant_crid"],
        preferred_crid=diagnostics["preferred_crid"],
    )


def crid_diagnostics(fields: Sequence[Dict[str, str]]) -> Dict[str, str]:
    """Return CRID summary values for a mosaic report row."""
    source_crids = [field.get("crid", "") for field in fields]
    source_counters = [field.get("product_counter", "") for field in fields]
    unique_crids = set(source_crids)
    crid = source_crids[0] if len(unique_crids) == 1 else "MIXD"

    counts = Counter(source_crids)
    dominant_candidates = [
        value for value, count in counts.items() if count == max(counts.values(), default=0)
    ]
    dominant_crid = max(dominant_candidates, key=lambda value: swot_product_rank(value, 0), default="")
    preferred_field = max(
        fields,
        key=lambda field: swot_product_rank(
            field.get("crid", ""),
            field.get("product_counter", ""),
        ),
        default={},
    )
    preferred_crid = preferred_field.get("crid", "")
    return {
        "crid": crid,
        "mixed_crid": "true" if len(unique_crids) > 1 else "false",
        "source_crids": json.dumps(source_crids),
        "source_product_counters": json.dumps(source_counters),
        "dominant_crid": dominant_crid,
        "preferred_crid": preferred_crid,
    }


def report_row(
    status: str,
    message: str,
    input_files: Sequence[Path],
    output_file: Path | None = None,
    input_count: int | None = None,
    original_input_count: int | None = None,
    excluded_input_count: int = 0,
    excluded_input_files: Sequence[Path] = (),
    excluded_reasons: Sequence[str] = (),
    cycle_id: str = "",
    pass_id: str = "",
    start_date: str = "",
    coordinate_system: str = "",
    descriptor: str = "",
    range_beginning: str = "",
    range_ending: str = "",
    crid: str = "",
    product_counter: str = "",
    mixed_crid: str = "",
    source_crids: str = "",
    source_product_counters: str = "",
    dominant_crid: str = "",
    preferred_crid: str = "",
    source_signature: str = "",
    output_exists: str = "",
    known_from_manifest: str = "no",
    stale: str = "false",
) -> Dict[str, str]:
    """Create a normalized report row."""
    return {
        "status": status,
        "output_file": "" if output_file is None else str(output_file),
        "input_count": str(input_count if input_count is not None else len(input_files)),
        "original_input_count": str(
            original_input_count if original_input_count is not None else input_count if input_count is not None else len(input_files)
        ),
        "excluded_input_count": str(excluded_input_count),
        "cycle_id": cycle_id,
        "pass_id": pass_id,
        "start_date": start_date,
        "coordinate_system": coordinate_system,
        "descriptor": descriptor,
        "range_beginning": range_beginning,
        "range_ending": range_ending,
        "crid": crid,
        "product_counter": product_counter,
        "mixed_crid": mixed_crid,
        "source_crids": source_crids,
        "source_product_counters": source_product_counters,
        "dominant_crid": dominant_crid,
        "preferred_crid": preferred_crid,
        "source_signature": source_signature,
        "output_exists": output_exists,
        "known_from_manifest": known_from_manifest,
        "stale": stale,
        "message": message,
        "excluded_input_files": json.dumps([str(path) for path in excluded_input_files]),
        "excluded_reasons": json.dumps([str(reason) for reason in excluded_reasons]),
        "input_files": json.dumps([str(path) for path in input_files]),
    }


def write_report(report_path: Path, rows: Iterable[Dict[str, str]]) -> None:
    """Write the mosaic CSV report."""
    materialized = list(rows)
    dataset = dataset_for_path(report_path)
    if dataset:
        upsert_project_rows(
            report_path,
            materialized,
            dataset=dataset,
            replace_dataset=dataset != "mosaic_manifest",
            export_csv=True,
            fieldnames=REPORT_COLUMNS,
        )
        return
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for row in materialized:
            writer.writerow({column: row.get(column, "") for column in REPORT_COLUMNS})


def write_mixed_crid_report(
    report_path: Optional[Path],
    rows: Iterable[Dict[str, str]],
) -> None:
    """Write a focused report containing only mixed-CRID mosaic groups."""
    if report_path is None:
        return
    mixed_rows = [row for row in rows if row.get("mixed_crid") == "true"]
    upsert_project_rows(
        report_path,
        mixed_rows,
        dataset="mixed_crid_mosaics",
        replace_dataset=True,
        export_csv=True,
        fieldnames=REPORT_COLUMNS,
    )


def summarize_exit_code(rows: Sequence[Dict[str, str]]) -> int:
    """Return a process exit code for report statuses."""
    failing = {"ERROR", "INVALID_FILENAME", "SKIPPED_INCOMPATIBLE", "STALE_EXISTS"}
    return 2 if any(row.get("status") in failing or row.get("status", "").startswith("ERROR_") for row in rows) else 0


def summarize_rows(rows: Sequence[Dict[str, str]]) -> Dict[str, int]:
    """Count report rows by status."""
    summary: Dict[str, int] = {}
    for row in rows:
        status = row.get("status", "")
        summary[status] = summary.get(status, 0) + 1
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Group and mosaic SWOT GeoTIFF files before Earth Engine upload."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml. Defaults to ./config.yaml.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only scan and report planned mosaic outputs.",
    )
    parser.add_argument(
        "--check-gdal",
        action="store_true",
        help="Validate that the current Python process can import GDAL and load required drivers.",
    )
    return parser


def print_progress(current: int, total: int, message: str) -> None:
    """Emit one machine-readable progress line for the GUI launcher."""
    safe_message = message.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    print(f"{PROGRESS_PREFIX}\tmosaic\t{current}\t{total}\t{safe_message}", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    args = build_arg_parser().parse_args(argv)
    if args.check_gdal:
        print(current_process_gdal_check(REQUIRED_GDAL_DRIVERS))
        return 0
    config = load_config_file(args.config.resolve())
    if not args.dry_run:
        print(current_process_gdal_check(REQUIRED_GDAL_DRIVERS))
    exit_code, rows = run_mosaic(config, dry_run=args.dry_run, progress_callback=print_progress)
    summary = summarize_rows(rows)
    mode = "dry run" if args.dry_run else "mosaic run"
    print(
        textwrap.dedent(
            f"""
            SWOT GeoTIFF {mode} complete.
            Input folder: {config.input_folder}
            Output folder: {config.output_folder}
            Grouping mode: {config.grouping_mode}
            Target CRS label: {config.target_crs_label or DEFAULT_COMMON_CRS_LABEL}
            Report CSV: {config.report_csv}
            Status counts: {summary}
            """
        ).strip()
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
