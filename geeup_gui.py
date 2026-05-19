"""Desktop GUI for GeeUp SWOT download, processing, and upload workflows."""

from __future__ import annotations

import os
import csv
import shutil
import subprocess
import sys
import threading
import textwrap
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "config.example.yaml"
UPLOADER_SCRIPT = PROJECT_ROOT / "ee_ui_uploader.py"
EXTRACT_SCRIPT = PROJECT_ROOT / "swot_extract_tool.py"
MOSAIC_SCRIPT = PROJECT_ROOT / "ee_mosaic_tool.py"
DUPLICATE_SCRIPT = PROJECT_ROOT / "swot_duplicate_remover.py"
PROGRESS_PREFIX = "GEEUP_PROGRESS\t"
DEFAULT_PROCESSING_ROOT = "./SWOT_Processing"
DEFAULT_PROCESSING_PATHS = {
    "root": DEFAULT_PROCESSING_ROOT,
    "raw_downloads": f"{DEFAULT_PROCESSING_ROOT}/01_raw_downloads",
    "extracted_geotiffs": f"{DEFAULT_PROCESSING_ROOT}/02_extracted_geotiffs",
    "mosaics": f"{DEFAULT_PROCESSING_ROOT}/03_mosaics",
    "logs": f"{DEFAULT_PROCESSING_ROOT}/00_logs",
}
MOSAIC_GROUPING_LABELS = {
    "Original projection / split by UTM zone": "utm_zone",
    "Original projection / group UTM latitude bands by hemisphere": "utm_zone_hemisphere",
    "Reprojected common CRS / whole pass-date mosaic": "pass_date_common_crs",
}
MOSAIC_GROUPING_LABEL_BY_VALUE = {
    value: label for label, value in MOSAIC_GROUPING_LABELS.items()
}
EXTRACT_CRS_LABELS = {
    "Original projection": "original",
    "Africa LAEA": "africa_laea",
    "WGS84": "wgs84",
}
EXTRACT_CRS_LABEL_BY_VALUE = {
    value: label for label, value in EXTRACT_CRS_LABELS.items()
}
UPLOAD_SCOPE_LABELS = {
    "All files in origin folder": "all",
    "Selected UTM/source tiles only": "selected_utm",
}
UPLOAD_SCOPE_LABEL_BY_VALUE = {
    value: label for label, value in UPLOAD_SCOPE_LABELS.items()
}
UPLOAD_SUCCESS_STATUSES = {
    "COMPLETED",
    "SKIPPED_ALREADY_EXISTS",
    "EE_VERIFIED_EXISTS",
}


def in_isolated_python_environment() -> bool:
    """Return True when Python appears to be running inside an isolated env."""
    return bool(
        os.environ.get("VIRTUAL_ENV")
        or os.environ.get("CONDA_PREFIX")
        or getattr(sys, "real_prefix", None)
        or getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    )


def ensure_isolated_python_environment() -> None:
    """Exit unless the GUI is started from an activated environment."""
    if in_isolated_python_environment():
        return
    raise SystemExit(
        textwrap.dedent(
            """
            This GUI must be run from an activated Python environment.

            Recommended Windows setup:
              python -m venv .venv
              .\\.venv\\Scripts\\Activate.ps1
              python -m pip install --upgrade pip
              python -m pip install -r requirements.txt
              python geeup_gui.py
            """
        ).strip()
    )


ensure_isolated_python_environment()

import yaml

from gdal_runtime import DEFAULT_GDAL_PYTHON, build_gdal_runtime_env
from geeup_project import (
    GeeUpProject,
    TilePreset,
    config_for_project,
    create_project,
    ensure_project_structure,
    load_builtin_tile_presets,
    load_project,
    load_project_tile_profiles,
    prepare_update_dates,
    save_project_config,
    save_tile_profile,
)
from project_insights import (
    CleanupCandidate,
    collect_project_insights,
    delete_cleanup_candidates,
    format_bytes as format_insight_bytes,
    lookup_path_value,
    load_project_insights_snapshot,
    mosaic_source_tiles,
    normalize_utm_tile_token,
    plan_cleanup_candidates,
    path_lookup_keys,
    read_csv_rows,
    upload_row_source_tiles,
    write_project_insights_snapshot,
)
from swot_download_tool import (
    COLLECTION_LABELS,
    COLLECTION_LABEL_BY_SHORT_NAME,
    DEFAULT_COLLECTION_LABEL,
    DEFAULT_COLLECTION_SHORT_NAME,
    DEFAULT_PRODUCT_VERSION_FILTER,
    DEFAULT_PRODUCT_VERSION_FILTER_LABEL,
    DownloadConfig,
    PRODUCT_VERSION_FILTER_LABELS,
    PRODUCT_VERSION_FILTER_LABEL_BY_VALUE,
    authenticate as authenticate_earthdata,
    build_download_preview,
    format_size,
    generate_utm_tiles,
    manifest_downloaded_tiles,
    normalize_utm_tiles,
    run_download,
    write_download_report,
)
from swot_metadata import parse_swot_l2_hr_raster_metadata
from utm_map_selector import UTMMapSelectorDialog, load_display_geometry


