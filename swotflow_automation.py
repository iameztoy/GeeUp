"""Tile-by-tile unattended workflow orchestration for SWOTFlow projects."""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

import yaml

from gdal_runtime import build_gdal_runtime_env
from power_management import keep_system_awake, windows_automation_reboot_warnings
from project_database import read_project_rows
from project_insights import (
    CleanupCandidate,
    collect_project_insights,
    delete_cleanup_candidates,
    format_bytes,
    plan_cleanup_candidates,
)
from project_updates import record_update_preview
from project_updates import expected_rows as read_update_expected_rows
from project_updates import update_campaign_id
from swot_download_tool import (
    DEFAULT_CMR_REQUEST_TIMEOUT_SECONDS,
    DownloadConfig,
    DownloadGranule,
    DownloadPreview,
    build_download_query,
    build_download_preview,
    existing_download_path,
    load_config_file as load_download_config_file,
    manifest_has_downloaded,
    normalize_utm_tiles,
    read_download_manifest,
    run_download,
)
from swot_metadata import parse_swot_l2_hr_raster_metadata


PROJECT_ROOT = Path(__file__).resolve().parent
DOWNLOAD_SCRIPT = PROJECT_ROOT / "swot_download_tool.py"
DUPLICATE_SCRIPT = PROJECT_ROOT / "swot_duplicate_remover.py"
EXTRACT_SCRIPT = PROJECT_ROOT / "swot_extract_tool.py"
MOSAIC_SCRIPT = PROJECT_ROOT / "ee_mosaic_tool.py"
UPLOAD_SCRIPT = PROJECT_ROOT / "ee_ui_uploader.py"
AUTOMATION_RUN_COLUMNS = [
    "run_id",
    "tile",
    "stage",
    "status",
    "start_time",
    "end_time",
    "return_code",
    "message",
    "config_path",
    "log_path",
    "deleted_files",
    "deleted_bytes",
]
RETRYABLE_DOWNLOAD_FAILURE_MARKERS = (
    "cmr search failed",
    "cmr request failed",
    "read timed out",
    "internal error",
    "temporarily unavailable",
)


def is_retryable_download_message(message: str) -> bool:
    """Return True when a download/search message looks like a transient remote failure."""
    text = str(message or "").lower()
    return any(marker in text for marker in RETRYABLE_DOWNLOAD_FAILURE_MARKERS)


AUTOMATION_STAGES = [
    "download",
    "duplicates",
    "extract",
    "raw_cleanup",
    "mosaic",
    "extracted_cleanup",
    "upload",
    "ee_sync",
    "mosaic_cleanup",
]


ProgressCallback = Callable[[str, str, str, str], None]


@dataclass
class AutomationConfig:
    """Settings for one unattended automation run."""

    project_root: Path
    base_config: Dict[str, Any]
    utm_tiles: list[str]
    start_date: str
    end_date: str
    include_upload: bool = False
    cleanup_enabled: bool = True
    min_free_space_gb: float = 50.0
    continue_on_tile_failure: bool = True
    deferred_download_retry_passes: int = 1
    prevent_system_sleep: bool = True
    prevent_display_sleep: bool = False
    run_id: str = ""
    run_dir: Optional[Path] = None


@dataclass
class AutomationTilePlan:
    """Preflight classification for one requested UTM tile."""

    tile: str
    classification: str
    message: str
    matched_granules: int = 0
    selected_granules: int = 0
    pending_downloads: int = 0
    estimated_mosaic_groups: int = 0
    recorded_mosaic_groups: int = 0
    pending_mosaic_groups: int = 0
    excluded_older_versions: int = 0
    downloaded: int = 0
    extracted: int = 0
    mosaic_sources: int = 0
    submitted: int = 0
    uploaded: int = 0
    missing_upload: int = 0


@dataclass
class AutomationStageResult:
    """One executed or skipped automation stage."""

    run_id: str
    tile: str
    stage: str
    status: str
    start_time: str = ""
    end_time: str = ""
    return_code: int = 0
    message: str = ""
    config_path: str = ""
    log_path: str = ""
    deleted_files: int = 0
    deleted_bytes: int = 0


@dataclass
class AutomationRunState:
    """Complete preflight/run state written to JSON and CSV."""

    run_id: str
    run_dir: Path
    config: AutomationConfig
    tile_plans: list[AutomationTilePlan] = field(default_factory=list)
    stage_results: list[AutomationStageResult] = field(default_factory=list)
    preflight_ok: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    stopped: bool = False

    @property
    def csv_path(self) -> Path:
        return self.run_dir / "automation_run.csv"

    @property
    def json_path(self) -> Path:
        return self.run_dir / "automation_run.json"


def now_text() -> str:
    """Return a seconds-resolution timestamp."""
    return datetime.now().replace(microsecond=0).isoformat()


