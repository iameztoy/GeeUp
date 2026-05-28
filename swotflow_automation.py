"""Tile-by-tile unattended workflow orchestration for SWOTFlow projects."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

import yaml

from gdal_runtime import build_gdal_runtime_env
from project_insights import (
    CleanupCandidate,
    collect_project_insights,
    delete_cleanup_candidates,
    format_bytes,
    plan_cleanup_candidates,
)
from swot_download_tool import (
    DownloadConfig,
    build_download_preview,
    existing_download_path,
    load_config_file as load_download_config_file,
    manifest_has_downloaded,
    normalize_utm_tiles,
    read_download_manifest,
)


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
    excluded_older_versions: int = 0
    downloaded: int = 0
    extracted: int = 0
    mosaic_sources: int = 0
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


def tile_counts_from_insights(config: Mapping[str, Any]) -> Dict[str, tuple[int, int, int, int, int]]:
    """Return per-tile pipeline counts from ProjectInsights."""
    insights = collect_project_insights(config)
    return {
        str(tile): (
            int(downloaded),
            int(extracted),
            int(mosaic_sources),
            int(uploaded),
            int(missing_upload),
        )
        for tile, downloaded, extracted, mosaic_sources, uploaded, missing_upload in insights.upload_qa_tile_rows
    }


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
) -> AutomationRunState:
    """Validate an automation request and classify each selected tile."""
    normalized = normalize_automation_config(config)
    state = ensure_run_state(normalized)
    state.started_at = now_text()
    base_config = normalized.base_config

    gdal_python = Path(str(base_config.get("gdal", {}).get("python", ""))) if isinstance(base_config.get("gdal", {}), Mapping) else Path("")
    if not str(gdal_python) or not gdal_python.exists():
        state.errors.append(f"Configured GDAL Python does not exist: {gdal_python}")

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

    counts_by_tile = tile_counts_from_insights(base_config)
    manifest = read_download_manifest(project_manifest_path(base_config, "download", "manifest_csv"))

    for tile in normalized.utm_tiles:
        try:
            download_config = download_config_for_tile(
                base_config,
                tile,
                state.run_dir,
                start_date=normalized.start_date,
                end_date=normalized.end_date,
                include_upload=normalized.include_upload,
            )
            preview = preview_builder(download_config)
            pending = 0
            for granule in preview.selected_granules:
                if manifest_has_downloaded(manifest, granule):
                    continue
                if existing_download_path(download_config, granule) is not None:
                    continue
                pending += 1
            downloaded, extracted, mosaic_sources, uploaded, missing_upload = counts_by_tile.get(
                tile,
                (0, 0, 0, 0, 0),
            )
            classification, message = classify_tile(
                pending_downloads=pending,
                downloaded=downloaded,
                extracted=extracted,
                mosaic_sources=mosaic_sources,
                uploaded=uploaded,
                missing_upload=missing_upload,
            )
            state.tile_plans.append(
                AutomationTilePlan(
                    tile=tile,
                    classification=classification,
                    message=message,
                    matched_granules=len(preview.granules),
                    selected_granules=len(preview.selected_granules),
                    pending_downloads=pending,
                    excluded_older_versions=len(preview.excluded_granules),
                    downloaded=downloaded,
                    extracted=extracted,
                    mosaic_sources=mosaic_sources,
                    uploaded=uploaded,
                    missing_upload=missing_upload,
                )
            )
        except Exception as exc:
            state.errors.append(f"{tile}: preflight failed: {exc}")
            state.tile_plans.append(
                AutomationTilePlan(
                    tile=tile,
                    classification="blocked",
                    message=str(exc),
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
            output_tail.append(line.rstrip())
            output_tail = output_tail[-20:]
        return_code = process.wait()
    return return_code, "\n".join(output_tail)


def path_mentions_tile(path: Path, tile: str) -> bool:
    """Return True when a path name appears to belong to one UTM tile."""
    return tile.upper() in path.name.upper()


def cleanup_stage(
    config: Mapping[str, Any],
    tile: str,
    stage: str,
) -> tuple[int, int, list[str]]:
    """Delete safe cleanup candidates for one stage/tile only."""
    candidates = [
        candidate
        for candidate in plan_cleanup_candidates(config)
        if candidate.stage == stage and path_mentions_tile(candidate.path, tile)
    ]
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
    message = f"Deleted {deleted} file(s), {format_bytes(bytes_deleted)}."
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
) -> AutomationStageResult:
    """Run one command stage and return its result."""
    started = now_text()
    command, env = stage_command(stage, tile_config_path, gdal_python)
    log_path = tile_config_path.parent / f"{stage}.log"
    return_code, tail = run_subprocess_stage(command, log_path, env=env)
    status = "success" if return_code == 0 else "failed"
    return AutomationStageResult(
        run_id=state.run_id,
        tile=tile,
        stage=stage,
        status=status,
        start_time=started,
        end_time=now_text(),
        return_code=return_code,
        message=tail[-800:] if tail else status,
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
    state = preflight_state if preflight_state is not None else preflight_automation(normalized)
    if not state.preflight_ok:
        return state
    state.config = normalized
    state.started_at = now_text()
    state.finished_at = ""
    write_run_state(state)

    gdal_python = Path(str(normalized.base_config.get("gdal", {}).get("python", "")))
    plan_by_tile = {plan.tile: plan for plan in state.tile_plans}
    for tile in normalized.utm_tiles:
        plan = plan_by_tile.get(tile)
        if stop_event is not None and stop_event.is_set():
            state.stopped = True
            break
        if plan is not None and plan.classification == "already complete" and plan.pending_downloads == 0:
            for stage in AUTOMATION_STAGES:
                if stage in {"upload", "ee_sync", "mosaic_cleanup"} and not normalized.include_upload:
                    continue
                result = stage_skipped_result(state, tile, stage, "Tile already complete for this requested date range.")
                append_stage_result(state, result)
            continue

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
        stages = ["download", "duplicates", "extract", "raw_cleanup", "mosaic", "extracted_cleanup"]
        if normalized.include_upload:
            stages.extend(["upload", "ee_sync", "mosaic_cleanup"])

        tile_failed = False
        for stage in stages:
            if stop_event is not None and stop_event.is_set():
                state.stopped = True
                break
            if progress_callback is not None:
                progress_callback(tile, stage, "running", f"Starting {stage}")
            if stage.endswith("_cleanup"):
                if normalized.cleanup_enabled:
                    result = execute_cleanup_result(state, tile_config, tile, stage)
                else:
                    result = stage_skipped_result(state, tile, stage, "Cleanup disabled for this automation run.")
            else:
                result = execute_command_result(state, tile, stage, tile_config_path, gdal_python)
            append_stage_result(state, result)
            if progress_callback is not None:
                progress_callback(tile, stage, result.status, result.message)
            if result.status == "failed":
                tile_failed = True
                break
        if state.stopped:
            break
        if tile_failed and not normalized.continue_on_tile_failure:
            break

    state.finished_at = now_text()
    write_run_state(state)
    return state