def load_config() -> Dict[str, Any]:
    """Load config.yaml when present, otherwise use config.example.yaml."""
    source = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    with source.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class LauncherApp:
    """Tkinter-based launcher for configuring and running SWOT processing tools."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("SWOT Processing Tools")
        self.root.geometry("860x760")
        self.root.minsize(780, 620)

        self.data = load_config()
        processing_data = self.data.get("processing", {})
        download_data = self.data.get("download", {})
        duplicate_data = self.data.get("duplicates", {})
        extract_data = self.data.get("extract", {})
        metadata_data = self.data.get("metadata", {})
        mosaic_data = self.data.get("mosaic", {})
        gdal_data = self.data.get("gdal", {})
        upload_data = self.data.get("upload", {})

        processing_root = processing_data.get("root", DEFAULT_PROCESSING_PATHS["root"])
        raw_downloads = processing_data.get(
            "raw_downloads", DEFAULT_PROCESSING_PATHS["raw_downloads"]
        )
        extracted_geotiffs = processing_data.get(
            "extracted_geotiffs", DEFAULT_PROCESSING_PATHS["extracted_geotiffs"]
        )
        mosaic_outputs = processing_data.get(
            "mosaics", DEFAULT_PROCESSING_PATHS["mosaics"]
        )
        processing_logs = processing_data.get(
            "logs", DEFAULT_PROCESSING_PATHS["logs"]
        )

        self.processing_root_var = tk.StringVar(value=processing_root)
        self.processing_raw_downloads_var = tk.StringVar(value=raw_downloads)
        self.processing_extracted_geotiffs_var = tk.StringVar(value=extracted_geotiffs)
        self.processing_mosaics_var = tk.StringVar(value=mosaic_outputs)
        self.processing_logs_var = tk.StringVar(value=processing_logs)

        download_short_name = str(
            download_data.get("collection_short_name", DEFAULT_COLLECTION_SHORT_NAME)
        ).strip()
        download_label = str(
            download_data.get(
                "collection_version_label",
                COLLECTION_LABEL_BY_SHORT_NAME.get(
                    download_short_name,
                    DEFAULT_COLLECTION_LABEL,
                ),
            )
        ).strip()
        if download_label not in COLLECTION_LABELS:
            download_label = COLLECTION_LABEL_BY_SHORT_NAME.get(
                download_short_name,
                DEFAULT_COLLECTION_LABEL,
            )
        download_product_filter = str(
            download_data.get("product_version_filter", DEFAULT_PRODUCT_VERSION_FILTER)
        ).strip()
        download_product_filter_label = PRODUCT_VERSION_FILTER_LABEL_BY_VALUE.get(
            download_product_filter,
            download_product_filter
            if download_product_filter in PRODUCT_VERSION_FILTER_LABELS
            else DEFAULT_PRODUCT_VERSION_FILTER_LABEL,
        )
        try:
            selected_tiles = normalize_utm_tiles(download_data.get("utm_tiles", []))
        except ValueError:
            selected_tiles = []
        self.download_collection_var = tk.StringVar(value=download_label)
        self.download_product_filter_var = tk.StringVar(value=download_product_filter_label)
        self.download_start_date_var = tk.StringVar(
            value=str(download_data.get("start_date", ""))
        )
        self.download_end_date_var = tk.StringVar(
            value=str(download_data.get("end_date", ""))
        )
        self.download_tile_filter_var = tk.StringVar(value="")
        self.download_selected_tiles_var = tk.StringVar(
            value=", ".join(selected_tiles)
        )
        self.download_output_var = tk.StringVar(
            value=download_data.get("output_folder", raw_downloads)
        )
        max_granules = download_data.get("max_granules")
        self.download_max_granules_var = tk.StringVar(
            value="" if max_granules in (None, "") else str(max_granules)
        )
        self.download_report_var = tk.StringVar(
            value=download_data.get(
                "report_csv",
                f"{processing_logs}/download_preview.csv",
            )
        )
        self.download_manifest_var = tk.StringVar(
            value=download_data.get(
                "manifest_csv",
                f"{processing_logs}/download_manifest.csv",
            )
        )
        self.download_threads_var = tk.StringVar(
            value=str(download_data.get("threads", 6))
        )
        self.download_batch_size_var = tk.StringVar(
            value=str(download_data.get("batch_size", 25))
        )
        self.download_skip_existing_var = tk.BooleanVar(
            value=bool(download_data.get("skip_existing", True))
        )
        self.download_skip_manifest_existing_var = tk.BooleanVar(
            value=bool(download_data.get("skip_manifest_existing", True))
        )
        self.all_utm_tiles = generate_utm_tiles()
        self.download_visible_utm_tiles = list(self.all_utm_tiles)
        self.download_selected_tiles = set(selected_tiles)
        self.current_project_root_var = tk.StringVar(value="")
        self.current_project_name_var = tk.StringVar(value="No project")
        self.project_status_var = tk.StringVar(
            value="No project is open. Create or open a project before previewing, downloading, or running processing steps."
        )
        self.project_created_at = ""
        self.project_download_history: list[dict[str, Any]] = []
        self.tile_preset_var = tk.StringVar(value="")
        self.tile_preset_choices: dict[str, TilePreset] = {}
        self.upload_stats_poll_after_id: str | None = None

        self.duplicate_input_var = tk.StringVar(
            value=duplicate_data.get("input_folder", raw_downloads)
        )
        self.duplicate_moved_folder_var = tk.StringVar(
            value=duplicate_data.get("moved_folder_name", "moved")
        )
        self.duplicate_log_folder_var = tk.StringVar(
            value=duplicate_data.get("log_folder", processing_logs)
        )
        self.extract_input_var = tk.StringVar(
            value=extract_data.get("input_folder", raw_downloads)
        )
        self.extract_output_var = tk.StringVar(
            value=extract_data.get("output_folder", extracted_geotiffs)
        )
        extract_crs_mode = extract_data.get("target_crs_mode", "original")
        self.extract_crs_mode_var = tk.StringVar(
            value=EXTRACT_CRS_LABEL_BY_VALUE.get(
                extract_crs_mode,
                "Original projection",
            )
        )
        self.extract_year_selection_var = tk.StringVar(
            value=str(extract_data.get("year_selection", "all"))
        )
        limit_files = extract_data.get("limit_files")
        self.extract_limit_files_var = tk.StringVar(
            value="" if limit_files in (None, "") else str(limit_files)
        )
        self.extract_manifest_var = tk.StringVar(
            value=extract_data.get("manifest_csv", f"{processing_logs}/extract_manifest.csv")
        )
        self.extract_errors_var = tk.StringVar(
            value=extract_data.get("errors_csv", f"{processing_logs}/extract_errors.csv")
        )

        self.folder_var = tk.StringVar(
            value=self.data.get("input_folder", mosaic_outputs)
        )
        self.destination_var = tk.StringVar(
            value=self.data.get("destination_parent", "")
        )
        upload_scope = str(upload_data.get("scope", "all")).strip()
        self.upload_scope_var = tk.StringVar(
            value=UPLOAD_SCOPE_LABEL_BY_VALUE.get(
                upload_scope,
                "All files in origin folder",
            )
        )
        try:
            upload_tiles = normalize_utm_tiles(upload_data.get("utm_tiles", []))
        except ValueError:
            upload_tiles = []
        self.upload_tile_filter_var = tk.StringVar(value="")
        self.upload_selected_tiles_var = tk.StringVar(value=", ".join(upload_tiles))
        self.upload_tile_availability_var = tk.StringVar(
            value="Upload tiles are derived from the current origin folder when possible."
        )
        self.upload_tile_status_var = tk.StringVar(
            value="No upload tiles selected. Click a listed tile, or type/paste tile IDs and validate typed tiles."
        )
        self.upload_visible_utm_tiles = list(self.all_utm_tiles)
        self.upload_selected_tiles = set(upload_tiles)
        self.batch_size_var = tk.StringVar(
            value=str(self.data.get("upload", {}).get("batch_size", 50))
        )
        self.max_active_var = tk.StringVar(
            value=str(self.data.get("upload", {}).get("max_active_ingestions", 0))
        )
        self.prefix_var = tk.StringVar(
            value=self.data.get("upload", {}).get("prefix", "")
        )
        self.suffix_var = tk.StringVar(
            value=self.data.get("upload", {}).get("suffix", "")
        )
        self.pyramiding_var = tk.StringVar(
            value=self.data.get("upload", {})
            .get("pyramiding_policy", {})
            .get("default", "")
            or ""
        )
        self.profile_dir_var = tk.StringVar(
            value=self.data.get("chrome", {}).get("user_data_dir", "./chrome-profile")
        )
        self.retry_attempts_var = tk.StringVar(
            value=str(self.data.get("upload", {}).get("retry_attempts", 3))
        )
        self.retry_wait_var = tk.StringVar(
            value=str(self.data.get("upload", {}).get("retry_wait_seconds", 3.0))
        )
        self.gdal_python_var = tk.StringVar(
            value=gdal_data.get("python", str(DEFAULT_GDAL_PYTHON))
        )
        self.mosaic_input_var = tk.StringVar(
            value=mosaic_data.get("input_folder", extracted_geotiffs)
        )
        self.mosaic_output_var = tk.StringVar(
            value=mosaic_data.get("output_folder", mosaic_outputs)
        )
        mosaic_grouping_mode = mosaic_data.get("grouping_mode", "utm_zone")
        self.mosaic_grouping_mode_var = tk.StringVar(
            value=MOSAIC_GROUPING_LABEL_BY_VALUE.get(
                mosaic_grouping_mode,
                "Original projection / split by UTM zone",
            )
        )
        self.mosaic_target_crs_label_var = tk.StringVar(
            value=mosaic_data.get("target_crs_label", "")
        )
        self.mosaic_report_var = tk.StringVar(
            value=mosaic_data.get("report_csv", f"{processing_logs}/mosaic_report.csv")
        )

        self.resume_var = tk.BooleanVar(
            value=bool(self.data.get("execution", {}).get("resume", True))
        )
        self.dry_run_var = tk.BooleanVar(
            value=bool(self.data.get("execution", {}).get("dry_run", True))
        )
        self.recursive_var = tk.BooleanVar(
            value=bool(self.data.get("upload", {}).get("recursive", False))
        )
        self.fail_fast_var = tk.BooleanVar(
            value=bool(self.data.get("upload", {}).get("fail_fast", False))
        )
        self.headless_var = tk.BooleanVar(
            value=bool(self.data.get("chrome", {}).get("headless", False))
        )
        self.metadata_enabled_var = tk.BooleanVar(
            value=bool(metadata_data.get("enabled", True))
        )
        self.metadata_require_match_var = tk.BooleanVar(
            value=bool(metadata_data.get("require_match", True))
        )
        self.metadata_add_end_time_var = tk.BooleanVar(
            value=bool(metadata_data.get("add_end_time", True))
        )
        self.mosaic_recursive_var = tk.BooleanVar(
            value=bool(mosaic_data.get("recursive", False))
        )
        self.mosaic_overwrite_var = tk.BooleanVar(
            value=bool(mosaic_data.get("overwrite", False))
        )
        self.mosaic_write_world_file_var = tk.BooleanVar(
            value=bool(mosaic_data.get("write_world_file", True))
        )
        self.mosaic_set_upload_folder_var = tk.BooleanVar(value=True)
        self.duplicate_recursive_var = tk.BooleanVar(
            value=bool(duplicate_data.get("recursive", False))
        )
        self.extract_skip_existing_var = tk.BooleanVar(
            value=bool(extract_data.get("skip_existing", True))
        )
        self.extract_skip_manifest_existing_var = tk.BooleanVar(
            value=bool(extract_data.get("skip_manifest_existing", True))
        )
        self.mosaic_manifest_var = tk.StringVar(
            value=mosaic_data.get("manifest_csv", f"{processing_logs}/mosaic_manifest.csv")
        )
        self.mosaic_skip_manifest_existing_var = tk.BooleanVar(
            value=bool(mosaic_data.get("skip_manifest_existing", True))
        )

        self.status_var = tk.StringVar(
            value="Open a project, then run a dry run or real upload."
        )
        self.download_status_var = tk.StringVar(
            value="Authenticate, choose dates and UTM tiles, then preview matching SWOT granules."
        )
        self.download_auth_status_var = tk.StringVar(
            value="Earthdata authentication: not checked"
        )
        self.mosaic_status_var = tk.StringVar(
            value="Choose a SWOT GeoTIFF folder, then plan mosaics."
        )
        self.duplicate_status_var = tk.StringVar(
            value="Choose the raw SWOT download folder, then plan duplicate removal."
        )
        self.extract_status_var = tk.StringVar(
            value="Choose the cleaned NetCDF folder, then plan extraction."
        )
        self.download_progress_var = tk.DoubleVar(value=0.0)
        self.download_progress_text_var = tk.StringVar(value="Progress: not started")
        self.download_stop_event: threading.Event | None = None
        self.extract_progress_var = tk.DoubleVar(value=0.0)
        self.extract_progress_text_var = tk.StringVar(value="Progress: not started")
        self.mosaic_progress_var = tk.DoubleVar(value=0.0)
        self.mosaic_progress_text_var = tk.StringVar(value="Progress: not started")
        self.statistics_status_var = tk.StringVar(
            value="Open a project, then refresh statistics to summarize manifests and local files."
        )
        self.statistics_summary_var = tk.StringVar(value="")
        self.cleanup_status_var = tk.StringVar(
            value="Open a project, then preview safe cleanup candidates."
        )
        self.cleanup_candidates: list[CleanupCandidate] = []

        self.build_layout()
        self.try_auto_open_project_from_config()

    def build_layout(self) -> None:
        """Create the tabbed launcher layout."""
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        title = ttk.Label(
            outer,
            text="SWOT Processing Tools",
            font=("Segoe UI", 16, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")

        intro = ttk.Label(
            outer,
            text=(
                "Process SWOT files in separated steps: remove duplicate downloads, mosaic extracted GeoTIFFs, then upload to Earth Engine.\n"
                "Heavy raster processing runs through the configured GDAL conda runtime."
            ),
            justify="left",
        )
        intro.grid(row=1, column=0, sticky="w", pady=(6, 14))

        self.build_project_bar(outer)

        notebook = ttk.Notebook(outer)
        notebook.grid(row=3, column=0, sticky="nsew")

        download_tab = ttk.Frame(notebook, padding=12)
        duplicate_tab = ttk.Frame(notebook, padding=12)
        extract_tab = ttk.Frame(notebook, padding=12)
        mosaic_tab = ttk.Frame(notebook, padding=12)
        upload_tab = ttk.Frame(notebook, padding=12)
        statistics_tab = ttk.Frame(notebook, padding=12)
        cleanup_tab = ttk.Frame(notebook, padding=12)
        notebook.add(download_tab, text="Download")
        notebook.add(duplicate_tab, text="Duplicate Removal")
        notebook.add(extract_tab, text="Extraction")
        notebook.add(mosaic_tab, text="Mosaic")
        notebook.add(upload_tab, text="Upload")
        notebook.add(statistics_tab, text="Statistics")
        notebook.add(cleanup_tab, text="Cleanup")

        self.build_download_tab(download_tab)
        self.build_duplicate_tab(duplicate_tab)
        self.build_extract_tab(extract_tab)
        self.build_mosaic_tab(mosaic_tab)
        self.build_upload_tab(upload_tab)
        self.build_statistics_tab(statistics_tab)
        self.build_cleanup_tab(cleanup_tab)

    def build_project_bar(self, parent: ttk.Frame) -> None:
        """Create project controls above the processing tabs."""
        project = ttk.LabelFrame(parent, text="Project", padding=10)
        project.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        project.columnconfigure(6, weight=1)

        ttk.Button(project, text="New Project", command=self.new_project).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(project, text="Open Project", command=self.open_project).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Button(project, text="Save Project", command=self.save_project_action).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )
        ttk.Button(project, text="Save Project As", command=self.save_project_as).grid(
            row=0, column=3, sticky="w", padx=(8, 0)
        )
        ttk.Button(project, text="Prepare Update", command=self.prepare_project_update).grid(
            row=0, column=4, sticky="w", padx=(8, 0)
        )
        ttk.Label(project, textvariable=self.current_project_name_var).grid(
            row=0, column=5, sticky="w", padx=(12, 0)
        )
        ttk.Label(
            project,
            textvariable=self.current_project_root_var,
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))
        ttk.Label(
            project,
            textvariable=self.project_status_var,
            foreground="#184a8b",
            justify="left",
        ).grid(row=2, column=0, columnspan=7, sticky="w", pady=(4, 0))

    def build_download_tab(self, parent: ttk.Frame) -> None:
        """Create SWOT Earthdata download controls."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        canvas = tk.Canvas(parent, borderwidth=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        content = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )
        parent = content
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)

        auth = ttk.LabelFrame(parent, text="Earthdata Authentication", padding=12)
        auth.grid(row=0, column=0, sticky="ew")
        auth.columnconfigure(1, weight=1)
        ttk.Button(
            auth,
            text="Authenticate",
            command=self.authenticate_download,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            auth,
            textvariable=self.download_auth_status_var,
            foreground="#184a8b",
            justify="left",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        form = ttk.Frame(parent)
        form.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        form.columnconfigure(1, weight=1)

        row = 0
        row = self.add_combo_row(
            form,
            row,
            "Collection",
            self.download_collection_var,
            list(COLLECTION_LABELS.keys()),
            "Default is the active Version D 100 m product; Version C remains available for compatibility",
        )
        row = self.add_combo_row(
            form,
            row,
            "Product version filter",
            self.download_product_filter_var,
            list(PRODUCT_VERSION_FILTER_LABELS.keys()),
            "Best version skips older CRID/product-counter revisions after preview while keeping them in the report",
        )
        row = self.add_entry_row(
            form,
            row,
            "Start date",
            self.download_start_date_var,
            "Use YYYY-MM-DD",
        )
        row = self.add_entry_row(
            form,
            row,
            "End date",
            self.download_end_date_var,
            "Use YYYY-MM-DD",
        )
        row = self.add_path_row(
            form,
            row,
            "Output folder",
            self.download_output_var,
            self.browse_download_output_folder,
            "Folder for downloaded SWOT NetCDF files; defaults to 01_raw_downloads",
        )
        row = self.add_entry_row(
            form,
            row,
            "Max granules",
            self.download_max_granules_var,
            "Optional safety limit; leave blank to retrieve all matches",
        )
        row = self.add_entry_row(
            form,
            row,
            "Download threads",
            self.download_threads_var,
            "Parallel earthaccess workers for each batch; start with 6 to 8 for large runs",
        )
        row = self.add_entry_row(
            form,
            row,
            "Batch size",
            self.download_batch_size_var,
            "Granules submitted to earthaccess at once; 25 is conservative, 50 can be faster",
        )
        row = self.add_path_row(
            form,
            row,
            "Preview/report CSV",
            self.download_report_var,
            self.browse_download_report_file,
            "CSV report for matched, downloaded, skipped, and failed granules",
        )
        row = self.add_path_row(
            form,
            row,
            "Download manifest CSV",
            self.download_manifest_var,
            self.browse_download_manifest_file,
            "Cumulative project memory of granules already downloaded, even if raw files are later deleted",
        )

        tiles = ttk.LabelFrame(parent, text="UTM Tiles", padding=12)
        tiles.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        tiles.columnconfigure(1, weight=1)
        tiles.rowconfigure(2, weight=1)

        ttk.Label(tiles, text="Filter").grid(row=0, column=0, sticky="w", pady=(0, 2))
        ttk.Entry(tiles, textvariable=self.download_tile_filter_var).grid(
            row=0, column=1, sticky="ew", pady=(0, 2)
        )
        ttk.Label(
            tiles,
            text="Select tokens such as UTM30R; list clicks add to the current set, while the text box or map can remove tiles",
            foreground="#666666",
        ).grid(row=1, column=1, sticky="w", pady=(0, 8))

        list_frame = ttk.Frame(tiles)
        list_frame.grid(row=2, column=1, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        self.download_tile_listbox = tk.Listbox(
            list_frame,
            selectmode="extended",
            height=7,
            exportselection=False,
        )
        self.download_tile_listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.download_tile_listbox.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.download_tile_listbox.configure(yscrollcommand=scrollbar.set)
        self.download_tile_listbox.bind("<<ListboxSelect>>", self.on_download_tile_select)

        ttk.Label(tiles, text="Selected tiles").grid(row=3, column=0, sticky="w", pady=(8, 2))
        selected_frame = ttk.Frame(tiles)
        selected_frame.grid(row=3, column=1, sticky="ew", pady=(8, 2))
        selected_frame.columnconfigure(0, weight=1)
        ttk.Entry(selected_frame, textvariable=self.download_selected_tiles_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            selected_frame,
            text="Apply",
            command=self.apply_download_tiles_from_text,
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            selected_frame,
            text="Clear",
            command=self.clear_download_tiles,
        ).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(
            selected_frame,
            text="Open UTM Map Selector",
            command=self.open_utm_map_selector,
        ).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(tiles, text="Tile preset").grid(row=4, column=0, sticky="w", pady=(8, 2))
        preset_frame = ttk.Frame(tiles)
        preset_frame.grid(row=4, column=1, sticky="ew", pady=(8, 2))
        preset_frame.columnconfigure(0, weight=1)
        self.tile_preset_combo = ttk.Combobox(
            preset_frame,
            textvariable=self.tile_preset_var,
            values=[],
            state="readonly",
        )
        self.tile_preset_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            preset_frame,
            text="Apply Preset",
            command=self.apply_tile_preset,
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            preset_frame,
            text="Save Selected As Preset",
            command=self.save_selected_tiles_as_preset,
        ).grid(row=0, column=2, padx=(8, 0))

        options = ttk.LabelFrame(parent, text="Options", padding=12)
        options.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        ttk.Checkbutton(
            options,
            text="Skip files that already exist in the output folder",
            variable=self.download_skip_existing_var,
        ).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Checkbutton(
            options,
            text="Skip granules already recorded in the project download manifest",
            variable=self.download_skip_manifest_existing_var,
        ).grid(row=1, column=0, sticky="w", pady=4)

        preview = ttk.LabelFrame(parent, text="Preview", padding=12)
        preview.grid(row=4, column=0, sticky="nsew", pady=(14, 0))
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)
        columns = (
            "file_name",
            "utm_tile",
            "start_time",
            "end_time",
            "size_mb",
            "downloaded",
            "raw_exists",
            "known_from_manifest",
            "selected_for_download",
            "duplicate_filter_status",
            "status",
        )
        self.download_preview_tree = ttk.Treeview(
            preview,
            columns=columns,
            show="headings",
            height=7,
        )
        headings = {
            "file_name": ("File name", 330),
            "utm_tile": ("UTM", 70),
            "start_time": ("Start", 145),
            "end_time": ("End", 145),
            "size_mb": ("MB", 70),
            "downloaded": ("Downloaded", 90),
            "raw_exists": ("Raw exists", 85),
            "known_from_manifest": ("Manifest", 85),
            "selected_for_download": ("Selected", 75),
            "duplicate_filter_status": ("Version filter", 145),
            "status": ("Status", 110),
        }
        for column, (label, width) in headings.items():
            self.download_preview_tree.heading(column, text=label)
            self.download_preview_tree.column(column, width=width, anchor="w")
        self.download_preview_tree.grid(row=0, column=0, sticky="nsew")
        preview_scrollbar = ttk.Scrollbar(
            preview,
            orient="vertical",
            command=self.download_preview_tree.yview,
        )
        preview_scrollbar.grid(row=0, column=1, sticky="ns")
        self.download_preview_tree.configure(yscrollcommand=preview_scrollbar.set)

        controls = ttk.Frame(parent)
        controls.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        controls.columnconfigure(0, weight=1)
        ttk.Button(controls, text="Preview Search", command=self.preview_download).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(controls, text="Download Matches", command=self.start_download).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Button(controls, text="Stop Download", command=self.stop_download).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        progress = ttk.Frame(parent)
        progress.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        progress.columnconfigure(0, weight=1)
        ttk.Progressbar(
            progress,
            variable=self.download_progress_var,
            maximum=100,
            mode="determinate",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Label(
            progress,
            textvariable=self.download_progress_text_var,
            foreground="#555555",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        ttk.Label(
            parent,
            textvariable=self.download_status_var,
            foreground="#184a8b",
            justify="left",
        ).grid(row=7, column=0, sticky="w", pady=(12, 0))

        self.download_tile_filter_var.trace_add(
            "write",
            lambda *_args: self.refresh_download_tile_list(),
        )
        self.refresh_download_tile_list()
        self.refresh_tile_presets()
        self.load_download_report_preview(limit=100)

    def build_duplicate_tab(self, parent: ttk.Frame) -> None:
        """Create duplicate-removal controls for raw SWOT downloads."""
        parent.columnconfigure(0, weight=1)

        form = ttk.Frame(parent)
        form.grid(row=0, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)

        row = 0
        row = self.add_path_row(
            form,
            row,
            "Input folder",
            self.duplicate_input_var,
            self.browse_duplicate_input_folder,
            "Folder containing raw downloaded SWOT files, usually .nc files",
        )
        row = self.add_entry_row(
            form,
            row,
            "Moved folder name",
            self.duplicate_moved_folder_var,
            "Older versions move into this subfolder inside the input folder",
        )
        row = self.add_path_row(
            form,
            row,
            "Log folder",
            self.duplicate_log_folder_var,
            self.browse_duplicate_log_folder,
            "Folder for timestamped duplicate-removal .txt logs",
        )

        toggles = ttk.LabelFrame(parent, text="Options", padding=12)
        toggles.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ttk.Checkbutton(
            toggles,
            text="Scan subfolders recursively",
            variable=self.duplicate_recursive_var,
        ).grid(row=0, column=0, sticky="w", padx=(0, 18), pady=4)

        notes = ttk.LabelFrame(parent, text="Behavior", padding=12)
        notes.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            notes,
            text=(
                "Files are grouped when they are identical except for the final numeric suffix, such as _01, _02, or _05.\n"
                "The highest numeric version remains in place. Older versions move into the configured moved subfolder."
            ),
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(parent)
        buttons.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        buttons.columnconfigure(0, weight=1)

        ttk.Button(
            buttons,
            text="Plan Duplicate Removal",
            command=self.plan_duplicate_removal,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            buttons,
            text="Run Duplicate Removal",
            command=self.run_duplicate_removal,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))

        status = ttk.Label(
            parent,
            textvariable=self.duplicate_status_var,
            foreground="#184a8b",
            justify="left",
        )
        status.grid(row=4, column=0, sticky="w", pady=(14, 0))

    def build_extract_tab(self, parent: ttk.Frame) -> None:
        """Create the SWOT NetCDF extraction controls."""
        parent.columnconfigure(0, weight=1)

        form = ttk.Frame(parent)
        form.grid(row=0, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)

        row = 0
        row = self.add_path_row(
            form,
            row,
            "GDAL Python",
            self.gdal_python_var,
            self.browse_gdal_python,
            "Python executable from the conda GDAL environment",
        )
        row = self.add_path_row(
            form,
            row,
            "Input NetCDF folder",
            self.extract_input_var,
            self.browse_extract_input_folder,
            "Folder containing cleaned SWOT .nc files",
        )
        row = self.add_path_row(
            form,
            row,
            "Output GeoTIFF folder",
            self.extract_output_var,
            self.browse_extract_output_folder,
            "Folder where extracted two-band GeoTIFFs will be written",
        )
        row = self.add_combo_row(
            form,
            row,
            "CRS mode",
            self.extract_crs_mode_var,
            list(EXTRACT_CRS_LABELS.keys()),
            "Matches the notebook options: original, Africa LAEA, or WGS84",
        )
        row = self.add_entry_row(
            form,
            row,
            "Year selection",
            self.extract_year_selection_var,
            "Use all, one year such as 2025, or comma-separated years such as 2024,2025",
        )
        row = self.add_entry_row(
            form,
            row,
            "Limit files",
            self.extract_limit_files_var,
            "Optional testing limit; leave blank to process all selected files",
        )
        row = self.add_path_row(
            form,
            row,
            "Manifest CSV",
            self.extract_manifest_var,
            self.browse_extract_manifest_file,
            "CSV for written and skipped outputs",
        )
        row = self.add_path_row(
            form,
            row,
            "Errors CSV",
            self.extract_errors_var,
            self.browse_extract_errors_file,
            "CSV for failed or unmatched NetCDF files",
        )

        toggles = ttk.LabelFrame(parent, text="Options", padding=12)
        toggles.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ttk.Checkbutton(
            toggles,
            text="Skip existing valid GeoTIFF outputs",
            variable=self.extract_skip_existing_var,
        ).grid(row=0, column=0, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Skip NetCDFs already recorded in the extraction manifest",
            variable=self.extract_skip_manifest_existing_var,
        ).grid(row=1, column=0, sticky="w", padx=(0, 18), pady=4)

        notes = ttk.LabelFrame(parent, text="Notebook Workflow", padding=12)
        notes.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            notes,
            text=(
                "Extraction uses GDAL only: open NetCDF subdatasets wse and wse_qual, build a two-band VRT, "
                "then write GeoTIFF.\n"
                "Original CRS uses gdal.Translate. LAEA/WGS84 use gdal.Warp with nearest-neighbor resampling."
            ),
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(parent)
        buttons.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        buttons.columnconfigure(0, weight=1)

        ttk.Button(buttons, text="Plan Extraction", command=self.plan_extraction).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(buttons, text="Run Extraction", command=self.run_extraction).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )

        progress = ttk.Frame(parent)
        progress.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        progress.columnconfigure(0, weight=1)
        ttk.Progressbar(
            progress,
            variable=self.extract_progress_var,
            maximum=100,
            mode="determinate",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Label(
            progress,
            textvariable=self.extract_progress_text_var,
            foreground="#555555",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        status = ttk.Label(
            parent,
            textvariable=self.extract_status_var,
            foreground="#184a8b",
            justify="left",
        )
        status.grid(row=5, column=0, sticky="w", pady=(14, 0))

    def build_upload_tab(self, parent: ttk.Frame) -> None:
        """Create the existing Earth Engine upload controls."""
        parent.columnconfigure(0, weight=1)

        form = ttk.Frame(parent)
        form.grid(row=0, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)

        row = 0
        row = self.add_path_row(
            form,
            row,
            "Origin folder",
            self.folder_var,
            self.browse_input_folder,
            "Folder containing your .tif / .tiff files",
        )
        row = self.add_entry_row(
            form,
            row,
            "Destination collection",
            self.destination_var,
            "Example: projects/MY_PROJECT/assets/MY_COLLECTION",
        )
        ttk.Label(form, text="Upload scope").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(
            form,
            textvariable=self.upload_scope_var,
            values=list(UPLOAD_SCOPE_LABELS),
            state="readonly",
        ).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Label(
            form,
            text="All files is the default; selected mode uses output UTM or mosaic source UTM tiles.",
            foreground="#555555",
        ).grid(row=row + 1, column=1, sticky="w", pady=(0, 4))
        row += 2

        upload_tiles = ttk.LabelFrame(form, text="Upload UTM Tiles", padding=8)
        upload_tiles.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 8))
        upload_tiles.columnconfigure(1, weight=1)
        upload_tiles.columnconfigure(3, weight=1)
        ttk.Label(upload_tiles, text="Filter").grid(row=0, column=0, sticky="w")
        ttk.Entry(upload_tiles, textvariable=self.upload_tile_filter_var).grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(8, 8),
        )
        ttk.Label(
            upload_tiles,
            textvariable=self.upload_tile_availability_var,
            foreground="#555555",
            wraplength=520,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        list_frame = ttk.Frame(upload_tiles)
        list_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        list_frame.columnconfigure(0, weight=1)
        self.upload_tile_listbox = tk.Listbox(
            list_frame,
            height=5,
            selectmode=tk.MULTIPLE,
            exportselection=False,
        )
        self.upload_tile_listbox.grid(row=0, column=0, sticky="nsew")
        upload_scroll = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.upload_tile_listbox.yview,
        )
        upload_scroll.grid(row=0, column=1, sticky="ns")
        self.upload_tile_listbox.configure(yscrollcommand=upload_scroll.set)
        self.upload_tile_listbox.bind("<<ListboxSelect>>", self.on_upload_tile_select)
        ttk.Label(upload_tiles, text="Selected upload tiles").grid(
            row=0,
            column=2,
            sticky="w",
            padx=(8, 0),
        )
        selected_upload_frame = ttk.Frame(upload_tiles)
        selected_upload_frame.grid(row=0, column=3, rowspan=2, sticky="nsew")
        selected_upload_frame.columnconfigure(0, weight=1)
        ttk.Entry(
            selected_upload_frame,
            textvariable=self.upload_selected_tiles_var,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            selected_upload_frame,
            text="Validate Typed Tiles",
            command=self.apply_upload_tiles_from_text,
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(
            selected_upload_frame,
            text="Clear Upload Tiles",
            command=self.clear_upload_tiles,
        ).grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(
            selected_upload_frame,
            text="Refresh Available Tiles",
            command=self.refresh_upload_tile_list,
        ).grid(row=3, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(
            selected_upload_frame,
            textvariable=self.upload_tile_status_var,
            foreground="#184a8b",
            wraplength=320,
            justify="left",
        ).grid(row=4, column=0, sticky="ew", pady=(6, 0))
        row += 1

        row = self.add_entry_row(
            form,
            row,
            "Batch size",
            self.batch_size_var,
            "Use 50 if you want to mirror the common Earth Engine UI limit",
        )
        row = self.add_entry_row(
            form,
            row,
            "Max active ingestions",
            self.max_active_var,
            "Use 0 if you want the next batch to wait until the previous batch is fully finished",
        )
        row = self.add_entry_row(
            form,
            row,
            "Asset prefix",
            self.prefix_var,
            "Optional text added before each generated image name",
        )
        row = self.add_entry_row(
            form,
            row,
            "Asset suffix",
            self.suffix_var,
            "Optional text added after each generated image name",
        )
        row = self.add_entry_row(
            form,
            row,
            "Global pyramiding policy",
            self.pyramiding_var,
            "Leave blank to keep the Earth Engine default",
        )
        row = self.add_path_row(
            form,
            row,
            "Chrome profile folder",
            self.profile_dir_var,
            self.browse_profile_folder,
            "Dedicated local Chrome profile for this tool",
        )
        row = self.add_entry_row(
            form,
            row,
            "Retry attempts",
            self.retry_attempts_var,
            "How many times to retry transient UI failures",
        )
        row = self.add_entry_row(
            form,
            row,
            "Retry wait seconds",
            self.retry_wait_var,
            "Pause between retries",
        )

        toggles = ttk.LabelFrame(parent, text="Options", padding=12)
        toggles.grid(row=1, column=0, sticky="ew", pady=(14, 0))

        ttk.Checkbutton(
            toggles, text="Resume previous run", variable=self.resume_var
        ).grid(row=0, column=0, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles, text="Dry run", variable=self.dry_run_var
        ).grid(row=0, column=1, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles, text="Scan subfolders recursively", variable=self.recursive_var
        ).grid(row=1, column=0, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles, text="Fail fast on first error", variable=self.fail_fast_var
        ).grid(row=1, column=1, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Run Chrome headless (advanced, not for Google login)",
            variable=self.headless_var,
        ).grid(row=2, column=0, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Add SWOT metadata properties",
            variable=self.metadata_enabled_var,
        ).grid(row=3, column=0, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Require SWOT filename match",
            variable=self.metadata_require_match_var,
        ).grid(row=3, column=1, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Add end time property",
            variable=self.metadata_add_end_time_var,
        ).grid(row=4, column=0, sticky="w", padx=(0, 18), pady=4)

        buttons = ttk.LabelFrame(parent, text="Execution", padding=12)
        buttons.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        buttons.columnconfigure(0, weight=1)

        ttk.Button(
            buttons,
            text="Open Chrome For Manual Login",
            command=self.open_manual_login_browser,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            buttons, text="Run Dry Run", command=self.save_and_run_dry_run
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(
            buttons, text="Run Real Upload", command=self.save_and_run_real
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(
            buttons,
            text="List clicks update the optional UTM filter immediately. Use Validate Typed Tiles only to check typed or pasted IDs before running; use Run Dry Run or Run Real Upload to execute.",
            foreground="#555555",
            wraplength=900,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        notes = ttk.LabelFrame(parent, text="Notes", padding=12)
        notes.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            notes,
            text=(
                "1. Run buttons save the active project settings before launching.\n"
                "2. By default the tool starts normal Chrome in attach mode, then Selenium attaches to it.\n"
                "3. On the first real run, Chrome may ask you to sign in manually.\n"
                "4. Keep the dedicated Chrome profile for future runs.\n"
                "5. To upload mosaics, set Origin folder to the Mosaic tab output folder.\n"
                "6. Real uploads still ask for final confirmation in the console unless you later change that in config."
            ),
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.upload_tile_filter_var.trace_add(
            "write",
            lambda *_args: self.refresh_upload_tile_list(),
        )
        self.folder_var.trace_add(
            "write",
            lambda *_args: self.refresh_upload_tile_list(),
        )
        self.recursive_var.trace_add(
            "write",
            lambda *_args: self.refresh_upload_tile_list(),
        )
        self.refresh_upload_tile_list()

        status = ttk.Label(
            parent,
            textvariable=self.status_var,
            foreground="#184a8b",
            justify="left",
        )
        status.grid(row=4, column=0, sticky="w", pady=(14, 0))

    def build_statistics_tab(self, parent: ttk.Frame) -> None:
        """Create project statistics, plots, and QA controls."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        status = ttk.Label(
            parent,
            textvariable=self.statistics_status_var,
            foreground="#184a8b",
            justify="left",
        )
        status.grid(row=0, column=0, sticky="w", pady=(0, 8))

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        actions.columnconfigure(1, weight=1)
        ttk.Button(
            actions,
            text="Refresh Statistics",
            command=self.refresh_project_statistics,
        ).grid(row=0, column=0, sticky="w")

        inner = ttk.Notebook(parent)
        inner.grid(row=2, column=0, sticky="nsew")

        overview = ttk.Frame(inner, padding=8)
        tiles = ttk.Frame(inner, padding=8)
        levels = ttk.Frame(inner, padding=8)
        mosaics = ttk.Frame(inner, padding=8)
        uploaded = ttk.Frame(inner, padding=8)
        inner.add(overview, text="Overview")
        inner.add(tiles, text="Tiles And Dates")
        inner.add(levels, text="Processing Levels")
        inner.add(mosaics, text="Mosaics")
        inner.add(uploaded, text="Uploaded")

        overview.columnconfigure(0, weight=1)
        overview.rowconfigure(0, weight=1)
        overview.rowconfigure(1, weight=0)
        self.stats_metrics_tree = ttk.Treeview(
            overview,
            columns=("metric", "value"),
            show="headings",
            height=12,
        )
        self.stats_metrics_tree.heading("metric", text="Metric")
        self.stats_metrics_tree.heading("value", text="Value")
        self.stats_metrics_tree.column("metric", width=310, anchor="w")
        self.stats_metrics_tree.column("value", width=180, anchor="w")
        self.stats_metrics_tree.grid(row=0, column=0, sticky="nsew")
        metrics_scroll = ttk.Scrollbar(
            overview,
            orient="vertical",
            command=self.stats_metrics_tree.yview,
        )
        metrics_scroll.grid(row=0, column=1, sticky="ns")
        self.stats_metrics_tree.configure(yscrollcommand=metrics_scroll.set)

        charts = ttk.LabelFrame(overview, text="Plots", padding=8)
        charts.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        charts.columnconfigure(0, weight=1)
        charts.columnconfigure(1, weight=1)
        self.stats_stage_chart_canvas = tk.Canvas(
            charts,
            height=150,
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            background="white",
        )
        self.stats_stage_chart_canvas.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.stats_tile_chart_canvas = tk.Canvas(
            charts,
            height=150,
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            background="white",
        )
        self.stats_tile_chart_canvas.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        tiles.columnconfigure(0, weight=1)
        tiles.columnconfigure(1, weight=1)
        tiles.rowconfigure(0, weight=1)
        tile_frame = ttk.LabelFrame(tiles, text="Files By UTM Tile", padding=8)
        tile_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        tile_frame.columnconfigure(0, weight=1)
        tile_frame.rowconfigure(0, weight=1)
        self.stats_tile_tree = ttk.Treeview(
            tile_frame,
            columns=("tile", "count"),
            show="headings",
            height=16,
        )
        self.stats_tile_tree.heading("tile", text="Tile")
        self.stats_tile_tree.heading("count", text="Count")
        self.stats_tile_tree.column("tile", width=120, anchor="w")
        self.stats_tile_tree.column("count", width=80, anchor="e")
        self.stats_tile_tree.grid(row=0, column=0, sticky="nsew")

        date_frame = ttk.LabelFrame(tiles, text="Files By Date", padding=8)
        date_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        date_frame.columnconfigure(0, weight=1)
        date_frame.rowconfigure(0, weight=1)
        self.stats_date_tree = ttk.Treeview(
            date_frame,
            columns=("date", "count"),
            show="headings",
            height=16,
        )
        self.stats_date_tree.heading("date", text="Date")
        self.stats_date_tree.heading("count", text="Count")
        self.stats_date_tree.column("date", width=130, anchor="w")
        self.stats_date_tree.column("count", width=80, anchor="e")
        self.stats_date_tree.grid(row=0, column=0, sticky="nsew")

        levels.columnconfigure(0, weight=1)
        levels.columnconfigure(1, weight=1)
        levels.rowconfigure(0, weight=1)
        level_summary_frame = ttk.LabelFrame(
            levels,
            text="Processing Levels Across Stages",
            padding=8,
        )
        level_summary_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        level_summary_frame.columnconfigure(0, weight=1)
        level_summary_frame.rowconfigure(0, weight=1)
        self.stats_processing_level_tree = ttk.Treeview(
            level_summary_frame,
            columns=(
                "level",
                "remote",
                "selected",
                "downloaded",
                "extracted",
                "mosaic_sources",
                "uploaded",
            ),
            show="headings",
            height=16,
        )
        level_headings = {
            "level": "Level",
            "remote": "Remote",
            "selected": "Selected",
            "downloaded": "Downloaded",
            "extracted": "Extracted",
            "mosaic_sources": "Mosaic src",
            "uploaded": "Uploaded/verified",
        }
        for column, heading in level_headings.items():
            self.stats_processing_level_tree.heading(column, text=heading)
            width = 105 if column != "level" else 115
            anchor = "w" if column == "level" else "e"
            self.stats_processing_level_tree.column(column, width=width, anchor=anchor)
        self.stats_processing_level_tree.grid(row=0, column=0, sticky="nsew")

        tile_level_frame = ttk.LabelFrame(
            levels,
            text="Processing Levels By UTM Tile",
            padding=8,
        )
        tile_level_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        tile_level_frame.columnconfigure(0, weight=1)
        tile_level_frame.rowconfigure(0, weight=1)
        self.stats_processing_level_tile_tree = ttk.Treeview(
            tile_level_frame,
            columns=("tile", "level", "remote", "downloaded", "extracted", "mosaic_sources"),
            show="headings",
            height=16,
        )
        tile_level_headings = {
            "tile": "Tile",
            "level": "Level",
            "remote": "Remote",
            "downloaded": "Downloaded",
            "extracted": "Extracted",
            "mosaic_sources": "Mosaic src",
        }
        for column, heading in tile_level_headings.items():
            self.stats_processing_level_tile_tree.heading(column, text=heading)
            width = 100 if column in {"tile", "level"} else 90
            anchor = "w" if column in {"tile", "level"} else "e"
            self.stats_processing_level_tile_tree.column(column, width=width, anchor=anchor)
        self.stats_processing_level_tile_tree.grid(row=0, column=0, sticky="nsew")

        mosaics.columnconfigure(0, weight=1)
        mosaics.columnconfigure(1, weight=1)
        mosaics.rowconfigure(0, weight=1)
        mosaic_grid_frame = ttk.LabelFrame(
            mosaics,
            text="Completed Mosaics By Output Tile/Grid",
            padding=8,
        )
        mosaic_grid_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        mosaic_grid_frame.columnconfigure(0, weight=1)
        mosaic_grid_frame.rowconfigure(0, weight=1)
        self.stats_mosaic_output_grid_tree = ttk.Treeview(
            mosaic_grid_frame,
            columns=("grid", "count"),
            show="headings",
            height=16,
        )
        self.stats_mosaic_output_grid_tree.heading("grid", text="Tile/Grid")
        self.stats_mosaic_output_grid_tree.heading("count", text="Count")
        self.stats_mosaic_output_grid_tree.column("grid", width=150, anchor="w")
        self.stats_mosaic_output_grid_tree.column("count", width=80, anchor="e")
        self.stats_mosaic_output_grid_tree.grid(row=0, column=0, sticky="nsew")

        mosaic_source_frame = ttk.LabelFrame(
            mosaics,
            text="Completed Mosaics By Source UTM Tile",
            padding=8,
        )
        mosaic_source_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        mosaic_source_frame.columnconfigure(0, weight=1)
        mosaic_source_frame.rowconfigure(0, weight=1)
        self.stats_mosaic_source_tile_tree = ttk.Treeview(
            mosaic_source_frame,
            columns=("tile", "count"),
            show="headings",
            height=16,
        )
        self.stats_mosaic_source_tile_tree.heading("tile", text="Source tile")
        self.stats_mosaic_source_tile_tree.heading("count", text="Count")
        self.stats_mosaic_source_tile_tree.column("tile", width=150, anchor="w")
        self.stats_mosaic_source_tile_tree.column("count", width=80, anchor="e")
        self.stats_mosaic_source_tile_tree.grid(row=0, column=0, sticky="nsew")

        uploaded.columnconfigure(0, weight=1)
        uploaded.rowconfigure(0, weight=1)
        uploaded_inner = ttk.Notebook(uploaded)
        uploaded_inner.grid(row=0, column=0, sticky="nsew")
        uploaded_summary = ttk.Frame(uploaded_inner, padding=4)
        uploaded_qa = ttk.Frame(uploaded_inner, padding=4)
        uploaded_inner.add(uploaded_summary, text="Summary")
        uploaded_inner.add(uploaded_qa, text="QA")

        uploaded_summary.columnconfigure(0, weight=1)
        uploaded_summary.columnconfigure(1, weight=1)
        uploaded_summary.rowconfigure(0, weight=1)
        uploaded_summary.rowconfigure(1, weight=1)

        upload_status_frame = ttk.LabelFrame(uploaded_summary, text="Upload Status Counts", padding=8)
        upload_status_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 5))
        upload_status_frame.columnconfigure(0, weight=1)
        upload_status_frame.rowconfigure(0, weight=1)
        self.stats_upload_status_tree = ttk.Treeview(
            upload_status_frame,
            columns=("status", "count"),
            show="headings",
            height=8,
        )
        self.stats_upload_status_tree.heading("status", text="Status")
        self.stats_upload_status_tree.heading("count", text="Count")
        self.stats_upload_status_tree.column("status", width=180, anchor="w")
        self.stats_upload_status_tree.column("count", width=80, anchor="e")
        self.stats_upload_status_tree.grid(row=0, column=0, sticky="nsew")

        uploaded_tile_frame = ttk.LabelFrame(uploaded_summary, text="Uploaded/Verified By Source Tile", padding=8)
        uploaded_tile_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=(0, 5))
        uploaded_tile_frame.columnconfigure(0, weight=1)
        uploaded_tile_frame.rowconfigure(0, weight=1)
        self.stats_uploaded_tile_tree = ttk.Treeview(
            uploaded_tile_frame,
            columns=("tile", "count"),
            show="headings",
            height=8,
        )
        self.stats_uploaded_tile_tree.heading("tile", text="Tile")
        self.stats_uploaded_tile_tree.heading("count", text="Count")
        self.stats_uploaded_tile_tree.column("tile", width=130, anchor="w")
        self.stats_uploaded_tile_tree.column("count", width=80, anchor="e")
        self.stats_uploaded_tile_tree.grid(row=0, column=0, sticky="nsew")

        uploaded_date_frame = ttk.LabelFrame(uploaded_summary, text="Uploaded/Verified By Date", padding=8)
        uploaded_date_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(5, 0))
        uploaded_date_frame.columnconfigure(0, weight=1)
        uploaded_date_frame.rowconfigure(0, weight=1)
        self.stats_uploaded_date_tree = ttk.Treeview(
            uploaded_date_frame,
            columns=("date", "count"),
            show="headings",
            height=8,
        )
        self.stats_uploaded_date_tree.heading("date", text="Date")
        self.stats_uploaded_date_tree.heading("count", text="Count")
        self.stats_uploaded_date_tree.column("date", width=130, anchor="w")
        self.stats_uploaded_date_tree.column("count", width=80, anchor="e")
        self.stats_uploaded_date_tree.grid(row=0, column=0, sticky="nsew")

        uploaded_level_frame = ttk.LabelFrame(uploaded_summary, text="Uploaded/Verified By Processing Level", padding=8)
        uploaded_level_frame.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(5, 0))
        uploaded_level_frame.columnconfigure(0, weight=1)
        uploaded_level_frame.rowconfigure(0, weight=1)
        self.stats_uploaded_level_tree = ttk.Treeview(
            uploaded_level_frame,
            columns=("level", "count"),
            show="headings",
            height=8,
        )
        self.stats_uploaded_level_tree.heading("level", text="Level")
        self.stats_uploaded_level_tree.heading("count", text="Count")
        self.stats_uploaded_level_tree.column("level", width=130, anchor="w")
        self.stats_uploaded_level_tree.column("count", width=80, anchor="e")
        self.stats_uploaded_level_tree.grid(row=0, column=0, sticky="nsew")

        uploaded_qa.columnconfigure(0, weight=1)
        uploaded_qa.columnconfigure(1, weight=1)
        uploaded_qa.rowconfigure(0, weight=1)
        uploaded_qa.rowconfigure(1, weight=1)

        qa_tile_frame = ttk.LabelFrame(uploaded_qa, text="Pipeline Completeness By UTM Tile", padding=8)
        qa_tile_frame.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 5))
        qa_tile_frame.columnconfigure(0, weight=1)
        qa_tile_frame.rowconfigure(0, weight=1)
        self.stats_upload_qa_tile_tree = ttk.Treeview(
            qa_tile_frame,
            columns=("tile", "downloaded", "extracted", "mosaic_sources", "uploaded", "missing_upload"),
            show="headings",
            height=9,
        )
        qa_headings = {
            "tile": "Tile",
            "downloaded": "Downloaded",
            "extracted": "Extracted",
            "mosaic_sources": "Mosaic src",
            "uploaded": "Uploaded/verified",
            "missing_upload": "Missing upload",
        }
        for column, heading in qa_headings.items():
            self.stats_upload_qa_tile_tree.heading(column, text=heading)
            self.stats_upload_qa_tile_tree.column(
                column,
                width=130 if column == "tile" else 110,
                anchor="w" if column == "tile" else "e",
            )
        self.stats_upload_qa_tile_tree.grid(row=0, column=0, sticky="nsew")

        ready_frame = ttk.LabelFrame(uploaded_qa, text="Ready Mosaics Not Uploaded/Verified", padding=8)
        ready_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(5, 0))
        ready_frame.columnconfigure(0, weight=1)
        ready_frame.rowconfigure(0, weight=1)
        self.stats_ready_not_uploaded_tree = ttk.Treeview(
            ready_frame,
            columns=("date", "grid", "source_tiles", "output_file"),
            show="headings",
            height=8,
        )
        self.stats_ready_not_uploaded_tree.heading("date", text="Date")
        self.stats_ready_not_uploaded_tree.heading("grid", text="Grid")
        self.stats_ready_not_uploaded_tree.heading("source_tiles", text="Source tiles")
        self.stats_ready_not_uploaded_tree.heading("output_file", text="Output file")
        self.stats_ready_not_uploaded_tree.column("date", width=95, anchor="w")
        self.stats_ready_not_uploaded_tree.column("grid", width=90, anchor="w")
        self.stats_ready_not_uploaded_tree.column("source_tiles", width=130, anchor="w")
        self.stats_ready_not_uploaded_tree.column("output_file", width=360, anchor="w")
        self.stats_ready_not_uploaded_tree.grid(row=0, column=0, sticky="nsew")

        upload_errors_frame = ttk.LabelFrame(uploaded_qa, text="Upload Failures / Warnings", padding=8)
        upload_errors_frame.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(5, 0))
        upload_errors_frame.columnconfigure(0, weight=1)
        upload_errors_frame.rowconfigure(0, weight=1)
        self.stats_upload_errors_tree = ttk.Treeview(
            upload_errors_frame,
            columns=("status", "count", "message"),
            show="headings",
            height=8,
        )
        self.stats_upload_errors_tree.heading("status", text="Status")
        self.stats_upload_errors_tree.heading("count", text="Count")
        self.stats_upload_errors_tree.heading("message", text="Message")
        self.stats_upload_errors_tree.column("status", width=120, anchor="w")
        self.stats_upload_errors_tree.column("count", width=70, anchor="e")
        self.stats_upload_errors_tree.column("message", width=360, anchor="w")
        self.stats_upload_errors_tree.grid(row=0, column=0, sticky="nsew")

        ttk.Label(
            parent,
            textvariable=self.statistics_summary_var,
            foreground="#555555",
            justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

    def build_cleanup_tab(self, parent: ttk.Frame) -> None:
        """Create conservative intermediate-file cleanup controls."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        ttk.Label(
            parent,
            textvariable=self.cleanup_status_var,
            foreground="#184a8b",
            justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        actions.columnconfigure(3, weight=1)
        ttk.Button(
            actions,
            text="Preview Cleanup",
            command=self.preview_cleanup_candidates,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            actions,
            text="Delete Selected Cleanup Files",
            command=self.delete_selected_cleanup_files,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(
            actions,
            text="Delete All Cleanup Candidates",
            command=self.delete_all_cleanup_files,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(
            parent,
            text=(
                "Cleanup candidates are local intermediate files with downstream manifest proof. "
                "Preview first, then delete selected rows or all candidates."
            ),
            foreground="#555555",
            justify="left",
            wraplength=780,
        ).grid(row=2, column=0, sticky="w", pady=(0, 8))

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=3, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.cleanup_tree = ttk.Treeview(
            table_frame,
            columns=("stage", "size", "reason", "path"),
            show="headings",
            height=16,
            selectmode="extended",
        )
        self.cleanup_tree.heading("stage", text="Level")
        self.cleanup_tree.heading("size", text="Size")
        self.cleanup_tree.heading("reason", text="Reason")
        self.cleanup_tree.heading("path", text="File")
        self.cleanup_tree.column("stage", width=90, anchor="w")
        self.cleanup_tree.column("size", width=90, anchor="e")
        self.cleanup_tree.column("reason", width=300, anchor="w")
        self.cleanup_tree.column("path", width=420, anchor="w")
        self.cleanup_tree.grid(row=0, column=0, sticky="nsew")
        cleanup_scroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.cleanup_tree.yview,
        )
        cleanup_scroll.grid(row=0, column=1, sticky="ns")
        self.cleanup_tree.configure(yscrollcommand=cleanup_scroll.set)

    def build_mosaic_tab(self, parent: ttk.Frame) -> None:
        """Create the SWOT GeoTIFF mosaic controls."""
        parent.columnconfigure(0, weight=1)

        status = ttk.Label(
            parent,
            textvariable=self.mosaic_status_var,
            foreground="#184a8b",
            justify="left",
        )
        status.grid(row=0, column=0, sticky="w", pady=(0, 8))

        progress = ttk.Frame(parent)
        progress.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        progress.columnconfigure(0, weight=1)
        ttk.Progressbar(
            progress,
            variable=self.mosaic_progress_var,
            maximum=100,
            mode="determinate",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Label(
            progress,
            textvariable=self.mosaic_progress_text_var,
            foreground="#555555",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        form = ttk.Frame(parent)
        form.grid(row=2, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)

        row = 0
        row = self.add_path_row(
            form,
            row,
            "GDAL Python",
            self.gdal_python_var,
            self.browse_gdal_python,
            "Python executable from the conda GDAL environment",
        )
        row = self.add_path_row(
            form,
            row,
            "Mosaic input folder",
            self.mosaic_input_var,
            self.browse_mosaic_input_folder,
            "Folder containing original SWOT .tif / .tiff tiles",
        )
        row = self.add_path_row(
            form,
            row,
            "Mosaic output folder",
            self.mosaic_output_var,
            self.browse_mosaic_output_folder,
            "Folder where upload-ready mosaic GeoTIFFs will be written",
        )
        row = self.add_combo_row(
            form,
            row,
            "Grouping mode",
            self.mosaic_grouping_mode_var,
            list(MOSAIC_GROUPING_LABELS.keys()),
            "Use common-CRS mode only for LAEA/WGS84 or other already reprojected outputs",
        )
        row = self.add_entry_row(
            form,
            row,
            "Target CRS label",
            self.mosaic_target_crs_label_var,
            "Optional for common-CRS mode; examples: LAEA or WGS84. Blank uses COMMON.",
        )
        row = self.add_entry_row(
            form,
            row,
            "Mosaic report CSV",
            self.mosaic_report_var,
            "CSV report path for planned, created, skipped, and invalid groups",
        )
        row = self.add_entry_row(
            form,
            row,
            "Mosaic manifest CSV",
            self.mosaic_manifest_var,
            "Cumulative mosaic status; records source signatures for incremental updates",
        )

        toggles = ttk.LabelFrame(parent, text="Options", padding=12)
        toggles.grid(row=3, column=0, sticky="ew", pady=(14, 0))

        ttk.Checkbutton(
            toggles,
            text="Scan subfolders recursively",
            variable=self.mosaic_recursive_var,
        ).grid(row=0, column=0, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Overwrite existing mosaic outputs",
            variable=self.mosaic_overwrite_var,
        ).grid(row=0, column=1, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Write .tfw world files beside mosaic GeoTIFFs",
            variable=self.mosaic_write_world_file_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Skip mosaics already recorded with the same source set",
            variable=self.mosaic_skip_manifest_existing_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=(0, 18), pady=4)
        ttk.Checkbutton(
            toggles,
            text="Set Upload origin folder to mosaic output after successful run",
            variable=self.mosaic_set_upload_folder_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=(0, 18), pady=4)

        notes = ttk.LabelFrame(parent, text="Grouping", padding=12)
        notes.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            notes,
            text=(
                "Original-projection mode groups by descriptor, cycle ID, pass ID, start date, and exact UTM token.\n"
                "Common-CRS mode ignores the original UTM token and groups the whole pass/date after reprojection.\n"
                "Singleton groups are written to the output folder. Original files are never moved or deleted."
            ),
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(parent)
        buttons.grid(row=5, column=0, sticky="ew", pady=(16, 0))
        buttons.columnconfigure(0, weight=1)

        ttk.Button(buttons, text="Plan Mosaics", command=self.plan_mosaics).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(buttons, text="Run Mosaic", command=self.run_mosaics).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )

    def add_entry_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        help_text: str,
    ) -> int:
        """Add a label, entry, and help text row."""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(0, 2))
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=(0, 2))
        ttk.Label(parent, text=help_text, foreground="#666666").grid(
            row=row + 1, column=1, sticky="w", pady=(0, 8)
        )
        return row + 2

    def add_combo_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        values: list[str],
        help_text: str,
    ) -> int:
        """Add a label, readonly combobox, and help text row."""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(0, 2))
        combo = ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            state="readonly",
        )
        combo.grid(row=row, column=1, sticky="ew", pady=(0, 2))
        ttk.Label(parent, text=help_text, foreground="#666666").grid(
            row=row + 1, column=1, sticky="w", pady=(0, 8)
        )
        return row + 2

    def add_path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse_command: Callable[[], None],
        help_text: str,
    ) -> int:
        """Add a label, path entry, browse button, and help text row."""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(0, 2))
        entry_frame = ttk.Frame(parent)
        entry_frame.grid(row=row, column=1, sticky="ew", pady=(0, 2))
        entry_frame.columnconfigure(0, weight=1)
        ttk.Entry(entry_frame, textvariable=variable).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(entry_frame, text="Browse", command=browse_command).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Label(parent, text=help_text, foreground="#666666").grid(
            row=row + 1, column=1, sticky="w", pady=(0, 8)
        )
        return row + 2

    @staticmethod
    def parse_int_or_default(value: str, default: int) -> int:
        """Return an int from UI text, or a safe default when another tab is invalid."""
        try:
            return int(value)
        except ValueError:
            return default

    @staticmethod
    def parse_float_or_default(value: str, default: float) -> float:
        """Return a float from UI text, or a safe default when another tab is invalid."""
        try:
            return float(value)
        except ValueError:
            return default

    @staticmethod
    def parse_optional_int_or_none(value: str) -> int | None:
        """Return an optional int from UI text, or None when blank/invalid."""
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None

    def selected_mosaic_grouping_mode(self) -> str:
        """Return the config value for the selected Mosaic grouping label."""
        return MOSAIC_GROUPING_LABELS.get(
            self.mosaic_grouping_mode_var.get(),
            "utm_zone",
        )

    def selected_extract_crs_mode(self) -> str:
        """Return the config value for the selected Extraction CRS label."""
        return EXTRACT_CRS_LABELS.get(
            self.extract_crs_mode_var.get(),
            "original",
        )

    def browse_input_folder(self) -> None:
        """Let the user choose the local GeoTIFF folder."""
        selected = filedialog.askdirectory(
            title="Choose the folder containing GeoTIFF files",
            initialdir=self.folder_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.folder_var.set(selected)

    def browse_download_output_folder(self) -> None:
        """Let the user choose the raw SWOT download output folder."""
        selected = filedialog.askdirectory(
            title="Choose the folder for downloaded SWOT NetCDF files",
            initialdir=self.download_output_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.download_output_var.set(selected)

    def browse_download_report_file(self) -> None:
        """Let the user choose the download preview/report CSV path."""
        selected = filedialog.asksaveasfilename(
            title="Choose the download report CSV path",
            initialfile=Path(self.download_report_var.get() or "download_preview.csv").name,
            initialdir=str(Path(self.download_report_var.get() or PROJECT_ROOT).parent),
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if selected:
            self.download_report_var.set(selected)

    def browse_download_manifest_file(self) -> None:
        """Let the user choose the cumulative download manifest CSV path."""
        selected = filedialog.asksaveasfilename(
            title="Choose the download manifest CSV path",
            initialfile=Path(self.download_manifest_var.get() or "download_manifest.csv").name,
            initialdir=str(Path(self.download_manifest_var.get() or PROJECT_ROOT).parent),
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if selected:
            self.download_manifest_var.set(selected)

    def new_project(self) -> None:
        """Create a new GeeUp project from the current GUI settings."""
        root = filedialog.askdirectory(
            title="Choose a folder for the new GeeUp project",
            initialdir=str(PROJECT_ROOT),
        )
        if not root:
            return
        default_name = Path(root).name or "GeeUp Project"
        name = simpledialog.askstring(
            "New GeeUp project",
            "Project name:",
            initialvalue=default_name,
            parent=self.root,
        )
        if name is None:
            return
        try:
            project = create_project(root, name, self.build_config())
        except OSError as exc:
            messagebox.showerror(
                "Could not create project",
                f"Failed to create the GeeUp project folders:\n{exc}",
            )
            return
        self.apply_project(project, write_config=True)
        messagebox.showinfo("Project created", f"Created project:\n{project.project_file}")

    def open_project(self) -> None:
        """Open a GeeUp project.yaml file and populate the GUI."""
        selected = filedialog.askopenfilename(
            title="Open GeeUp project.yaml",
            initialdir=str(PROJECT_ROOT),
            filetypes=[("GeeUp project", "project.yaml"), ("YAML files", "*.yaml"), ("All files", "*.*")],
        )
        if not selected:
            return
        try:
            project = load_project(selected)
            ensure_project_structure(project.root)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            messagebox.showerror(
                "Could not open project",
                f"Failed to open the GeeUp project:\n{exc}",
            )
            return
        self.apply_project(project, write_config=True)
        messagebox.showinfo("Project opened", f"Opened project:\n{project.project_file}")

    def save_project_action(self) -> None:
        """Save the current GUI state to the active project."""
        root = self.current_project_root_var.get().strip()
        if not root:
            self.save_project_as()
            return
        data = self.build_config()
        self.write_config_data(data)
        try:
            path = save_project_config(
                root,
                self.current_project_name_var.get(),
                data,
                self.project_download_history,
                created_at=self.project_created_at,
            )
        except OSError as exc:
            messagebox.showerror(
                "Could not save project",
                f"Failed to save the GeeUp project:\n{exc}",
            )
            return
        self.project_status_var.set(f"Saved project to {path}")
        messagebox.showinfo("Project saved", f"Saved project:\n{path}")

    def save_project_as(self) -> None:
        """Save current settings as a new project root."""
        root = filedialog.askdirectory(
            title="Choose a folder for the GeeUp project",
            initialdir=self.current_project_root_var.get() or str(PROJECT_ROOT),
        )
        if not root:
            return
        default_name = (
            self.current_project_name_var.get()
            if self.current_project_root_var.get().strip()
            else Path(root).name
        )
        name = simpledialog.askstring(
            "Save GeeUp project as",
            "Project name:",
            initialvalue=default_name or "GeeUp Project",
            parent=self.root,
        )
        if name is None:
            return
        try:
            ensure_project_structure(root)
            data = config_for_project(self.build_config(), root)
            path = save_project_config(root, name, data, [], created_at="")
            project = load_project(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            messagebox.showerror(
                "Could not save project",
                f"Failed to save the GeeUp project:\n{exc}",
            )
            return
        self.apply_project(project, write_config=True)
        messagebox.showinfo("Project saved", f"Saved project:\n{path}")

    def write_config_data(self, data: Dict[str, Any]) -> None:
        """Write the active config.yaml mirror."""
        with CONFIG_PATH.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=False)

    def apply_project(self, project: GeeUpProject, write_config: bool = False) -> None:
        """Apply a loaded project to the GUI and optionally refresh config.yaml."""
        project.config = config_for_project(project.config, project.root)
        self.current_project_name_var.set(project.name)
        self.current_project_root_var.set(str(project.root))
        self.project_created_at = project.created_at
        self.project_download_history = [dict(row) for row in project.download_history]
        self.apply_config_to_ui(project.config)
        self.refresh_tile_presets()
        self.project_status_var.set(f"Project open: {project.name}")
        if write_config:
            data = self.build_config()
            self.write_config_data(data)
        self.load_saved_project_statistics()

    def try_auto_open_project_from_config(self) -> None:
        """Auto-open the project whose root is stored in the active config mirror."""
        root_text = str(self.data.get("processing", {}).get("root", "")).strip()
        if not root_text:
            return
        project_file = Path(root_text) / "project.yaml"
        if not project_file.exists():
            return
        try:
            project = load_project(project_file)
            ensure_project_structure(project.root)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self.project_status_var.set(
                f"config.yaml points at a project folder, but it could not be opened: {exc}"
            )
            return
        self.apply_project(project, write_config=True)
        self.project_status_var.set(
            f"Project auto-opened from config.yaml: {project.name}"
        )

    def require_active_project(self, action: str = "continue") -> bool:
        """Require an explicitly active GUI project before writing or running tools."""
        if self.current_project_root_var.get().strip():
            return True
        message = (
            f"Create or open a GeeUp project before you {action}.\n\n"
            "The GUI may still show paths from config.yaml, including paths from a previous session, "
            "but no project is currently active. Opening a project makes the output folders, "
            "download manifest, reports, and Earth Engine target explicit."
        )
        self.project_status_var.set("No project open. Create or open a project before running workflow steps.")
        messagebox.showwarning("No project open", message)
        return False

    def prepare_project_update(self) -> None:
        """Set the Download date range from project download history."""
        if not self.require_active_project("prepare an update"):
            return
        start, end = prepare_update_dates(self.project_download_history, today=date.today())
        self.download_end_date_var.set(end)
        if start:
            self.download_start_date_var.set(start)
            self.download_status_var.set(
                f"Prepared project update window from {start} through {end}."
            )
            self.project_status_var.set(
                f"Prepared update from the last successful download end date: {start}"
            )
            return
        self.project_status_var.set(
            "No successful project download history yet; set the start date manually."
        )
        messagebox.showinfo(
            "No download history",
            "This project has no successful download history yet. The end date was set to today; choose the start date manually.",
        )

    def apply_config_to_ui(self, data: Dict[str, Any]) -> None:
        """Populate existing Tkinter variables from a config dictionary."""
        self.data = data or {}
        processing_data = self.data.get("processing", {})
        download_data = self.data.get("download", {})
        duplicate_data = self.data.get("duplicates", {})
        extract_data = self.data.get("extract", {})
        metadata_data = self.data.get("metadata", {})
        mosaic_data = self.data.get("mosaic", {})
        gdal_data = self.data.get("gdal", {})
        upload_data = self.data.get("upload", {})
        execution_data = self.data.get("execution", {})
        chrome_data = self.data.get("chrome", {})

        processing_root = processing_data.get("root", DEFAULT_PROCESSING_PATHS["root"])
        raw_downloads = processing_data.get(
            "raw_downloads", DEFAULT_PROCESSING_PATHS["raw_downloads"]
        )
        extracted_geotiffs = processing_data.get(
            "extracted_geotiffs", DEFAULT_PROCESSING_PATHS["extracted_geotiffs"]
        )
        mosaic_outputs = processing_data.get(
            "mosaics", DEFAULT_PROCESSING_PATHS["mosaics"]
        )
        processing_logs = processing_data.get("logs", DEFAULT_PROCESSING_PATHS["logs"])

        self.processing_root_var.set(processing_root)
        self.processing_raw_downloads_var.set(raw_downloads)
        self.processing_extracted_geotiffs_var.set(extracted_geotiffs)
        self.processing_mosaics_var.set(mosaic_outputs)
        self.processing_logs_var.set(processing_logs)

        download_short_name = str(
            download_data.get("collection_short_name", DEFAULT_COLLECTION_SHORT_NAME)
        ).strip()
        download_label = str(
            download_data.get(
                "collection_version_label",
                COLLECTION_LABEL_BY_SHORT_NAME.get(download_short_name, DEFAULT_COLLECTION_LABEL),
            )
        ).strip()
        if download_label not in COLLECTION_LABELS:
            download_label = COLLECTION_LABEL_BY_SHORT_NAME.get(
                download_short_name,
                DEFAULT_COLLECTION_LABEL,
            )
        download_product_filter = str(
            download_data.get("product_version_filter", DEFAULT_PRODUCT_VERSION_FILTER)
        ).strip()
        download_product_filter_label = PRODUCT_VERSION_FILTER_LABEL_BY_VALUE.get(
            download_product_filter,
            download_product_filter
            if download_product_filter in PRODUCT_VERSION_FILTER_LABELS
            else DEFAULT_PRODUCT_VERSION_FILTER_LABEL,
        )
        try:
            selected_tiles = normalize_utm_tiles(download_data.get("utm_tiles", []))
        except ValueError:
            selected_tiles = []
        self.download_collection_var.set(download_label)
        self.download_product_filter_var.set(download_product_filter_label)
        self.download_start_date_var.set(str(download_data.get("start_date", "")))
        self.download_end_date_var.set(str(download_data.get("end_date", "")))
        self.download_output_var.set(download_data.get("output_folder", raw_downloads))
        max_granules = download_data.get("max_granules")
        self.download_max_granules_var.set(
            "" if max_granules in (None, "") else str(max_granules)
        )
        self.download_report_var.set(
            download_data.get("report_csv", f"{processing_logs}/download_preview.csv")
        )
        self.download_manifest_var.set(
            download_data.get("manifest_csv", f"{processing_logs}/download_manifest.csv")
        )
        self.download_threads_var.set(str(download_data.get("threads", 6)))
        self.download_batch_size_var.set(str(download_data.get("batch_size", 25)))
        self.download_skip_existing_var.set(bool(download_data.get("skip_existing", True)))
        self.download_skip_manifest_existing_var.set(
            bool(download_data.get("skip_manifest_existing", True))
        )
        self.download_selected_tiles = set(selected_tiles)
        self.download_selected_tiles_var.set(", ".join(selected_tiles))
        self.refresh_download_tile_list()

        self.duplicate_input_var.set(duplicate_data.get("input_folder", raw_downloads))
        self.duplicate_moved_folder_var.set(
            duplicate_data.get("moved_folder_name", "moved")
        )
        self.duplicate_log_folder_var.set(duplicate_data.get("log_folder", processing_logs))
        self.duplicate_recursive_var.set(bool(duplicate_data.get("recursive", False)))

        self.extract_input_var.set(extract_data.get("input_folder", raw_downloads))
        self.extract_output_var.set(extract_data.get("output_folder", extracted_geotiffs))
        self.extract_crs_mode_var.set(
            EXTRACT_CRS_LABEL_BY_VALUE.get(
                extract_data.get("target_crs_mode", "original"),
                "Original projection",
            )
        )
        self.extract_year_selection_var.set(str(extract_data.get("year_selection", "all")))
        limit_files = extract_data.get("limit_files")
        self.extract_limit_files_var.set("" if limit_files in (None, "") else str(limit_files))
        self.extract_manifest_var.set(
            extract_data.get("manifest_csv", f"{processing_logs}/extract_manifest.csv")
        )
        self.extract_errors_var.set(
            extract_data.get("errors_csv", f"{processing_logs}/extract_errors.csv")
        )
        self.extract_skip_existing_var.set(bool(extract_data.get("skip_existing", True)))
        self.extract_skip_manifest_existing_var.set(
            bool(extract_data.get("skip_manifest_existing", True))
        )

        self.folder_var.set(self.data.get("input_folder", mosaic_outputs))
        self.destination_var.set(self.data.get("destination_parent", ""))
        self.upload_scope_var.set(
            UPLOAD_SCOPE_LABEL_BY_VALUE.get(
                str(upload_data.get("scope", "all")).strip(),
                "All files in origin folder",
            )
        )
        try:
            upload_tiles = normalize_utm_tiles(upload_data.get("utm_tiles", []))
        except ValueError:
            upload_tiles = []
        self.upload_selected_tiles = set(upload_tiles)
        self.upload_selected_tiles_var.set(", ".join(upload_tiles))
        self.refresh_upload_tile_list()
        self.batch_size_var.set(str(upload_data.get("batch_size", 50)))
        self.max_active_var.set(str(upload_data.get("max_active_ingestions", 0)))
        self.prefix_var.set(upload_data.get("prefix", ""))
        self.suffix_var.set(upload_data.get("suffix", ""))
        self.pyramiding_var.set(
            upload_data.get("pyramiding_policy", {}).get("default", "") or ""
        )
        self.profile_dir_var.set(chrome_data.get("user_data_dir", "./chrome-profile"))
        self.retry_attempts_var.set(str(upload_data.get("retry_attempts", 3)))
        self.retry_wait_var.set(str(upload_data.get("retry_wait_seconds", 3.0)))
        self.gdal_python_var.set(gdal_data.get("python", str(DEFAULT_GDAL_PYTHON)))
        self.recursive_var.set(bool(upload_data.get("recursive", False)))
        self.fail_fast_var.set(bool(upload_data.get("fail_fast", False)))
        self.headless_var.set(bool(chrome_data.get("headless", False)))

        self.mosaic_input_var.set(mosaic_data.get("input_folder", extracted_geotiffs))
        self.mosaic_output_var.set(mosaic_data.get("output_folder", mosaic_outputs))
        self.mosaic_grouping_mode_var.set(
            MOSAIC_GROUPING_LABEL_BY_VALUE.get(
                mosaic_data.get("grouping_mode", "utm_zone"),
                "Original projection / split by UTM zone",
            )
        )
        self.mosaic_target_crs_label_var.set(mosaic_data.get("target_crs_label", ""))
        self.mosaic_report_var.set(
            mosaic_data.get("report_csv", f"{processing_logs}/mosaic_report.csv")
        )
        self.mosaic_manifest_var.set(
            mosaic_data.get("manifest_csv", f"{processing_logs}/mosaic_manifest.csv")
        )
        self.mosaic_recursive_var.set(bool(mosaic_data.get("recursive", False)))
        self.mosaic_overwrite_var.set(bool(mosaic_data.get("overwrite", False)))
        self.mosaic_skip_manifest_existing_var.set(
            bool(mosaic_data.get("skip_manifest_existing", True))
        )
        self.mosaic_write_world_file_var.set(bool(mosaic_data.get("write_world_file", True)))

        self.resume_var.set(bool(execution_data.get("resume", True)))
        self.dry_run_var.set(bool(execution_data.get("dry_run", True)))
        self.metadata_enabled_var.set(bool(metadata_data.get("enabled", True)))
        self.metadata_require_match_var.set(bool(metadata_data.get("require_match", True)))
        self.metadata_add_end_time_var.set(bool(metadata_data.get("add_end_time", True)))
        self.load_download_report_preview(limit=100)

    def browse_gdal_python(self) -> None:
        """Let the user choose the Python executable from the GDAL conda env."""
        selected = filedialog.askopenfilename(
            title="Choose GDAL conda Python executable",
            initialdir=str(Path(self.gdal_python_var.get()).parent or PROJECT_ROOT),
            filetypes=[("Python executable", "python.exe"), ("All files", "*.*")],
        )
        if selected:
            self.gdal_python_var.set(selected)

    def browse_duplicate_input_folder(self) -> None:
        """Let the user choose the raw SWOT download folder."""
        selected = filedialog.askdirectory(
            title="Choose the folder containing raw downloaded SWOT files",
            initialdir=self.duplicate_input_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.duplicate_input_var.set(selected)

    def browse_duplicate_log_folder(self) -> None:
        """Let the user choose the duplicate-removal log folder."""
        selected = filedialog.askdirectory(
            title="Choose the duplicate-removal log folder",
            initialdir=self.duplicate_log_folder_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.duplicate_log_folder_var.set(selected)

    def browse_extract_input_folder(self) -> None:
        """Let the user choose the cleaned SWOT NetCDF folder."""
        selected = filedialog.askdirectory(
            title="Choose the folder containing cleaned SWOT NetCDF files",
            initialdir=self.extract_input_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.extract_input_var.set(selected)

    def browse_extract_output_folder(self) -> None:
        """Let the user choose the extraction GeoTIFF output folder."""
        selected = filedialog.askdirectory(
            title="Choose the folder for extracted GeoTIFF outputs",
            initialdir=self.extract_output_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.extract_output_var.set(selected)

    def browse_extract_manifest_file(self) -> None:
        """Let the user choose the extraction manifest CSV path."""
        selected = filedialog.asksaveasfilename(
            title="Choose the extraction manifest CSV path",
            initialfile=Path(self.extract_manifest_var.get() or "extract_manifest.csv").name,
            initialdir=str(Path(self.extract_manifest_var.get() or PROJECT_ROOT).parent),
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if selected:
            self.extract_manifest_var.set(selected)

    def browse_extract_errors_file(self) -> None:
        """Let the user choose the extraction errors CSV path."""
        selected = filedialog.asksaveasfilename(
            title="Choose the extraction errors CSV path",
            initialfile=Path(self.extract_errors_var.get() or "extract_errors.csv").name,
            initialdir=str(Path(self.extract_errors_var.get() or PROJECT_ROOT).parent),
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if selected:
            self.extract_errors_var.set(selected)

    def browse_mosaic_input_folder(self) -> None:
        """Let the user choose the local folder to mosaic."""
        selected = filedialog.askdirectory(
            title="Choose the folder containing SWOT GeoTIFF tiles",
            initialdir=self.mosaic_input_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.mosaic_input_var.set(selected)

    def browse_mosaic_output_folder(self) -> None:
        """Let the user choose the output folder for mosaics."""
        selected = filedialog.askdirectory(
            title="Choose the folder for mosaic GeoTIFF outputs",
            initialdir=self.mosaic_output_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.mosaic_output_var.set(selected)

    def browse_profile_folder(self) -> None:
        """Let the user choose a dedicated Chrome profile folder."""
        selected = filedialog.askdirectory(
            title="Choose the dedicated Chrome profile folder",
            initialdir=self.profile_dir_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.profile_dir_var.set(selected)

    def selected_download_collection_short_name(self) -> str:
        """Return the collection short name for the selected Download label."""
        return COLLECTION_LABELS.get(
            self.download_collection_var.get(),
            DEFAULT_COLLECTION_SHORT_NAME,
        )

    def selected_download_product_filter(self) -> str:
        """Return the config value for the selected product-version filter."""
        return PRODUCT_VERSION_FILTER_LABELS.get(
            self.download_product_filter_var.get(),
            DEFAULT_PRODUCT_VERSION_FILTER,
        )

    def refresh_download_tile_list(self) -> None:
        """Refresh the filtered UTM listbox while preserving selected tiles."""
        if not hasattr(self, "download_tile_listbox"):
            return
        self.download_tile_listbox.delete(0, tk.END)
        needle = self.download_tile_filter_var.get().strip().upper()
        self.download_visible_utm_tiles = [
            tile for tile in self.all_utm_tiles if needle in tile
        ]
        for tile in self.download_visible_utm_tiles:
            self.download_tile_listbox.insert(tk.END, tile)
            if tile in self.download_selected_tiles:
                self.download_tile_listbox.selection_set(tk.END)

    def on_download_tile_select(self, _event: tk.Event | None = None) -> None:
        """Add the current listbox selection to the selected tile state."""
        if not hasattr(self, "download_tile_listbox"):
            return
        selected_indices = set(self.download_tile_listbox.curselection())
        for index, tile in enumerate(self.download_visible_utm_tiles):
            if index in selected_indices:
                self.download_selected_tiles.add(tile)
        self.download_selected_tiles_var.set(
            ", ".join(sorted(self.download_selected_tiles))
        )
        self.refresh_download_tile_list()

    def apply_download_tiles_from_text(self) -> None:
        """Parse the selected-tile text box and apply it to the listbox."""
        try:
            tiles = normalize_utm_tiles(self.download_selected_tiles_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid UTM tiles", str(exc))
            return
        self.download_selected_tiles = set(tiles)
        self.download_selected_tiles_var.set(", ".join(tiles))
        self.refresh_download_tile_list()

    def clear_download_tiles(self) -> None:
        """Clear all selected UTM tiles."""
        self.download_selected_tiles.clear()
        self.download_selected_tiles_var.set("")
        self.refresh_download_tile_list()

    def set_download_tiles(self, tiles: list[str]) -> None:
        """Set the Download tab's canonical selected UTM tiles."""
        normalized = normalize_utm_tiles(tiles)
        self.download_selected_tiles = set(normalized)
        self.download_selected_tiles_var.set(", ".join(normalized))
        self.refresh_download_tile_list()

    def selected_upload_scope(self) -> str:
        """Return the config value for the selected Upload scope."""
        return UPLOAD_SCOPE_LABELS.get(self.upload_scope_var.get(), "all")

    def upload_report_successful_local_files(self) -> set[str]:
        """Return local files already recorded as uploaded or EE-verified."""
        uploaded: set[str] = set()
        report_path = self.current_upload_report_path()
        for row in read_csv_rows(report_path):
            if str(row.get("final_status", "") or "").upper() not in UPLOAD_SUCCESS_STATUSES:
                continue
            local_file = str(row.get("local_file", "") or "").strip()
            if not local_file:
                continue
            uploaded.update(path_lookup_keys(local_file))
        return uploaded

    def upload_report_completed_tiles(self) -> list[str]:
        """Return source UTM tiles with completed or EE-verified upload rows."""
        completed: set[str] = set()
        report_path = self.current_upload_report_path()
        unresolved_rows: list[dict[str, str]] = []
        for row in read_csv_rows(report_path):
            if str(row.get("final_status", "") or "").upper() not in UPLOAD_SUCCESS_STATUSES:
                continue
            tiles = upload_row_source_tiles(row, {})
            if tiles:
                completed.update(tiles)
            else:
                unresolved_rows.append(row)
        if unresolved_rows:
            mosaic_lookup = self.mosaic_source_tiles_by_output()
            for row in unresolved_rows:
                completed.update(upload_row_source_tiles(row, mosaic_lookup))
        return sorted(tile for tile in completed if tile)

    def compact_tile_list(self, tiles: Sequence[str], limit: int = 10) -> str:
        """Return a compact comma-separated tile summary for status labels."""
        if not tiles:
            return "none"
        shown = list(tiles[:limit])
        suffix = "" if len(tiles) <= limit else f", +{len(tiles) - limit} more"
        return f"{', '.join(shown)}{suffix}"

    def update_upload_tile_status(
        self,
        *,
        available_tiles: Sequence[str] | None = None,
        completed_tiles: Sequence[str] | None = None,
        prefix: str = "",
    ) -> None:
        """Update the Upload tile status text from selected, pending, and completed tiles."""
        if not hasattr(self, "upload_tile_status_var"):
            return
        available = (
            sorted(available_tiles)
            if available_tiles is not None
            else self.available_upload_tiles()
        )
        completed = (
            sorted(completed_tiles)
            if completed_tiles is not None
            else self.upload_report_completed_tiles()
        )
        selected = sorted(self.upload_selected_tiles)
        parts: list[str] = []
        if prefix:
            parts.append(prefix)
        if selected:
            parts.append(
                f"Selected filter tiles: {len(selected)} "
                f"({self.compact_tile_list(selected)})."
            )
        else:
            parts.append("No upload filter tiles selected.")

        if completed:
            parts.append(
                f"Completed/EE-verified tiles in upload report: {len(completed)} "
                f"({self.compact_tile_list(completed)})."
            )
        else:
            parts.append("No completed/EE-verified upload tiles are recorded yet.")

        if selected:
            pending_selected = sorted(set(selected) & set(available))
            completed_selected = sorted(set(selected) & set(completed))
            if completed_selected and not pending_selected:
                parts.append(
                    "The selected tile(s) have no pending local upload candidates in the current list; "
                    "run a dry run to confirm they are skipped or already verified."
                )
            elif completed_selected:
                parts.append(
                    "Some selected tile(s) already have completed assets; only pending files/assets should be planned."
                )

        self.upload_tile_status_var.set(" ".join(parts))

    def mosaic_source_tiles_by_output(self) -> dict[str, list[str]]:
        """Return source UTM tiles for mosaic outputs from the mosaic manifest."""
        lookup: dict[str, list[str]] = {}
        manifest_path = Path(self.mosaic_manifest_var.get().strip())
        for row in read_csv_rows(manifest_path):
            output = str(row.get("output_file", "") or "").strip()
            if not output:
                continue
            tiles = mosaic_source_tiles(row)
            if not tiles:
                continue
            for key in path_lookup_keys(output):
                lookup[key] = tiles
        return lookup

    def upload_file_source_tiles(
        self,
        path: Path,
        mosaic_lookup: dict[str, list[str]],
    ) -> list[str]:
        """Return upload UTM/source tiles for one local upload candidate."""
        source_tiles = lookup_path_value(mosaic_lookup, path)
        if source_tiles:
            return source_tiles
        parsed = parse_swot_l2_hr_raster_metadata(path)
        if parsed is None:
            return []
        tile = normalize_utm_tile_token(parsed.fields.get("coordinate_system", ""))
        return [tile] if tile else []

    def available_upload_tiles(self) -> list[str]:
        """Return UTM tiles represented by local upload candidates not already uploaded."""
        folder = Path(self.folder_var.get().strip())
        if not folder.exists() or not folder.is_dir():
            return []
        uploaded_keys = self.upload_report_successful_local_files()
        mosaic_lookup = self.mosaic_source_tiles_by_output()
        patterns = ("*.tif", "*.tiff")
        tiles: set[str] = set()
        for pattern in patterns:
            iterator = folder.rglob(pattern) if self.recursive_var.get() else folder.glob(pattern)
            for path in iterator:
                if not path.is_file():
                    continue
                if any(key in uploaded_keys for key in path_lookup_keys(path)):
                    continue
                tiles.update(self.upload_file_source_tiles(path, mosaic_lookup))
        return sorted(tile for tile in tiles if tile)

    def refresh_upload_tile_list(self) -> None:
        """Refresh the filtered Upload UTM listbox while preserving selection."""
        if not hasattr(self, "upload_tile_listbox"):
            return
        self.upload_tile_listbox.delete(0, tk.END)
        needle = self.upload_tile_filter_var.get().strip().upper()
        available_tiles = self.available_upload_tiles()
        completed_tiles = self.upload_report_completed_tiles()
        if available_tiles:
            base_tiles = sorted(set(available_tiles) | set(self.upload_selected_tiles))
            self.upload_tile_availability_var.set(
                f"Showing {len(available_tiles)} upload-ready tile(s) found in the current origin folder. Files already marked completed or EE-verified in upload_report.csv are excluded."
            )
        else:
            base_tiles = list(self.all_utm_tiles)
            self.upload_tile_availability_var.set(
                "No upload-ready UTM tiles were found in the current origin folder after local report filtering; showing the global list as a fallback."
            )
        self.upload_visible_utm_tiles = [
            tile for tile in base_tiles if needle in tile
        ]
        for tile in self.upload_visible_utm_tiles:
            self.upload_tile_listbox.insert(tk.END, tile)
            if tile in self.upload_selected_tiles:
                self.upload_tile_listbox.selection_set(tk.END)
        self.update_upload_tile_status(
            available_tiles=available_tiles,
            completed_tiles=completed_tiles,
        )

    def on_upload_tile_select(self, _event: tk.Event | None = None) -> None:
        """Add the current Upload listbox selection to the selected tile state."""
        if not hasattr(self, "upload_tile_listbox"):
            return
        selected_indices = set(self.upload_tile_listbox.curselection())
        for index, tile in enumerate(self.upload_visible_utm_tiles):
            if index in selected_indices:
                self.upload_selected_tiles.add(tile)
        self.upload_selected_tiles_var.set(
            ", ".join(sorted(self.upload_selected_tiles))
        )
        self.refresh_upload_tile_list()
        self.update_upload_tile_status(prefix="List selection updated.")

    def apply_upload_tiles_from_text(self) -> None:
        """Parse the Upload selected-tile text box and apply it to the listbox."""
        try:
            tiles = normalize_utm_tiles(self.upload_selected_tiles_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid upload UTM tiles", str(exc))
            return
        self.upload_selected_tiles = set(tiles)
        self.upload_selected_tiles_var.set(", ".join(tiles))
        self.refresh_upload_tile_list()
        self.update_upload_tile_status(prefix="Typed upload tiles validated.")

    def clear_upload_tiles(self) -> None:
        """Clear Upload UTM tile selection."""
        self.upload_selected_tiles.clear()
        self.upload_selected_tiles_var.set("")
        self.refresh_upload_tile_list()
        self.update_upload_tile_status(prefix="Upload tile selection cleared.")

    def current_upload_tiles(self) -> list[str]:
        """Return normalized selected Upload tiles."""
        tiles = normalize_utm_tiles(self.upload_selected_tiles_var.get())
        self.upload_selected_tiles = set(tiles)
        return tiles

    def upload_tiles_for_config(self) -> list[str]:
        """Return upload tiles without blocking unrelated config writes."""
        try:
            return self.current_upload_tiles()
        except ValueError:
            return sorted(self.upload_selected_tiles)

    def refresh_tile_presets(self) -> None:
        """Refresh built-in and project tile preset choices."""
        choices: dict[str, TilePreset] = {}
        for name, preset in load_builtin_tile_presets().items():
            choices[f"Continent: {name}"] = preset
        project_root = self.current_project_root_var.get().strip()
        if project_root:
            for name, preset in load_project_tile_profiles(project_root).items():
                choices[f"Project: {name}"] = preset
        self.tile_preset_choices = choices
        values = sorted(choices)
        if hasattr(self, "tile_preset_combo"):
            self.tile_preset_combo.configure(values=values)
        current = self.tile_preset_var.get()
        if current not in choices:
            self.tile_preset_var.set(values[0] if values else "")

    def apply_tile_preset(self) -> None:
        """Apply a selected tile preset to the manual UTM selector."""
        self.refresh_tile_presets()
        label = self.tile_preset_var.get()
        preset = self.tile_preset_choices.get(label)
        if preset is None:
            messagebox.showerror(
                "No tile preset selected",
                "Please choose a continent or project tile preset first.",
            )
            return
        self.set_download_tiles(preset.tiles)
        self.download_status_var.set(
            f"Applied tile preset '{preset.name}' with {len(preset.tiles)} tile(s)."
        )

    def open_utm_map_selector(self) -> None:
        """Open the visual UTM tile selector dialog."""
        try:
            selected_tiles = self.current_download_tiles()
        except ValueError as exc:
            messagebox.showerror("Invalid UTM tiles", str(exc))
            return
        try:
            geometry = load_display_geometry()
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror(
                "UTM map geometry unavailable",
                (
                    "Could not load the visual UTM selector geometry. "
                    "Regenerate it with build_spatial_presets.py.\n\n"
                    f"{exc}"
                ),
            )
            return
        self.refresh_tile_presets()
        coverage_tiles = self.current_manifest_coverage_tiles()
        UTMMapSelectorDialog(
            self.root,
            geometry,
            selected_tiles,
            self.apply_utm_map_selection,
            preset_choices=self.tile_preset_choices,
            coverage_tiles=coverage_tiles,
        )

    def current_manifest_coverage_tiles(self) -> list[str]:
        """Return UTM tiles with at least one downloaded granule in the manifest."""
        manifest_path = Path(self.download_manifest_var.get().strip() or "")
        try:
            return manifest_downloaded_tiles(manifest_path)
        except OSError:
            return []

    def apply_utm_map_selection(self, tiles: list[str]) -> None:
        """Apply visual selector output to the Download tab."""
        self.set_download_tiles(tiles)
        self.download_status_var.set(
            f"Applied {len(tiles)} UTM tile(s) from the map selector."
        )

    def save_selected_tiles_as_preset(self) -> None:
        """Save the current manual UTM selection as a project preset."""
        project_root = self.current_project_root_var.get().strip()
        if not project_root:
            messagebox.showerror(
                "No project open",
                "Create or open a project before saving tile presets.",
            )
            return
        try:
            tiles = self.current_download_tiles()
        except ValueError as exc:
            messagebox.showerror("Invalid UTM tiles", str(exc))
            return
        if not tiles:
            messagebox.showerror(
                "No UTM tiles selected",
                "Select one or more UTM tiles before saving a preset.",
            )
            return
        name = simpledialog.askstring(
            "Save tile preset",
            "Preset name:",
            initialvalue="New tile preset",
            parent=self.root,
        )
        if name is None:
            return
        try:
            path = save_tile_profile(project_root, name, tiles)
        except (OSError, ValueError) as exc:
            messagebox.showerror(
                "Could not save tile preset",
                f"Failed to save the tile preset:\n{exc}",
            )
            return
        self.refresh_tile_presets()
        label = f"Project: {name.strip() or path.stem}"
        if label in self.tile_preset_choices:
            self.tile_preset_var.set(label)
        self.project_status_var.set(f"Saved tile preset to {path}")
        messagebox.showinfo("Tile preset saved", f"Saved tile preset:\n{path}")

    def current_download_tiles(self) -> list[str]:
        """Return the selected tiles from text/listbox state."""
        tiles = normalize_utm_tiles(self.download_selected_tiles_var.get())
        self.download_selected_tiles = set(tiles)
        return tiles

    def download_tiles_for_config(self) -> list[str]:
        """Return download tiles without letting another tab's save fail."""
        try:
            return self.current_download_tiles()
        except ValueError:
            return sorted(self.download_selected_tiles)

    def download_config_from_ui(self) -> DownloadConfig:
        """Build a DownloadConfig from the Download tab."""
        max_granules = self.parse_optional_int_or_none(
            self.download_max_granules_var.get()
        )
        threads = self.parse_int_or_default(self.download_threads_var.get(), 6)
        batch_size = self.parse_int_or_default(self.download_batch_size_var.get(), 25)
        return DownloadConfig(
            collection_short_name=self.selected_download_collection_short_name(),
            collection_version_label=self.download_collection_var.get(),
            output_folder=Path(self.download_output_var.get().strip()),
            start_date=self.download_start_date_var.get().strip(),
            end_date=self.download_end_date_var.get().strip(),
            utm_tiles=self.current_download_tiles(),
            max_granules=max_granules,
            report_csv=Path(self.download_report_var.get().strip()),
            manifest_csv=Path(self.download_manifest_var.get().strip()),
            skip_existing=self.download_skip_existing_var.get(),
            skip_manifest_existing=self.download_skip_manifest_existing_var.get(),
            threads=threads,
            batch_size=batch_size,
            product_version_filter=self.selected_download_product_filter(),
            base_dir=PROJECT_ROOT,
        )

    def validate_download(self) -> bool:
        """Check download form values before previewing or downloading."""
        if not self.download_output_var.get().strip():
            messagebox.showerror(
                "Missing download output folder",
                "Please choose where downloaded SWOT NetCDF files should be written.",
            )
            return False
        if not self.download_report_var.get().strip():
            messagebox.showerror(
                "Missing download report CSV",
                "Please choose a CSV path for the download preview/report.",
            )
            return False
        if not self.download_manifest_var.get().strip():
            messagebox.showerror(
                "Missing download manifest CSV",
                "Please choose a CSV path for the cumulative download manifest.",
            )
            return False
        try:
            config = self.download_config_from_ui()
            from swot_download_tool import validate_config

            validate_config(config)
        except ValueError as exc:
            messagebox.showerror("Invalid download settings", str(exc))
            return False

        try:
            config.output_folder.mkdir(parents=True, exist_ok=True)
            config.report_csv.parent.mkdir(parents=True, exist_ok=True)
            config.manifest_csv.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Could not create download folders",
                f"Failed to create download output/report/manifest folders:\n{exc}",
            )
            return False
        return True

    def validate_duplicate_removal(self) -> bool:
        """Check duplicate-removal form values before planning or running."""
        input_folder = self.duplicate_input_var.get().strip()
        moved_folder_name = self.duplicate_moved_folder_var.get().strip()
        log_folder = self.duplicate_log_folder_var.get().strip()
        if not input_folder:
            messagebox.showerror(
                "Missing duplicate input folder",
                "Please choose the folder containing raw downloaded SWOT files.",
            )
            return False
        if not moved_folder_name:
            messagebox.showerror(
                "Missing moved folder name",
                "Please enter the moved subfolder name.",
            )
            return False
        moved_path = Path(moved_folder_name)
        if moved_path.name != moved_folder_name or moved_folder_name in {".", ".."}:
            messagebox.showerror(
                "Invalid moved folder name",
                "Moved folder name must be a folder name such as moved, not a path.",
            )
            return False
        if not log_folder:
            messagebox.showerror(
                "Missing log folder",
                "Please choose where duplicate-removal logs should be written.",
            )
            return False

        try:
            Path(input_folder).mkdir(parents=True, exist_ok=True)
            Path(log_folder).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Could not create folders",
                f"Failed to create duplicate-removal folders:\n{exc}",
            )
            return False

        if not Path(input_folder).is_dir():
            messagebox.showerror(
                "Invalid duplicate input",
                "The selected duplicate input path is not a folder.",
            )
            return False
        if not Path(log_folder).is_dir():
            messagebox.showerror(
                "Invalid duplicate log folder",
                "The selected duplicate log path is not a folder.",
            )
            return False
        return True

    def validate_extraction(self) -> bool:
        """Check extraction form values before planning or running."""
        gdal_python = self.gdal_python_var.get().strip()
        input_folder = self.extract_input_var.get().strip()
        output_folder = self.extract_output_var.get().strip()
        crs_label = self.extract_crs_mode_var.get().strip()
        year_selection = self.extract_year_selection_var.get().strip()
        limit_files = self.extract_limit_files_var.get().strip()
        manifest_csv = self.extract_manifest_var.get().strip()
        errors_csv = self.extract_errors_var.get().strip()

        if not gdal_python:
            messagebox.showerror(
                "Missing GDAL Python",
                "Please choose the Python executable from the GDAL conda environment.",
            )
            return False
        if not Path(gdal_python).exists():
            messagebox.showerror(
                "GDAL Python not found",
                "The configured GDAL Python executable does not exist.",
            )
            return False
        if not input_folder:
            messagebox.showerror(
                "Missing extraction input folder",
                "Please choose the folder containing cleaned SWOT NetCDF files.",
            )
            return False
        if not Path(input_folder).exists() or not Path(input_folder).is_dir():
            messagebox.showerror(
                "Extraction input folder not found",
                "The selected extraction input folder does not exist or is not a folder.",
            )
            return False
        if not output_folder:
            messagebox.showerror(
                "Missing extraction output folder",
                "Please choose where extracted GeoTIFF outputs should be written.",
            )
            return False
        if Path(input_folder).resolve() == Path(output_folder).resolve():
            messagebox.showerror(
                "Invalid extraction output folder",
                "Extraction output folder must be different from the input folder.",
            )
            return False
        if crs_label not in EXTRACT_CRS_LABELS:
            messagebox.showerror(
                "Invalid CRS mode",
                "Please choose a valid Extraction CRS mode.",
            )
            return False
        try:
            self.parse_year_selection_for_ui(year_selection)
        except ValueError as exc:
            messagebox.showerror("Invalid year selection", str(exc))
            return False
        if limit_files:
            try:
                parsed_limit = int(limit_files)
            except ValueError:
                messagebox.showerror("Invalid limit files", "Limit files must be an integer or blank.")
                return False
            if parsed_limit < 0:
                messagebox.showerror("Invalid limit files", "Limit files cannot be negative.")
                return False
        if not manifest_csv or not errors_csv:
            messagebox.showerror(
                "Missing extraction CSV path",
                "Please choose both the manifest CSV and errors CSV paths.",
            )
            return False
        try:
            Path(output_folder).mkdir(parents=True, exist_ok=True)
            Path(manifest_csv).parent.mkdir(parents=True, exist_ok=True)
            Path(errors_csv).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Could not create extraction folders",
                f"Failed to create extraction output/report folders:\n{exc}",
            )
            return False
        return True

    @staticmethod
    def parse_year_selection_for_ui(value: str) -> None:
        """Validate the Extraction year-selection text."""
        stripped = value.strip()
        if not stripped or stripped.lower() == "all":
            return
        for part in stripped.split(","):
            if not part.strip():
                continue
            int(part.strip())

    def validate_upload(self) -> bool:
        """Check upload form values before saving or running."""
        folder = self.folder_var.get().strip()
        destination = self.destination_var.get().strip()
        if not folder:
            messagebox.showerror("Missing folder", "Please choose the origin folder.")
            return False
        if not Path(folder).exists():
            messagebox.showerror(
                "Folder not found",
                "The selected origin folder does not exist.",
            )
            return False
        if not destination:
            messagebox.showerror(
                "Missing destination",
                "Please enter the Earth Engine destination collection.",
            )
            return False
        if self.upload_scope_var.get() not in UPLOAD_SCOPE_LABELS:
            messagebox.showerror(
                "Invalid upload scope",
                "Please choose a valid Upload scope.",
            )
            return False
        try:
            upload_tiles = self.current_upload_tiles()
        except ValueError as exc:
            messagebox.showerror("Invalid upload UTM tiles", str(exc))
            return False
        if self.selected_upload_scope() == "selected_utm" and not upload_tiles:
            messagebox.showerror(
                "Missing upload UTM tiles",
                "Choose at least one UTM tile or switch Upload scope back to all files.",
            )
            return False

        try:
            batch_size = int(self.batch_size_var.get())
            max_active = int(self.max_active_var.get())
            retry_attempts = int(self.retry_attempts_var.get())
            retry_wait = float(self.retry_wait_var.get())
        except ValueError:
            messagebox.showerror(
                "Invalid numbers",
                "Batch size, max active ingestions, retry attempts, and retry wait must be numeric.",
            )
            return False

        if batch_size < 1:
            messagebox.showerror("Invalid batch size", "Batch size must be at least 1.")
            return False
        if max_active < 0:
            messagebox.showerror(
                "Invalid queue setting",
                "Max active ingestions cannot be negative.",
            )
            return False
        if retry_attempts < 1:
            messagebox.showerror(
                "Invalid retry attempts",
                "Retry attempts must be at least 1.",
            )
            return False
        if retry_wait < 0:
            messagebox.showerror(
                "Invalid retry wait",
                "Retry wait seconds cannot be negative.",
            )
            return False
        return True

    def validate_mosaic(self) -> bool:
        """Check mosaic form values before planning or running."""
        gdal_python = self.gdal_python_var.get().strip()
        input_folder = self.mosaic_input_var.get().strip()
        output_folder = self.mosaic_output_var.get().strip()
        grouping_label = self.mosaic_grouping_mode_var.get().strip()
        target_crs_label = self.mosaic_target_crs_label_var.get().strip()
        if not gdal_python:
            messagebox.showerror(
                "Missing GDAL Python",
                "Please choose the Python executable from the GDAL conda environment.",
            )
            return False
        if not Path(gdal_python).exists():
            messagebox.showerror(
                "GDAL Python not found",
                "The configured GDAL Python executable does not exist.",
            )
            return False
        if not input_folder:
            messagebox.showerror(
                "Missing mosaic input folder",
                "Please choose the folder containing SWOT GeoTIFF tiles.",
            )
            return False
        if not Path(input_folder).exists():
            messagebox.showerror(
                "Mosaic input folder not found",
                "The selected mosaic input folder does not exist.",
            )
            return False
        if not Path(input_folder).is_dir():
            messagebox.showerror(
                "Invalid mosaic input",
                "The selected mosaic input path is not a folder.",
            )
            return False
        if not output_folder:
            messagebox.showerror(
                "Missing mosaic output folder",
                "Please choose where mosaic GeoTIFF outputs should be written.",
            )
            return False
        if Path(input_folder).resolve() == Path(output_folder).resolve():
            messagebox.showerror(
                "Invalid mosaic output folder",
                "Mosaic output folder must be different from the input folder.",
            )
            return False
        if grouping_label not in MOSAIC_GROUPING_LABELS:
            messagebox.showerror(
                "Invalid grouping mode",
                "Please choose a valid Mosaic grouping mode.",
            )
            return False
        if target_crs_label and not target_crs_label.replace("_", "").isalnum():
            messagebox.showerror(
                "Invalid target CRS label",
                "Target CRS label may contain only letters, numbers, and underscores.",
            )
            return False
        try:
            Path(output_folder).mkdir(parents=True, exist_ok=True)
            Path(self.mosaic_report_var.get().strip()).parent.mkdir(parents=True, exist_ok=True)
            Path(self.mosaic_manifest_var.get().strip()).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Could not create mosaic folders",
                f"Failed to create mosaic output/report/manifest folders:\n{exc}",
            )
            return False

        return True

    def build_config(self, dry_run_override: bool | None = None) -> Dict[str, Any]:
        """Build a config dictionary from the UI state."""
        effective_dry_run = (
            self.dry_run_var.get() if dry_run_override is None else dry_run_override
        )
        batch_size = self.parse_int_or_default(self.batch_size_var.get(), 50)
        max_active = self.parse_int_or_default(self.max_active_var.get(), 0)
        retry_attempts = self.parse_int_or_default(self.retry_attempts_var.get(), 3)
        retry_wait = self.parse_float_or_default(self.retry_wait_var.get(), 3.0)
        mosaic_report_csv = (
            self.mosaic_report_var.get().strip() or "./reports/mosaic_report.csv"
        )
        mixed_crid_report_csv = str(
            Path(mosaic_report_csv).with_name("mixed_crid_mosaics.csv")
        )
        return {
            "earth_engine_url": "https://code.earthengine.google.com/",
            "input_folder": self.folder_var.get().strip(),
            "destination_parent": self.destination_var.get().strip(),
            "processing": {
                "root": self.processing_root_var.get().strip() or DEFAULT_PROCESSING_PATHS["root"],
                "raw_downloads": self.download_output_var.get().strip()
                or self.duplicate_input_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["raw_downloads"],
                "extracted_geotiffs": self.extract_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["extracted_geotiffs"],
                "mosaics": self.mosaic_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["mosaics"],
                "logs": self.duplicate_log_folder_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["logs"],
            },
            "download": {
                "collection_short_name": self.selected_download_collection_short_name(),
                "collection_version_label": self.download_collection_var.get(),
                "product_version_filter": self.selected_download_product_filter(),
                "output_folder": self.download_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["raw_downloads"],
                "start_date": self.download_start_date_var.get().strip(),
                "end_date": self.download_end_date_var.get().strip(),
                "utm_tiles": self.download_tiles_for_config(),
                "max_granules": self.parse_optional_int_or_none(
                    self.download_max_granules_var.get()
                ),
                "report_csv": self.download_report_var.get().strip()
                or f"{DEFAULT_PROCESSING_PATHS['logs']}/download_preview.csv",
                "manifest_csv": self.download_manifest_var.get().strip()
                or f"{DEFAULT_PROCESSING_PATHS['logs']}/download_manifest.csv",
                "skip_existing": self.download_skip_existing_var.get(),
                "skip_manifest_existing": self.download_skip_manifest_existing_var.get(),
                "threads": self.parse_int_or_default(
                    self.download_threads_var.get(),
                    6,
                ),
                "batch_size": self.parse_int_or_default(
                    self.download_batch_size_var.get(),
                    25,
                ),
            },
            "duplicates": {
                "input_folder": self.duplicate_input_var.get().strip()
                or self.download_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["raw_downloads"],
                "moved_folder_name": self.duplicate_moved_folder_var.get().strip()
                or "moved",
                "log_folder": self.duplicate_log_folder_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["logs"],
                "recursive": self.duplicate_recursive_var.get(),
            },
            "extract": {
                "input_folder": self.extract_input_var.get().strip()
                or self.download_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["raw_downloads"],
                "output_folder": self.extract_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["extracted_geotiffs"],
                "target_crs_mode": self.selected_extract_crs_mode(),
                "year_selection": self.extract_year_selection_var.get().strip() or "all",
                "limit_files": self.parse_optional_int_or_none(
                    self.extract_limit_files_var.get()
                ),
                "skip_existing": self.extract_skip_existing_var.get(),
                "skip_manifest_existing": self.extract_skip_manifest_existing_var.get(),
                "resampling_alg": "near",
                "manifest_csv": self.extract_manifest_var.get().strip()
                or f"{DEFAULT_PROCESSING_PATHS['logs']}/extract_manifest.csv",
                "errors_csv": self.extract_errors_var.get().strip()
                or f"{DEFAULT_PROCESSING_PATHS['logs']}/extract_errors.csv",
            },
            "gdal": {
                "python": self.gdal_python_var.get().strip() or str(DEFAULT_GDAL_PYTHON),
            },
            "mosaic": {
                "input_folder": self.mosaic_input_var.get().strip(),
                "output_folder": self.mosaic_output_var.get().strip() or "./mosaics",
                "grouping_mode": self.selected_mosaic_grouping_mode(),
                "target_crs_label": self.mosaic_target_crs_label_var.get().strip().upper(),
                "recursive": self.mosaic_recursive_var.get(),
                "overwrite": self.mosaic_overwrite_var.get(),
                "write_world_file": self.mosaic_write_world_file_var.get(),
                "extensions": [".tif", ".tiff"],
                "report_csv": mosaic_report_csv,
                "manifest_csv": self.mosaic_manifest_var.get().strip()
                or str(Path(mosaic_report_csv).with_name("mosaic_manifest.csv")),
                "skip_manifest_existing": self.mosaic_skip_manifest_existing_var.get(),
                "mixed_crid_report_csv": mixed_crid_report_csv,
            },
            "chrome": {
                "user_data_dir": self.profile_dir_var.get().strip() or "./chrome-profile",
                "profile_directory": None,
                "binary_location": None,
                "connection_mode": "attach",
                "remote_debugging_port": 9222,
                "headless": self.headless_var.get(),
                "start_maximized": True,
            },
            "upload": {
                "batch_size": batch_size,
                "max_active_ingestions": max_active,
                "scope": self.selected_upload_scope(),
                "utm_tiles": self.upload_tiles_for_config(),
                "ee_sync_before_upload": True,
                "ee_asset_inventory_page_size": 1000,
                "prefix": self.prefix_var.get(),
                "suffix": self.suffix_var.get(),
                "replacement_rules": {" ": "_"},
                "invalid_char_pattern": "[^A-Za-z0-9._-]+",
                "invalid_char_replacement": "_",
                "recursive": self.recursive_var.get(),
                "extensions": [".tif", ".tiff"],
                "pyramiding_policy": {
                    "default": self.pyramiding_var.get().strip() or None,
                    "per_band": {},
                },
                "retry_attempts": retry_attempts,
                "retry_wait_seconds": retry_wait,
                "fail_fast": self.fail_fast_var.get(),
            },
            "execution": {
                "dry_run": effective_dry_run,
                "resume": self.resume_var.get(),
                "require_confirmation": True,
                "task_poll_seconds": 20,
                "short_ui_wait_seconds": 1.5,
                "wait_timeout_minutes": 720,
                "page_load_timeout_seconds": 90,
                "verbose_console": True,
            },
            "metadata": {
                "enabled": self.metadata_enabled_var.get(),
                "parser": "swot_l2_hr_raster",
                "require_match": self.metadata_require_match_var.get(),
                "add_start_time": True,
                "add_end_time": self.metadata_add_end_time_var.get(),
                "extra_properties": {
                    "swot_descriptor": "descriptor",
                    "swot_grid_resolution": "grid_resolution",
                    "swot_coordinate_system": "coordinate_system",
                    "swot_granule_overlap": "granule_overlap",
                    "swot_cycle_id": "cycle_id",
                    "swot_pass_id": "pass_id",
                    "swot_scene_id": "scene_id",
                    "swot_crid": "crid",
                    "swot_product_counter": "product_counter",
                },
            },
            "artifacts": {
                "logs_dir": self.processing_logs_var.get().strip()
                or f"{DEFAULT_PROCESSING_PATHS['logs']}",
                "artifacts_dir": str(
                    Path(
                        self.processing_logs_var.get().strip()
                        or f"{DEFAULT_PROCESSING_PATHS['logs']}"
                    )
                    / "upload_artifacts"
                ),
                "report_csv": str(
                    Path(
                        self.processing_logs_var.get().strip()
                        or f"{DEFAULT_PROCESSING_PATHS['logs']}"
                    )
                    / "upload_report.csv"
                ),
                "ee_asset_inventory_csv": str(
                    Path(
                        self.processing_logs_var.get().strip()
                        or f"{DEFAULT_PROCESSING_PATHS['logs']}"
                    )
                    / "ee_asset_inventory.csv"
                ),
            },
        }

    def save_config(
        self,
        notify: bool = True,
        dry_run_override: bool | None = None,
        validate_download: bool = False,
        validate_upload: bool = True,
        validate_mosaic: bool = False,
        validate_duplicates: bool = False,
        validate_extract: bool = False,
        require_project: bool = True,
    ) -> bool:
        """Save config.yaml from the current form values."""
        if require_project and not self.require_active_project("save or run this workflow step"):
            return False
        if validate_download and not self.validate_download():
            return False
        if validate_duplicates and not self.validate_duplicate_removal():
            return False
        if validate_extract and not self.validate_extraction():
            return False
        if validate_upload and not self.validate_upload():
            return False
        if validate_mosaic and not self.validate_mosaic():
            return False
        data = self.build_config(dry_run_override=dry_run_override)
        self.write_config_data(data)
        project_root = self.current_project_root_var.get().strip()
        if project_root:
            try:
                save_project_config(
                    project_root,
                    self.current_project_name_var.get(),
                    data,
                    self.project_download_history,
                    created_at=self.project_created_at,
                )
            except OSError as exc:
                messagebox.showerror(
                    "Could not save project",
                    f"Saved the CLI config mirror, but failed to save the active project:\n{exc}",
                )
                return False
        message = "Saved active project settings."
        self.download_status_var.set(message)
        self.status_var.set(message)
        self.mosaic_status_var.set(message)
        self.duplicate_status_var.set(message)
        self.extract_status_var.set(message)
        if notify:
            messagebox.showinfo("Project settings saved", message)
        return True

    def save_download_config(self) -> bool:
        """Save config after validating the download tab."""
        return self.save_config(
            notify=True,
            validate_download=True,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=False,
            validate_extract=False,
        )

    def save_upload_config(self) -> bool:
        """Save config after validating the upload tab."""
        return self.save_config(
            notify=True,
            validate_download=False,
            validate_upload=True,
            validate_mosaic=False,
            validate_duplicates=False,
        )

    def save_mosaic_config(self) -> bool:
        """Save config after validating the mosaic tab."""
        return self.save_config(
            notify=True,
            validate_download=False,
            validate_upload=False,
            validate_mosaic=True,
            validate_duplicates=False,
            validate_extract=False,
        )

    def save_duplicate_config(self) -> bool:
        """Save config after validating the duplicate-removal tab."""
        return self.save_config(
            notify=True,
            validate_download=False,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=True,
            validate_extract=False,
        )

    def save_extract_config(self) -> bool:
        """Save config after validating the extraction tab."""
        return self.save_config(
            notify=True,
            validate_download=False,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=False,
            validate_extract=True,
        )

    def authenticate_download(self) -> None:
        """Authenticate Earthdata access in a background thread."""
        self.download_auth_status_var.set(
            "Earthdata authentication: checking. Watch the console if credentials are requested."
        )
        thread = threading.Thread(
            target=self.run_download_authentication,
            daemon=True,
        )
        thread.start()

    def run_download_authentication(self) -> None:
        """Run earthaccess.login off the Tkinter UI thread."""
        try:
            authenticate_earthdata(strategy="all", persist=False)
        except Exception as exc:
            self.root.after(0, self.finish_download_authentication_error, exc)
            return
        self.root.after(0, self.finish_download_authentication)

    def finish_download_authentication(self) -> None:
        """Show successful Earthdata authentication."""
        self.download_auth_status_var.set("Earthdata authentication: succeeded")
        self.download_status_var.set("Earthdata authentication succeeded for this session.")

    def finish_download_authentication_error(self, exc: Exception) -> None:
        """Show failed Earthdata authentication."""
        self.download_auth_status_var.set(f"Earthdata authentication: failed ({exc})")
        self.download_status_var.set("Earthdata authentication failed. Check credentials or _netrc.")
        messagebox.showerror(
            "Earthdata authentication failed",
            f"earthaccess could not authenticate with Earthdata Login:\n{exc}",
        )

    def preview_download(self) -> None:
        """Save config and search matching SWOT granules without downloading."""
        if not self.save_config(
            notify=False,
            validate_download=True,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=False,
            validate_extract=False,
        ):
            return
        config = self.download_config_from_ui()
        self.download_status_var.set("Started SWOT download preview. Searching CMR...")
        self.download_progress_var.set(0.0)
        self.download_progress_text_var.set("Progress: searching CMR")
        thread = threading.Thread(
            target=self.run_download_preview_process,
            args=(config,),
            daemon=True,
        )
        thread.start()

    def run_download_preview_process(self, config: DownloadConfig) -> None:
        """Run the Earthdata search off the Tkinter UI thread."""
        try:
            preview = build_download_preview(
                config,
                progress_callback=lambda current, total, message: self.root.after(
                    0,
                    self.update_download_progress,
                    current,
                    total,
                    message,
                ),
            )
            report_csv = write_download_report(config, preview)
        except Exception as exc:
            self.root.after(0, self.finish_download_process_error, exc)
            return
        self.root.after(0, self.finish_download_preview_process, preview, report_csv)

    def finish_download_preview_process(self, preview: Any, report_csv: Path) -> None:
        """Update the UI after a download preview finishes."""
        self.load_download_report_preview(limit=300)
        self.download_progress_var.set(100.0)
        self.download_progress_text_var.set("Progress: preview complete")
        selected_count = len(preview.selected_granules)
        excluded_count = len(preview.excluded_granules)
        selected_size_text = format_size(
            preview.selected_known_size_mb,
            preview.selected_missing_size_count,
        )
        excluded_size_text = format_size(
            preview.excluded_known_size_mb,
            preview.excluded_missing_size_count,
        )
        self.download_status_var.set(
            f"Preview found {len(preview.granules)} granules; {selected_count} selected for download and {excluded_count} older version(s) excluded. Selected size: {selected_size_text}. Report: {report_csv}"
        )
        messagebox.showinfo(
            "Download preview finished",
            (
                f"Matched granules: {len(preview.granules)}\n"
                f"Selected for download: {selected_count}\n"
                f"Excluded older versions: {excluded_count}\n"
                f"Estimated selected size: {selected_size_text}\n"
                f"Estimated excluded size: {excluded_size_text}\n"
                f"Report CSV:\n{report_csv}"
            ),
        )

    def start_download(self) -> None:
        """Save config and download matching SWOT granules."""
        if not self.save_config(
            notify=False,
            validate_download=True,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=False,
            validate_extract=False,
        ):
            return
        config = self.download_config_from_ui()
        self.download_status_var.set("Started SWOT download. Searching CMR...")
        self.download_progress_var.set(0.0)
        self.download_progress_text_var.set("Progress: starting")
        self.download_stop_event = threading.Event()
        thread = threading.Thread(
            target=self.run_download_process,
            args=(config, self.download_stop_event),
            daemon=True,
        )
        thread.start()

    def stop_download(self) -> None:
        """Request the background download loop to stop after the current file."""
        if self.download_stop_event is None:
            self.download_status_var.set("No active download to stop.")
            return
        self.download_stop_event.set()
        self.download_status_var.set(
            "Stop requested. The current file will finish or fail, then the download loop will stop."
        )
        self.download_progress_text_var.set("Progress: stop requested")

    def run_download_process(self, config: DownloadConfig, stop_event: threading.Event) -> None:
        """Run search and downloads off the Tkinter UI thread."""
        try:
            result = run_download(
                config,
                progress_callback=lambda current, total, message: self.root.after(
                    0,
                    self.update_download_progress,
                    current,
                    total,
                    message,
                ),
                stop_event=stop_event,
            )
        except Exception as exc:
            self.root.after(0, self.finish_download_process_error, exc)
            return
        self.root.after(0, self.finish_download_process, result)

    def finish_download_process_error(self, exc: Exception) -> None:
        """Show a failed download preview or run."""
        self.download_stop_event = None
        self.download_status_var.set(f"Download module failed: {exc}")
        messagebox.showerror(
            "Download module failed",
            f"The SWOT download module failed:\n{exc}",
        )

    def finish_download_process(self, result: Any) -> None:
        """Update the UI after a download run exits."""
        self.download_stop_event = None
        self.load_download_report_preview(limit=300)
        self.download_progress_var.set(100.0)
        matched_count = len(result.preview.granules)
        selected_count = len(result.preview.selected_granules)
        excluded_count = len(result.preview.excluded_granules)
        missing_count = len(result.missing_granules)
        complete_count = result.complete_count
        if result.all_complete and not result.failures and not result.stopped:
            self.apply_download_handoff_to_processing()
            self.record_project_download_history(result)
            self.refresh_project_statistics_if_active("download")
            self.download_progress_text_var.set("Progress: download complete")
            self.download_status_var.set(
                f"Download finished and verified. All {selected_count} selected granule(s) are accounted for; {excluded_count} older version(s) were recorded but not downloaded."
            )
            messagebox.showinfo(
                "Download finished",
                (
                    f"Matched granules: {matched_count}\n"
                    f"Selected for download: {selected_count}\n"
                    f"Excluded older versions: {excluded_count}\n"
                    f"Accounted for: {complete_count}\n"
                    f"Downloaded files: {len(result.downloaded_files)}\n"
                    f"Skipped existing: {len(result.skipped_existing)}\n"
                    f"Skipped manifest-known: {len(result.skipped_manifest)}\n"
                    "Missing files: 0\n"
                    f"Report CSV:\n{result.report_csv}\n\n"
                    "All selected granules are accounted for. Excluded older versions remain listed in the report/manifest for audit. The output folder is now set as the input for Duplicate Removal and Extraction."
                ),
            )
            return

        self.record_project_download_history(result)
        self.refresh_project_statistics_if_active("download")
        if result.stopped:
            self.download_progress_text_var.set("Progress: download stopped")
            title = "Download stopped"
            summary = (
                f"Download stopped. Accounted for: {complete_count}/{matched_count} matched; "
                f"{selected_count} selected, {excluded_count} excluded. "
                f"Missing files: {missing_count}. Restart with skip-existing enabled to continue."
            )
        else:
            self.download_progress_text_var.set("Progress: download finished with missing files")
            title = "Download verification found missing files"
            summary = (
                f"Download finished but not all matched files are present. "
                f"Accounted for: {complete_count}/{matched_count}. "
                f"{selected_count} selected, {excluded_count} excluded. Missing files: {missing_count}."
            )
        self.download_status_var.set(
            f"{summary} Review the report CSV."
        )
        messagebox.showwarning(
            title,
            (
                f"Matched granules: {matched_count}\n"
                f"Selected for download: {selected_count}\n"
                f"Excluded older versions: {excluded_count}\n"
                f"Accounted for: {complete_count}\n"
                f"Missing files: {missing_count}\n"
                f"Downloaded files: {len(result.downloaded_files)}\n"
                f"Skipped existing: {len(result.skipped_existing)}\n"
                f"Skipped manifest-known: {len(result.skipped_manifest)}\n"
                f"Failed granules: {len(result.failures)}\n"
                f"Stopped by user: {'yes' if result.stopped else 'no'}\n"
                f"Report CSV:\n{result.report_csv}"
            ),
        )

    def record_project_download_history(self, result: Any) -> None:
        """Append a project download-history entry after a download run."""
        project_root = self.current_project_root_var.get().strip()
        if not project_root:
            return
        entry = {
            "timestamp": datetime.now().replace(microsecond=0).isoformat(),
            "status": (
                "success"
                if result.all_complete and not result.failures and not result.stopped
                else "stopped" if result.stopped else "failed"
            ),
            "collection_short_name": self.selected_download_collection_short_name(),
            "collection_version_label": self.download_collection_var.get(),
            "product_version_filter": self.selected_download_product_filter(),
            "utm_tiles": self.download_tiles_for_config(),
            "start_date": self.download_start_date_var.get().strip(),
            "end_date": self.download_end_date_var.get().strip(),
            "matched_count": len(result.preview.granules),
            "selected_count": len(result.preview.selected_granules),
            "excluded_older_version_count": len(result.preview.excluded_granules),
            "downloaded_count": len(result.downloaded_files),
            "skipped_count": len(result.skipped_existing),
            "skipped_manifest_count": len(result.skipped_manifest),
            "failed_count": len(result.failures),
            "missing_count": len(result.missing_granules),
            "complete_count": result.complete_count,
            "stopped": result.stopped,
            "report_csv": str(result.report_csv or ""),
            "manifest_csv": self.download_manifest_var.get().strip(),
        }
        self.project_download_history.append(entry)
        data = self.build_config()
        self.write_config_data(data)
        try:
            save_project_config(
                project_root,
                self.current_project_name_var.get(),
                data,
                self.project_download_history,
                created_at=self.project_created_at,
            )
        except OSError as exc:
            self.project_status_var.set(f"Could not update project download history: {exc}")
            return
        self.project_status_var.set("Recorded download history in the active project.")

    def apply_download_handoff_to_processing(self) -> None:
        """Point the next processing steps at the download output folder."""
        output_folder = self.download_output_var.get().strip()
        self.duplicate_input_var.set(output_folder)
        self.extract_input_var.set(output_folder)

    def plan_duplicate_removal(self) -> None:
        """Save config and run duplicate removal in dry-run mode."""
        if not self.save_config(
            notify=False,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=True,
        ):
            return
        self.launch_duplicate_removal(dry_run=True)

    def run_duplicate_removal(self) -> None:
        """Save config and move older duplicate versions."""
        if not self.save_config(
            notify=False,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=True,
        ):
            return
        self.launch_duplicate_removal(dry_run=False)

    def launch_duplicate_removal(self, dry_run: bool) -> None:
        """Start the duplicate-removal CLI in a background process."""
        command = [
            sys.executable,
            str(DUPLICATE_SCRIPT),
            "--config",
            str(CONFIG_PATH),
        ]
        if dry_run:
            command.append("--dry-run")

        run_type = "duplicate-removal dry run" if dry_run else "duplicate-removal run"
        self.duplicate_status_var.set(f"Started {run_type}. Waiting for results...")
        thread = threading.Thread(
            target=self.run_duplicate_process,
            args=(command, dry_run),
            daemon=True,
        )
        thread.start()

    def run_duplicate_process(self, command: list[str], dry_run: bool) -> None:
        """Run duplicate removal off the Tkinter UI thread."""
        try:
            result = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            self.root.after(0, self.finish_duplicate_process_error, exc)
            return
        self.root.after(0, self.finish_duplicate_process, result, dry_run)

    def finish_duplicate_process_error(self, exc: OSError) -> None:
        """Show a failed duplicate-removal process launch."""
        self.duplicate_status_var.set(f"Could not start duplicate-removal tool: {exc}")
        messagebox.showerror(
            "Could not start duplicate-removal tool",
            f"Failed to start the duplicate-removal process:\n{exc}",
        )

    def finish_duplicate_process(
        self,
        result: subprocess.CompletedProcess[str],
        dry_run: bool,
    ) -> None:
        """Update the UI after duplicate removal exits."""
        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        )
        output_tail = output[-1800:] if output else "No console output was captured."

        if result.returncode == 0:
            action = "planned" if dry_run else "finished"
            self.duplicate_status_var.set(
                f"Duplicate removal {action} successfully. Input: {self.duplicate_input_var.get().strip()}"
            )
            if not dry_run:
                self.refresh_project_statistics_if_active("duplicate removal")
            messagebox.showinfo("Duplicate removal finished", output_tail)
            return

        self.duplicate_status_var.set(
            f"Duplicate removal finished with exit code {result.returncode}. Check the console output."
        )
        if not dry_run:
            self.refresh_project_statistics_if_active("duplicate removal")
        messagebox.showwarning("Duplicate removal finished with warnings", output_tail)

    def parse_progress_line(self, line: str) -> Optional[Tuple[str, int, int, str]]:
        """Parse one machine-readable progress line emitted by a processing CLI."""
        if not line.startswith(PROGRESS_PREFIX):
            return None
        parts = line.rstrip("\r\n").split("\t", 4)
        if len(parts) != 5:
            return None
        _prefix, tool, current_text, total_text, message = parts
        try:
            current = int(current_text)
            total = int(total_text)
        except ValueError:
            return None
        return tool, current, total, message

    def clean_process_output(self, output: str) -> str:
        """Remove machine-readable progress lines from popup console output."""
        visible_lines = [
            line
            for line in output.splitlines()
            if not line.startswith(PROGRESS_PREFIX)
        ]
        return "\n".join(visible_lines).strip()

    def run_streamed_process(
        self,
        command: list[str],
        env: dict[str, str],
        progress_tool: str,
        progress_handler: Callable[[int, int, str], None],
    ) -> subprocess.CompletedProcess[str]:
        """Run a subprocess while forwarding progress lines to Tkinter."""
        output_lines: list[str] = []
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            output_lines.append(line)
            parsed = self.parse_progress_line(line)
            if parsed is None:
                continue
            tool, current, total, message = parsed
            if tool == progress_tool:
                self.root.after(0, progress_handler, current, total, message)
        return_code = process.wait()
        return subprocess.CompletedProcess(
            command,
            return_code,
            "".join(output_lines),
            "",
        )

    def update_download_progress(self, current: int, total: int, message: str) -> None:
        """Update Download progress widgets from worker progress events."""
        percent = 0.0 if total <= 0 else min(100.0, max(0.0, current / total * 100.0))
        self.download_progress_var.set(percent)
        self.download_progress_text_var.set(f"Progress: {current}/{total} - {message}")

    def load_download_report_preview(self, limit: int = 300) -> int:
        """Load recent download report rows into the preview tree."""
        if not hasattr(self, "download_preview_tree"):
            return 0
        for item in self.download_preview_tree.get_children():
            self.download_preview_tree.delete(item)
        report_path = Path(self.download_report_var.get().strip() or "")
        if not report_path.exists() or not report_path.is_file():
            return 0
        count = 0
        try:
            with report_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    if count >= limit:
                        break
                    self.download_preview_tree.insert(
                        "",
                        tk.END,
                        values=(
                            row.get("file_name", ""),
                            row.get("utm_tile", ""),
                            row.get("start_time", ""),
                            row.get("end_time", ""),
                            row.get("size_mb", ""),
                            row.get("downloaded", ""),
                            row.get("raw_exists", ""),
                            row.get("known_from_manifest", ""),
                            row.get("selected_for_download", ""),
                            row.get("duplicate_filter_status", ""),
                            row.get("status", ""),
                        ),
                    )
                    count += 1
        except OSError:
            return count
        return count

    def clear_treeview(self, tree: ttk.Treeview) -> None:
        """Remove all rows from a Treeview."""
        for item in tree.get_children():
            tree.delete(item)

    def clear_project_statistics_display(self, message: str = "") -> None:
        """Clear statistics widgets."""
        for tree in (
            self.stats_metrics_tree,
            self.stats_tile_tree,
            self.stats_date_tree,
            self.stats_processing_level_tree,
            self.stats_processing_level_tile_tree,
            self.stats_mosaic_output_grid_tree,
            self.stats_mosaic_source_tile_tree,
            self.stats_upload_status_tree,
            self.stats_uploaded_tile_tree,
            self.stats_uploaded_date_tree,
            self.stats_uploaded_level_tree,
            self.stats_upload_qa_tile_tree,
            self.stats_ready_not_uploaded_tree,
            self.stats_upload_errors_tree,
        ):
            self.clear_treeview(tree)
        self.cleanup_candidates = []
        self.populate_cleanup_tree([])
        self.draw_bar_chart(self.stats_stage_chart_canvas, [], "Rows by processing stage")
        self.draw_bar_chart(self.stats_tile_chart_canvas, [], "Top UTM tiles by recorded files")
        self.statistics_summary_var.set("")
        self.cleanup_status_var.set("Open a project, then preview safe cleanup candidates.")
        if message:
            self.statistics_status_var.set(message)

    def display_project_statistics(
        self,
        insights: Any,
        *,
        status_text: str,
        include_cleanup: bool = True,
    ) -> None:
        """Render project statistics into the GUI tables and charts."""
        self.clear_treeview(self.stats_metrics_tree)
        for metric, value in insights.metrics.items():
            self.stats_metrics_tree.insert("", tk.END, values=(metric, value))
        if insights.stage_status_counts:
            self.stats_metrics_tree.insert("", tk.END, values=("", ""))
            self.stats_metrics_tree.insert("", tk.END, values=("Stage status counts", ""))
            for stage, status, count in insights.stage_status_counts:
                self.stats_metrics_tree.insert(
                    "",
                    tk.END,
                    values=(f"{stage}: {status}", str(count)),
                )

        self.clear_treeview(self.stats_tile_tree)
        for tile, count in insights.tile_counts:
            self.stats_tile_tree.insert("", tk.END, values=(tile, str(count)))

        self.clear_treeview(self.stats_date_tree)
        for date_text, count in insights.date_counts:
            self.stats_date_tree.insert("", tk.END, values=(date_text, str(count)))

        self.clear_treeview(self.stats_processing_level_tree)
        for (
            level,
            remote,
            selected,
            downloaded,
            extracted,
            mosaic_sources,
            uploaded,
        ) in insights.processing_level_counts:
            self.stats_processing_level_tree.insert(
                "",
                tk.END,
                values=(
                    level,
                    str(remote),
                    str(selected),
                    str(downloaded),
                    str(extracted),
                    str(mosaic_sources),
                    str(uploaded),
                ),
            )

        self.clear_treeview(self.stats_processing_level_tile_tree)
        for tile, level, remote, downloaded, extracted, mosaic_sources in insights.processing_level_tile_counts:
            self.stats_processing_level_tile_tree.insert(
                "",
                tk.END,
                values=(
                    tile,
                    level,
                    str(remote),
                    str(downloaded),
                    str(extracted),
                    str(mosaic_sources),
                ),
            )

        self.clear_treeview(self.stats_mosaic_output_grid_tree)
        for grid, count in insights.mosaic_output_grid_counts:
            self.stats_mosaic_output_grid_tree.insert("", tk.END, values=(grid, str(count)))

        self.clear_treeview(self.stats_mosaic_source_tile_tree)
        for tile, count in insights.mosaic_source_tile_counts:
            self.stats_mosaic_source_tile_tree.insert("", tk.END, values=(tile, str(count)))

        self.clear_treeview(self.stats_upload_status_tree)
        for status, count in insights.upload_status_counts:
            self.stats_upload_status_tree.insert("", tk.END, values=(status, str(count)))

        self.clear_treeview(self.stats_uploaded_tile_tree)
        for tile, count in insights.uploaded_tile_counts:
            self.stats_uploaded_tile_tree.insert("", tk.END, values=(tile, str(count)))

        self.clear_treeview(self.stats_uploaded_date_tree)
        for date_text, count in insights.uploaded_date_counts:
            self.stats_uploaded_date_tree.insert("", tk.END, values=(date_text, str(count)))

        self.clear_treeview(self.stats_uploaded_level_tree)
        for level, count in insights.uploaded_processing_level_counts:
            self.stats_uploaded_level_tree.insert("", tk.END, values=(level, str(count)))

        self.clear_treeview(self.stats_upload_qa_tile_tree)
        for tile, downloaded, extracted, mosaic_sources, uploaded, missing_upload in insights.upload_qa_tile_rows:
            self.stats_upload_qa_tile_tree.insert(
                "",
                tk.END,
                values=(
                    tile,
                    str(downloaded),
                    str(extracted),
                    str(mosaic_sources),
                    str(uploaded),
                    str(missing_upload),
                ),
            )

        self.clear_treeview(self.stats_ready_not_uploaded_tree)
        for output_file, source_tiles, date_text, grid in insights.ready_not_uploaded_rows:
            self.stats_ready_not_uploaded_tree.insert(
                "",
                tk.END,
                values=(date_text, grid, source_tiles, output_file),
            )

        self.clear_treeview(self.stats_upload_errors_tree)
        for status, message, count in insights.upload_error_counts:
            self.stats_upload_errors_tree.insert(
                "",
                tk.END,
                values=(status, str(count), message),
            )

        cleanup_size = insights.metrics.get("Cleanup candidate size", "0 B")
        if include_cleanup:
            self.cleanup_candidates = list(insights.cleanup_candidates)
            self.populate_cleanup_tree(self.cleanup_candidates)
            self.cleanup_status_var.set(
                f"Cleanup preview updated: {len(self.cleanup_candidates)} files, {cleanup_size}."
            )
        else:
            self.cleanup_candidates = []
            self.populate_cleanup_tree([])
            self.cleanup_status_var.set(
                "Cleanup preview is not loaded from the saved statistics snapshot. "
                "Click Preview Cleanup or Refresh Statistics to compute current candidates."
            )

        stage_totals: Dict[str, int] = {}
        for stage, _status, count in insights.stage_status_counts:
            if stage == "workflow":
                continue
            stage_totals[stage] = stage_totals.get(stage, 0) + count
        self.draw_bar_chart(
            self.stats_stage_chart_canvas,
            list(stage_totals.items()),
            "Rows by processing stage",
        )
        self.draw_bar_chart(
            self.stats_tile_chart_canvas,
            insights.tile_counts[:12],
            "Top UTM tiles by recorded files",
        )

        coverage = insights.metrics.get("Date coverage", "") or "not available"
        self.statistics_summary_var.set(
            f"Date coverage: {coverage}."
        )
        self.statistics_status_var.set(status_text)

    def load_saved_project_statistics(self) -> None:
        """Load the last saved statistics snapshot for the active project."""
        if not self.current_project_root_var.get().strip():
            self.clear_project_statistics_display(
                "Open a project, then refresh statistics to summarize manifests and local files."
            )
            return
        loaded = load_project_insights_snapshot(self.build_config())
        if loaded is None:
            self.clear_project_statistics_display(
                "No saved statistics snapshot yet. Click Refresh Statistics to compute and save one."
            )
            return
        insights, generated_at = loaded
        suffix = f" from {generated_at}" if generated_at else ""
        self.display_project_statistics(
            insights,
            status_text=f"Loaded saved project statistics{suffix}. Refresh to update from current files.",
            include_cleanup=False,
        )

    def refresh_project_statistics_if_active(self, source: str) -> None:
        """Refresh statistics silently when a project is open."""
        if self.current_project_root_var.get().strip():
            self.refresh_project_statistics(notify=False, source=source)

    def refresh_project_statistics(
        self,
        notify: bool = True,
        source: str = "",
    ) -> None:
        """Refresh project statistics from manifests, reports, and local files."""
        if not self.current_project_root_var.get().strip():
            if notify:
                self.require_active_project("refresh project statistics")
            return
        try:
            insights = collect_project_insights(self.build_config())
        except Exception as exc:
            self.statistics_status_var.set(f"Could not refresh project statistics: {exc}")
            if notify:
                messagebox.showerror(
                    "Could not refresh statistics",
                    f"Failed to read project manifests or folders:\n{exc}",
                )
            return

        suffix = f" after {source}" if source else ""
        snapshot_text = ""
        try:
            snapshot_path = write_project_insights_snapshot(self.build_config(), insights)
            snapshot_text = f" Saved to {snapshot_path}."
        except OSError as exc:
            snapshot_text = f" Could not save statistics snapshot: {exc}"
        self.display_project_statistics(
            insights,
            status_text=f"Project statistics refreshed{suffix}.{snapshot_text}",
            include_cleanup=True,
        )

    def draw_bar_chart(
        self,
        canvas: tk.Canvas,
        values: Sequence[tuple[str, int]],
        title: str,
    ) -> None:
        """Draw a small horizontal bar chart without external plotting dependencies."""
        canvas.delete("all")
        width = max(canvas.winfo_width(), 340)
        height = int(str(canvas.cget("height")) or "150")
        canvas.create_text(8, 8, anchor="nw", text=title, font=("Segoe UI", 9, "bold"))
        if not values:
            canvas.create_text(8, 42, anchor="nw", text="No data yet", fill="#666666")
            return

        top = 32
        left = 92
        right_pad = 42
        row_height = max(14, min(22, (height - top - 8) // max(1, len(values))))
        bar_height = max(8, row_height - 5)
        max_value = max(count for _label, count in values) or 1
        palette = ["#2878b5", "#d95f02", "#4d9221", "#7b3294", "#c51b7d", "#5c5c5c"]

        for index, (label, count) in enumerate(values):
            y = top + index * row_height
            if y + bar_height > height - 4:
                break
            short_label = label if len(label) <= 12 else f"{label[:11]}..."
            canvas.create_text(8, y + bar_height / 2, anchor="w", text=short_label)
            available = max(20, width - left - right_pad)
            bar_width = int(available * (count / max_value))
            canvas.create_rectangle(
                left,
                y,
                left + bar_width,
                y + bar_height,
                fill=palette[index % len(palette)],
                outline="",
            )
            canvas.create_text(
                left + bar_width + 5,
                y + bar_height / 2,
                anchor="w",
                text=str(count),
            )

    def preview_cleanup_candidates(self, notify: bool = True) -> None:
        """Load conservative cleanup candidates into the Cleanup table."""
        if not self.require_active_project("preview cleanup candidates"):
            return
        try:
            self.cleanup_candidates = plan_cleanup_candidates(self.build_config())
        except Exception as exc:
            self.cleanup_status_var.set(f"Could not preview cleanup candidates: {exc}")
            messagebox.showerror(
                "Could not preview cleanup",
                f"Failed to inspect project manifests:\n{exc}",
            )
            return
        self.populate_cleanup_tree(self.cleanup_candidates)
        total_size = sum(candidate.size_bytes for candidate in self.cleanup_candidates)
        self.cleanup_status_var.set(
            f"Cleanup preview: {len(self.cleanup_candidates)} files, {format_insight_bytes(total_size)}."
        )
        if notify and not self.cleanup_candidates:
            messagebox.showinfo(
                "No cleanup candidates",
                "No local intermediate files currently have downstream manifest proof for cleanup.",
            )

    def populate_cleanup_tree(self, candidates: Sequence[CleanupCandidate]) -> None:
        """Show cleanup candidates in the cleanup table."""
        self.clear_treeview(self.cleanup_tree)
        for index, candidate in enumerate(candidates):
            self.cleanup_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    candidate.stage,
                    format_insight_bytes(candidate.size_bytes),
                    candidate.reason,
                    str(candidate.path),
                ),
            )

    def selected_cleanup_candidates(self) -> list[CleanupCandidate]:
        """Return cleanup candidates selected in the cleanup table."""
        selected: list[CleanupCandidate] = []
        for item in self.cleanup_tree.selection():
            try:
                index = int(item)
            except ValueError:
                continue
            if 0 <= index < len(self.cleanup_candidates):
                selected.append(self.cleanup_candidates[index])
        return selected

    def delete_selected_cleanup_files(self) -> None:
        """Delete selected cleanup candidates after confirmation."""
        candidates = self.selected_cleanup_candidates()
        if not candidates:
            messagebox.showinfo(
                "No cleanup rows selected",
                "Select one or more cleanup rows first, or use Delete All Cleanup Candidates.",
            )
            return
        self.delete_cleanup_files(candidates, "selected cleanup files")

    def delete_all_cleanup_files(self) -> None:
        """Delete all currently previewed cleanup candidates after confirmation."""
        if not self.cleanup_candidates:
            self.preview_cleanup_candidates(notify=False)
        if not self.cleanup_candidates:
            messagebox.showinfo(
                "No cleanup candidates",
                "No cleanup candidates are currently available.",
            )
            return
        self.delete_cleanup_files(self.cleanup_candidates, "all cleanup candidates")

    def delete_cleanup_files(
        self,
        candidates: Sequence[CleanupCandidate],
        label: str,
    ) -> None:
        """Delete a cleanup candidate set after an explicit confirmation."""
        total_size = sum(candidate.size_bytes for candidate in candidates)
        confirmed = messagebox.askyesno(
            "Delete cleanup files",
            (
                f"Delete {len(candidates)} {label} "
                f"({format_insight_bytes(total_size)})?\n\n"
                "Only files listed in the cleanup preview will be deleted. "
                "This cannot be undone."
            ),
        )
        if not confirmed:
            return
        deleted, bytes_deleted, errors = delete_cleanup_candidates(candidates)
        self.preview_cleanup_candidates(notify=False)
        self.refresh_project_statistics()
        message = (
            f"Deleted {deleted} files and freed {format_insight_bytes(bytes_deleted)}."
        )
        if errors:
            self.cleanup_status_var.set(f"{message} {len(errors)} files could not be deleted.")
            messagebox.showwarning(
                "Cleanup finished with warnings",
                f"{message}\n\nSome files could not be deleted:\n" + "\n".join(errors[:10]),
            )
            return
        self.cleanup_status_var.set(message)
        messagebox.showinfo("Cleanup finished", message)

    def update_extraction_progress(self, current: int, total: int, message: str) -> None:
        """Update Extraction progress widgets from subprocess progress events."""
        percent = 100.0 if total <= 0 else min(100.0, max(0.0, current / total * 100.0))
        self.extract_progress_var.set(percent)
        self.extract_progress_text_var.set(f"Progress: {current}/{total} - {message}")

    def update_mosaic_progress(self, current: int, total: int, message: str) -> None:
        """Update Mosaic progress widgets from subprocess progress events."""
        percent = 100.0 if total <= 0 else min(100.0, max(0.0, current / total * 100.0))
        self.mosaic_progress_var.set(percent)
        self.mosaic_progress_text_var.set(f"Progress: {current}/{total} - {message}")

    def plan_extraction(self) -> None:
        """Save config and run extraction in dry-run mode."""
        if not self.save_config(
            notify=False,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=False,
            validate_extract=True,
        ):
            return
        self.launch_extraction(dry_run=True)

    def run_extraction(self) -> None:
        """Save config and run the GDAL extraction tool."""
        if not self.save_config(
            notify=False,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=False,
            validate_extract=True,
        ):
            return
        self.launch_extraction(dry_run=False)

    def launch_extraction(self, dry_run: bool) -> None:
        """Start the extraction CLI in a background process."""
        gdal_python = Path(self.gdal_python_var.get().strip())
        command = [
            str(gdal_python),
            str(EXTRACT_SCRIPT),
            "--config",
            str(CONFIG_PATH),
        ]
        if dry_run:
            command.append("--dry-run")

        run_type = "extraction dry run" if dry_run else "extraction run"
        self.extract_status_var.set(f"Started {run_type}. Waiting for results...")
        self.extract_progress_var.set(0.0)
        self.extract_progress_text_var.set("Progress: planning files..." if dry_run else "Progress: starting...")
        thread = threading.Thread(
            target=self.run_extraction_process,
            args=(command, dry_run, build_gdal_runtime_env(gdal_python)),
            daemon=True,
        )
        thread.start()

    def run_extraction_process(
        self,
        command: list[str],
        dry_run: bool,
        env: dict[str, str],
    ) -> None:
        """Run extraction off the Tkinter UI thread."""
        try:
            result = self.run_streamed_process(
                command,
                env,
                "extract",
                self.update_extraction_progress,
            )
        except OSError as exc:
            self.root.after(0, self.finish_extraction_process_error, exc)
            return
        self.root.after(0, self.finish_extraction_process, result, dry_run)

    def finish_extraction_process_error(self, exc: OSError) -> None:
        """Show a failed extraction process launch."""
        self.extract_status_var.set(f"Could not start extraction tool: {exc}")
        messagebox.showerror(
            "Could not start extraction tool",
            f"Failed to start the extraction process:\n{exc}",
        )

    def finish_extraction_process(
        self,
        result: subprocess.CompletedProcess[str],
        dry_run: bool,
    ) -> None:
        """Update the UI after extraction exits."""
        output = "\n".join(
            part.strip()
            for part in (self.clean_process_output(result.stdout or ""), result.stderr)
            if part and part.strip()
        )
        output_tail = output[-1800:] if output else "No console output was captured."

        if result.returncode == 0:
            if not dry_run:
                self.apply_extraction_handoff_to_mosaic()
                self.refresh_project_statistics_if_active("extraction")
            self.extract_progress_var.set(100.0)
            action = "planned" if dry_run else "finished"
            if dry_run:
                self.extract_progress_text_var.set("Progress: dry run complete")
            self.extract_status_var.set(
                f"Extraction {action} successfully. Manifest: {self.extract_manifest_var.get().strip()}"
            )
            messagebox.showinfo("Extraction finished", output_tail)
            return

        self.extract_status_var.set(
            f"Extraction finished with exit code {result.returncode}. Check the CSV reports and console output."
        )
        if not dry_run:
            self.refresh_project_statistics_if_active("extraction")
        messagebox.showwarning("Extraction finished with warnings", output_tail)

    def apply_extraction_handoff_to_mosaic(self) -> None:
        """Point Mosaic at Extraction outputs and set grouping from Extraction CRS mode."""
        self.mosaic_input_var.set(self.extract_output_var.get().strip())
        crs_mode = self.selected_extract_crs_mode()
        if crs_mode == "original":
            self.mosaic_grouping_mode_var.set(
                MOSAIC_GROUPING_LABEL_BY_VALUE["utm_zone"]
            )
            self.mosaic_target_crs_label_var.set("")
            return

        self.mosaic_grouping_mode_var.set(
            MOSAIC_GROUPING_LABEL_BY_VALUE["pass_date_common_crs"]
        )
        self.mosaic_target_crs_label_var.set(
            "LAEA" if crs_mode == "africa_laea" else "WGS84"
        )

    def save_and_run_dry_run(self) -> None:
        """Save config and start the uploader in dry-run mode."""
        if not self.save_config(
            notify=False,
            dry_run_override=True,
            validate_upload=True,
            validate_mosaic=False,
        ):
            return
        self.launch_uploader(dry_run=True)

    def save_and_run_real(self) -> None:
        """Save config and start the uploader in real mode."""
        if not self.save_config(
            notify=False,
            dry_run_override=False,
            validate_upload=True,
            validate_mosaic=False,
        ):
            return
        self.launch_uploader(dry_run=False)

    def launch_uploader(self, dry_run: bool) -> None:
        """Start the CLI uploader in a new console window when possible."""
        upload_report_path = self.current_upload_report_path()
        upload_report_mtime = self.file_mtime_ns(upload_report_path)
        uploader_command = [
            sys.executable,
            str(UPLOADER_SCRIPT),
            "--config",
            str(CONFIG_PATH),
        ]
        if dry_run:
            uploader_command.append("--dry-run")

        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        command = ["cmd.exe", "/k", subprocess.list2cmdline(uploader_command)]
        try:
            subprocess.Popen(command, cwd=str(PROJECT_ROOT), creationflags=creationflags)
        except OSError as exc:
            messagebox.showerror(
                "Could not start uploader",
                f"Failed to start the uploader process:\n{exc}",
            )
            return

        run_type = "dry run" if dry_run else "real upload"
        self.start_upload_statistics_watcher(upload_report_path, upload_report_mtime)
        self.status_var.set(
            f"Started {run_type} in a separate console window. That window now stays open after the run finishes."
        )
        messagebox.showinfo(
            "Uploader started",
            (
                f"Started {run_type}.\n\n"
                "A separate console window should open for logs and prompts.\n"
                "It will now stay open after the run finishes, so you can read the result."
            ),
        )

    def current_upload_report_path(self) -> Path:
        """Return the configured upload report path."""
        artifacts = self.build_config().get("artifacts", {})
        if not isinstance(artifacts, dict):
            return Path("")
        return Path(str(artifacts.get("report_csv", "")))

    def file_mtime_ns(self, path: Path) -> int | None:
        """Return file modification time in nanoseconds when available."""
        try:
            return path.stat().st_mtime_ns if path.exists() and path.is_file() else None
        except OSError:
            return None

    def start_upload_statistics_watcher(
        self,
        report_path: Path,
        previous_mtime: int | None,
    ) -> None:
        """Watch the upload report for changes while the separate console runs."""
        if self.upload_stats_poll_after_id is not None:
            try:
                self.root.after_cancel(self.upload_stats_poll_after_id)
            except tk.TclError:
                pass
            self.upload_stats_poll_after_id = None
        if not str(report_path):
            return
        self.upload_stats_poll_after_id = self.root.after(
            10000,
            self.poll_upload_statistics_report,
            str(report_path),
            previous_mtime,
            1440,
        )

    def poll_upload_statistics_report(
        self,
        report_path_text: str,
        previous_mtime: int | None,
        remaining_polls: int,
    ) -> None:
        """Refresh Statistics when the upload report changes."""
        report_path = Path(report_path_text)
        current_mtime = self.file_mtime_ns(report_path)
        if current_mtime is not None and current_mtime != previous_mtime:
            self.refresh_project_statistics_if_active("upload report")
            previous_mtime = current_mtime
        if remaining_polls <= 0:
            self.upload_stats_poll_after_id = None
            return
        self.upload_stats_poll_after_id = self.root.after(
            30000,
            self.poll_upload_statistics_report,
            report_path_text,
            previous_mtime,
            remaining_polls - 1,
        )

    def plan_mosaics(self) -> None:
        """Save config and run the mosaic tool in dry-run mode."""
        if not self.save_config(
            notify=False,
            validate_upload=False,
            validate_mosaic=True,
        ):
            return
        self.launch_mosaic(dry_run=True)

    def run_mosaics(self) -> None:
        """Save config and run the mosaic tool."""
        if not self.save_config(
            notify=False,
            validate_upload=False,
            validate_mosaic=True,
        ):
            return
        self.launch_mosaic(dry_run=False)

    def launch_mosaic(self, dry_run: bool) -> None:
        """Start the mosaic CLI in a background process."""
        gdal_python = Path(self.gdal_python_var.get().strip())
        command = [
            str(gdal_python),
            str(MOSAIC_SCRIPT),
            "--config",
            str(CONFIG_PATH),
        ]
        if dry_run:
            command.append("--dry-run")

        run_type = "mosaic dry run" if dry_run else "mosaic run"
        self.mosaic_status_var.set(f"Started {run_type}. Waiting for results...")
        self.mosaic_progress_var.set(0.0)
        self.mosaic_progress_text_var.set("Progress: planning groups..." if dry_run else "Progress: starting...")
        thread = threading.Thread(
            target=self.run_mosaic_process,
            args=(command, dry_run, build_gdal_runtime_env(gdal_python)),
            daemon=True,
        )
        thread.start()

    def run_mosaic_process(
        self,
        command: list[str],
        dry_run: bool,
        env: dict[str, str],
    ) -> None:
        """Run the mosaic process off the Tkinter UI thread."""
        try:
            result = self.run_streamed_process(
                command,
                env,
                "mosaic",
                self.update_mosaic_progress,
            )
        except OSError as exc:
            self.root.after(0, self.finish_mosaic_process_error, exc)
            return
        self.root.after(0, self.finish_mosaic_process, result, dry_run)

    def finish_mosaic_process_error(self, exc: OSError) -> None:
        """Show a failed mosaic process launch."""
        self.mosaic_status_var.set(f"Could not start mosaic tool: {exc}")
        messagebox.showerror(
            "Could not start mosaic tool",
            f"Failed to start the mosaic process:\n{exc}",
        )

    def finish_mosaic_process(
        self,
        result: subprocess.CompletedProcess[str],
        dry_run: bool,
    ) -> None:
        """Update the UI after the mosaic process exits."""
        output = "\n".join(
            part.strip()
            for part in (self.clean_process_output(result.stdout or ""), result.stderr)
            if part and part.strip()
        )
        output_tail = output[-1800:] if output else "No console output was captured."

        if result.returncode == 0:
            if not dry_run and self.mosaic_set_upload_folder_var.get():
                self.folder_var.set(self.mosaic_output_var.get().strip())
            if not dry_run:
                self.refresh_project_statistics_if_active("mosaic")
            self.mosaic_progress_var.set(100.0)
            if dry_run:
                self.mosaic_progress_text_var.set("Progress: dry run complete")
            self.mosaic_status_var.set(
                f"Mosaic command finished successfully. Report: {self.mosaic_report_var.get().strip()}"
            )
            messagebox.showinfo("Mosaic finished", output_tail)
            return

        self.mosaic_status_var.set(
            f"Mosaic command finished with exit code {result.returncode}. Check the report and console output."
        )
        if not dry_run:
            self.refresh_project_statistics_if_active("mosaic")
        messagebox.showwarning("Mosaic finished with warnings", output_tail)

    def open_manual_login_browser(self) -> None:
        """Open a normal Chrome window with the dedicated profile for manual login."""
        if not self.save_config(
            notify=False,
            validate_upload=True,
            validate_mosaic=False,
        ):
            return

        try:
            chrome_binary = self.find_chrome_binary()
        except FileNotFoundError as exc:
            messagebox.showerror("Chrome not found", str(exc))
            return
        profile_dir = self.profile_dir_var.get().strip() or str(PROJECT_ROOT / "chrome-profile")
        Path(profile_dir).mkdir(parents=True, exist_ok=True)

        command = [
            chrome_binary,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://code.earthengine.google.com/",
        ]
        creationflags = 0
        for flag_name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS"):
            creationflags |= getattr(subprocess, flag_name, 0)

        try:
            subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            messagebox.showerror(
                "Could not open Chrome",
                f"Failed to open normal Chrome for manual login:\n{exc}",
            )
            return

        self.status_var.set(
            "Opened normal Chrome for manual login. Sign in there, then close Chrome and run the uploader."
        )
        messagebox.showinfo(
            "Chrome opened",
            (
                "Opened a normal Chrome window using the dedicated profile.\n\n"
                "1. Sign in to Google / Earth Engine there if needed.\n"
                "2. Confirm Earth Engine opens correctly.\n"
                "3. Close that Chrome window.\n"
                "4. Return here and run the uploader."
            ),
        )

    def find_chrome_binary(self) -> str:
        """Return the best available Chrome executable path on Windows."""
        candidates = [
            shutil.which("chrome"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        raise FileNotFoundError(
            "Could not find Google Chrome automatically. Install Chrome or set binary_location manually in config.yaml."
        )


def main() -> int:
    """Launch the Tkinter app."""
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app = LauncherApp(root)
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