def run_id_text() -> str:
    """Return a filesystem-safe automation run id."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def automation_runs_root(base_config: Mapping[str, Any], project_root: Path) -> Path:
    """Return the folder that stores automation run artifacts."""
    processing = base_config.get("processing", {})
    logs = ""
    if isinstance(processing, Mapping):
        logs = str(processing.get("logs", "") or "")
    return Path(logs or project_root / "00_logs") / "automation_runs"


def ensure_run_state(config: AutomationConfig) -> AutomationRunState:
    """Create run id/folders and return an empty run state."""
    if not config.run_id:
        config.run_id = run_id_text()
    if config.run_dir is None:
        config.run_dir = automation_runs_root(config.base_config, config.project_root) / config.run_id
    config.run_dir.mkdir(parents=True, exist_ok=True)
    return AutomationRunState(
        run_id=config.run_id,
        run_dir=config.run_dir,
        config=config,
    )


def normalize_automation_config(config: AutomationConfig) -> AutomationConfig:
    """Return a validated copy of automation settings."""
    normalized = AutomationConfig(
        project_root=Path(config.project_root),
        base_config=deepcopy(config.base_config),
        utm_tiles=normalize_utm_tiles(config.utm_tiles),
        start_date=str(config.start_date or "").strip(),
        end_date=str(config.end_date or "").strip(),
        include_upload=bool(config.include_upload),
        cleanup_enabled=bool(config.cleanup_enabled),
        min_free_space_gb=float(config.min_free_space_gb),
        continue_on_tile_failure=bool(config.continue_on_tile_failure),
        deferred_download_retry_passes=max(0, int(config.deferred_download_retry_passes)),
        prevent_system_sleep=bool(config.prevent_system_sleep),
        prevent_display_sleep=bool(config.prevent_display_sleep),
        run_id=config.run_id,
        run_dir=Path(config.run_dir) if config.run_dir is not None else None,
    )
    if not normalized.project_root:
        raise ValueError("Automation requires an open project root.")
    if not normalized.utm_tiles:
        raise ValueError("Automation requires at least one UTM tile.")
    if not normalized.start_date or not normalized.end_date:
        raise ValueError("Automation requires a start date and end date.")
    return normalized


def project_manifest_path(config: Mapping[str, Any], section: str, key: str) -> Path:
    """Return a configured project manifest/report path."""
    data = config.get(section, {})
    if not isinstance(data, Mapping):
        return Path("")
    return Path(str(data.get(key, "") or ""))


def tile_run_dir(state: AutomationRunState, tile: str) -> Path:
    """Return the artifact folder for one tile inside an automation run."""
    path = state.run_dir / tile
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_tile_config_dict(
    base_config: Mapping[str, Any],
    tile: str,
    run_dir: Path,
    *,
    start_date: str,
    end_date: str,
    include_upload: bool,
) -> Dict[str, Any]:
    """Return a temporary config scoped to one tile and one automation run."""
    config = deepcopy(dict(base_config))
    tile_dir = run_dir / tile
    tile_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = tile_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    chrome = dict(config.get("chrome", {}))
    user_data_dir = str(chrome.get("user_data_dir", "") or "").strip()
    if user_data_dir:
        profile_path = Path(user_data_dir)
        if not profile_path.is_absolute():
            chrome["user_data_dir"] = str((PROJECT_ROOT / profile_path).resolve())
        config["chrome"] = chrome

    processing = dict(config.get("processing", {}))
    project_logs = Path(str(processing.get("logs", "") or run_dir.parent))
    project_raw = Path(str(processing.get("raw_downloads", "") or ""))
    project_extract = Path(str(processing.get("extracted_geotiffs", "") or ""))
    project_mosaic = Path(str(processing.get("mosaics", "") or ""))

    download = dict(config.get("download", {}))
    download.update(
        {
            "start_date": start_date,
            "end_date": end_date,
            "utm_tiles": [tile],
            "output_folder": str(project_raw),
            "report_csv": str(tile_dir / "download_preview.csv"),
            "manifest_csv": str(download.get("manifest_csv") or project_logs / "download_manifest.csv"),
            "skip_existing": True,
            "skip_manifest_existing": True,
        }
    )
    download.setdefault("search_request_timeout_seconds", DEFAULT_CMR_REQUEST_TIMEOUT_SECONDS)
    config["download"] = download

    duplicates = dict(config.get("duplicates", {}))
    duplicates.update(
        {
            "input_folder": str(project_raw),
            "log_folder": str(logs_dir),
            "utm_tiles": [tile],
        }
    )
    config["duplicates"] = duplicates

    extract = dict(config.get("extract", {}))
    extract.update(
        {
            "input_folder": str(project_raw),
            "output_folder": str(project_extract),
            "target_crs_mode": "original",
            "manifest_csv": str(extract.get("manifest_csv") or project_logs / "extract_manifest.csv"),
            "errors_csv": str(tile_dir / "extract_errors.csv"),
            "skip_existing": True,
            "skip_manifest_existing": True,
            "utm_tiles": [tile],
        }
    )
    config["extract"] = extract

    mosaic = dict(config.get("mosaic", {}))
    mosaic.update(
        {
            "input_folder": str(project_extract),
            "output_folder": str(project_mosaic),
            "grouping_mode": "utm_zone",
            "target_crs_label": "",
            "report_csv": str(tile_dir / "mosaic_report.csv"),
            "manifest_csv": str(mosaic.get("manifest_csv") or project_logs / "mosaic_manifest.csv"),
            "mixed_crid_report_csv": str(tile_dir / "mixed_crid_mosaics.csv"),
            "skip_manifest_existing": True,
            "utm_tiles": [tile],
        }
    )
    config["mosaic"] = mosaic

    upload = dict(config.get("upload", {}))
    upload.update(
        {
            "scope": "selected_utm",
            "utm_tiles": [tile],
            "ee_sync_before_upload": True,
        }
    )
    config["upload"] = upload

    execution = dict(config.get("execution", {}))
    execution.update(
        {
            "dry_run": False,
            "resume": True,
            "require_confirmation": False,
        }
    )
    config["execution"] = execution

    artifacts = dict(config.get("artifacts", {}))
    artifacts.update(
        {
            "logs_dir": str(project_logs),
            "artifacts_dir": str(project_logs / "upload_artifacts"),
            "report_csv": str(artifacts.get("report_csv") or project_logs / "upload_report.csv"),
            "ee_asset_inventory_csv": str(
                artifacts.get("ee_asset_inventory_csv") or project_logs / "ee_asset_inventory.csv"
            ),
        }
    )
    config["artifacts"] = artifacts
    config["input_folder"] = str(project_mosaic)
    config.setdefault("automation", {})
    config["automation"].update({"include_upload": include_upload, "tile": tile})
    return config


def write_tile_config(config: Mapping[str, Any], path: Path) -> Path:
    """Write a temporary YAML config for one tile stage run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(dict(config), handle, sort_keys=False, allow_unicode=False)
    return path


def download_config_for_tile(
    base_config: Mapping[str, Any],
    tile: str,
    run_dir: Path,
    *,
    start_date: str,
    end_date: str,
    include_upload: bool,
) -> DownloadConfig:
    """Build a DownloadConfig through the normal YAML parser."""
    temp_config = write_tile_config(
        build_tile_config_dict(
            base_config,
            tile,
            run_dir,
            start_date=start_date,
            end_date=end_date,
            include_upload=include_upload,
        ),
        run_dir / tile / "preflight_config.yaml",
    )
    return load_download_config_file(temp_config)


