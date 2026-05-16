"""Simple desktop GUI for configuring and launching the Earth Engine uploader."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


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
              python ee_uploader_gui.py
            """
        ).strip()
    )


ensure_isolated_python_environment()

import yaml

from gdal_runtime import DEFAULT_GDAL_PYTHON, build_gdal_runtime_env


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
        duplicate_data = self.data.get("duplicates", {})
        extract_data = self.data.get("extract", {})
        metadata_data = self.data.get("metadata", {})
        mosaic_data = self.data.get("mosaic", {})
        gdal_data = self.data.get("gdal", {})

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

        self.status_var = tk.StringVar(
            value="Fill the form, save config, then start a dry run."
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
        self.extract_progress_var = tk.DoubleVar(value=0.0)
        self.extract_progress_text_var = tk.StringVar(value="Progress: not started")
        self.mosaic_progress_var = tk.DoubleVar(value=0.0)
        self.mosaic_progress_text_var = tk.StringVar(value="Progress: not started")

        self.build_layout()

    def build_layout(self) -> None:
        """Create the tabbed launcher layout."""
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

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

        notebook = ttk.Notebook(outer)
        notebook.grid(row=2, column=0, sticky="nsew")

        duplicate_tab = ttk.Frame(notebook, padding=12)
        extract_tab = ttk.Frame(notebook, padding=12)
        mosaic_tab = ttk.Frame(notebook, padding=12)
        upload_tab = ttk.Frame(notebook, padding=12)
        notebook.add(duplicate_tab, text="Duplicate Removal")
        notebook.add(extract_tab, text="Extraction")
        notebook.add(mosaic_tab, text="Mosaic")
        notebook.add(upload_tab, text="Upload")

        self.build_duplicate_tab(duplicate_tab)
        self.build_extract_tab(extract_tab)
        self.build_mosaic_tab(mosaic_tab)
        self.build_upload_tab(upload_tab)

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

        ttk.Button(buttons, text="Save Config", command=self.save_duplicate_config).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            buttons,
            text="Plan Duplicate Removal",
            command=self.plan_duplicate_removal,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(
            buttons,
            text="Run Duplicate Removal",
            command=self.run_duplicate_removal,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

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

        ttk.Button(buttons, text="Save Config", command=self.save_extract_config).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(buttons, text="Plan Extraction", command=self.plan_extraction).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Button(buttons, text="Run Extraction", command=self.run_extraction).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
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

        notes = ttk.LabelFrame(parent, text="Notes", padding=12)
        notes.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            notes,
            text=(
                "1. Save config before running.\n"
                "2. By default the tool starts normal Chrome in attach mode, then Selenium attaches to it.\n"
                "3. On the first real run, Chrome may ask you to sign in manually.\n"
                "4. Keep the dedicated Chrome profile for future runs.\n"
                "5. To upload mosaics, set Origin folder to the Mosaic tab output folder.\n"
                "6. Real uploads still ask for final confirmation in the console unless you later change that in config."
            ),
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(parent)
        buttons.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        buttons.columnconfigure(0, weight=1)

        ttk.Button(buttons, text="Save Config", command=self.save_upload_config).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(
            buttons,
            text="Open Chrome For Manual Login",
            command=self.open_manual_login_browser,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(
            buttons, text="Save And Run Dry Run", command=self.save_and_run_dry_run
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Button(
            buttons, text="Save And Run Real Upload", command=self.save_and_run_real
        ).grid(row=0, column=3, sticky="w", padx=(8, 0))

        status = ttk.Label(
            parent,
            textvariable=self.status_var,
            foreground="#184a8b",
            justify="left",
        )
        status.grid(row=4, column=0, sticky="w", pady=(14, 0))

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
            text="Set Upload origin folder to mosaic output after successful run",
            variable=self.mosaic_set_upload_folder_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=(0, 18), pady=4)

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

        ttk.Button(buttons, text="Save Config", command=self.save_mosaic_config).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(buttons, text="Plan Mosaics", command=self.plan_mosaics).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Button(buttons, text="Run Mosaic", command=self.run_mosaics).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
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
                "raw_downloads": self.duplicate_input_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["raw_downloads"],
                "extracted_geotiffs": self.extract_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["extracted_geotiffs"],
                "mosaics": self.mosaic_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["mosaics"],
                "logs": self.duplicate_log_folder_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["logs"],
            },
            "duplicates": {
                "input_folder": self.duplicate_input_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["raw_downloads"],
                "moved_folder_name": self.duplicate_moved_folder_var.get().strip()
                or "moved",
                "log_folder": self.duplicate_log_folder_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["logs"],
                "recursive": self.duplicate_recursive_var.get(),
            },
            "extract": {
                "input_folder": self.extract_input_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["raw_downloads"],
                "output_folder": self.extract_output_var.get().strip()
                or DEFAULT_PROCESSING_PATHS["extracted_geotiffs"],
                "target_crs_mode": self.selected_extract_crs_mode(),
                "year_selection": self.extract_year_selection_var.get().strip() or "all",
                "limit_files": self.parse_optional_int_or_none(
                    self.extract_limit_files_var.get()
                ),
                "skip_existing": self.extract_skip_existing_var.get(),
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
                "logs_dir": "./logs",
                "artifacts_dir": "./artifacts",
                "report_csv": "./reports/upload_report.csv",
            },
        }

    def save_config(
        self,
        notify: bool = True,
        dry_run_override: bool | None = None,
        validate_upload: bool = True,
        validate_mosaic: bool = False,
        validate_duplicates: bool = False,
        validate_extract: bool = False,
    ) -> bool:
        """Save config.yaml from the current form values."""
        if validate_duplicates and not self.validate_duplicate_removal():
            return False
        if validate_extract and not self.validate_extraction():
            return False
        if validate_upload and not self.validate_upload():
            return False
        if validate_mosaic and not self.validate_mosaic():
            return False
        data = self.build_config(dry_run_override=dry_run_override)
        with CONFIG_PATH.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=False)
        self.status_var.set(f"Saved config to {CONFIG_PATH}")
        self.mosaic_status_var.set(f"Saved config to {CONFIG_PATH}")
        self.duplicate_status_var.set(f"Saved config to {CONFIG_PATH}")
        self.extract_status_var.set(f"Saved config to {CONFIG_PATH}")
        if notify:
            messagebox.showinfo("Config saved", f"Saved:\n{CONFIG_PATH}")
        return True

    def save_upload_config(self) -> bool:
        """Save config after validating the upload tab."""
        return self.save_config(
            notify=True,
            validate_upload=True,
            validate_mosaic=False,
            validate_duplicates=False,
        )

    def save_mosaic_config(self) -> bool:
        """Save config after validating the mosaic tab."""
        return self.save_config(
            notify=True,
            validate_upload=False,
            validate_mosaic=True,
            validate_duplicates=False,
            validate_extract=False,
        )

    def save_duplicate_config(self) -> bool:
        """Save config after validating the duplicate-removal tab."""
        return self.save_config(
            notify=True,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=True,
            validate_extract=False,
        )

    def save_extract_config(self) -> bool:
        """Save config after validating the extraction tab."""
        return self.save_config(
            notify=True,
            validate_upload=False,
            validate_mosaic=False,
            validate_duplicates=False,
            validate_extract=True,
        )

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
            messagebox.showinfo("Duplicate removal finished", output_tail)
            return

        self.duplicate_status_var.set(
            f"Duplicate removal finished with exit code {result.returncode}. Check the console output."
        )
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
