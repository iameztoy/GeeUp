"""Upload local GeoTIFF files to Google Earth Engine through the web UI.

This script uses Selenium with Google Chrome in headed mode by default.
It is intentionally written to be beginner-friendly and easy to modify.

Run with:
    python ee_ui_uploader.py --config config.yaml

Or run without a config file for interactive prompts:
    python ee_ui_uploader.py
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


def in_isolated_python_environment() -> bool:
    """Return True when Python appears to be running inside an isolated env."""
    return bool(
        os.environ.get("VIRTUAL_ENV")
        or os.environ.get("CONDA_PREFIX")
        or getattr(sys, "real_prefix", None)
        or getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    )


def ensure_isolated_python_environment() -> None:
    """Exit early unless the script is running inside a virtual environment.

    This project intentionally requires an isolated Python environment so that
    dependencies do not get installed into the user's global interpreter.
    """
    if in_isolated_python_environment():
        return
    raise SystemExit(
        textwrap.dedent(
            """
            This project must be run from an activated Python environment.

            Recommended Windows setup:
              python -m venv .venv
              .\\.venv\\Scripts\\Activate.ps1
              python -m pip install --upgrade pip
              python -m pip install -r requirements.txt
              python ee_ui_uploader.py --config config.yaml --dry-run

            The script exits here on purpose to avoid using your global Python installation.
            """
        ).strip()
    )


ensure_isolated_python_environment()

import yaml
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    InvalidSessionIdException,
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver, WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

import ee_selectors
from swot_metadata import (
    DEFAULT_METADATA_EXTRA_PROPERTIES,
    ParsedMetadata,
    parse_swot_l2_hr_raster_metadata,
)
from workflow_manifest import upsert_workflow_manifest, workflow_manifest_path


DEFAULT_CONFIG: Dict[str, Any] = {
    "earth_engine_url": ee_selectors.DEFAULT_CODE_EDITOR_URL,
    "input_folder": "",
    "destination_parent": "",
    "chrome": {
        "user_data_dir": "./chrome-profile",
        "profile_directory": None,
        "binary_location": None,
        "connection_mode": "attach",
        "remote_debugging_port": 9222,
        "headless": False,
        "start_maximized": True,
    },
    "upload": {
        "batch_size": 50,
        "max_active_ingestions": 2,
        "scope": "all",
        "utm_tiles": [],
        "ee_sync_before_upload": True,
        "ee_asset_inventory_page_size": 1000,
        "prefix": "",
        "suffix": "",
        "replacement_rules": {},
        "invalid_char_pattern": r"[^A-Za-z0-9._-]+",
        "invalid_char_replacement": "_",
        "recursive": False,
        "extensions": [".tif", ".tiff"],
        "pyramiding_policy": {
            "default": None,
            "per_band": {},
        },
        "retry_attempts": 3,
        "retry_wait_seconds": 3.0,
        "fail_fast": False,
    },
    "metadata": {
        "enabled": True,
        "parser": "swot_l2_hr_raster",
        "require_match": True,
        "add_start_time": True,
        "add_end_time": True,
        "extra_properties": DEFAULT_METADATA_EXTRA_PROPERTIES,
    },
    "execution": {
        "dry_run": False,
        "resume": True,
        "require_confirmation": True,
        "task_poll_seconds": 20,
        "short_ui_wait_seconds": 1.5,
        "wait_timeout_minutes": 720,
        "page_load_timeout_seconds": 90,
        "verbose_console": True,
    },
    "artifacts": {
        "logs_dir": "./logs",
        "artifacts_dir": "./artifacts",
        "report_csv": "./reports/upload_report.csv",
        "ee_asset_inventory_csv": "./reports/ee_asset_inventory.csv",
    },
}

REPORT_COLUMNS = [
    "local_file",
    "asset_id",
    "batch_number",
    "upload_selected",
    "upload_filter_status",
    "output_grid",
    "source_utm_tiles",
    "ee_asset_exists",
    "ee_verified_at",
    "verification_source",
    "submit_time",
    "detected_task_name",
    "final_status",
    "error_message",
    "metadata_start_time",
    "metadata_end_time",
    "metadata_properties",
    "metadata_status",
]

RESUME_SKIP_STATUSES = {
    "SUBMITTED",
    "READY",
    "RUNNING",
    "COMPLETED",
    "SKIPPED_ALREADY_EXISTS",
    "EE_VERIFIED_EXISTS",
}

TERMINAL_STATUSES = {
    "COMPLETED",
    "FAILED",
    "ERROR",
    "UNKNOWN_AFTER_CLICK",
    "SKIPPED_ALREADY_EXISTS",
    "PLANNED_DRY_RUN",
    "FILTERED_UTM_TILE",
    "EE_VERIFIED_EXISTS",
}

ACTIVE_STATUSES = {
    "SUBMITTED",
    "READY",
    "RUNNING",
}
PRESERVE_ON_FILTER_STATUSES = RESUME_SKIP_STATUSES | {
    "FAILED",
    "ERROR",
    "UNKNOWN_AFTER_CLICK",
}

UNKNOWN_AFTER_CLICK_STATUS = "UNKNOWN_AFTER_CLICK"
PLANNED_UPLOAD_STATUS = "PLANNED_UPLOAD"
UPLOAD_SCOPE_ALL = "all"
UPLOAD_SCOPE_SELECTED_UTM = "selected_utm"
VALID_UPLOAD_SCOPES = {UPLOAD_SCOPE_ALL, UPLOAD_SCOPE_SELECTED_UTM}
FILTER_STATUS_SELECTED_ALL = "selected_all"
FILTER_STATUS_SELECTED_UTM = "selected_utm_match"
FILTER_STATUS_FILTERED_UTM = "filtered_no_matching_utm"
FILTER_STATUS_FILTERED_UNKNOWN = "filtered_no_utm_provenance"
FILTERED_UTM_STATUS = "FILTERED_UTM_TILE"
EE_VERIFIED_EXISTS_STATUS = "EE_VERIFIED_EXISTS"
UTM_TILE_RE = re.compile(r"^UTM(?P<zone>\d{1,2})(?P<band>[C-HJ-NP-X])$")
FATAL_BROWSER_SESSION_MESSAGE = (
    "Browser/WebDriver session ended. Stop the run and restart Chrome before continuing."
)


class AlreadyExistsError(Exception):
    """Raised when Earth Engine reports that an asset already exists."""


class RetryableUIError(Exception):
    """Raised for transient UI failures that should be retried."""


class UnknownAfterClickError(Exception):
    """Raised when upload was clicked but no task or dialog error was detected."""


class FatalBrowserSessionError(RuntimeError):
    """Raised when the controlled Chrome/WebDriver session is no longer usable."""


def is_invalid_browser_session(exc: BaseException) -> bool:
    """Return True when Selenium reports that the browser session has ended."""
    return isinstance(exc, InvalidSessionIdException) or "invalid session id" in str(exc).lower()


def raise_if_invalid_browser_session(exc: BaseException) -> None:
    """Raise a fatal run-level error for ended WebDriver sessions."""
    if is_invalid_browser_session(exc):
        raise FatalBrowserSessionError(FATAL_BROWSER_SESSION_MESSAGE) from exc


def active_upload_dialog_helper_js() -> str:
    """Return JavaScript helpers for selecting the visible current upload dialog."""
    return f"""
    function uploadElementVisible(element) {{
      if (!element || !element.getClientRects || !element.getBoundingClientRect) return false;
      const style = window.getComputedStyle ? window.getComputedStyle(element) : null;
      if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
      const rects = element.getClientRects();
      const box = element.getBoundingClientRect();
      return rects.length > 0 || box.width > 0 || box.height > 0;
    }}

    function uploadDialogLooksOpen(dialog) {{
      if (!dialog || !dialog.shadowRoot) return false;
      if (dialog.hasAttribute('opened') || dialog.hasAttribute('open')) return true;
      const innerDialog = dialog.shadowRoot.querySelector('#asset-upload-dialog, paper-dialog, mwc-dialog, dialog');
      if (innerDialog && (innerDialog.hasAttribute('opened') || innerDialog.hasAttribute('open'))) return true;
      return uploadElementVisible(dialog) || uploadElementVisible(innerDialog);
    }}

    function findActiveUploadDialog() {{
      const dialogs = Array.from(document.querySelectorAll('{ee_selectors.SHADOW_SELECTORS["upload_dialog_host"]}'));
      return dialogs.reverse().find(uploadDialogLooksOpen) || null;
    }}
    """


DIALOG_ERROR_KEYWORD_RE = re.compile(
    r"(already exists|does not exist|not found|invalid|error|failed|required)",
    re.IGNORECASE,
)


def normalize_dialog_error_messages(messages: Iterable[str]) -> str:
    """Normalize explicit upload-dialog validation messages."""
    normalized_messages: List[str] = []
    for message in messages:
        for line in str(message or "").splitlines():
            normalized = line.strip()
            if not normalized:
                continue
            if DIALOG_ERROR_KEYWORD_RE.search(normalized):
                normalized_messages.append(normalized)
    return " | ".join(dict.fromkeys(normalized_messages))


@dataclass
class ChromeConfig:
    """Browser settings."""

    user_data_dir: Path
    profile_directory: Optional[str] = None
    binary_location: Optional[str] = None
    connection_mode: str = "attach"
    remote_debugging_port: int = 9222
    headless: bool = False
    start_maximized: bool = True


@dataclass
class PyramidingPolicyConfig:
    """Optional Earth Engine pyramiding policy settings."""

    default: Optional[str] = None
    per_band: Dict[str, str] = field(default_factory=dict)

    def enabled(self) -> bool:
        """Return True when any pyramiding policy value is configured."""
        return bool(self.default or self.per_band)


@dataclass
class MetadataConfig:
    """Optional Earth Engine asset metadata settings."""

    enabled: bool = True
    parser: str = "swot_l2_hr_raster"
    require_match: bool = True
    add_start_time: bool = True
    add_end_time: bool = True
    extra_properties: Dict[str, str] = field(default_factory=dict)


@dataclass
class UploadConfig:
    """Upload behavior settings."""

    batch_size: int = 50
    max_active_ingestions: int = 2
    scope: str = UPLOAD_SCOPE_ALL
    utm_tiles: List[str] = field(default_factory=list)
    ee_sync_before_upload: bool = True
    ee_asset_inventory_page_size: int = 1000
    prefix: str = ""
    suffix: str = ""
    replacement_rules: Dict[str, str] = field(default_factory=dict)
    invalid_char_pattern: str = r"[^A-Za-z0-9._-]+"
    invalid_char_replacement: str = "_"
    recursive: bool = False
    extensions: List[str] = field(default_factory=lambda: [".tif", ".tiff"])
    pyramiding_policy: PyramidingPolicyConfig = field(
        default_factory=PyramidingPolicyConfig
    )
    retry_attempts: int = 3
    retry_wait_seconds: float = 3.0
    fail_fast: bool = False


@dataclass
class ExecutionConfig:
    """Runtime behavior settings."""

    dry_run: bool = False
    resume: bool = True
    require_confirmation: bool = True
    task_poll_seconds: int = 20
    short_ui_wait_seconds: float = 1.5
    wait_timeout_minutes: int = 720
    page_load_timeout_seconds: int = 90
    verbose_console: bool = True


@dataclass
class ArtifactConfig:
    """Output paths for logs, reports, and failure artifacts."""

    logs_dir: Path
    artifacts_dir: Path
    report_csv: Path
    ee_asset_inventory_csv: Path


@dataclass
class AppConfig:
    """Top-level application configuration."""

    earth_engine_url: str
    input_folder: Path
    destination_parent: str
    chrome: ChromeConfig
    upload: UploadConfig
    metadata: MetadataConfig
    execution: ExecutionConfig
    artifacts: ArtifactConfig
    mosaic_manifest_csv: Optional[Path]
    base_dir: Path


@dataclass
class UploadItem:
    """A single planned upload."""

    local_file: Path
    asset_name: str
    asset_id: str
    batch_number: int = 0
    upload_selected: bool = True
    upload_filter_status: str = FILTER_STATUS_SELECTED_ALL
    output_grid: str = ""
    source_utm_tiles: List[str] = field(default_factory=list)
    ee_asset_exists: bool = False
    ee_verified_at: str = ""
    verification_source: str = ""


@dataclass(frozen=True)
class AssetInventoryRecord:
    """One Earth Engine asset listed from a destination collection."""

    asset_id: str
    asset_type: str
    asset_name: str = ""


@dataclass
class TaskRow:
    """A parsed row from the Tasks panel."""

    raw_text: str
    normalized_status: str
    is_ingestion: bool

    @property
    def task_name(self) -> str:
        """Return a compact task label."""
        first_line = self.raw_text.splitlines()[0].strip()
        return first_line[:300]


class ReportManager:
    """Read, write, and update the CSV report file."""

    def __init__(self, report_path: Path) -> None:
        self.report_path = report_path
        self.rows: Dict[str, Dict[str, str]] = {}

    def load(self) -> None:
        """Load an existing CSV report if one exists."""
        if not self.report_path.exists():
            return
        with self.report_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                asset_id = row.get("asset_id", "").strip()
                if not asset_id:
                    continue
                normalized = {column: row.get(column, "") for column in REPORT_COLUMNS}
                self.rows[asset_id] = normalized

    def get_status(self, asset_id: str) -> str:
        """Return the recorded status for an asset ID."""
        return self.rows.get(asset_id, {}).get("final_status", "").strip().upper()

    def merge_row(self, row: Dict[str, str]) -> Dict[str, str]:
        """Insert or replace one in-memory report row and return the stored row."""
        asset_id = row["asset_id"]
        existing = self.rows.get(asset_id, {column: "" for column in REPORT_COLUMNS})
        existing.update({column: row.get(column, existing.get(column, "")) for column in REPORT_COLUMNS})
        self.rows[asset_id] = existing
        return existing

    def upsert(self, row: Dict[str, str]) -> None:
        """Insert or replace a report row."""
        existing = self.merge_row(row)
        self.write()
        self.write_workflow_rows([existing])

    def upsert_many(self, rows: Iterable[Dict[str, str]]) -> None:
        """Insert or replace multiple rows, then write reports once."""
        existing_rows = [self.merge_row(row) for row in rows]
        if not existing_rows:
            return
        self.write()
        self.write_workflow_rows(existing_rows)

    def workflow_row(self, row: Dict[str, str]) -> Dict[str, str]:
        """Return the shared workflow manifest row for one upload report row."""
        local_file = Path(row.get("local_file", ""))
        return {
            "stage": "upload",
            "record_id": row.get("asset_id", ""),
            "record_type": "earth_engine_asset",
            "status": row.get("final_status", ""),
            "source_path": row.get("local_file", ""),
            "output_path": row.get("asset_id", ""),
            "start_time": row.get("metadata_start_time", ""),
            "end_time": row.get("metadata_end_time", ""),
            "output_exists": (
                "yes"
                if row.get("final_status", "") == EE_VERIFIED_EXISTS_STATUS
                or row.get("ee_asset_exists", "").lower() == "yes"
                else "unknown"
            ),
            "raw_exists": "yes" if local_file.exists() else "no",
            "message": row.get("error_message", ""),
        }

    def write_workflow_rows(self, rows: Iterable[Dict[str, str]]) -> None:
        """Mirror upload report statuses into the shared workflow manifest."""
        upsert_workflow_manifest(
            workflow_manifest_path(self.report_path),
            [self.workflow_row(row) for row in rows],
        )

    def write(self) -> None:
        """Write the report to disk."""
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        with self.report_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            for asset_id in sorted(self.rows):
                writer.writerow(self.rows[asset_id])

    def counts(self, asset_ids: Optional[Iterable[str]] = None) -> Dict[str, int]:
        """Return a simple status count summary."""
        selected = set(asset_ids) if asset_ids is not None else None
        counts: Dict[str, int] = {}
        for asset_id, row in self.rows.items():
            if selected is not None and asset_id not in selected:
                continue
            status = row.get("final_status", "UNKNOWN") or "UNKNOWN"
            counts[status] = counts.get(status, 0) + 1
        return counts


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
    """Resolve a possibly relative path against a base directory."""
    if value in (None, ""):
        raise ValueError("A required path value was empty.")
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def normalize_utm_tile_token(value: str) -> str:
    """Normalize one UTM/MGRS tile token, returning blank when invalid."""
    match = UTM_TILE_RE.match(str(value or "").strip().upper())
    if match is None:
        return ""
    return f"UTM{int(match.group('zone')):02d}{match.group('band')}"


def normalize_utm_tile_list(value: Any) -> List[str]:
    """Normalize an upload UTM tile list from YAML or text."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_tiles = re.split(r"[\s,;]+", value.strip())
    elif isinstance(value, (list, tuple, set)):
        raw_tiles = [str(item) for item in value]
    else:
        raise ValueError("upload.utm_tiles must be a list or comma-separated string.")

    tiles: List[str] = []
    seen: set[str] = set()
    invalid: List[str] = []
    for raw_tile in raw_tiles:
        tile = normalize_utm_tile_token(raw_tile)
        if not raw_tile:
            continue
        if not tile:
            invalid.append(str(raw_tile).strip())
            continue
        if tile not in seen:
            seen.add(tile)
            tiles.append(tile)
    if invalid:
        raise ValueError(f"Invalid upload UTM tile token(s): {', '.join(invalid)}")
    return tiles