def tile_counts_from_insights(config: Mapping[str, Any]) -> Dict[str, tuple[int, int, int, int, int, int]]:
    """Return per-tile pipeline counts from ProjectInsights."""
    insights = collect_project_insights(config)
    return {
        str(tile): (
            int(downloaded),
            int(extracted),
            int(mosaic_sources),
            int(submitted),
            int(uploaded),
            int(missing_upload),
        )
        for (
            tile,
            downloaded,
            extracted,
            mosaic_sources,
            submitted,
            uploaded,
            missing_upload,
        ) in insights.upload_qa_tile_rows
    }


COMPLETED_MOSAIC_STATUSES = {
    "MOSAIC_CREATED",
    "MOSAIC_CREATED_WITH_EXCLUSIONS",
    "COPIED_SINGLETON",
    "SKIPPED_EXISTS",
    "SKIPPED_MANIFEST",
}


def estimate_mosaic_output_names(granules: Iterable[Any]) -> set[str]:
    """Estimate original-UTM mosaic output names from selected SWOT granule filenames."""
    grouped: Dict[tuple[str, str, str, str, str], list[Dict[str, str]]] = {}
    for granule in granules:
        file_name = str(getattr(granule, "file_name", "") or "")
        metadata = parse_swot_l2_hr_raster_metadata(file_name)
        if metadata is None:
            continue
        fields = metadata.fields
        key = (
            fields["descriptor"],
            fields["cycle_id"],
            fields["pass_id"],
            fields["range_beginning"][:8],
            fields["coordinate_system"],
        )
        grouped.setdefault(key, []).append(fields)

    output_names: set[str] = set()
    for key, field_rows in grouped.items():
        descriptor, cycle_id, pass_id, _start_date, _coordinate_system = key
        range_beginning = min(row["range_beginning"] for row in field_rows)
        range_ending = max(row["range_ending"] for row in field_rows)
        crids = {row["crid"] for row in field_rows}
        crid = next(iter(crids)) if len(crids) == 1 else "MIXD"
        output_names.add(
            "SWOT_L2_HR_Raster_"
            f"{descriptor}_"
            f"{cycle_id}_"
            f"{pass_id}_"
            "MOSA_"
            f"{range_beginning}_"
            f"{range_ending}_"
            f"{crid}_"
            "01.tif"
        )
    return output_names


def completed_mosaic_output_names(config: Mapping[str, Any]) -> set[str]:
    """Return completed/known mosaic output names from the cumulative mosaic manifest."""
    manifest_path = project_manifest_path(config, "mosaic", "manifest_csv")
    if not str(manifest_path) or str(manifest_path) == ".":
        return set()
    output_names: set[str] = set()
    try:
        rows = read_project_rows(manifest_path, "mosaic_manifest")
    except (OSError, ValueError, sqlite3.Error):
        return set()
    for row in rows:
        status = str(row.get("status", "") or "").upper()
        if status not in COMPLETED_MOSAIC_STATUSES:
            continue
        name = Path(str(row.get("output_file", "") or "")).name
        if name:
            output_names.add(name)
    return output_names


def cached_update_preview(download_config: DownloadConfig) -> DownloadPreview | None:
    """Return a preview from stored update expected rows for this exact campaign/tile."""
    campaign_id = update_campaign_id(download_config)
    tiles = set(download_config.utm_tiles)
    granules: list[DownloadGranule] = []
    for row in read_update_expected_rows(download_config):
        if str(row.get("campaign_id", "") or "") != campaign_id:
            continue
        file_name = str(row.get("file_name", "") or "").strip()
        if not file_name:
            continue
        tile = str(row.get("utm_tile", "") or "").strip().upper()
        if tiles and tile not in tiles:
            continue
        size_mb = None
        size_text = str(row.get("size_mb", "") or "").strip()
        if size_text:
            try:
                size_mb = float(size_text)
            except ValueError:
                size_mb = None
        granules.append(
            DownloadGranule(
                identity=str(row.get("granule_id", "") or file_name),
                file_name=file_name,
                utm_tile=tile,
                start_time=str(row.get("start_time", "") or ""),
                end_time=str(row.get("end_time", "") or ""),
                size_mb=size_mb,
                selected_for_download=True,
                duplicate_filter_status="selected_cached",
            )
        )
    if not granules:
        return None
    return DownloadPreview(
        query=build_download_query(download_config),
        granules=sorted(granules, key=lambda granule: granule.file_name.lower()),
    )


def classify_tile(
    *,
    pending_downloads: int,
    downloaded: int,
    extracted: int,
    mosaic_sources: int,
    uploaded: int,
    missing_upload: int,
) -> tuple[str, str]:
    """Return a coarse automation classification and message for one tile."""
    if pending_downloads > 0 and not any((downloaded, extracted, mosaic_sources, uploaded)):
        return "new", f"{pending_downloads} new selected granule(s) need download."
    if pending_downloads > 0:
        return "needs update", f"{pending_downloads} selected granule(s) are new for this date range."
    if uploaded > 0 and missing_upload == 0:
        return "already complete", "No new downloads found and uploaded/verified records already exist."
    if any((downloaded, extracted, mosaic_sources, uploaded, missing_upload)):
        return "partial/resumable", "No new downloads found, but project records are partial or need verification."
    return "new", "No previous project records were found for this tile."


