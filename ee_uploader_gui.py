"""Simple desktop GUI for configuring and launching the Earth Engine uploader."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "config.example.yaml"
UPLOADER_SCRIPT = PROJECT_ROOT / "ee_ui_uploader.py"


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


def load_config() -> Dict[str, Any]:
    """Load config.yaml when present, otherwise use config.example.yaml."""
    source = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    with source.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class LauncherApp:
    """Tkinter-based launcher for configuring and running uploads."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Earth Engine UI Uploader")
        self.root.geometry("860x720")
        self.root.minsize(780, 620)

        self.data = load_config()

        self.folder_var = tk.StringVar(value=self.data.get("input_folder", ""))
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

        self.status_var = tk.StringVar(
            value="Fill the form, save config, then start a dry run."
        )

        self.build_layout()

    def build_layout(self) -> None:
        """Create all widgets."""
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        title = ttk.Label(
            outer,
            text="Earth Engine UI Uploader",
            font=("Segoe UI", 16, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")

        intro = ttk.Label(
            outer,
            text=(
                "This launcher writes config.yaml for you and then starts the uploader.\n"
                "Recommended queue setting for strict 50-at-a-time uploads: batch size 50 and max active ingestions 0."
            ),
            justify="left",
        )
        intro.grid(row=1, column=0, sticky="w", pady=(6, 14))

        form = ttk.Frame(outer)
        form.grid(row=2, column=0, sticky="nsew")
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

        toggles = ttk.LabelFrame(outer, text="Options", padding=12)
        toggles.grid(row=3, column=0, sticky="ew", pady=(14, 0))

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

        notes = ttk.LabelFrame(outer, text="Notes", padding=12)
        notes.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            notes,
            text=(
                "1. Save config before running.\n"
                "2. By default the tool starts normal Chrome in attach mode, then Selenium attaches to it.\n"
                "3. On the first real run, Chrome may ask you to sign in manually.\n"
                "4. Keep the dedicated Chrome profile for future runs.\n"
                "5. Real uploads still ask for final confirmation in the console unless you later change that in config."
            ),
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(outer)
        buttons.grid(row=5, column=0, sticky="ew", pady=(16, 0))
        buttons.columnconfigure(0, weight=1)

        ttk.Button(buttons, text="Save Config", command=self.save_config).grid(
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
            outer,
            textvariable=self.status_var,
            foreground="#184a8b",
            justify="left",
        )
        status.grid(row=6, column=0, sticky="w", pady=(14, 0))

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

    def browse_input_folder(self) -> None:
        """Let the user choose the local GeoTIFF folder."""
        selected = filedialog.askdirectory(
            title="Choose the folder containing GeoTIFF files",
            initialdir=self.folder_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.folder_var.set(selected)

    def browse_profile_folder(self) -> None:
        """Let the user choose a dedicated Chrome profile folder."""
        selected = filedialog.askdirectory(
            title="Choose the dedicated Chrome profile folder",
            initialdir=self.profile_dir_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.profile_dir_var.set(selected)

    def validate(self) -> bool:
        """Check form values before saving or running."""
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

    def build_config(self, dry_run_override: bool | None = None) -> Dict[str, Any]:
        """Build a config dictionary from the UI state."""
        effective_dry_run = (
            self.dry_run_var.get() if dry_run_override is None else dry_run_override
        )
        return {
            "earth_engine_url": "https://code.earthengine.google.com/",
            "input_folder": self.folder_var.get().strip(),
            "destination_parent": self.destination_var.get().strip(),
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
                "batch_size": int(self.batch_size_var.get()),
                "max_active_ingestions": int(self.max_active_var.get()),
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
                "retry_attempts": int(self.retry_attempts_var.get()),
                "retry_wait_seconds": float(self.retry_wait_var.get()),
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
    ) -> bool:
        """Save config.yaml from the current form values."""
        if not self.validate():
            return False
        data = self.build_config(dry_run_override=dry_run_override)
        with CONFIG_PATH.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=False)
        self.status_var.set(f"Saved config to {CONFIG_PATH}")
        if notify:
            messagebox.showinfo("Config saved", f"Saved:\n{CONFIG_PATH}")
        return True

    def save_and_run_dry_run(self) -> None:
        """Save config and start the uploader in dry-run mode."""
        if not self.save_config(notify=False, dry_run_override=True):
            return
        self.launch_uploader(dry_run=True)

    def save_and_run_real(self) -> None:
        """Save config and start the uploader in real mode."""
        if not self.save_config(notify=False, dry_run_override=False):
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

    def open_manual_login_browser(self) -> None:
        """Open a normal Chrome window with the dedicated profile for manual login."""
        if not self.save_config(notify=False):
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