def parse_csv_path_list(value: str) -> List[str]:
    """Parse a JSON list or conservative delimited path list from a CSV cell."""
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    return [part.strip() for part in re.split(r"[;|]", text) if part.strip()]


def source_tiles_from_file_name(file_name: str | Path) -> List[str]:
    """Return source UTM tile tokens parsed from one SWOT-style filename."""
    parsed = parse_swot_l2_hr_raster_metadata(file_name)
    if parsed is None:
        return []
    tile = normalize_utm_tile_token(parsed.fields.get("coordinate_system", ""))
    return [tile] if tile else []


def normalize_asset_record(asset: Dict[str, Any]) -> AssetInventoryRecord:
    """Normalize Earth Engine listAssets output across API versions."""
    asset_id = str(asset.get("id") or asset.get("name") or "")
    asset_type = str(asset.get("type") or "").upper()
    return AssetInventoryRecord(
        asset_id=asset_id,
        asset_type=asset_type,
        asset_name=Path(asset_id).name,
    )


def infer_project_from_asset(asset_id: str) -> Optional[str]:
    """Infer the Cloud project from a modern Earth Engine asset id."""
    match = re.match(r"^projects/([^/]+)/assets(?:/|$)", str(asset_id or ""))
    return match.group(1) if match else None


def import_ee():
    """Import the Earth Engine API only when inventory sync is needed."""
    try:
        import ee  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "The Earth Engine Python API is not installed. Install requirements and run "
            "earthengine authenticate before using upload verification."
        ) from exc
    return ee


def initialize_earth_engine(ee: Any, asset_parent: str) -> None:
    """Initialize Earth Engine for asset inventory listing."""
    project = infer_project_from_asset(asset_parent)
    try:
        if project:
            ee.Initialize(project=project)
        else:
            ee.Initialize()
    except Exception as exc:
        raise RuntimeError(
            "Could not initialize Earth Engine for upload verification. Run "
            "earthengine authenticate, then retry the upload."
        ) from exc


def load_config_file(config_path: Path) -> AppConfig:
    """Load YAML or JSON configuration from disk."""
    suffix = config_path.suffix.lower()
    with config_path.open("r", encoding="utf-8") as handle:
        if suffix == ".json":
            user_config = json.load(handle)
        else:
            user_config = yaml.safe_load(handle) or {}
    merged = deep_merge(DEFAULT_CONFIG, user_config)
    return parse_config(merged, config_path.parent.resolve())


def parse_config(data: Dict[str, Any], base_dir: Path) -> AppConfig:
    """Convert a raw dictionary into a validated AppConfig."""
    chrome_data = data.get("chrome", {})
    upload_data = data.get("upload", {})
    metadata_data = data.get("metadata", {})
    execution_data = data.get("execution", {})
    artifact_data = data.get("artifacts", {})
    mosaic_data = data.get("mosaic", {})
    pyramiding_data = upload_data.get("pyramiding_policy", {})
    upload_scope = str(upload_data.get("scope", UPLOAD_SCOPE_ALL)).strip().lower()
    if upload_scope not in VALID_UPLOAD_SCOPES:
        upload_scope = UPLOAD_SCOPE_ALL

    config = AppConfig(
        earth_engine_url=str(data.get("earth_engine_url", DEFAULT_CONFIG["earth_engine_url"])),
        input_folder=resolve_path(data.get("input_folder", ""), base_dir),
        destination_parent=str(data.get("destination_parent", "")).strip().rstrip("/"),
        chrome=ChromeConfig(
            user_data_dir=resolve_path(chrome_data.get("user_data_dir", "./chrome-profile"), base_dir),
            profile_directory=chrome_data.get("profile_directory"),
            binary_location=chrome_data.get("binary_location"),
            connection_mode=str(chrome_data.get("connection_mode", "attach")).strip().lower(),
            remote_debugging_port=int(chrome_data.get("remote_debugging_port", 9222)),
            headless=bool(chrome_data.get("headless", False)),
            start_maximized=bool(chrome_data.get("start_maximized", True)),
        ),
        upload=UploadConfig(
            batch_size=int(upload_data.get("batch_size", 50)),
            max_active_ingestions=int(upload_data.get("max_active_ingestions", 2)),
            scope=upload_scope,
            utm_tiles=normalize_utm_tile_list(upload_data.get("utm_tiles", [])),
            ee_sync_before_upload=bool(upload_data.get("ee_sync_before_upload", True)),
            ee_asset_inventory_page_size=max(
                1,
                int(upload_data.get("ee_asset_inventory_page_size", 1000)),
            ),
            prefix=str(upload_data.get("prefix", "")),
            suffix=str(upload_data.get("suffix", "")),
            replacement_rules=dict(upload_data.get("replacement_rules", {})),
            invalid_char_pattern=str(upload_data.get("invalid_char_pattern", r"[^A-Za-z0-9._-]+")),
            invalid_char_replacement=str(upload_data.get("invalid_char_replacement", "_")),
            recursive=bool(upload_data.get("recursive", False)),
            extensions=[str(value).lower() for value in upload_data.get("extensions", [".tif", ".tiff"])],
            pyramiding_policy=PyramidingPolicyConfig(
                default=pyramiding_data.get("default"),
                per_band=dict(pyramiding_data.get("per_band", {})),
            ),
            retry_attempts=max(1, int(upload_data.get("retry_attempts", 3))),
            retry_wait_seconds=float(upload_data.get("retry_wait_seconds", 3.0)),
            fail_fast=bool(upload_data.get("fail_fast", False)),
        ),
        metadata=MetadataConfig(
            enabled=bool(metadata_data.get("enabled", True)),
            parser=str(metadata_data.get("parser", "swot_l2_hr_raster")).strip().lower(),
            require_match=bool(metadata_data.get("require_match", True)),
            add_start_time=bool(metadata_data.get("add_start_time", True)),
            add_end_time=bool(metadata_data.get("add_end_time", True)),
            extra_properties=dict(metadata_data.get("extra_properties", {})),
        ),
        execution=ExecutionConfig(
            dry_run=bool(execution_data.get("dry_run", False)),
            resume=bool(execution_data.get("resume", True)),
            require_confirmation=bool(execution_data.get("require_confirmation", True)),
            task_poll_seconds=max(5, int(execution_data.get("task_poll_seconds", 20))),
            short_ui_wait_seconds=float(execution_data.get("short_ui_wait_seconds", 1.5)),
            wait_timeout_minutes=max(1, int(execution_data.get("wait_timeout_minutes", 720))),
            page_load_timeout_seconds=max(15, int(execution_data.get("page_load_timeout_seconds", 90))),
            verbose_console=bool(execution_data.get("verbose_console", True)),
        ),
        artifacts=ArtifactConfig(
            logs_dir=resolve_path(artifact_data.get("logs_dir", "./logs"), base_dir),
            artifacts_dir=resolve_path(artifact_data.get("artifacts_dir", "./artifacts"), base_dir),
            report_csv=resolve_path(artifact_data.get("report_csv", "./reports/upload_report.csv"), base_dir),
            ee_asset_inventory_csv=resolve_path(
                artifact_data.get("ee_asset_inventory_csv", "./reports/ee_asset_inventory.csv"),
                base_dir,
            ),
        ),
        mosaic_manifest_csv=(
            resolve_path(mosaic_data.get("manifest_csv"), base_dir)
            if mosaic_data.get("manifest_csv")
            else None
        ),
        base_dir=base_dir,
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    """Raise ValueError when the configuration is invalid."""
    if not config.input_folder.exists():
        raise ValueError(f"Input folder does not exist: {config.input_folder}")
    if not config.input_folder.is_dir():
        raise ValueError(f"Input folder is not a directory: {config.input_folder}")
    if not config.destination_parent:
        raise ValueError("destination_parent is required.")
    if config.upload.batch_size < 1:
        raise ValueError("batch_size must be 1 or greater.")
    if config.upload.max_active_ingestions < 0:
        raise ValueError("max_active_ingestions cannot be negative.")
    if config.upload.scope not in VALID_UPLOAD_SCOPES:
        raise ValueError(f"upload.scope must be one of: {', '.join(sorted(VALID_UPLOAD_SCOPES))}.")
    if config.upload.scope == UPLOAD_SCOPE_SELECTED_UTM and not config.upload.utm_tiles:
        raise ValueError("upload.utm_tiles must contain at least one tile when upload.scope is selected_utm.")
    if config.chrome.connection_mode not in {"attach", "webdriver"}:
        raise ValueError("chrome.connection_mode must be either 'attach' or 'webdriver'.")
    if config.chrome.remote_debugging_port < 1:
        raise ValueError("chrome.remote_debugging_port must be 1 or greater.")
    if config.chrome.connection_mode == "attach" and config.chrome.headless:
        raise ValueError(
            "chrome.headless cannot be true when chrome.connection_mode is 'attach'."
        )
    if config.metadata.enabled and config.metadata.parser != "swot_l2_hr_raster":
        raise ValueError("metadata.parser must be 'swot_l2_hr_raster'.")
    for property_name, source_field in config.metadata.extra_properties.items():
        if not str(property_name).strip() or not str(source_field).strip():
            raise ValueError("metadata.extra_properties cannot contain empty names or source fields.")


def prompt_bool(prompt: str, default: bool) -> bool:
    """Ask the user a yes/no question in interactive mode."""
    default_text = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def prompt_int(prompt: str, default: int) -> int:
    """Ask the user for an integer value."""
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        try:
            return int(answer)
        except ValueError:
            print("Please enter a whole number.")


def prompt_text(prompt: str, default: str = "", allow_empty: bool = True) -> str:
    """Ask the user for a text value."""
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default:
            return default
        if allow_empty:
            return ""
        print("This value is required.")


def prompt_interactive_config() -> AppConfig:
    """Build configuration interactively when --config is omitted."""
    print(
        textwrap.dedent(
            """
            Interactive mode
            ----------------
            Press Enter to accept the default shown in brackets.
            A dedicated Chrome profile will be stored in ./chrome-profile unless you edit the config later.
            """
        ).strip()
    )
    local_folder = prompt_text("Local folder containing GeoTIFF files", allow_empty=False)
    destination_parent = prompt_text(
        "Earth Engine destination parent asset path",
        allow_empty=False,
    )
    batch_size = prompt_int("Batch size", 50)
    prefix = prompt_text("Optional asset name prefix", default="")
    suffix = prompt_text("Optional asset name suffix", default="")
    pyramiding_default = prompt_text(
        "Optional global pyramiding policy (leave blank to keep Earth Engine default)",
        default="",
    )
    add_metadata = prompt_bool("Add SWOT metadata properties from filenames", True)
    require_metadata_match = prompt_bool("Require SWOT filename match", True)
    resume = prompt_bool("Resume mode", True)
    dry_run = prompt_bool("Dry run first", True)

    config_data = deep_merge(
        DEFAULT_CONFIG,
        {
            "input_folder": local_folder,
            "destination_parent": destination_parent,
            "upload": {
                "batch_size": batch_size,
                "prefix": prefix,
                "suffix": suffix,
                "pyramiding_policy": {
                    "default": pyramiding_default or None,
                    "per_band": {},
                },
            },
            "execution": {
                "resume": resume,
                "dry_run": dry_run,
            },
            "metadata": {
                "enabled": add_metadata,
                "parser": "swot_l2_hr_raster",
                "require_match": require_metadata_match,
                "add_start_time": True,
                "add_end_time": True,
                "extra_properties": DEFAULT_CONFIG["metadata"]["extra_properties"],
            },
        },
    )
    return parse_config(config_data, Path.cwd())


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Path to a YAML or JSON config file.")
    parser.add_argument("--dry-run", action="store_true", help="Override config and do not click UPLOAD.")
    parser.add_argument("--resume", action="store_true", help="Override config and enable resume mode.")
    parser.add_argument("--no-resume", action="store_true", help="Override config and disable resume mode.")
    parser.add_argument("--yes", action="store_true", help="Skip the safety confirmation prompt.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless.")
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="Only sync Earth Engine assets into the local upload report; do not upload.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show debug logging in the console.")
    return parser