def preflight_automation(
    config: AutomationConfig,
    preview_builder: Callable[..., Any] = build_download_preview,
    progress_callback: ProgressCallback | None = None,
) -> AutomationRunState:
    """Validate an automation request and classify each selected tile."""
    normalized = normalize_automation_config(config)
    state = ensure_run_state(normalized)
    state.started_at = now_text()
    base_config = normalized.base_config

    gdal_python = Path(str(base_config.get("gdal", {}).get("python", ""))) if isinstance(base_config.get("gdal", {}), Mapping) else Path("")
    if not str(gdal_python) or not gdal_python.exists():
        state.errors.append(f"Configured GDAL Python does not exist: {gdal_python}")

    state.warnings.extend(windows_automation_reboot_warnings())

    try:
        usage = shutil.disk_usage(normalized.project_root)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < normalized.min_free_space_gb:
            state.warnings.append(
                f"Free disk space is {free_gb:.1f} GB, below the configured {normalized.min_free_space_gb:.1f} GB threshold."
            )
    except OSError as exc:
        state.warnings.append(f"Could not check free disk space: {exc}")

    if normalized.include_upload:
        state.warnings.append(
            "Upload is enabled. Browser-based Earth Engine upload still requires a dedicated untouched Chrome/session."
        )
        if progress_callback is not None:
            progress_callback(
                "ALL",
                "ee_sync",
                "running",
                "Synchronizing the target Earth Engine collection before preflight classification...",
            )
        return_code, message, log_path = sync_ee_assets_for_preflight(
            base_config,
            state.run_dir,
            progress_callback=progress_callback,
        )
        if return_code != 0:
            state.errors.append(
                f"Earth Engine asset sync failed before automation preflight: {message} Log: {log_path}"
            )
            state.preflight_ok = False
            state.finished_at = now_text()
            write_run_state(state)
            return state
        if progress_callback is not None:
            progress_callback("ALL", "ee_sync", "success", message)

    counts_by_tile = tile_counts_from_insights(base_config)
    manifest = read_download_manifest(project_manifest_path(base_config, "download", "manifest_csv"))
    recorded_mosaic_names = completed_mosaic_output_names(base_config)

    for tile in normalized.utm_tiles:
        try:
            if progress_callback is not None:
                progress_callback(
                    tile,
                    "preflight",
                    "running",
                    f"Checking cached preview/CMR and classifying {tile}...",
                )
            download_config = download_config_for_tile(
                base_config,
                tile,
                state.run_dir,
                start_date=normalized.start_date,
                end_date=normalized.end_date,
                include_upload=normalized.include_upload,
            )
            preview = cached_update_preview(download_config)
            preview_source = "cached_update_expected"
            if preview is None:
                preview = preview_builder(download_config)
                preview_source = f"automation_preflight:{state.run_id}"
                record_update_preview(
                    download_config,
                    preview,
                    source=preview_source,
                    campaign_tiles=normalized.utm_tiles,
                )
            pending = 0
            for granule in preview.selected_granules:
                if manifest_has_downloaded(manifest, granule):
                    continue
                if existing_download_path(download_config, granule) is not None:
                    continue
                pending += 1
            estimated_mosaic_names = estimate_mosaic_output_names(preview.selected_granules)
            recorded_mosaics = len(estimated_mosaic_names & recorded_mosaic_names)
            pending_mosaics = max(0, len(estimated_mosaic_names) - recorded_mosaics)
            downloaded, extracted, mosaic_sources, submitted, uploaded, missing_upload = counts_by_tile.get(
                tile,
                (0, 0, 0, 0, 0, 0),
            )
            classification, message = classify_tile(
                pending_downloads=pending,
                downloaded=downloaded,
                extracted=extracted,
                mosaic_sources=mosaic_sources,
                uploaded=uploaded,
                missing_upload=missing_upload,
            )
            if preview_source == "cached_update_expected":
                message = f"{message} Used cached expected granules for this date window."
            state.tile_plans.append(
                AutomationTilePlan(
                    tile=tile,
                    classification=classification,
                    message=message,
                    matched_granules=len(preview.granules),
                    selected_granules=len(preview.selected_granules),
                    pending_downloads=pending,
                    estimated_mosaic_groups=len(estimated_mosaic_names),
                    recorded_mosaic_groups=recorded_mosaics,
                    pending_mosaic_groups=pending_mosaics,
                    excluded_older_versions=len(preview.excluded_granules),
                    downloaded=downloaded,
                    extracted=extracted,
                    mosaic_sources=mosaic_sources,
                    submitted=submitted,
                    uploaded=uploaded,
                    missing_upload=missing_upload,
                )
            )
            if progress_callback is not None:
                progress_callback(tile, "preflight", "success", f"{tile}: {classification}. {message}")
        except Exception as exc:
            error_message = str(exc)
            if is_retryable_download_message(error_message):
                state.warnings.append(
                    f"{tile}: CMR search timed out or failed during preflight. "
                    "Automation will try again during the download stage."
                )
                state.tile_plans.append(
                    AutomationTilePlan(
                        tile=tile,
                        classification="cmr retry later",
                        message="CMR search unavailable during preflight; download stage will retry.",
                    )
                )
                if progress_callback is not None:
                    progress_callback(
                        tile,
                        "preflight",
                        "warning",
                        "CMR search unavailable during preflight; download stage will retry.",
                    )
                continue
            state.errors.append(f"{tile}: preflight failed: {error_message}")
            state.tile_plans.append(
                AutomationTilePlan(
                    tile=tile,
                    classification="blocked",
                    message=error_message,
                )
            )

    state.preflight_ok = not state.errors
    state.finished_at = now_text()
    write_run_state(state)
    return state


def stage_command(stage: str, config_path: Path, gdal_python: Path) -> tuple[list[str], Optional[dict[str, str]]]:
    """Return command/env for one automation stage."""
    if stage == "download":
        return [sys.executable, str(DOWNLOAD_SCRIPT), "--config", str(config_path)], None
    if stage == "duplicates":
        return [sys.executable, str(DUPLICATE_SCRIPT), "--config", str(config_path)], None
    if stage == "extract":
        return [str(gdal_python), str(EXTRACT_SCRIPT), "--config", str(config_path)], build_gdal_runtime_env(gdal_python)
    if stage == "mosaic":
        return [str(gdal_python), str(MOSAIC_SCRIPT), "--config", str(config_path)], build_gdal_runtime_env(gdal_python)
    if stage == "upload":
        return [sys.executable, str(UPLOAD_SCRIPT), "--config", str(config_path)], None
    if stage == "ee_sync":
        return [sys.executable, str(UPLOAD_SCRIPT), "--config", str(config_path), "--sync-only"], None
    raise ValueError(f"Unsupported automation stage: {stage}")


def run_subprocess_stage(
    command: Sequence[str],
    log_path: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    progress_callback: ProgressCallback | None = None,
    tile: str = "",
    stage: str = "",
) -> tuple[int, str]:
    """Run one stage command, streaming output into a log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(dict(env))
    output_tail: list[str] = []
    with log_path.open("w", encoding="utf-8", newline="") as log_handle:
        process = subprocess.Popen(
            list(command),
            cwd=str(PROJECT_ROOT),
            env=merged_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log_handle.write(line)
            log_handle.flush()
            message = line.rstrip()
            output_tail.append(message)
            output_tail = output_tail[-20:]
            if progress_callback is not None and message:
                display_message = message
                if message.startswith("GEEUP_PROGRESS\t"):
                    parts = message.split("\t", 4)
                    if len(parts) == 5:
                        display_message = parts[4]
                progress_callback(tile, stage, "running", display_message)
        return_code = process.wait()
    return return_code, "\n".join(output_tail)


def sync_ee_assets_for_preflight(
    base_config: Mapping[str, Any],
    run_dir: Path,
    *,
    progress_callback: ProgressCallback | None = None,
) -> tuple[int, str, Path]:
    """Synchronize EE inventory once before an upload-enabled automation preflight."""
    sync_config = deepcopy(dict(base_config))
    upload = dict(sync_config.get("upload", {}))
    upload["ee_sync_before_upload"] = True
    sync_config["upload"] = upload
    execution = dict(sync_config.get("execution", {}))
    execution.update({"dry_run": False, "require_confirmation": False})
    sync_config["execution"] = execution

    config_path = write_tile_config(sync_config, run_dir / "preflight_ee_sync_config.yaml")
    log_path = run_dir / "preflight_ee_sync.log"
    command = [
        sys.executable,
        str(UPLOAD_SCRIPT),
        "--config",
        str(config_path),
        "--sync-only",
    ]
    return_code, tail = run_subprocess_stage(
        command,
        log_path,
        progress_callback=progress_callback,
        tile="ALL",
        stage="ee_sync",
    )
    message = summarize_subprocess_result("Earth Engine asset sync", return_code, tail, log_path)
    return return_code, message, log_path


def meaningful_stage_line(tail: str) -> str:
    """Return the most useful non-progress line from a subprocess output tail."""
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    non_progress = [line for line in lines if not line.startswith("GEEUP_PROGRESS")]
    if non_progress:
        return non_progress[-1]
    return lines[-1] if lines else ""


def summarize_subprocess_result(stage: str, return_code: int, tail: str, log_path: Path) -> str:
    """Return a GUI-friendly subprocess stage message."""
    if return_code == 0:
        return tail[-800:] if tail else f"{stage} completed."
    detail = meaningful_stage_line(tail)
    if detail:
        return f"{stage} failed with return code {return_code}: {detail}. Log: {log_path}"
    return f"{stage} failed with return code {return_code}. Log: {log_path}"


def summarize_download_result(result: Any) -> tuple[str, str, int]:
    """Return status/message/return-code for an in-process download result."""
    matched = len(result.preview.granules)
    selected = len(result.preview.selected_granules)
    excluded = len(result.preview.excluded_granules)
    downloaded = len(result.downloaded_files)
    skipped_local = len(result.skipped_existing)
    skipped_manifest = len(result.skipped_manifest)
    failures = len(result.failures)
    missing = len(result.missing_granules)
    report = f" Report: {result.report_csv}" if result.report_csv else ""
    counts = (
        f"matched {matched}, selected {selected}, excluded older/version {excluded}, "
        f"downloaded {downloaded}, skipped local {skipped_local}, "
        f"skipped manifest {skipped_manifest}, accounted for {result.complete_count}, "
        f"missing {missing}, failures {failures}."
    )
    if result.stopped:
        return "stopped", f"Download stopped. {counts}{report}", 130
    if result.failures or result.missing_granules:
        first_failure = ""
        if result.failures:
            granule, error = result.failures[0]
            first_failure = f" First failure: {granule.file_name}: {error}"
        return "failed", f"Download incomplete. {counts}{first_failure}{report}", 1
    return "success", f"Download complete. {counts}{report}", 0


def execute_download_result(
    state: AutomationRunState,
    tile: str,
    tile_config_path: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    stop_event: Any = None,
) -> AutomationStageResult:
    """Run the download stage in-process so GUI Earthdata auth is reused."""
    started = now_text()
    log_path = tile_config_path.parent / "download.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("w", encoding="utf-8", newline="") as log_handle:
            log_handle.write(f"Automation download stage started: {started}\n")
            log_handle.write(f"Config: {tile_config_path}\n")
            download_config = load_download_config_file(tile_config_path)

            def report_progress(current: int, total: int, message: str) -> None:
                progress_message = str(message).replace("\r", " ").replace("\n", " ")
                log_handle.write(f"GEEUP_PROGRESS\tdownload\t{current}\t{total}\t{progress_message}\n")
                log_handle.flush()
                if progress_callback is not None:
                    progress_callback(tile, "download", "running", progress_message)

            result = run_download(
                download_config,
                progress_callback=report_progress,
                stop_event=stop_event,
            )
            status, message, return_code = summarize_download_result(result)
            log_handle.write(f"{message}\n")
            return AutomationStageResult(
                run_id=state.run_id,
                tile=tile,
                stage="download",
                status=status,
                start_time=started,
                end_time=now_text(),
                return_code=return_code,
                message=message,
                config_path=str(tile_config_path),
                log_path=str(log_path),
            )
    except Exception as exc:
        with log_path.open("a", encoding="utf-8", newline="") as log_handle:
            log_handle.write(f"ERROR: {exc}\n")
            log_handle.write(traceback.format_exc())
        if is_retryable_download_message(str(exc)):
            message = (
                "CMR search failed or timed out. This is usually temporary; "
                f"automation can continue with later tiles and retry this tile. Log: {log_path}"
            )
        else:
            message = f"Download failed: {exc}. Log: {log_path}"
        return AutomationStageResult(
            run_id=state.run_id,
            tile=tile,
            stage="download",
            status="failed",
            start_time=started,
            end_time=now_text(),
            return_code=1,
            message=message,
            config_path=str(tile_config_path),
            log_path=str(log_path),
        )


def path_mentions_tile(path: Path, tile: str) -> bool:
    """Return True when a path name appears to belong to one UTM tile."""
    return tile.upper() in path.name.upper()


def cleanup_stage(
    config: Mapping[str, Any],
    tile: str,
    stage: str,
) -> tuple[int, int, list[str]]:
    """Delete safe cleanup candidates for one automation cleanup stage.

    Raw and extracted cleanup stay tile-scoped because later stages may still
    need neighboring files. Mosaic cleanup is intentionally global: a later
    Earth Engine sync can verify assets from earlier tiles after their own
    cleanup stage has passed, so each upload-enabled cleanup sweep removes all
    currently verified mosaics.
    """
    candidates = []
    for candidate in plan_cleanup_candidates(config):
        if candidate.stage != stage:
            continue
        if stage != "mosaic" and not path_mentions_tile(candidate.path, tile):
            continue
        candidates.append(candidate)
    return delete_cleanup_candidates(candidates)


def append_stage_result(state: AutomationRunState, result: AutomationStageResult) -> None:
    """Append one stage result and persist run state."""
    state.stage_results.append(result)
    write_run_state(state)


def write_run_state(state: AutomationRunState) -> None:
    """Write automation JSON and CSV state files."""
    state.run_dir.mkdir(parents=True, exist_ok=True)
    serializable = {
        "run_id": state.run_id,
        "run_dir": str(state.run_dir),
        "config": {
            **asdict(state.config),
            "project_root": str(state.config.project_root),
            "run_dir": str(state.config.run_dir or ""),
        },
        "tile_plans": [asdict(plan) for plan in state.tile_plans],
        "stage_results": [asdict(result) for result in state.stage_results],
        "preflight_ok": state.preflight_ok,
        "warnings": state.warnings,
        "errors": state.errors,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "stopped": state.stopped,
    }
    with state.json_path.open("w", encoding="utf-8") as handle:
        json.dump(serializable, handle, indent=2, default=str)
    with state.csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUTOMATION_RUN_COLUMNS)
        writer.writeheader()
        for result in state.stage_results:
            row = asdict(result)
            writer.writerow({column: row.get(column, "") for column in AUTOMATION_RUN_COLUMNS})


def _automation_config_from_mapping(data: Mapping[str, Any]) -> AutomationConfig:
    """Rebuild an AutomationConfig from a persisted JSON mapping."""
    run_dir_text = str(data.get("run_dir", "") or "").strip()
    return AutomationConfig(
        project_root=Path(str(data.get("project_root", "") or "")),
        base_config=deepcopy(dict(data.get("base_config", {}) or {})),
        utm_tiles=list(data.get("utm_tiles", []) or []),
        start_date=str(data.get("start_date", "") or ""),
        end_date=str(data.get("end_date", "") or ""),
        include_upload=bool(data.get("include_upload", False)),
        cleanup_enabled=bool(data.get("cleanup_enabled", True)),
        min_free_space_gb=float(data.get("min_free_space_gb", 50.0) or 50.0),
        continue_on_tile_failure=bool(data.get("continue_on_tile_failure", True)),
        deferred_download_retry_passes=max(
            0,
            int(data.get("deferred_download_retry_passes", 1) or 0),
        ),
        prevent_system_sleep=bool(data.get("prevent_system_sleep", True)),
        prevent_display_sleep=bool(data.get("prevent_display_sleep", False)),
        run_id=str(data.get("run_id", "") or ""),
        run_dir=Path(run_dir_text) if run_dir_text else None,
    )


def _coerce_int(value: Any, default: int = 0) -> int:
    """Return an int from persisted JSON/CSV-like values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tile_plan_from_mapping(data: Mapping[str, Any]) -> AutomationTilePlan:
    """Rebuild an AutomationTilePlan from persisted JSON."""
    return AutomationTilePlan(
        tile=str(data.get("tile", "") or ""),
        classification=str(data.get("classification", "") or ""),
        message=str(data.get("message", "") or ""),
        matched_granules=_coerce_int(data.get("matched_granules")),
        selected_granules=_coerce_int(data.get("selected_granules")),
        pending_downloads=_coerce_int(data.get("pending_downloads")),
        estimated_mosaic_groups=_coerce_int(data.get("estimated_mosaic_groups")),
        recorded_mosaic_groups=_coerce_int(data.get("recorded_mosaic_groups")),
        pending_mosaic_groups=_coerce_int(data.get("pending_mosaic_groups")),
        excluded_older_versions=_coerce_int(data.get("excluded_older_versions")),
        downloaded=_coerce_int(data.get("downloaded")),
        extracted=_coerce_int(data.get("extracted")),
        mosaic_sources=_coerce_int(data.get("mosaic_sources")),
        submitted=_coerce_int(data.get("submitted")),
        uploaded=_coerce_int(data.get("uploaded")),
        missing_upload=_coerce_int(data.get("missing_upload")),
    )