def configure_logging(config: AppConfig, verbose_override: bool) -> logging.Logger:
    """Create a logger that writes to both the console and a log file."""
    config.artifacts.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.artifacts.logs_dir / f"ee_ui_uploader_{timestamp_for_filename()}.log"

    logger = logging.getLogger("ee_ui_uploader")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_level = logging.DEBUG if verbose_override else logging.INFO
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.debug("Log file: %s", log_path)
    return logger


def timestamp_for_filename() -> str:
    """Return a filesystem-friendly timestamp."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    """Return the current local timestamp in ISO-like format."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def chunked(items: Sequence[UploadItem], size: int) -> List[List[UploadItem]]:
    """Split a sequence into fixed-size chunks."""
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


class EarthEngineUIUploader:
    """Main Selenium automation controller."""

    def __init__(self, config: AppConfig, logger: logging.Logger, assume_yes: bool) -> None:
        self.config = config
        self.logger = logger
        self.assume_yes = assume_yes
        self.driver: Optional[WebDriver] = None
        self.report = ReportManager(self.config.artifacts.report_csv)
        self.report.load()
        self.resume_skipped_assets: List[str] = []
        self.tracked_items: Dict[str, UploadItem] = {}
        self.ee_inventory_synced = False
        self.ee_existing_asset_ids: set[str] = set()

        self.config.artifacts.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.config.artifacts.logs_dir.mkdir(parents=True, exist_ok=True)
        self.config.artifacts.report_csv.parent.mkdir(parents=True, exist_ok=True)

    def run(self) -> int:
        """Run the upload workflow end to end."""
        try:
            planned_items = self.build_upload_plan()
        except KeyboardInterrupt:
            self.logger.warning("Interrupted by user while building the upload plan.")
            return 130
        except Exception:
            self.logger.exception("Fatal error while building the upload plan.")
            return 1
        if not planned_items:
            self.logger.info("No GeoTIFF files need processing. Nothing to do.")
            return 0

        if self.config.execution.dry_run:
            self.run_dry_run(planned_items)
            return 0

        self.confirm_before_real_upload(planned_items)
        self.driver = self.create_driver()

        try:
            self.open_earth_engine()
            self.ensure_logged_in()
            self.process_batches(planned_items)
            self.wait_for_all_tracked_tasks()
        except FatalBrowserSessionError as exc:
            self.logger.error("%s", exc)
            return 1
        except WebDriverException as exc:
            if is_invalid_browser_session(exc):
                self.logger.error("%s", FATAL_BROWSER_SESSION_MESSAGE)
                return 1
            self.logger.exception("Fatal WebDriver error during upload automation.")
            self.capture_debug_artifacts("fatal_webdriver_error")
            return 1
        except KeyboardInterrupt:
            self.logger.warning("Interrupted by user. Current report has been saved.")
            self.capture_debug_artifacts("keyboard_interrupt")
            return 130
        except Exception:
            self.logger.exception("Fatal error during upload automation.")
            self.capture_debug_artifacts("fatal_error")
            return 1
        finally:
            if self.driver is not None:
                try:
                    self.driver.quit()
                except WebDriverException:
                    pass

        counts = self.report.counts(asset.asset_id for asset in planned_items)
        self.logger.info("Run finished. Status summary: %s", counts)
        if counts.get("FAILED") or counts.get("ERROR") or counts.get(UNKNOWN_AFTER_CLICK_STATUS):
            return 2
        return 0

    def build_upload_plan(self) -> List[UploadItem]:
        """Scan the input folder and build the list of uploads."""
        self.sync_earth_engine_inventory()
        files = self.collect_input_files()
        if not files:
            self.logger.warning("No matching GeoTIFF files were found in %s", self.config.input_folder)
            return []
        self.logger.info(
            "Found %s GeoTIFF file(s) in %s. Building upload plan...",
            len(files),
            self.config.input_folder,
        )

        planned: List[UploadItem] = []
        report_rows: List[Dict[str, str]] = []
        seen_asset_ids: Dict[str, Path] = {}
        source_tile_lookup = self.load_mosaic_source_tile_lookup()
        filtered_count = 0
        ee_verified_count = 0

        for file_path in files:
            asset_name = self.build_asset_name(file_path.stem)
            asset_id = f"{self.config.destination_parent}/{asset_name}"
            item = UploadItem(
                local_file=file_path,
                asset_name=asset_name,
                asset_id=asset_id,
            )
            self.apply_upload_filter_metadata(item, source_tile_lookup)
            existing_status = self.report.get_status(asset_id)
            if self.ee_inventory_synced and asset_id in self.ee_existing_asset_ids:
                item.ee_asset_exists = True
                item.ee_verified_at = now_iso()
                item.verification_source = "ee.data.listAssets"
                ee_verified_count += 1
                report_rows.append(
                    self.make_report_row(
                        item=item,
                        submit_time="",
                        detected_task_name="",
                        final_status=EE_VERIFIED_EXISTS_STATUS,
                        error_message="Asset exists in the target Earth Engine collection.",
                    )
                )
                self.resume_skipped_assets.append(asset_id)
                self.logger.info("Earth Engine verified skip: %s", asset_id)
                continue
            if self.should_resume_skip_existing_status(existing_status):
                self.resume_skipped_assets.append(asset_id)
                self.logger.info("Resume skip: %s (%s)", asset_id, existing_status)
                continue
            if not item.upload_selected:
                filtered_count += 1
                if existing_status in PRESERVE_ON_FILTER_STATUSES:
                    self.logger.info(
                        "Preserving existing upload status for non-selected asset: %s (%s)",
                        asset_id,
                        existing_status,
                    )
                    continue
                report_rows.append(
                    self.make_report_row(
                        item=item,
                        submit_time="",
                        detected_task_name="",
                        final_status=FILTERED_UTM_STATUS,
                        error_message="Excluded by upload UTM/source-tile selection.",
                    )
                )
                continue
            if asset_id in seen_asset_ids:
                previous = seen_asset_ids[asset_id]
                raise ValueError(
                    "Two files resolve to the same Earth Engine asset ID after naming rules were applied:\n"
                    f"  - {previous}\n"
                    f"  - {file_path}\n"
                    f"Duplicate asset ID: {asset_id}"
                )
            seen_asset_ids[asset_id] = file_path
            planned.append(item)

        for batch_index, batch in enumerate(chunked(planned, self.config.upload.batch_size), start=1):
            for item in batch:
                item.batch_number = batch_index
                self.tracked_items[item.asset_id] = item
                if not self.config.execution.dry_run:
                    report_rows.append(
                        self.make_report_row(
                            item=item,
                            submit_time="",
                            detected_task_name="",
                            final_status=PLANNED_UPLOAD_STATUS,
                            error_message="Planned for real upload; awaiting submission.",
                        )
                    )
        self.logger.info(
            "Prepared %s upload(s). Resume skipped %s asset(s). Filtered %s file(s) by upload scope. EE-verified skipped %s asset(s).",
            len(planned),
            len(self.resume_skipped_assets),
            filtered_count,
            ee_verified_count,
        )
        if report_rows:
            self.logger.info(
                "Writing %s planning report row(s): %s filtered by UTM, %s EE-verified skip(s), %s planned upload(s).",
                len(report_rows),
                filtered_count,
                ee_verified_count,
                0 if self.config.execution.dry_run else len(planned),
            )
            self.report.upsert_many(report_rows)
        return planned

    def should_resume_skip_existing_status(self, existing_status: str) -> bool:
        """Return whether a previous report status should skip this upload."""
        status = str(existing_status or "").strip().upper()
        if not self.config.execution.resume or status not in RESUME_SKIP_STATUSES:
            return False
        if not self.ee_inventory_synced:
            return True
        if status in ACTIVE_STATUSES:
            return True
        if status == EE_VERIFIED_EXISTS_STATUS:
            return True
        return False

    def sync_earth_engine_inventory(self) -> None:
        """List target Earth Engine assets and cache them for resume decisions."""
        if not self.config.upload.ee_sync_before_upload:
            return
        ee = import_ee()
        initialize_earth_engine(ee, self.config.destination_parent)
        records = self.list_earth_engine_assets(ee)
        self.write_asset_inventory(records)
        self.ee_inventory_synced = True
        self.ee_existing_asset_ids = {
            record.asset_id for record in records if record.asset_type in {"IMAGE", ""}
        }
        self.logger.info(
            "Earth Engine inventory sync listed %s asset(s) under %s.",
            len(records),
            self.config.destination_parent,
        )

    def sync_existing_assets_to_report(self) -> int:
        """Sync EE inventory and mark matching local files as verified existing."""
        previous_sync_setting = self.config.upload.ee_sync_before_upload
        self.config.upload.ee_sync_before_upload = True
        try:
            self.sync_earth_engine_inventory()
        finally:
            self.config.upload.ee_sync_before_upload = previous_sync_setting

        files = self.collect_input_files()
        if not files:
            self.logger.warning("No matching GeoTIFF files were found in %s", self.config.input_folder)
            return 0

        source_tile_lookup = self.load_mosaic_source_tile_lookup()
        rows: List[Dict[str, str]] = []
        for file_path in files:
            asset_name = self.build_asset_name(file_path.stem)
            asset_id = f"{self.config.destination_parent}/{asset_name}"
            if asset_id not in self.ee_existing_asset_ids:
                continue
            item = UploadItem(
                local_file=file_path,
                asset_name=asset_name,
                asset_id=asset_id,
                ee_asset_exists=True,
                ee_verified_at=now_iso(),
                verification_source="ee.data.listAssets",
            )
            self.apply_upload_filter_metadata(item, source_tile_lookup)
            rows.append(
                self.make_report_row(
                    item=item,
                    submit_time="",
                    detected_task_name="",
                    final_status=EE_VERIFIED_EXISTS_STATUS,
                    error_message="Asset exists in the target Earth Engine collection.",
                )
            )

        self.report.upsert_many(rows)
        self.logger.info(
            "EE asset sync marked %s local file(s) as %s in %s.",
            len(rows),
            EE_VERIFIED_EXISTS_STATUS,
            self.config.artifacts.report_csv,
        )
        return len(rows)

    def list_earth_engine_assets(self, ee: Any) -> List[AssetInventoryRecord]:
        """Return direct child assets from the configured destination parent."""
        records: List[AssetInventoryRecord] = []
        params: Dict[str, str] = {
            "parent": self.config.destination_parent,
            "pageSize": str(self.config.upload.ee_asset_inventory_page_size),
            "view": "BASIC",
        }
        while True:
            try:
                response = ee.data.listAssets(params)
            except TypeError:
                list_params = {key: value for key, value in params.items() if key != "parent"}
                response = ee.data.listAssets(self.config.destination_parent, list_params)
            records.extend(
                normalize_asset_record(asset)
                for asset in response.get("assets", [])
            )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = str(page_token)
        return records

    def write_asset_inventory(self, records: Sequence[AssetInventoryRecord]) -> None:
        """Write a compact Earth Engine asset inventory CSV."""
        path = self.config.artifacts.ee_asset_inventory_csv
        path.parent.mkdir(parents=True, exist_ok=True)
        listed_at = now_iso()
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["asset_id", "asset_name", "asset_type", "parent", "listed_at"],
            )
            writer.writeheader()
            for record in records:
                writer.writerow(
                    {
                        "asset_id": record.asset_id,
                        "asset_name": record.asset_name,
                        "asset_type": record.asset_type,
                        "parent": self.config.destination_parent,
                        "listed_at": listed_at,
                    }
                )

    def load_mosaic_source_tile_lookup(self) -> Dict[str, List[str]]:
        """Map mosaic outputs to source UTM tiles from the mosaic manifest."""
        path = self.config.mosaic_manifest_csv
        if path is None or not path.exists():
            return {}
        lookup: Dict[str, List[str]] = {}
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = csv.DictReader(handle)
                for row in rows:
                    output = str(row.get("output_file", "") or "").strip()
                    if not output:
                        continue
                    tiles: set[str] = set()
                    for source in parse_csv_path_list(row.get("input_files", "")):
                        tiles.update(source_tiles_from_file_name(source))
                    if not tiles:
                        continue
                    sorted_tiles = sorted(tiles)
                    output_path = Path(output)
                    lookup[output_path.name] = sorted_tiles
                    try:
                        lookup[str(output_path.resolve())] = sorted_tiles
                    except OSError:
                        lookup[str(output_path)] = sorted_tiles
        except OSError as exc:
            self.logger.warning("Could not read mosaic source-tile manifest %s: %s", path, exc)
        return lookup

    def apply_upload_filter_metadata(
        self,
        item: UploadItem,
        source_tile_lookup: Dict[str, List[str]],
    ) -> None:
        """Attach output-grid/source-tile metadata and filter one upload item."""
        parsed = parse_swot_l2_hr_raster_metadata(item.local_file)
        if parsed is not None:
            item.output_grid = str(parsed.fields.get("coordinate_system", "") or "").upper()
            item.source_utm_tiles = source_tiles_from_file_name(item.local_file)
        manifest_tiles = self.lookup_mosaic_source_tiles(item.local_file, source_tile_lookup)
        if manifest_tiles:
            item.source_utm_tiles = sorted(set(item.source_utm_tiles).union(manifest_tiles))

        if self.config.upload.scope == UPLOAD_SCOPE_ALL:
            item.upload_selected = True
            item.upload_filter_status = FILTER_STATUS_SELECTED_ALL
            return

        selected_tiles = set(self.config.upload.utm_tiles)
        item_tiles = set(item.source_utm_tiles)
        item.upload_selected = bool(item_tiles & selected_tiles)
        if item.upload_selected:
            item.upload_filter_status = FILTER_STATUS_SELECTED_UTM
        elif item_tiles:
            item.upload_filter_status = FILTER_STATUS_FILTERED_UTM
        else:
            item.upload_filter_status = FILTER_STATUS_FILTERED_UNKNOWN

    def lookup_mosaic_source_tiles(
        self,
        file_path: Path,
        source_tile_lookup: Dict[str, List[str]],
    ) -> List[str]:
        """Return source UTM tiles for a mosaic output path when known."""
        keys = [file_path.name]
        try:
            keys.append(str(file_path.resolve()))
        except OSError:
            keys.append(str(file_path))
        for key in keys:
            if key in source_tile_lookup:
                return list(source_tile_lookup[key])
        return []

    def collect_input_files(self) -> List[Path]:
        """Return sorted GeoTIFF files from the input directory."""
        extensions = {ext.lower() for ext in self.config.upload.extensions}
        globber = self.config.input_folder.rglob if self.config.upload.recursive else self.config.input_folder.glob
        files = [
            path.resolve()
            for path in globber("*")
            if path.is_file() and path.suffix.lower() in extensions
        ]
        return sorted(files)

    def build_asset_name(self, stem: str) -> str:
        """Create an Earth Engine asset name from a file stem."""
        name = stem
        for old, new in self.config.upload.replacement_rules.items():
            name = name.replace(old, new)
        name = re.sub(
            self.config.upload.invalid_char_pattern,
            self.config.upload.invalid_char_replacement,
            name,
        )
        name = f"{self.config.upload.prefix}{name}{self.config.upload.suffix}"
        name = re.sub(
            self.config.upload.invalid_char_pattern,
            self.config.upload.invalid_char_replacement,
            name,
        )
        name = re.sub(r"_+", "_", name).strip("._-")
        return name or "asset"

    def run_dry_run(self, planned_items: Sequence[UploadItem]) -> None:
        """Record and print planned uploads without touching the UI."""
        self.logger.info("Dry-run mode is enabled. No browser automation will run.")
        report_rows: List[Dict[str, str]] = []
        preview_limit = 25
        for index, item in enumerate(planned_items, start=1):
            metadata = self.parse_metadata_for_item(item, required=False)
            report_rows.append(
                self.make_report_row(
                    item=item,
                    submit_time="",
                    detected_task_name="",
                    final_status="PLANNED_DRY_RUN",
                    error_message="Dry run only. No upload clicked.",
                )
            )
            if index <= preview_limit:
                self.logger.info("Plan: %s -> %s", item.local_file, item.asset_id)
                if metadata.status == "METADATA_PARSED":
                    self.logger.info(
                        "Metadata: start=%s end=%s properties=%s",
                        metadata.start_time,
                        metadata.end_time,
                        metadata.properties,
                    )
                elif metadata.error_message:
                    self.logger.warning("Metadata: %s", metadata.error_message)
        if len(planned_items) > preview_limit:
            self.logger.info(
                "Dry-run console preview limited to first %s of %s planned upload(s). Full details are in the report CSV.",
                preview_limit,
                len(planned_items),
            )
        self.report.upsert_many(report_rows)
        self.logger.info(
            "Dry-run complete. Planned %s upload(s). Report written to %s",
            len(planned_items),
            self.config.artifacts.report_csv,
        )

    def confirm_before_real_upload(self, planned_items: Sequence[UploadItem]) -> None:
        """Ask for a final confirmation before real uploads begin."""
        if self.assume_yes or not self.config.execution.require_confirmation:
            return
        summary = textwrap.dedent(
            f"""
            About to perform real Earth Engine uploads.

            Files to submit: {len(planned_items)}
            Destination parent: {self.config.destination_parent}
            Batch size: {self.config.upload.batch_size}
            Max active ingestions between batches: {self.config.upload.max_active_ingestions}
            Chrome profile directory: {self.config.chrome.user_data_dir}

            Type YES to continue:
            """
        ).strip()
        print(summary)
        answer = input("> ").strip()
        if answer != "YES":
            raise SystemExit("Upload cancelled before any real UI action.")

    def create_driver(self) -> WebDriver:
        """Start a Chrome browser using a persistent profile folder."""
        self.config.chrome.user_data_dir.mkdir(parents=True, exist_ok=True)
        if self.config.chrome.connection_mode == "attach":
            return self.create_attached_driver()

        options = ChromeOptions()
        options.add_argument(f"--user-data-dir={self.config.chrome.user_data_dir}")
        if self.config.chrome.profile_directory:
            options.add_argument(f"--profile-directory={self.config.chrome.profile_directory}")
        if self.config.chrome.start_maximized:
            options.add_argument("--start-maximized")
        if self.config.chrome.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-popup-blocking")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        if self.config.chrome.binary_location:
            options.binary_location = self.config.chrome.binary_location

        try:
            driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise RuntimeError(
                "Could not start Chrome. Check the README and TROUBLESHOOTING guide for "
                "Chrome version, driver, and profile-lock guidance."
            ) from exc

        driver.set_page_load_timeout(self.config.execution.page_load_timeout_seconds)
        return driver

    def create_attached_driver(self) -> WebDriver:
        """Attach Selenium to a normal Chrome instance started with remote debugging.

        This is the default mode because Google sign-in often rejects directly
        automated Chrome sessions during login.
        """
        port = self.config.chrome.remote_debugging_port
        if not self.is_debug_port_open("127.0.0.1", port):
            self.launch_attachable_chrome()
            self.wait_for_debug_port("127.0.0.1", port, timeout_seconds=20)

        options = ChromeOptions()
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
        if self.config.chrome.binary_location:
            options.binary_location = self.config.chrome.binary_location

        try:
            driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise RuntimeError(
                "Could not attach to the normal Chrome instance. "
                "Check whether Chrome was launched with the configured remote debugging port."
            ) from exc

        driver.set_page_load_timeout(self.config.execution.page_load_timeout_seconds)
        return driver

    def launch_attachable_chrome(self) -> None:
        """Start a normal Chrome process with remote debugging enabled."""
        chrome_binary = self.find_chrome_binary()
        port = self.config.chrome.remote_debugging_port
        command = [
            chrome_binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.config.chrome.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.config.chrome.profile_directory:
            command.append(f"--profile-directory={self.config.chrome.profile_directory}")
        if self.config.chrome.start_maximized:
            command.append("--start-maximized")
        command.append(self.config.earth_engine_url)

        creationflags = 0
        for flag_name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS"):
            creationflags |= getattr(subprocess, flag_name, 0)

        try:
            subprocess.Popen(
                command,
                cwd=str(self.config.base_dir),
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Could not launch Chrome from '{chrome_binary}'."
            ) from exc

        self.logger.info(
            "Started normal Chrome in attach mode on port %s using profile %s",
            port,
            self.config.chrome.user_data_dir,
        )

    def find_chrome_binary(self) -> str:
        """Return the best available Chrome executable path on Windows."""
        if self.config.chrome.binary_location:
            return self.config.chrome.binary_location

        candidates = [
            shutil.which("chrome"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        raise RuntimeError(
            "Could not find Google Chrome automatically. Set chrome.binary_location in config.yaml."
        )

    def is_debug_port_open(self, host: str, port: int) -> bool:
        """Return True when a Chrome debugging port is already reachable."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            return connection.connect_ex((host, port)) == 0

    def wait_for_debug_port(self, host: str, port: int, timeout_seconds: int) -> None:
        """Wait for the Chrome debugging endpoint to become reachable."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.is_debug_port_open(host, port):
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"Chrome debugging port {port} did not become available in time."
        )

    def open_earth_engine(self) -> None:
        """Open the Earth Engine Code Editor."""
        assert self.driver is not None
        self.logger.info("Opening Earth Engine: %s", self.config.earth_engine_url)
        self.driver.get(self.config.earth_engine_url)

    def ensure_logged_in(self) -> None:
        """Wait for the user to log in manually when required."""
        if self.is_logged_in():
            self.logger.info("Earth Engine appears to be logged in.")
            return

        assert self.driver is not None
        browser_mode_message = (
            "The tool is using normal Chrome attach mode. Keep that Chrome window open while you sign in."
            if self.config.chrome.connection_mode == "attach"
            else "The tool is using Selenium-managed Chrome."
        )
        print(
            textwrap.dedent(
                f"""
                Manual login is required.
                1. Use the opened Chrome window.
                2. Sign in to your Google account if prompted.
                3. Wait until the Earth Engine Code Editor is fully visible.
                4. Return here and press Enter.
                5. Do not close the Chrome window.

                {browser_mode_message}
                """
            ).strip()
        )
        input()
        try:
            self.driver.get(self.config.earth_engine_url)
        except InvalidSessionIdException as exc:
            raise RuntimeError(
                "The Chrome window was closed or disconnected during the manual login step. "
                "Re-run the uploader and keep the Chrome window open."
            ) from exc
        try:
            self.wait_for_any_selector(["assets_tab", "new_button"], timeout=120, must_be_clickable=False)
            self.logger.info("Login confirmed after manual step.")
        except TimeoutException as exc:
            self.capture_debug_artifacts("login_timeout")
            raise RuntimeError(
                "Earth Engine still does not look logged in after the manual login step."
            ) from exc

    def is_logged_in(self) -> bool:
        """Best-effort check for login state."""
        assert self.driver is not None
        current_url = self.driver.current_url.lower()
        if "accounts.google.com" in current_url:
            return False
        try:
            self.wait_for_any_selector(["assets_tab", "new_button"], timeout=8, must_be_clickable=False)
            return True
        except TimeoutException:
            try:
                self.find_first_element("login_prompt", timeout=2, visible_only=False)
                return False
            except TimeoutException:
                return False

    def process_batches(self, planned_items: Sequence[UploadItem]) -> None:
        """Submit uploads batch by batch."""
        batches = chunked(list(planned_items), self.config.upload.batch_size)
        for batch_index, batch in enumerate(batches, start=1):
            self.logger.info("Starting batch %s of %s (%s files).", batch_index, len(batches), len(batch))
            for item in batch:
                self.submit_with_retries(item)
                if self.config.upload.fail_fast and self.report.get_status(item.asset_id) in {"FAILED", "ERROR"}:
                    raise RuntimeError(f"Fail-fast stopping after a failure on {item.asset_id}")
            self.wait_for_batch_gate(batch)

    def submit_with_retries(self, item: UploadItem) -> None:
        """Submit one upload with retry handling for transient UI failures."""
        last_exception: Optional[BaseException] = None
        for attempt in range(1, self.config.upload.retry_attempts + 1):
            try:
                self.logger.info(
                    "Submitting %s (batch %s, attempt %s/%s)",
                    item.asset_id,
                    item.batch_number,
                    attempt,
                    self.config.upload.retry_attempts,
                )
                self.ensure_assets_tab()
                self.open_image_upload_dialog()
                self.populate_upload_dialog(item)
                task_name = self.submit_upload_dialog(item)
                self.report.upsert(
                    self.make_report_row(
                        item=item,
                        submit_time=now_iso(),
                        detected_task_name=task_name,
                        final_status="SUBMITTED",
                        error_message="",
                    )
                )
                return
            except AlreadyExistsError as exc:
                self.logger.warning("%s", exc)
                self.report.upsert(
                    self.make_report_row(
                        item=item,
                        submit_time=now_iso(),
                        detected_task_name="",
                        final_status="SKIPPED_ALREADY_EXISTS",
                        error_message=str(exc),
                    )
                )
                self.close_dialog_if_possible()
                return
            except UnknownAfterClickError as exc:
                self.logger.warning("%s", exc)
                self.report.upsert(
                    self.make_report_row(
                        item=item,
                        submit_time=now_iso(),
                        detected_task_name="",
                        final_status=UNKNOWN_AFTER_CLICK_STATUS,
                        error_message=str(exc),
                    )
                )
                self.close_dialog_if_possible()
                return
            except (
                RetryableUIError,
                TimeoutException,
                StaleElementReferenceException,
                ElementClickInterceptedException,
                UnexpectedAlertPresentException,
                WebDriverException,
            ) as exc:
                raise_if_invalid_browser_session(exc)
                last_exception = exc
                self.logger.warning(
                    "Retryable UI problem while processing %s: %s",
                    item.asset_id,
                    exc,
                )
                self.capture_debug_artifacts(f"retry_{sanitize_for_filename(item.asset_name)}_{attempt}")
                if attempt < self.config.upload.retry_attempts:
                    self.recover_ui_state()
                    time.sleep(self.config.upload.retry_wait_seconds)
                    continue
                break
            except Exception as exc:
                raise_if_invalid_browser_session(exc)
                last_exception = exc
                self.capture_debug_artifacts(f"fatal_{sanitize_for_filename(item.asset_name)}")
                break

        message = str(last_exception) if last_exception else "Unknown error"
        self.logger.error("Giving up on %s: %s", item.asset_id, message)
        self.report.upsert(
            self.make_report_row(
                item=item,
                submit_time=now_iso(),
                detected_task_name="",
                final_status="ERROR",
                error_message=message,
            )
        )

    def ensure_assets_tab(self) -> None:
        """Switch to the Assets panel."""
        if self._select_code_editor_tab(
            panel_index=int(ee_selectors.SHADOW_SELECTORS["left_tab_panel_index"]),
            label=str(ee_selectors.SHADOW_SELECTORS["assets_tab_label"]),
        ):
            time.sleep(self.config.execution.short_ui_wait_seconds)
            return
        self.click_first_available("assets_tab")
        time.sleep(self.config.execution.short_ui_wait_seconds)

    def ensure_tasks_tab(self) -> None:
        """Switch to the Tasks panel."""
        if self._select_code_editor_tab(
            panel_index=int(ee_selectors.SHADOW_SELECTORS["right_tab_panel_index"]),
            label=str(ee_selectors.SHADOW_SELECTORS["tasks_tab_label"]),
        ):
            time.sleep(self.config.execution.short_ui_wait_seconds)
            return
        self.click_first_available("tasks_tab")
        time.sleep(self.config.execution.short_ui_wait_seconds)

    def open_image_upload_dialog(self) -> None:
        """Open the Earth Engine image upload dialog using fallback navigation."""
        attempts = [
            self._open_upload_via_assets_panel_menu,
            self._open_upload_via_new_menu,
            self._open_upload_directly,
        ]
        last_error: Optional[BaseException] = None
        for opener in attempts:
            try:
                opener()
                if self._wait_for_current_upload_dialog(timeout=15):
                    return
                self.find_first_element("file_input", timeout=15, visible_only=False)
                self.find_first_element("asset_id_field", timeout=15, visible_only=True)
                return
            except Exception as exc:
                last_error = exc
                self.close_dialog_if_possible()
        raise RetryableUIError("Could not open the Earth Engine image upload dialog.") from last_error

    def _open_upload_via_assets_panel_menu(self) -> None:
        """Use the Assets-side NEW control in the current Earth Engine UI."""
        self.ensure_assets_tab()
        self._open_current_ui_image_upload_dialog()

    def _open_upload_via_new_menu(self) -> None:
        """Try the common Assets > NEW > Image upload path."""
        self.ensure_assets_tab()
        self.click_first_available("new_button")
        time.sleep(self.config.execution.short_ui_wait_seconds)
        self.click_first_available("image_upload_button")

    def _open_upload_directly(self) -> None:
        """Try to click an Image upload control without the NEW menu."""
        self.ensure_assets_tab()
        self.click_first_available("image_upload_button")

    def populate_upload_dialog(self, item: UploadItem) -> None:
        """Fill the upload dialog fields for a single asset."""
        if self.is_current_upload_dialog_open():
            self._populate_current_ui_upload_dialog(item)
        else:
            file_input = self.find_first_element("file_input", timeout=15, visible_only=False)
            file_input.send_keys(str(item.local_file))

            if not self.populate_destination_fields(item):
                asset_field = self.find_first_element("asset_id_field", timeout=15, visible_only=True)
                self.clear_and_type(asset_field, item.asset_id)

            if self.config.upload.pyramiding_policy.enabled():
                self.apply_pyramiding_policy()

        self.apply_metadata_properties(item)

    def populate_destination_fields(self, item: UploadItem) -> bool:
        """Fill separate collection/name fields when the UI exposes them.

        Returns True when separate fields were found and populated.
        Returns False when the uploader should fall back to a single asset ID field.
        """
        collection_field = self.find_optional_element(
            "destination_collection_field",
            timeout=4,
            visible_only=True,
        )
        name_field = self.find_optional_element(
            "asset_name_field",
            timeout=4,
            visible_only=True,
        )

        if collection_field is None or name_field is None:
            return False

        self.clear_and_type(collection_field, self.config.destination_parent)
        self.clear_and_type(name_field, item.asset_name)
        return True

    def submit_upload_dialog(self, item: UploadItem) -> str:
        """Click UPLOAD and return a detected task name when possible."""
        if self.is_current_upload_dialog_open():
            self._click_current_ui_upload_button()
            time.sleep(self.config.execution.short_ui_wait_seconds)

            error_message = self.read_dialog_error_message()
            if error_message:
                if "already exists" in error_message.lower():
                    raise AlreadyExistsError(error_message)
                raise RetryableUIError(error_message)

            try:
                return self.wait_for_task_name(item, timeout_seconds=30)
            except TimeoutException as exc:
                error_message = self.read_dialog_error_message()
                if error_message:
                    if "already exists" in error_message.lower():
                        raise AlreadyExistsError(error_message) from exc
                    raise RetryableUIError(error_message) from exc
                raise UnknownAfterClickError(
                    "Upload was clicked, but no matching task row or explicit upload-dialog error was detected. "
                    "Verify the asset in Earth Engine before retrying."
                ) from exc

        self.click_first_available("upload_button")
        time.sleep(self.config.execution.short_ui_wait_seconds)

        error_message = self.read_dialog_error_message()
        if error_message:
            if "already exists" in error_message.lower():
                raise AlreadyExistsError(error_message)
            raise RetryableUIError(error_message)

        try:
            return self.wait_for_task_name(item, timeout_seconds=30)
        except TimeoutException as exc:
            error_message = self.read_dialog_error_message()
            if error_message:
                if "already exists" in error_message.lower():
                    raise AlreadyExistsError(error_message) from exc
                raise RetryableUIError(error_message) from exc
            raise UnknownAfterClickError(
                "Upload was clicked, but no matching task row or explicit upload-dialog error was detected. "
                "Verify the asset in Earth Engine before retrying."
            ) from exc

    def apply_pyramiding_policy(self) -> None:
        """Best-effort handling for optional pyramiding policy controls."""
        if self.is_current_upload_dialog_open() and self._apply_current_ui_pyramiding_policy():
            return

        try:
            self.click_first_available("pyramiding_policy_expand_button")
            time.sleep(self.config.execution.short_ui_wait_seconds)
        except TimeoutException:
            self.logger.warning(
                "Pyramiding policy section was not found. Edit ee_selectors.py if the UI changed."
            )
            raise RetryableUIError("Could not find the pyramiding policy section.")

        default_policy = self.config.upload.pyramiding_policy.default
        if default_policy:
            control = self.find_first_element(
                "pyramiding_policy_global_select",
                timeout=10,
                visible_only=True,
            )
            self.select_option(control, default_policy)

        for band_name, band_policy in self.config.upload.pyramiding_policy.per_band.items():
            self.set_band_policy(band_name, band_policy)

    def is_current_upload_dialog_open(self) -> bool:
        """Return True when the current Earth Engine upload dialog is present."""
        script = f"""
        {active_upload_dialog_helper_js()}
        const dialog = findActiveUploadDialog();
        return !!(dialog && dialog.shadowRoot);
        """
        try:
            return bool(self.execute_script(script))
        except WebDriverException as exc:
            raise_if_invalid_browser_session(exc)
            return False

    def _wait_for_current_upload_dialog(self, timeout: int) -> bool:
        """Wait until the current Earth Engine upload dialog is ready."""
        script = f"""
        {active_upload_dialog_helper_js()}
        const dialog = findActiveUploadDialog();
        return !!(dialog && dialog.shadowRoot);
        """
        try:
            self.wait_for_script_value(
                script=script,
                timeout=timeout,
                description="Earth Engine upload dialog",
            )
            return True
        except TimeoutException:
            return False

    def _open_current_ui_image_upload_dialog(self) -> None:
        """Open the GeoTIFF upload dialog using the Assets-side NEW menu."""
        script = f"""
        const host = document.querySelector('{ee_selectors.SHADOW_SELECTORS["new_asset_menu_host"]}');
        if (!host || !host.shadowRoot) {{
          return {{ok: false, reason: 'Assets NEW menu host was not found.'}};
        }}

        const openButton = host.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["new_asset_button"]}');
        if (!openButton) {{
          return {{ok: false, reason: 'Assets NEW button was not found.'}};
        }}
        openButton.click();

        const menuHost = host.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["new_asset_menu_component"]}');
        if (!menuHost || !menuHost.shadowRoot) {{
          return {{ok: false, reason: 'Assets NEW menu component was not found.'}};
        }}

        const geoTiffItem = Array.from(
          menuHost.shadowRoot.querySelectorAll('{ee_selectors.SHADOW_SELECTORS["new_asset_menu_item"]}')
        ).find((element) => /GeoTIFF/i.test((element.innerText || element.textContent || '').trim()));
        if (!geoTiffItem) {{
          return {{ok: false, reason: 'GeoTIFF upload menu item was not found.'}};
        }}

        geoTiffItem.click();
        return {{ok: true}};
        """
        result = self.execute_script(script)
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("reason") if isinstance(result, dict) else "Unknown UI error."
            raise RetryableUIError(str(reason))
        if not self._wait_for_current_upload_dialog(timeout=15):
            raise RetryableUIError("The Earth Engine upload dialog did not open after clicking Assets > NEW > GeoTIFF.")

    def _populate_current_ui_upload_dialog(self, item: UploadItem) -> None:
        """Fill the current Earth Engine upload dialog."""
        file_input = self.wait_for_script_value(
            script=f"""
            {active_upload_dialog_helper_js()}
            const dialog = findActiveUploadDialog();
            if (!dialog || !dialog.shadowRoot) return null;
            const fileList = dialog.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_file_list_host"]}');
            if (!fileList || !fileList.shadowRoot) return null;
            return fileList.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_file_input"]}');
            """,
            timeout=15,
            description="Earth Engine upload file input",
        )
        file_input.send_keys(str(item.local_file))

        asset_root, asset_trailer = self.split_current_ui_destination(item)
        self.set_current_ui_asset_root(asset_root)
        self.set_current_ui_asset_name(asset_trailer)

        if self.config.upload.pyramiding_policy.enabled():
            self.apply_pyramiding_policy()

    def split_current_ui_destination(self, item: UploadItem) -> tuple[str, str]:
        """Split destination_parent into the upload dialog root and trailer."""
        parent = self.config.destination_parent.strip().strip("/")

        project_match = re.match(r"^(projects/[^/]+/assets)(?:/(.*))?$", parent)
        if project_match:
            root = f"{project_match.group(1)}/"
            suffix = (project_match.group(2) or "").strip("/")
            trailer = item.asset_name if not suffix else f"{suffix}/{item.asset_name}"
            return root, trailer

        user_match = re.match(r"^(users/[^/]+)(?:/(.*))?$", parent)
        if user_match:
            root = f"{user_match.group(1)}/"
            suffix = (user_match.group(2) or "").strip("/")
            trailer = item.asset_name if not suffix else f"{suffix}/{item.asset_name}"
            return root, trailer

        raise RetryableUIError(
            "The current Earth Engine upload dialog expects a root asset dropdown plus an asset trailer. "
            f"Unsupported destination_parent format: {self.config.destination_parent}"
        )

    def set_current_ui_asset_root(self, asset_root: str) -> None:
        """Set the asset root dropdown in the current upload dialog."""
        options = self.execute_script(
            f"""
            {active_upload_dialog_helper_js()}
            const dialog = findActiveUploadDialog();
            if (!dialog || !dialog.shadowRoot) return [];
            const dropdownHost = dialog.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_root_dropdown_host"]}');
            if (!dropdownHost || !dropdownHost.shadowRoot) return [];
            const listbox = dropdownHost.shadowRoot.querySelector('paper-listbox');
            if (!listbox) return [];
            return Array.from(listbox.querySelectorAll('paper-item'))
              .map((item) => (item.innerText || item.textContent || '').trim())
              .filter(Boolean);
            """
        )
        if asset_root not in options:
            raise RetryableUIError(
                f"Destination root '{asset_root}' is not available in the Earth Engine upload dialog. "
                f"Available roots: {options}"
            )

        result = self.execute_script(
            f"""
            {active_upload_dialog_helper_js()}
            const dialog = findActiveUploadDialog();
            const dropdownHost = dialog && dialog.shadowRoot
              ? dialog.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_root_dropdown_host"]}')
              : null;
            if (!dropdownHost || !dropdownHost.shadowRoot) return {{ok: false, reason: 'Root dropdown host not found.'}};

            const menuLight = dropdownHost.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_root_dropdown"]}');
            const listbox = dropdownHost.shadowRoot.querySelector('paper-listbox');
            if (!menuLight || !listbox) return {{ok: false, reason: 'Root dropdown controls not found.'}};

            const items = Array.from(listbox.querySelectorAll('paper-item'));
            const index = items.findIndex((item) => (item.innerText || item.textContent || '').trim() === arguments[0]);
            if (index < 0) return {{ok: false, reason: 'Requested root is not present in the dropdown.'}};

            listbox.selected = index;
            menuLight.value = arguments[0];
            menuLight.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));

            const input = menuLight.shadowRoot ? menuLight.shadowRoot.querySelector('#input') : null;
            return {{
              ok: menuLight.value === arguments[0] || !!(input && input.innerText.trim() === arguments[0]),
              value: menuLight.value,
              text: input ? input.innerText.trim() : '',
            }};
            """,
            asset_root,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("reason") if isinstance(result, dict) else "Unknown UI error."
            raise RetryableUIError(f"Could not set the upload destination root. {reason}")

    def set_current_ui_asset_name(self, asset_trailer: str) -> None:
        """Fill the asset trailer input in the current upload dialog."""
        result = self.execute_script(
            f"""
            {active_upload_dialog_helper_js()}
            const dialog = findActiveUploadDialog();
            const host = dialog && dialog.shadowRoot
              ? dialog.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_asset_name_host"]}')
              : null;
            const paperInput = host && host.shadowRoot ? host.shadowRoot.querySelector('#paper-input') : null;
            const input = paperInput && paperInput.shadowRoot
              ? paperInput.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_asset_name_input"]}')
              : null;
            if (!input) return {{ok: false, reason: 'Asset Name input not found.'}};

            if ('value' in host) {{
              host.value = arguments[0];
            }}
            if ('value' in paperInput) {{
              paperInput.value = arguments[0];
            }}
            input.focus();
            input.value = arguments[0];
            input.dispatchEvent(new Event('input', {{bubbles: true, composed: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));
            input.blur();
            return {{ok: input.value === arguments[0], value: input.value}};
            """,
            asset_trailer,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("reason") if isinstance(result, dict) else "Unknown UI error."
            raise RetryableUIError(f"Could not fill the Asset Name field. {reason}")

    def _click_current_ui_upload_button(self) -> None:
        """Click the upload button inside the current upload dialog."""
        status = self.execute_script(
            f"""
            {active_upload_dialog_helper_js()}
            const dialog = findActiveUploadDialog();
            const innerDialog = dialog && dialog.shadowRoot
              ? dialog.shadowRoot.querySelector('#asset-upload-dialog')
              : null;
            const button = innerDialog && innerDialog.shadowRoot
              ? innerDialog.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_upload_button"]}')
              : null;
            if (!button) return {{found: false, disabled: false}};

            const paperButton = button.shadowRoot ? button.shadowRoot.querySelector('paper-button') : null;
            const disabled = button.hasAttribute('disabled') || !!(paperButton && paperButton.hasAttribute('disabled'));
            return {{found: true, disabled}};
            """
        )
        if not isinstance(status, dict) or not status.get("found"):
            raise RetryableUIError("Could not find the upload button in the Earth Engine upload dialog.")
        if status.get("disabled"):
            error_message = self.read_dialog_error_message()
            raise RetryableUIError(
                error_message or "The Earth Engine upload button is still disabled. Check the selected file and asset destination."
            )

        self.execute_script(
            f"""
            {active_upload_dialog_helper_js()}
            const dialog = findActiveUploadDialog();
            const innerDialog = dialog && dialog.shadowRoot
              ? dialog.shadowRoot.querySelector('#asset-upload-dialog')
              : null;
            const button = innerDialog && innerDialog.shadowRoot
              ? innerDialog.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_upload_button"]}')
              : null;
            if (button) button.click();
            """
        )

    def _apply_current_ui_pyramiding_policy(self) -> bool:
        """Apply the global pyramiding policy in the current upload dialog when possible."""
        if not self.is_current_upload_dialog_open():
            return False

        default_policy = self.config.upload.pyramiding_policy.default
        if not default_policy and not self.config.upload.pyramiding_policy.per_band:
            return True

        if default_policy:
            desired = default_policy.strip().upper()
            result = self.execute_script(
                f"""
                {active_upload_dialog_helper_js()}
                const dialog = findActiveUploadDialog();
                const advanced = dialog && dialog.shadowRoot ? dialog.shadowRoot.querySelector('image-advanced-options') : null;
                const dropdownHost = advanced && advanced.shadowRoot
                  ? advanced.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_pyramiding_host"]}')
                  : null;
                const menuLight = dropdownHost && dropdownHost.shadowRoot
                  ? dropdownHost.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_pyramiding_dropdown"]}')
                  : null;
                const listbox = dropdownHost && dropdownHost.shadowRoot
                  ? dropdownHost.shadowRoot.querySelector('paper-listbox')
                  : null;
                if (!menuLight || !listbox) return {{ok: false, reason: 'Pyramiding dropdown was not found.'}};

                const items = Array.from(listbox.querySelectorAll('paper-item'));
                const index = items.findIndex((item) => (item.innerText || item.textContent || '').trim().toUpperCase() === arguments[0]);
                if (index < 0) return {{ok: false, reason: `Value not available: ${arguments[0]}`}};

                listbox.selected = index;
                menuLight.value = arguments[0];
                menuLight.dispatchEvent(new Event('change', {{bubbles: true, composed: true}}));

                const input = menuLight.shadowRoot ? menuLight.shadowRoot.querySelector('#input') : null;
                return {{
                  ok: menuLight.value === arguments[0] || !!(input && input.innerText.trim().toUpperCase() === arguments[0]),
                  value: menuLight.value,
                  text: input ? input.innerText.trim() : '',
                }};
                """,
                desired,
            )
            if not isinstance(result, dict) or not result.get("ok"):
                reason = result.get("reason") if isinstance(result, dict) else "Unknown UI error."
                raise RetryableUIError(f"Could not set the global pyramiding policy. {reason}")

        if self.config.upload.pyramiding_policy.per_band:
            self.logger.warning(
                "Band-specific pyramiding policies are not mapped for this Earth Engine upload dialog layout. "
                "Only the global default was applied."
            )
        return True

    def parse_metadata_for_item(self, item: UploadItem, required: bool = False) -> ParsedMetadata:
        """Return parsed metadata for a planned upload."""
        if not self.config.metadata.enabled:
            return ParsedMetadata(status="METADATA_DISABLED")

        try:
            parsed = parse_swot_l2_hr_raster_metadata(
                item.local_file,
                self.config.metadata.extra_properties,
            )
        except ValueError as exc:
            message = f"Could not parse SWOT metadata timestamp in {item.local_file.name}: {exc}"
            if required and self.config.metadata.require_match:
                raise RetryableUIError(message) from exc
            return ParsedMetadata(status="METADATA_NOT_PARSED", error_message=message)
        if parsed is not None:
            return parsed

        message = (
            "Filename does not match the expected SWOT L2 HR Raster pattern: "
            f"{item.local_file.name}"
        )
        if required and self.config.metadata.require_match:
            raise RetryableUIError(message)
        return ParsedMetadata(status="METADATA_NOT_PARSED", error_message=message)

    def apply_metadata_properties(self, item: UploadItem) -> None:
        """Apply configured asset metadata in the Earth Engine upload dialog."""
        parsed = self.parse_metadata_for_item(item, required=True)
        if parsed.status != "METADATA_PARSED":
            if parsed.error_message:
                self.logger.warning("%s", parsed.error_message)
            return

        if not self.is_current_upload_dialog_open():
            raise RetryableUIError(
                "Metadata properties are currently mapped for the current Earth Engine upload dialog only."
            )
        self._apply_current_ui_metadata_properties(parsed)

    def _apply_current_ui_metadata_properties(self, metadata: ParsedMetadata) -> None:
        """Fill the current upload dialog's Properties controls."""
        if self.config.metadata.add_start_time and metadata.start_time:
            self._add_current_ui_metadata_property(
                "system:time_start",
                metadata.start_time,
                str(ee_selectors.SHADOW_SELECTORS["upload_dialog_add_start_time_label"]),
            )
        if self.config.metadata.add_end_time and metadata.end_time:
            self._add_current_ui_metadata_property(
                "system:time_end",
                metadata.end_time,
                str(ee_selectors.SHADOW_SELECTORS["upload_dialog_add_end_time_label"]),
            )
        for property_name, property_value in metadata.properties.items():
            self._add_current_ui_metadata_property(
                property_name,
                property_value,
                str(ee_selectors.SHADOW_SELECTORS["upload_dialog_add_property_label"]),
            )

    def _add_current_ui_metadata_property(
        self,
        property_name: str,
        property_value: str,
        button_label: str,
    ) -> None:
        """Click an Add-property control and fill the created row."""
        self._click_current_ui_metadata_button(button_label)
        result = self.wait_for_script_value(
            self.current_ui_metadata_fill_script(),
            10,
            f"metadata row for {property_name}",
            property_name,
            property_value,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("reason") if isinstance(result, dict) else "Unknown UI error."
            raise RetryableUIError(f"Could not fill metadata property '{property_name}'. {reason}")

    def _click_current_ui_metadata_button(self, button_label: str) -> None:
        """Click a metadata button inside the current upload dialog."""
        deadline = time.monotonic() + 5
        last_result: Any = None
        while time.monotonic() < deadline:
            result = self.execute_script(
                self.current_ui_metadata_click_script(),
                button_label,
                str(ee_selectors.SHADOW_SELECTORS["upload_dialog_properties_label"]),
            )
            last_result = result
            if isinstance(result, dict) and result.get("ok"):
                time.sleep(self.config.execution.short_ui_wait_seconds)
                return
            time.sleep(0.2)
        reason = last_result.get("reason") if isinstance(last_result, dict) else "Unknown UI error."
        raise RetryableUIError(f"Could not click metadata control '{button_label}'. {reason}")

    @staticmethod
    def current_ui_metadata_click_script() -> str:
        """Return JavaScript that opens Properties and clicks a named button."""
        return active_upload_dialog_helper_js() + """
        const wanted = String(arguments[0] || '').trim().toLowerCase();
        const propertiesLabel = String(arguments[1] || 'Properties').trim().toLowerCase();
        const dialog = findActiveUploadDialog();
        if (!dialog || !dialog.shadowRoot) return {ok: false, reason: 'Upload dialog not found.'};

        function collect(root, output = [], seen = new Set()) {
          if (!root || seen.has(root)) return output;
          seen.add(root);
          if (root.nodeType === Node.ELEMENT_NODE) output.push(root);
          const children = [
            ...Array.from(root.children || []),
            ...(root.shadowRoot ? Array.from(root.shadowRoot.children) : []),
          ];
          for (const child of children) collect(child, output, seen);
          return output;
        }

        function visible(element) {
          if (!element || !element.getClientRects) return false;
          return element.getClientRects().length > 0;
        }

        function text(element) {
          return ((element.innerText || element.textContent || element.getAttribute('aria-label') || '') + '').trim();
        }

        function isButtonLike(element) {
          const tag = element.tagName ? element.tagName.toLowerCase() : '';
          return tag === 'button'
            || tag === 'ee-button'
            || tag === 'paper-button'
            || element.getAttribute('role') === 'button'
            || element.getAttribute('role') === 'menuitem';
        }

        function findControl(label) {
          const normalized = label.toLowerCase();
          const elements = collect(dialog.shadowRoot).filter(visible);
          const exact = elements.find((element) => {
            const elementText = text(element).toLowerCase();
            return isButtonLike(element) && elementText.includes(normalized);
          });
          if (exact) return exact;
          const textMatch = elements.find((element) => text(element).toLowerCase().includes(normalized));
          if (!textMatch) return null;
          return textMatch.closest('button, ee-button, paper-button, [role="button"], [role="menuitem"]') || textMatch;
        }

        let control = findControl(wanted);
        if (!control) {
          const properties = findControl(propertiesLabel);
          if (properties) {
            properties.scrollIntoView({block: 'center', inline: 'nearest'});
            properties.click();
          }
          control = findControl(wanted);
        }
        if (!control) return {ok: false, reason: `Control not found: ${arguments[0]}`};

        control.scrollIntoView({block: 'center', inline: 'nearest'});
        control.click();
        return {ok: true};
        """

    @staticmethod
    def current_ui_metadata_fill_script() -> str:
        """Return JavaScript that fills the latest metadata property row."""
        return active_upload_dialog_helper_js() + """
        const propertyName = String(arguments[0] || '');
        const propertyValue = String(arguments[1] || '');
        const dialog = findActiveUploadDialog();
        if (!dialog || !dialog.shadowRoot) return {ok: false, reason: 'Upload dialog not found.'};

        function collect(root, output = [], seen = new Set()) {
          if (!root || seen.has(root)) return output;
          seen.add(root);
          if (root.nodeType === Node.ELEMENT_NODE) output.push(root);
          const children = [
            ...Array.from(root.children || []),
            ...(root.shadowRoot ? Array.from(root.shadowRoot.children) : []),
          ];
          for (const child of children) collect(child, output, seen);
          return output;
        }

        function visible(element) {
          if (!element || !element.getClientRects) return false;
          return element.getClientRects().length > 0;
        }

        function currentValue(element) {
          if ('value' in element && element.value !== undefined && element.value !== null) {
            return String(element.value).trim();
          }
          return String(element.getAttribute('value') || '').trim();
        }

        function editable(element) {
          const tag = element.tagName ? element.tagName.toLowerCase() : '';
          const type = String(element.getAttribute('type') || '').toLowerCase();
          return (tag === 'input' || tag === 'textarea')
            && type !== 'file'
            && type !== 'hidden'
            && !element.disabled;
        }

        function dispatchValueEvents(element) {
          for (const name of ['input', 'change']) {
            element.dispatchEvent(new Event(name, {bubbles: true, composed: true}));
          }
        }

        function setFieldValue(field, value) {
          field.focus();
          field.value = value;
          dispatchValueEvents(field);

          let root = field.getRootNode ? field.getRootNode() : null;
          let host = root && root.host ? root.host : null;
          while (host && host !== dialog) {
            if ('value' in host) {
              host.value = value;
              dispatchValueEvents(host);
            }
            root = host.getRootNode ? host.getRootNode() : null;
            host = root && root.host ? root.host : null;
          }
          field.blur();
        }

        const elements = collect(dialog.shadowRoot);
        const fields = elements.filter((element) => editable(element) && visible(element));
        const propertyIndex = fields.findIndex((field) => currentValue(field) === propertyName);
        const propertyTextIsVisible = elements.some((element) => {
          const text = String(element.innerText || element.textContent || '').trim();
          return text === propertyName || text.includes(propertyName);
        });

        if (propertyIndex >= 0) {
          const candidates = fields.slice(propertyIndex + 1);
          const valueField = candidates.find((field) => currentValue(field) === '' || currentValue(field) === propertyValue)
            || candidates[0];
          if (!valueField) return {ok: false, reason: `Value field not found for ${propertyName}.`};
          setFieldValue(valueField, propertyValue);
          return {ok: currentValue(valueField) === propertyValue, mode: 'existing-property-row'};
        }

        const emptyFields = fields.filter((field) => currentValue(field) === '');
        if (propertyTextIsVisible && emptyFields.length >= 1) {
          const valueField = emptyFields[emptyFields.length - 1];
          setFieldValue(valueField, propertyValue);
          return {ok: currentValue(valueField) === propertyValue, mode: 'visible-property-label'};
        }

        if (emptyFields.length >= 2) {
          const propertyField = emptyFields[emptyFields.length - 2];
          const valueField = emptyFields[emptyFields.length - 1];
          setFieldValue(propertyField, propertyName);
          setFieldValue(valueField, propertyValue);
          return {
            ok: currentValue(propertyField) === propertyName && currentValue(valueField) === propertyValue,
            mode: 'new-property-row',
          };
        }

        return {ok: false, reason: 'No editable metadata fields were found after clicking Add property.'};
        """

    def set_band_policy(self, band_name: str, policy: str) -> None:
        """Best-effort selection of a band-specific pyramiding policy."""
        assert self.driver is not None
        band_xpath = (
            "//*[contains(translate(normalize-space(.), "
            "'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), "
            f"'{band_name.upper()}')]"
        )
        select_xpath = f"{band_xpath}/following::select[1]"
        combo_xpath = f"{band_xpath}/following::*[@role='combobox'][1]"

        for by, value in [("xpath", select_xpath), ("xpath", combo_xpath)]:
            try:
                element = self.driver.find_element(by, value)
                self.select_option(element, policy)
                return
            except NoSuchElementException:
                continue

        raise RetryableUIError(
            f"Could not find a band-specific pyramiding control for band '{band_name}'."
        )

    def select_option(self, element: WebElement, desired_text: str) -> None:
        """Select a value from either a real <select> or a combobox widget."""
        tag_name = element.tag_name.lower()
        if tag_name == "select":
            Select(element).select_by_visible_text(desired_text)
            return

        try:
            element.click()
        except Exception as exc:  # noqa: BLE001
            raise RetryableUIError("Could not open the pyramiding policy control.") from exc

        option_xpath = (
            "//*[self::li or self::div or self::span]"
            "[contains(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz', "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), '{desired_text.upper()}')]"
        )
        assert self.driver is not None
        try:
            option = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(("xpath", option_xpath))
            )
            option.click()
        except TimeoutException as exc:
            raise RetryableUIError(
                f"Could not select pyramiding policy '{desired_text}'."
            ) from exc

    def wait_for_task_name(self, item: UploadItem, timeout_seconds: int) -> str:
        """Wait briefly for a corresponding task row to appear."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            rows = self.collect_task_rows()
            matched = self.find_matching_task(item, rows)
            if matched is not None:
                return matched.task_name
            time.sleep(2)
        return ""

    def wait_for_batch_gate(self, batch: Sequence[UploadItem]) -> None:
        """Wait until the batch completes or active ingestions drop below the threshold."""
        batch_asset_ids = [item.asset_id for item in batch]
        timeout_seconds = self.config.execution.wait_timeout_minutes * 60
        deadline = time.monotonic() + timeout_seconds

        while True:
            rows = self.collect_task_rows()
            active_ingestions = sum(1 for row in rows if row.is_ingestion and row.normalized_status in ACTIVE_STATUSES)
            self.update_report_from_tasks(batch, rows)

            if all(self.report.get_status(asset_id) in TERMINAL_STATUSES for asset_id in batch_asset_ids):
                self.logger.info("Batch %s reached terminal states.", batch[0].batch_number if batch else "?")
                return
            if active_ingestions <= self.config.upload.max_active_ingestions:
                self.logger.info(
                    "Active ingestion tasks (%s) are at or below the configured threshold (%s).",
                    active_ingestions,
                    self.config.upload.max_active_ingestions,
                )
                return
            if time.monotonic() > deadline:
                self.logger.warning(
                    "Batch wait timed out after %s minutes. Continuing to the next batch.",
                    self.config.execution.wait_timeout_minutes,
                )
                self.capture_debug_artifacts("batch_wait_timeout")
                return

            self.logger.info(
                "Waiting for queue to drain. Active ingestion tasks: %s. Polling again in %s seconds.",
                active_ingestions,
                self.config.execution.task_poll_seconds,
            )
            time.sleep(self.config.execution.task_poll_seconds)

    def wait_for_all_tracked_tasks(self) -> None:
        """Monitor the Tasks panel until tracked uploads reach terminal states or timeout."""
        pending = {
            asset_id
            for asset_id in self.tracked_items
            if self.report.get_status(asset_id) in ACTIVE_STATUSES
        }
        if not pending:
            return

        timeout_seconds = self.config.execution.wait_timeout_minutes * 60
        deadline = time.monotonic() + timeout_seconds

        while pending:
            rows = self.collect_task_rows()
            self.update_report_from_tasks(self.tracked_items.values(), rows)
            pending = {
                asset_id
                for asset_id in self.tracked_items
                if self.report.get_status(asset_id) in ACTIVE_STATUSES
            }
            if not pending:
                return
            if time.monotonic() > deadline:
                self.logger.warning(
                    "Final task monitoring timed out. Remaining assets will stay in their last known state."
                )
                self.capture_debug_artifacts("final_monitor_timeout")
                return
            self.logger.info(
                "Waiting for %s tracked uploads to finish. Polling again in %s seconds.",
                len(pending),
                self.config.execution.task_poll_seconds,
            )
            time.sleep(self.config.execution.task_poll_seconds)

    def collect_task_rows(self) -> List[TaskRow]:
        """Read task rows from the Tasks panel and normalize their statuses."""
        assert self.driver is not None
        self.ensure_tasks_tab()

        elements: List[WebElement] = []
        for by, value in ee_selectors.SELECTORS["task_rows"]:
            try:
                found = self.driver.find_elements(by, value)
                if found:
                    elements = found
                    break
            except WebDriverException as exc:
                raise_if_invalid_browser_session(exc)
                continue

        unique_texts: List[str] = []
        seen: set[str] = set()
        for element in elements:
            try:
                raw_text = element.text.strip()
            except StaleElementReferenceException:
                continue
            if not raw_text or raw_text in seen:
                continue
            seen.add(raw_text)
            unique_texts.append(raw_text)

        rows = [self.parse_task_row(text) for text in unique_texts]
        return rows

    def parse_task_row(self, text: str) -> TaskRow:
        """Normalize one task row into a status bucket."""
        lower = text.lower()
        if "already exists" in lower:
            status = "SKIPPED_ALREADY_EXISTS"
        elif any(keyword in lower for keyword in ee_selectors.FAILURE_TASK_KEYWORDS):
            status = "FAILED"
        elif any(keyword in lower for keyword in ee_selectors.SUCCESS_TASK_KEYWORDS):
            status = "COMPLETED"
        elif "ready" in lower:
            status = "READY"
        elif any(keyword in lower for keyword in ee_selectors.ACTIVE_TASK_KEYWORDS):
            status = "RUNNING"
        else:
            status = "UNKNOWN"

        tracked_names = [item.asset_name.lower() for item in self.tracked_items.values()]
        is_ingestion = (
            "ingest" in lower
            or "upload" in lower
            or any(name and name in lower for name in tracked_names)
        )
        return TaskRow(raw_text=text, normalized_status=status, is_ingestion=is_ingestion)

    def update_report_from_tasks(
        self,
        items: Iterable[UploadItem],
        task_rows: Sequence[TaskRow],
    ) -> None:
        """Apply task row information back to tracked report rows."""
        for item in items:
            matched = self.find_matching_task(item, task_rows)
            if matched is None:
                continue
            current_status = self.report.get_status(item.asset_id)
            new_status = matched.normalized_status
            if new_status == "UNKNOWN" and current_status:
                continue
            if current_status in TERMINAL_STATUSES and new_status in ACTIVE_STATUSES:
                continue

            error_message = ""
            if new_status in {"FAILED", "SKIPPED_ALREADY_EXISTS"}:
                error_message = matched.raw_text[:2000]

            self.report.upsert(
                self.make_report_row(
                    item=item,
                    submit_time=self.report.rows.get(item.asset_id, {}).get("submit_time", ""),
                    detected_task_name=matched.task_name,
                    final_status=new_status,
                    error_message=error_message,
                )
            )

    def find_matching_task(
        self,
        item: UploadItem,
        task_rows: Sequence[TaskRow],
    ) -> Optional[TaskRow]:
        """Best-effort task-to-asset matching using asset ID, asset name, and file stem."""
        needles = [
            item.asset_id.lower(),
            item.asset_name.lower(),
            item.local_file.stem.lower(),
        ]
        for row in task_rows:
            lower = row.raw_text.lower()
            if any(needle and needle in lower for needle in needles):
                return row
        return None

    def read_dialog_error_message(self) -> str:
        """Read a visible error message from the upload dialog when present."""
        shadow_message = self.read_current_ui_dialog_error_message()
        if shadow_message:
            return shadow_message[:2000]
        if self.current_ui_upload_dialog_hosts_present():
            return ""

        assert self.driver is not None
        for by, value in ee_selectors.SELECTORS["dialog_error_message"]:
            try:
                elements = self.driver.find_elements(by, value)
            except WebDriverException as exc:
                raise_if_invalid_browser_session(exc)
                continue
            for element in elements:
                try:
                    text = element.text.strip()
                except StaleElementReferenceException:
                    continue
                if text:
                    return text[:2000]
        return ""

    def current_ui_upload_dialog_hosts_present(self) -> bool:
        """Return True when the current Earth Engine upload-dialog component is in the page."""
        try:
            return bool(
                self.execute_script(
                    f"""
                    return document.querySelectorAll('{ee_selectors.SHADOW_SELECTORS["upload_dialog_host"]}').length > 0;
                    """
                )
            )
        except WebDriverException as exc:
            raise_if_invalid_browser_session(exc)
            return False

    def read_current_ui_dialog_error_message(self) -> str:
        """Read validation or upload errors from the current upload dialog."""
        if not self.is_current_upload_dialog_open():
            return ""

        messages = self.execute_script(
            f"""
            {active_upload_dialog_helper_js()}
            const dialog = findActiveUploadDialog();
            if (!dialog || !dialog.shadowRoot) return [];

            function collect(root, output = [], seen = new Set()) {{
              if (!root || seen.has(root)) return output;
              seen.add(root);
              if (root.nodeType === Node.ELEMENT_NODE) output.push(root);
              const children = [
                ...Array.from(root.children || []),
                ...(root.shadowRoot ? Array.from(root.shadowRoot.children) : []),
              ];
              for (const child of children) collect(child, output, seen);
              return output;
            }}

            function visible(element) {{
              if (!element || !element.getClientRects || !element.getBoundingClientRect) return false;
              const style = window.getComputedStyle ? window.getComputedStyle(element) : null;
              if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
              const rects = element.getClientRects();
              const box = element.getBoundingClientRect();
              return rects.length > 0 || box.width > 0 || box.height > 0;
            }}

            const explicitSelector = [
              '[role="alert"]',
              '[aria-live="assertive"]',
              '.error',
              '.error-message',
              '.validation-error',
              '.warning',
              '.warning-message',
              '.input-error',
              '.invalid',
              'paper-input-error',
            ].join(',');
            return collect(dialog.shadowRoot)
              .filter((element) => visible(element) && element.matches && element.matches(explicitSelector))
              .map((element) => (element.innerText || element.textContent || '').trim())
              .filter(Boolean);
            """
        )
        if not isinstance(messages, list):
            return ""
        return normalize_dialog_error_messages(str(message) for message in messages)

    def execute_script(self, script: str, *args: Any) -> Any:
        """Execute JavaScript against the current page."""
        assert self.driver is not None
        try:
            return self.driver.execute_script(script, *args)
        except WebDriverException as exc:
            raise_if_invalid_browser_session(exc)
            raise

    def wait_for_script_value(
        self,
        script: str,
        timeout: int,
        description: str,
        *args: Any,
    ) -> Any:
        """Poll a JavaScript expression until it returns a truthy value."""
        deadline = time.monotonic() + timeout
        last_exception: Optional[BaseException] = None

        while time.monotonic() < deadline:
            try:
                value = self.execute_script(script, *args)
            except (
                JavascriptException,
                InvalidSessionIdException,
                StaleElementReferenceException,
                WebDriverException,
            ) as exc:
                raise_if_invalid_browser_session(exc)
                last_exception = exc
                time.sleep(0.2)
                continue
            if value:
                return value
            time.sleep(0.2)

        raise TimeoutException(f"Timed out while waiting for {description}.") from last_exception

    def _select_code_editor_tab(self, panel_index: int, label: str) -> bool:
        """Select a tab by label from a specific Code Editor tab panel."""
        script = """
        const panels = document.querySelectorAll('ee-tab-panel');
        const panel = panels[arguments[0]];
        if (!panel || !panel.shadowRoot) return false;

        const button = Array.from(panel.shadowRoot.querySelectorAll(arguments[2]))
          .find((candidate) => (candidate.innerText || candidate.textContent || '').trim() === arguments[1]);
        if (!button) return false;

        if (button.classList.contains('selected')) return true;
        button.click();
        return button.classList.contains('selected');
        """
        try:
            self.wait_for_script_value(
                script,
                10,
                f"the '{label}' tab",
                panel_index,
                label,
                str(ee_selectors.SHADOW_SELECTORS["tab_button"]),
            )
            return True
        except TimeoutException:
            return False

    def click_first_available(self, selector_name: str, timeout: int = 20) -> WebElement:
        """Click the first element found for the given selector name."""
        element = self.find_first_element(selector_name, timeout=timeout, visible_only=True)
        try:
            element.click()
        except (ElementClickInterceptedException, WebDriverException) as exc:
            raise_if_invalid_browser_session(exc)
            assert self.driver is not None
            try:
                self.driver.execute_script("arguments[0].click();", element)
            except (JavascriptException, WebDriverException) as script_exc:
                raise_if_invalid_browser_session(script_exc)
                raise RetryableUIError(f"Could not click selector '{selector_name}'.") from script_exc
        return element

    def find_first_element(
        self,
        selector_name: str,
        timeout: int,
        visible_only: bool,
    ) -> WebElement:
        """Try all fallbacks for one selector name and return the first match."""
        assert self.driver is not None
        selectors = ee_selectors.SELECTORS[selector_name]
        deadline = time.monotonic() + timeout
        last_exception: Optional[BaseException] = None
        while time.monotonic() < deadline:
            for by, value in selectors:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    wait = WebDriverWait(self.driver, min(2, remaining))
                    if visible_only:
                        element = wait.until(EC.visibility_of_element_located((by, value)))
                    else:
                        element = wait.until(EC.presence_of_element_located((by, value)))
                    self.logger.debug("Matched selector %s using %s = %s", selector_name, by, value)
                    return element
                except TimeoutException as exc:
                    last_exception = exc
                    continue
        raise TimeoutException(f"Could not locate selector '{selector_name}'.") from last_exception

    def find_optional_element(
        self,
        selector_name: str,
        timeout: int,
        visible_only: bool,
    ) -> Optional[WebElement]:
        """Return a matching element or None when a selector is not present."""
        try:
            return self.find_first_element(
                selector_name=selector_name,
                timeout=timeout,
                visible_only=visible_only,
            )
        except TimeoutException:
            return None

    def wait_for_any_selector(
        self,
        selector_names: Sequence[str],
        timeout: int,
        must_be_clickable: bool,
    ) -> WebElement:
        """Return when any selector in a group becomes available."""
        assert self.driver is not None
        deadline = time.monotonic() + timeout
        last_error: Optional[BaseException] = None

        while time.monotonic() < deadline:
            for selector_name in selector_names:
                for by, value in ee_selectors.SELECTORS[selector_name]:
                    try:
                        if must_be_clickable:
                            element = WebDriverWait(self.driver, 2).until(
                                EC.element_to_be_clickable((by, value))
                            )
                        else:
                            element = WebDriverWait(self.driver, 2).until(
                                EC.presence_of_element_located((by, value))
                            )
                        return element
                    except TimeoutException as exc:
                        last_error = exc
                        continue
            time.sleep(0.2)
        raise TimeoutException("None of the expected selectors became available.") from last_error

    def clear_and_type(self, element: WebElement, value: str) -> None:
        """Replace the contents of a text field."""
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.DELETE)
        element.send_keys(value)

    def close_dialog_if_possible(self) -> None:
        """Try to close the active dialog to recover from errors."""
        if self.is_current_upload_dialog_open():
            self.execute_script(
                f"""
                {active_upload_dialog_helper_js()}
                const dialog = findActiveUploadDialog();
                const innerDialog = dialog && dialog.shadowRoot
                  ? dialog.shadowRoot.querySelector('#asset-upload-dialog')
                  : null;
                const button = innerDialog && innerDialog.shadowRoot
                  ? innerDialog.shadowRoot.querySelector('{ee_selectors.SHADOW_SELECTORS["upload_dialog_cancel_button"]}')
                  : null;
                if (button) button.click();
                """
            )
            return

        assert self.driver is not None
        for by, value in ee_selectors.SELECTORS["dialog_close_button"]:
            try:
                elements = self.driver.find_elements(by, value)
            except WebDriverException as exc:
                raise_if_invalid_browser_session(exc)
                continue
            for element in elements:
                try:
                    if element.is_displayed():
                        try:
                            element.click()
                        except Exception:  # noqa: BLE001
                            self.driver.execute_script("arguments[0].click();", element)
                        return
                except StaleElementReferenceException:
                    continue
        try:
            self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:  # noqa: BLE001
            pass

    def recover_ui_state(self) -> None:
        """Reload the page and return to a known-good state."""
        assert self.driver is not None
        self.logger.info("Refreshing the Earth Engine page to recover UI state.")
        self.driver.get(self.config.earth_engine_url)
        self.ensure_logged_in()
        self.ensure_assets_tab()

    def capture_debug_artifacts(self, name_prefix: str) -> None:
        """Save a screenshot and HTML dump when debugging is needed."""
        if self.driver is None:
            return
        safe_prefix = sanitize_for_filename(name_prefix)
        timestamp = timestamp_for_filename()
        screenshot_path = self.config.artifacts.artifacts_dir / f"{safe_prefix}_{timestamp}.png"
        html_path = self.config.artifacts.artifacts_dir / f"{safe_prefix}_{timestamp}.html"
        try:
            self.driver.save_screenshot(str(screenshot_path))
            self.logger.info("Saved screenshot: %s", screenshot_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("Could not save screenshot: %s", exc)
        try:
            html_path.write_text(self.driver.page_source, encoding="utf-8")
            self.logger.info("Saved HTML dump: %s", html_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("Could not save HTML dump: %s", exc)

    def make_report_row(
        self,
        item: UploadItem,
        submit_time: str,
        detected_task_name: str,
        final_status: str,
        error_message: str,
    ) -> Dict[str, str]:
        """Create a normalized report row dictionary."""
        metadata = self.parse_metadata_for_item(item, required=False)
        return {
            "local_file": str(item.local_file),
            "asset_id": item.asset_id,
            "batch_number": str(item.batch_number),
            "upload_selected": "yes" if item.upload_selected else "no",
            "upload_filter_status": item.upload_filter_status,
            "output_grid": item.output_grid,
            "source_utm_tiles": json.dumps(item.source_utm_tiles),
            "ee_asset_exists": "yes" if item.ee_asset_exists else "no",
            "ee_verified_at": item.ee_verified_at,
            "verification_source": item.verification_source,
            "submit_time": submit_time,
            "detected_task_name": detected_task_name,
            "final_status": final_status,
            "error_message": error_message,
            "metadata_start_time": metadata.start_time,
            "metadata_end_time": metadata.end_time,
            "metadata_properties": json.dumps(metadata.properties, sort_keys=True),
            "metadata_status": metadata.status,
        }


def sanitize_for_filename(value: str) -> str:
    """Make a string safe for filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "artifact"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.config:
        config = load_config_file(args.config.resolve())
    else:
        config = prompt_interactive_config()

    if args.dry_run:
        config.execution.dry_run = True
    if args.resume:
        config.execution.resume = True
    if args.no_resume:
        config.execution.resume = False
    if args.headless:
        config.chrome.headless = True

    logger = configure_logging(
        config=config,
        verbose_override=args.verbose or config.execution.verbose_console,
    )
    logger.info("Report CSV: %s", config.artifacts.report_csv)
    logger.info("Artifacts directory: %s", config.artifacts.artifacts_dir)

    uploader = EarthEngineUIUploader(
        config=config,
        logger=logger,
        assume_yes=args.yes,
    )
    if args.sync_only:
        count = uploader.sync_existing_assets_to_report()
        logger.info("Sync-only finished. Verified %s local file(s) against Earth Engine.", count)
        return 0
    return uploader.run()


if __name__ == "__main__":
    raise SystemExit(main())