def _stage_result_from_mapping(data: Mapping[str, Any]) -> AutomationStageResult:
    """Rebuild an AutomationStageResult from persisted JSON."""
    return AutomationStageResult(
        run_id=str(data.get("run_id", "") or ""),
        tile=str(data.get("tile", "") or ""),
        stage=str(data.get("stage", "") or ""),
        status=str(data.get("status", "") or ""),
        start_time=str(data.get("start_time", "") or ""),
        end_time=str(data.get("end_time", "") or ""),
        return_code=_coerce_int(data.get("return_code")),
        message=str(data.get("message", "") or ""),
        config_path=str(data.get("config_path", "") or ""),
        log_path=str(data.get("log_path", "") or ""),
        deleted_files=_coerce_int(data.get("deleted_files")),
        deleted_bytes=_coerce_int(data.get("deleted_bytes")),
    )


def load_automation_run_state(path: str | Path) -> AutomationRunState:
    """Load one persisted automation run from an automation_run.json file or run directory."""
    input_path = Path(path)
    json_path = input_path / "automation_run.json" if input_path.is_dir() else input_path
    with json_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    config = _automation_config_from_mapping(data.get("config", {}) or {})
    run_dir = Path(str(data.get("run_dir", "") or "")) if data.get("run_dir") else json_path.parent
    if config.run_dir is None:
        config.run_dir = run_dir
    if not config.run_id:
        config.run_id = str(data.get("run_id", "") or run_dir.name)
    state = AutomationRunState(
        run_id=str(data.get("run_id", "") or config.run_id or run_dir.name),
        run_dir=run_dir,
        config=config,
        tile_plans=[_tile_plan_from_mapping(row) for row in data.get("tile_plans", []) or []],
        stage_results=[_stage_result_from_mapping(row) for row in data.get("stage_results", []) or []],
        preflight_ok=bool(data.get("preflight_ok", False)),
        warnings=[str(value) for value in data.get("warnings", []) or []],
        errors=[str(value) for value in data.get("errors", []) or []],
        started_at=str(data.get("started_at", "") or ""),
        finished_at=str(data.get("finished_at", "") or ""),
        stopped=bool(data.get("stopped", False)),
    )
    return state


def latest_automation_run_dir(base_config: Mapping[str, Any], project_root: str | Path) -> Optional[Path]:
    """Return the newest automation run directory for a project, if one exists."""
    root = automation_runs_root(base_config, Path(project_root))
    if not root.exists():
        return None
    candidates = [path for path in root.iterdir() if (path / "automation_run.json").exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))


def load_latest_automation_run_state(
    base_config: Mapping[str, Any],
    project_root: str | Path,
) -> Optional[AutomationRunState]:
    """Load the newest persisted automation run for a project."""
    run_dir = latest_automation_run_dir(base_config, project_root)
    if run_dir is None:
        return None
    return load_automation_run_state(run_dir)


def completed_stage_keys(state: AutomationRunState) -> set[tuple[str, str]]:
    """Return tile/stage pairs that can be skipped when resuming the same run."""
    complete_statuses = {"success", "skipped"}
    return {
        (result.tile, result.stage)
        for result in state.stage_results
        if result.status.lower() in complete_statuses
    }


def latest_stage_results_by_key(state: AutomationRunState) -> dict[tuple[str, str], AutomationStageResult]:
    """Return the latest recorded result for each tile/stage pair."""
    latest: dict[tuple[str, str], AutomationStageResult] = {}
    for result in state.stage_results:
        latest[(result.tile, result.stage)] = result
    return latest


def stage_skipped_result(
    state: AutomationRunState,
    tile: str,
    stage: str,
    message: str,
) -> AutomationStageResult:
    """Return a skipped stage result."""
    timestamp = now_text()
    return AutomationStageResult(
        run_id=state.run_id,
        tile=tile,
        stage=stage,
        status="skipped",
        start_time=timestamp,
        end_time=timestamp,
        message=message,
    )


def is_retryable_download_failure(result: AutomationStageResult | None) -> bool:
    """Return True for transient remote download/search failures that should not stop the queue."""
    if result is None or result.stage != "download" or result.status.lower() != "failed":
        return False
    return is_retryable_download_message(result.message)


def execute_cleanup_result(
    state: AutomationRunState,
    tile_config: Mapping[str, Any],
    tile: str,
    stage: str,
) -> AutomationStageResult:
    """Run one cleanup stage and return its result."""
    cleanup_stage_name = {
        "raw_cleanup": "raw",
        "extracted_cleanup": "extracted",
        "mosaic_cleanup": "mosaic",
    }[stage]
    started = now_text()
    deleted, bytes_deleted, errors = cleanup_stage(tile_config, tile, cleanup_stage_name)
    status = "success" if not errors else "warning"
    scope = "global verified mosaic sweep" if stage == "mosaic_cleanup" else f"{tile} {cleanup_stage_name}"
    message = f"{scope}: deleted {deleted} file(s), {format_bytes(bytes_deleted)}."
    if errors:
        message = f"{message} {len(errors)} cleanup error(s): {'; '.join(errors[:3])}"
    return AutomationStageResult(
        run_id=state.run_id,
        tile=tile,
        stage=stage,
        status=status,
        start_time=started,
        end_time=now_text(),
        message=message,
        deleted_files=deleted,
        deleted_bytes=bytes_deleted,
    )


def execute_command_result(
    state: AutomationRunState,
    tile: str,
    stage: str,
    tile_config_path: Path,
    gdal_python: Path,
    progress_callback: ProgressCallback | None = None,
) -> AutomationStageResult:
    """Run one command stage and return its result."""
    started = now_text()
    command, env = stage_command(stage, tile_config_path, gdal_python)
    log_path = tile_config_path.parent / f"{stage}.log"
    return_code, tail = run_subprocess_stage(
        command,
        log_path,
        env=env,
        progress_callback=progress_callback,
        tile=tile,
        stage=stage,
    )
    if return_code == 0:
        status = "success"
    elif stage == "upload" and return_code == 2:
        status = "warning"
    else:
        status = "failed"
    message = summarize_subprocess_result(stage, return_code, tail, log_path)
    if status == "warning":
        message = (
            "Upload finished with one or more retryable file-level errors or unconfirmed submissions. "
            "Automation will continue to EE sync and preserve unverified mosaics for a later retry. "
            f"{message}"
        )
    return AutomationStageResult(
        run_id=state.run_id,
        tile=tile,
        stage=stage,
        status=status,
        start_time=started,
        end_time=now_text(),
        return_code=return_code,
        message=message,
        config_path=str(tile_config_path),
        log_path=str(log_path),
    )


def run_automation(
    config: AutomationConfig,
    *,
    preflight_state: AutomationRunState | None = None,
    progress_callback: ProgressCallback | None = None,
    stop_event: Any = None,
) -> AutomationRunState:
    """Run the tile-by-tile automation workflow."""
    normalized = normalize_automation_config(config)
    with keep_system_awake(
        enabled=normalized.prevent_system_sleep,
        keep_display_awake=normalized.prevent_display_sleep,
    ):
        return _run_automation_normalized(
            normalized,
            preflight_state=preflight_state,
            progress_callback=progress_callback,
            stop_event=stop_event,
        )


def _run_automation_normalized(
    normalized: AutomationConfig,
    *,
    preflight_state: AutomationRunState | None = None,
    progress_callback: ProgressCallback | None = None,
    stop_event: Any = None,
) -> AutomationRunState:
    """Run automation with a normalized config while the caller owns power policy."""
    state = preflight_state if preflight_state is not None else preflight_automation(normalized)
    if not state.preflight_ok:
        return state
    state.config = normalized
    state.started_at = now_text()
    state.finished_at = ""
    state.stopped = False
    write_run_state(state)

    gdal_python = Path(str(normalized.base_config.get("gdal", {}).get("python", "")))
    plan_by_tile = {plan.tile: plan for plan in state.tile_plans}
    resume_completed = completed_stage_keys(state)
    latest_results = latest_stage_results_by_key(state)

    def stages_for_current_run() -> list[str]:
        stages = ["download", "duplicates", "extract", "raw_cleanup", "mosaic", "extracted_cleanup"]
        if normalized.include_upload:
            stages.extend(["upload", "ee_sync", "mosaic_cleanup"])
        return stages

    def run_active_tile(tile: str) -> bool:
        """Run every needed stage for one non-complete tile. Return True on failure."""
        tile_config = build_tile_config_dict(
            normalized.base_config,
            tile,
            state.run_dir,
            start_date=normalized.start_date,
            end_date=normalized.end_date,
            include_upload=normalized.include_upload,
        )
        tile_dir_path = tile_run_dir(state, tile)
        tile_config_path = write_tile_config(tile_config, tile_dir_path / "automation_config.yaml")
        tile_failed = False
        for stage in stages_for_current_run():
            if stop_event is not None and stop_event.is_set():
                state.stopped = True
                break
            if (tile, stage) in resume_completed:
                if progress_callback is not None:
                    progress_callback(
                        tile,
                        stage,
                        "skipped",
                        "Stage already completed in this automation run; skipping during resume.",
                    )
                continue
            if progress_callback is not None:
                progress_callback(tile, stage, "running", f"Starting {stage}")
            if stage.endswith("_cleanup"):
                if normalized.cleanup_enabled:
                    result = execute_cleanup_result(state, tile_config, tile, stage)
                else:
                    result = stage_skipped_result(state, tile, stage, "Cleanup disabled for this automation run.")
            elif stage == "download":
                result = execute_download_result(
                    state,
                    tile,
                    tile_config_path,
                    progress_callback=progress_callback,
                    stop_event=stop_event,
                )
            else:
                result = execute_command_result(
                    state,
                    tile,
                    stage,
                    tile_config_path,
                    gdal_python,
                    progress_callback=progress_callback,
                )
            append_stage_result(state, result)
            latest_results[(tile, stage)] = result
            if progress_callback is not None:
                progress_callback(tile, stage, result.status, result.message)
            if result.status == "failed":
                tile_failed = True
                break
        return tile_failed

    deferred_retry_tiles: list[str] = []
    stop_after_failure = False
    for tile in normalized.utm_tiles:
        plan = plan_by_tile.get(tile)
        if stop_event is not None and stop_event.is_set():
            state.stopped = True
            break
        if plan is not None and plan.classification == "already complete" and plan.pending_downloads == 0:
            for stage in stages_for_current_run():
                if stage in {"upload", "ee_sync", "mosaic_cleanup"} and not normalized.include_upload:
                    continue
                if (tile, stage) in resume_completed:
                    if progress_callback is not None:
                        progress_callback(
                            tile,
                            stage,
                            "skipped",
                            "Stage already recorded as complete in this automation run.",
                        )
                    continue
                result = stage_skipped_result(
                    state,
                    tile,
                    stage,
                    (
                        "Tile already complete for this requested date range; "
                        "cleanup is not rerun during resume."
                    ),
                )
                append_stage_result(state, result)
                latest_results[(tile, stage)] = result
                if progress_callback is not None:
                    progress_callback(tile, stage, result.status, result.message)
            continue

        tile_failed = run_active_tile(tile)
        if state.stopped:
            break
        retryable_download = is_retryable_download_failure(latest_results.get((tile, "download")))
        if tile_failed and retryable_download:
            deferred_retry_tiles.append(tile)
        if tile_failed and not normalized.continue_on_tile_failure and not retryable_download:
            stop_after_failure = True
            break

    for retry_pass in range(1, normalized.deferred_download_retry_passes + 1):
        if state.stopped or stop_after_failure or not deferred_retry_tiles:
            break
        retry_tiles = list(dict.fromkeys(deferred_retry_tiles))
        deferred_retry_tiles = []
        for tile in retry_tiles:
            if stop_event is not None and stop_event.is_set():
                state.stopped = True
                break
            latest_download = latest_results.get((tile, "download"))
            if not is_retryable_download_failure(latest_download):
                continue
            if progress_callback is not None:
                progress_callback(
                    tile,
                    "download",
                    "running",
                    (
                        "Retrying deferred CMR/download search failure "
                        f"(pass {retry_pass}/{normalized.deferred_download_retry_passes})"
                    ),
                )
            tile_failed = run_active_tile(tile)
            if state.stopped:
                break
            retryable_download = is_retryable_download_failure(latest_results.get((tile, "download")))
            if tile_failed and retryable_download:
                deferred_retry_tiles.append(tile)
            if tile_failed and not normalized.continue_on_tile_failure and not retryable_download:
                stop_after_failure = True
                break

    state.finished_at = now_text()
    write_run_state(state)
    return state
