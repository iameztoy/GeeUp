"""Initial PIXC product-family GUI skeleton."""

from __future__ import annotations

import calendar
import webbrowser
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .config import COLLECTION_LABELS, DEFAULT_COLLECTION_LABEL, PRODUCT_NAME
from .download import DEFAULT_MAX_GRANULES, DEFAULT_RAW_DOWNLOADS_DIR
from .project import DEFAULT_PIXC_PROJECT_PARENT
from .visualize import DEFAULT_MAX_POINTS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIXC_DOCS_DIR = PROJECT_ROOT / "docs" / "pixc"
PIXC_README_PATH = PIXC_DOCS_DIR / "README.md"
PIXC_GETTING_STARTED_PATH = PIXC_DOCS_DIR / "GETTING_STARTED.md"
PIXC_INSPECTION_GUIDE_PATH = PIXC_DOCS_DIR / "INSPECTION_GUIDE.md"
DEFAULT_INSPECTION_OUTPUT_DIR = PROJECT_ROOT / "PIXC_Processing" / "00_logs" / "inspection"
SPATIAL_MODE_BBOX = "Bounding box"
SPATIAL_MODE_UTM = "UTM tile (approx bbox)"
SPATIAL_MODE_REFERENCE = "SWOT reference tile(s)"
SPATIAL_MODE_NONE = "No spatial filter"
DOWNLOAD_STATUS_FILTER_ALL = "All preview rows"
DOWNLOAD_STATUS_FILTER_MATCHED = "Matched only"
DOWNLOAD_STATUS_FILTER_COMPLETE = "Already complete/skipped"
DOWNLOAD_STATUS_FILTER_EXCLUDED = "Excluded rows"
DOWNLOAD_STATUS_FILTER_OPTIONS = (
    DOWNLOAD_STATUS_FILTER_ALL,
    DOWNLOAD_STATUS_FILTER_MATCHED,
    DOWNLOAD_STATUS_FILTER_COMPLETE,
    DOWNLOAD_STATUS_FILTER_EXCLUDED,
)
DOWNLOAD_TABLE_COMPLETE_STATUSES = {
    "DOWNLOADED",
    "LOCAL_COMPLETE",
    "SKIPPED_EXISTING",
    "SKIPPED_MANIFEST",
}
DOWNLOAD_TABLE_ELIGIBLE_STATUSES = {
    "AUTH_FAILED",
    "CANCELLED",
    "FAILED",
    "MATCHED",
    "MISSING",
}


class PixcApp:
    """Tkinter shell for the future SWOT PIXC workflow."""

    def __init__(self, root: tk.Tk, back_command: Callable[[], None] | None = None) -> None:
        self.root = root
        self.back_command = back_command
        self.root.title("SWOTFlow - Pixel Cloud / PIXC Workflow")
        self.root.geometry("980x720")
        self.root.minsize(760, 520)
        self.current_project_root_var = tk.StringVar(value="")
        self.current_project_name_var = tk.StringVar(value="No project")
        self.project_status_var = tk.StringVar(
            value="No PIXC project is open. Create or open a project before previewing or downloading."
        )
        self.collection_var = tk.StringVar(value=DEFAULT_COLLECTION_LABEL)
        self.download_start_var = tk.StringVar(value="")
        self.download_end_var = tk.StringVar(value="")
        self.download_spatial_mode_var = tk.StringVar(value=SPATIAL_MODE_BBOX)
        self.download_west_var = tk.StringVar(value="")
        self.download_south_var = tk.StringVar(value="")
        self.download_east_var = tk.StringVar(value="")
        self.download_north_var = tk.StringVar(value="")
        self.download_utm_tile_var = tk.StringVar(value="")
        self.download_cycle_var = tk.StringVar(value="")
        self.download_pass_var = tk.StringVar(value="")
        self.download_tiles_var = tk.StringVar(value="")
        self.download_reference_tiles_var = tk.StringVar(value="")
        self.download_max_granules_var = tk.StringVar(value=str(DEFAULT_MAX_GRANULES))
        self.download_output_var = tk.StringVar(value=str(DEFAULT_RAW_DOWNLOADS_DIR))
        self.download_status_var = tk.StringVar(
            value="Enter dates and a small AOI, then preview PIXC granules before downloading."
        )
        self.download_status_filter_var = tk.StringVar(value=DOWNLOAD_STATUS_FILTER_ALL)
        self.download_progress_var = tk.DoubleVar(value=0.0)
        self.download_progress_text_var = tk.StringVar(value="Progress: idle")
        self.visualize_file_var = tk.StringVar(value="")
        self.visualize_attribute_var = tk.StringVar(value="")
        self.visualize_latitude_var = tk.StringVar(value="")
        self.visualize_longitude_var = tk.StringVar(value="")
        self.visualize_max_points_var = tk.StringVar(value=str(DEFAULT_MAX_POINTS))
        self.visualize_status_var = tk.StringVar(
            value="Select one or more downloaded PIXC files, choose variables, then open the point map."
        )
        self.inspect_file_var = tk.StringVar(value="")
        self.inspect_output_var = tk.StringVar(value=str(DEFAULT_INSPECTION_OUTPUT_DIR))
        self.inspect_status_var = tk.StringVar(
            value="Choose a PIXC NetCDF file, then run inspection."
        )
        self.status_var = tk.StringVar(
            value="PIXC workflow ready. Use Download for CMR preview/download and Visualize Points for local point review."
        )
        self.download_tree: ttk.Treeview | None = None
        self.download_preview_item_granules: dict[str, object] = {}
        self.download_preview_button: ttk.Button | None = None
        self.download_missing_button: ttk.Button | None = None
        self.download_preview_map_button: ttk.Button | None = None
        self.download_stop_button: ttk.Button | None = None
        self.download_output_browse_button: ttk.Button | None = None
        self.download_progress_bar: ttk.Progressbar | None = None
        self.download_progress_indeterminate = False
        self.pixc_latest_preview: object | None = None
        self.pixc_latest_config: object | None = None
        self.download_stop_event: threading.Event | None = None
        self.aoi_picker_session: object | None = None
        self.aoi_poll_after_id: str | None = None
        self.active_pixc_project: object | None = None
        self.project_created_at = ""
        self.visualize_attribute_combo: ttk.Combobox | None = None
        self.visualize_latitude_combo: ttk.Combobox | None = None
        self.visualize_longitude_combo: ttk.Combobox | None = None
        self.visualize_files_tree: ttk.Treeview | None = None
        self.visualize_values_tree: ttk.Treeview | None = None
        self.visualize_values_attribute_path = ""
        self.visualize_tree: ttk.Treeview | None = None
        self.visualize_catalog: object | None = None
        self.visualize_file_paths: list[Path] = []
        self.point_viewer_session: object | None = None
        self.inspect_tree: ttk.Treeview | None = None
        self.build_layout()

    def build_layout(self) -> None:
        """Create the initial PIXC workflow shell."""
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="SWOTFlow",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        if self.back_command is not None:
            ttk.Button(
                header,
                text="Back To Products",
                command=self.back_command,
            ).grid(row=0, column=1, sticky="e")

        ttk.Label(
            outer,
            text="Pixel Cloud / PIXC NetCDF point-cloud workflow.",
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 14))

        notebook = ttk.Notebook(outer)
        notebook.grid(row=2, column=0, sticky="nsew")
        self.notebook = notebook

        home_tab = ttk.Frame(notebook, padding=12)
        download_tab = ttk.Frame(notebook, padding=12)
        visualize_tab = ttk.Frame(notebook, padding=12)
        summary_tab = ttk.Frame(notebook, padding=12)

        notebook.add(home_tab, text="Home")
        notebook.add(download_tab, text="Download")
        notebook.add(visualize_tab, text="Visualize Points")
        notebook.add(summary_tab, text="Summary / QA")

        self.build_home_tab(home_tab)
        self.build_download_tab(download_tab)
        self.build_visualize_tab(visualize_tab)
        self.build_summary_tab(summary_tab)
        self.set_download_running(False)

    def build_home_tab(self, parent: ttk.Frame) -> None:
        """Create the PIXC home tab."""
        parent.columnconfigure(0, weight=1)
        product_frame = ttk.LabelFrame(parent, text="Current Product", padding=12)
        product_frame.grid(
            row=0,
            column=0,
            sticky="ew",
        )
        ttk.Label(product_frame, text=PRODUCT_NAME).grid(row=0, column=0, sticky="w")
        ttk.Label(
            product_frame,
            textvariable=self.status_var,
            foreground="#184a8b",
            wraplength=720,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))

        project_frame = ttk.LabelFrame(parent, text="Project", padding=12)
        project_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        project_frame.columnconfigure(1, weight=1)
        ttk.Button(project_frame, text="New Project", command=self.new_pixc_project).grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Button(project_frame, text="Open Project", command=self.open_pixc_project).grid(
            row=0,
            column=1,
            sticky="w",
            padx=(8, 0),
        )
        ttk.Button(project_frame, text="Save Project", command=self.save_pixc_project_action).grid(
            row=0,
            column=2,
            sticky="w",
            padx=(8, 0),
        )
        ttk.Button(project_frame, text="Save Project As", command=self.save_pixc_project_as).grid(
            row=0,
            column=3,
            sticky="w",
            padx=(8, 0),
        )
        ttk.Label(project_frame, text="Name").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(project_frame, textvariable=self.current_project_name_var).grid(
            row=1,
            column=1,
            columnspan=3,
            sticky="ew",
            pady=(10, 0),
        )
        ttk.Label(project_frame, text="Root").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(
            project_frame,
            textvariable=self.current_project_root_var,
            wraplength=700,
            justify="left",
        ).grid(row=2, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(
            project_frame,
            textvariable=self.project_status_var,
            foreground="#184a8b",
            wraplength=720,
            justify="left",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))

        docs_frame = ttk.LabelFrame(parent, text="Documentation", padding=12)
        docs_frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(
            docs_frame,
            text="PIXC README",
            command=lambda: self.open_local_document(PIXC_README_PATH),
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            docs_frame,
            text="Getting Started",
            command=lambda: self.open_local_document(PIXC_GETTING_STARTED_PATH),
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))

    def open_local_document(self, path: Path) -> None:
        """Open a bundled PIXC document in the default browser/viewer."""
        if not path.exists():
            messagebox.showwarning(
                "Document not found",
                f"Could not find:\n{path}",
            )
            return
        webbrowser.open(path.resolve().as_uri())

    def has_active_pixc_project(self) -> bool:
        """Return True when a PIXC project is open."""
        return bool(self.current_project_root_var.get().strip())

    def require_active_pixc_project(self, action: str) -> bool:
        """Require an open PIXC project before writing or running workflow steps."""
        if self.has_active_pixc_project():
            return True
        message = (
            f"Create or open a PIXC project before you {action}.\n\n"
            "PIXC downloads and reports are stored inside the project folder."
        )
        self.project_status_var.set("No PIXC project open. Create or open a project first.")
        messagebox.showwarning("No PIXC project open", message)
        return False

    def default_pixc_project_initial_dir(self) -> str:
        """Return the default parent folder for PIXC project dialogs."""
        parent = DEFAULT_PIXC_PROJECT_PARENT
        if parent.exists():
            return str(parent)
        drive = Path("D:/")
        return str(drive if drive.exists() else PROJECT_ROOT)

    def current_pixc_settings(self) -> dict[str, object]:
        """Return GUI settings that should be persisted in project.yaml."""
        return {
            "collection_label": self.collection_var.get(),
            "download": {
                "start_date": self.download_start_var.get().strip(),
                "end_date": self.download_end_var.get().strip(),
                "spatial_mode": self.download_spatial_mode_var.get(),
                "west": self.download_west_var.get().strip(),
                "south": self.download_south_var.get().strip(),
                "east": self.download_east_var.get().strip(),
                "north": self.download_north_var.get().strip(),
                "utm_tile": self.download_utm_tile_var.get().strip(),
                "cycle": self.download_cycle_var.get().strip(),
                "pass": self.download_pass_var.get().strip(),
                "tiles": self.download_tiles_var.get().strip(),
                "reference_tiles": self.current_reference_tile_names(),
                "max_granules": self.download_max_granules_var.get().strip(),
            },
            "visualize": {
                "input_file": self.visualize_file_var.get().strip(),
                "input_files": [str(path) for path in self.current_visualize_file_paths()],
                "attribute": self.visualize_attribute_var.get().strip(),
                "latitude": self.visualize_latitude_var.get().strip(),
                "longitude": self.visualize_longitude_var.get().strip(),
                "max_points": self.visualize_max_points_var.get().strip(),
            },
            "inspect": {
                "input_file": self.inspect_file_var.get().strip(),
            },
        }

    def apply_pixc_settings(self, settings: object) -> None:
        """Apply persisted project settings to the PIXC GUI."""
        if not isinstance(settings, dict):
            return
        collection_label = str(settings.get("collection_label") or DEFAULT_COLLECTION_LABEL)
        if collection_label in COLLECTION_LABELS:
            self.collection_var.set(collection_label)
        download = settings.get("download", {})
        if isinstance(download, dict):
            self.download_start_var.set(str(download.get("start_date", "") or ""))
            self.download_end_var.set(str(download.get("end_date", "") or ""))
            spatial_mode = str(download.get("spatial_mode", "") or SPATIAL_MODE_BBOX)
            if spatial_mode in {SPATIAL_MODE_BBOX, SPATIAL_MODE_UTM, SPATIAL_MODE_REFERENCE, SPATIAL_MODE_NONE}:
                self.download_spatial_mode_var.set(spatial_mode)
            self.download_west_var.set(str(download.get("west", "") or ""))
            self.download_south_var.set(str(download.get("south", "") or ""))
            self.download_east_var.set(str(download.get("east", "") or ""))
            self.download_north_var.set(str(download.get("north", "") or ""))
            self.download_utm_tile_var.set(str(download.get("utm_tile", "") or ""))
            self.download_cycle_var.set(str(download.get("cycle", "") or ""))
            self.download_pass_var.set(str(download.get("pass", "") or ""))
            self.download_tiles_var.set(str(download.get("tiles", "") or ""))
            reference_tiles = download.get("reference_tiles", [])
            if isinstance(reference_tiles, list):
                self.download_reference_tiles_var.set(", ".join(str(item) for item in reference_tiles))
            else:
                self.download_reference_tiles_var.set(str(reference_tiles or ""))
            self.download_max_granules_var.set(
                str(download.get("max_granules", "") or DEFAULT_MAX_GRANULES)
            )
        inspect = settings.get("inspect", {})
        if isinstance(inspect, dict):
            self.inspect_file_var.set(str(inspect.get("input_file", "") or ""))
        visualize = settings.get("visualize", {})
        if isinstance(visualize, dict):
            input_files = visualize.get("input_files", [])
            if isinstance(input_files, list) and input_files:
                self.set_visualize_file_paths([str(item) for item in input_files if str(item)])
            else:
                self.set_visualize_file_paths([str(visualize.get("input_file", "") or "")])
            self.visualize_attribute_var.set(str(visualize.get("attribute", "") or ""))
            self.visualize_latitude_var.set(str(visualize.get("latitude", "") or ""))
            self.visualize_longitude_var.set(str(visualize.get("longitude", "") or ""))
            self.visualize_max_points_var.set(
                str(visualize.get("max_points", "") or DEFAULT_MAX_POINTS)
            )

    def set_project_paths(self, root: str | Path) -> None:
        """Point PIXC output/report paths at the active project."""
        from .project import pixc_project_paths

        paths = pixc_project_paths(root)
        self.download_output_var.set(str(paths["raw_downloads"]))
        self.inspect_output_var.set(str(paths["inspection"]))

    def apply_pixc_project(self, project: object) -> None:
        """Apply a loaded PIXC project to the GUI."""
        self.active_pixc_project = project
        self.current_project_name_var.set(str(getattr(project, "name", "") or "PIXC Project"))
        self.current_project_root_var.set(str(getattr(project, "root", "")))
        self.project_created_at = str(getattr(project, "created_at", "") or "")
        self.apply_pixc_settings(getattr(project, "settings", {}) or {})
        self.set_project_paths(getattr(project, "root"))
        self.pixc_latest_preview = None
        self.pixc_latest_config = None
        self.visualize_catalog = None
        self.project_status_var.set(f"PIXC project open: {self.current_project_name_var.get()}")
        self.download_status_var.set("Project paths are active. Preview PIXC granules before downloading.")
        self.set_download_running(False)
        self.refresh_visualize_downloaded_files(show_status=False)

    def new_pixc_project(self) -> None:
        """Create a new PIXC project from the current GUI settings."""
        selected = filedialog.askdirectory(
            title="Choose a folder for the new PIXC project",
            initialdir=self.default_pixc_project_initial_dir(),
        )
        if not selected:
            return
        default_name = Path(selected).name or "PIXC Project"
        name = simpledialog.askstring(
            "New PIXC project",
            "Project name:",
            initialvalue=default_name,
            parent=self.root,
        )
        if name is None:
            return
        try:
            from .project import create_pixc_project

            project = create_pixc_project(selected, name, self.current_pixc_settings())
        except Exception as exc:
            messagebox.showerror("Could not create project", str(exc))
            return
        self.apply_pixc_project(project)
        messagebox.showinfo("Project created", f"Created PIXC project:\n{project.project_file}")

    def open_pixc_project(self) -> None:
        """Open a PIXC project.yaml file and populate the GUI."""
        selected = filedialog.askopenfilename(
            title="Open PIXC project.yaml",
            initialdir=self.default_pixc_project_initial_dir(),
            filetypes=[("PIXC project", "project.yaml"), ("YAML files", "*.yaml"), ("All files", "*.*")],
        )
        if not selected:
            return
        try:
            from .project import load_pixc_project

            project = load_pixc_project(selected)
        except Exception as exc:
            messagebox.showerror("Could not open PIXC project", str(exc))
            return
        self.apply_pixc_project(project)
        messagebox.showinfo("Project opened", f"Opened PIXC project:\n{project.project_file}")

    def save_pixc_project_action(self) -> None:
        """Save current GUI state to the active PIXC project."""
        if not self.has_active_pixc_project():
            self.save_pixc_project_as()
            return
        try:
            from .project import PixcProject, save_pixc_project

            project = PixcProject(
                name=self.current_project_name_var.get(),
                root=Path(self.current_project_root_var.get()),
                settings=self.current_pixc_settings(),
                created_at=self.project_created_at,
            )
            path = save_pixc_project(project)
            self.active_pixc_project = project
            self.project_created_at = project.created_at
        except Exception as exc:
            messagebox.showerror("Could not save PIXC project", str(exc))
            return
        self.project_status_var.set(f"Saved PIXC project to {path}")
        messagebox.showinfo("Project saved", f"Saved PIXC project:\n{path}")

    def save_pixc_project_as(self) -> None:
        """Save current settings as a new PIXC project root."""
        selected = filedialog.askdirectory(
            title="Choose a folder for the PIXC project",
            initialdir=self.current_project_root_var.get() or self.default_pixc_project_initial_dir(),
        )
        if not selected:
            return
        default_name = (
            self.current_project_name_var.get()
            if self.has_active_pixc_project()
            else Path(selected).name or "PIXC Project"
        )
        name = simpledialog.askstring(
            "Save PIXC project as",
            "Project name:",
            initialvalue=default_name,
            parent=self.root,
        )
        if name is None:
            return
        try:
            from .project import create_pixc_project

            project = create_pixc_project(selected, name, self.current_pixc_settings())
        except Exception as exc:
            messagebox.showerror("Could not save PIXC project", str(exc))
            return
        self.apply_pixc_project(project)
        messagebox.showinfo("Project saved", f"Saved PIXC project:\n{project.project_file}")

    def save_active_pixc_project_quietly(self) -> None:
        """Persist active project settings without showing a success dialog."""
        if not self.has_active_pixc_project():
            return
        try:
            from .project import PixcProject, save_pixc_project

            project = PixcProject(
                name=self.current_project_name_var.get(),
                root=Path(self.current_project_root_var.get()),
                settings=self.current_pixc_settings(),
                created_at=self.project_created_at,
            )
            save_pixc_project(project)
            self.active_pixc_project = project
            self.project_created_at = project.created_at
        except Exception as exc:
            self.project_status_var.set(f"Could not autosave PIXC project: {exc}")

    def set_date_var(self, variable: tk.StringVar, selected_date: date) -> None:
        """Set a date variable using the project-standard date text format."""
        variable.set(selected_date.isoformat())

    def date_from_text_or_today(self, value: str) -> date:
        """Return a calendar date parsed from GUI text or today's date."""
        text = str(value or "").strip()
        if text:
            try:
                return datetime.fromisoformat(text[:10]).date()
            except ValueError:
                pass
        return date.today()

    def open_date_picker(self, variable: tk.StringVar, title: str) -> None:
        """Open a lightweight stdlib calendar popup for one date field."""
        initial = self.date_from_text_or_today(variable.get())
        popup = tk.Toplevel(self.root)
        popup.title(title)
        popup.transient(self.root)
        popup.resizable(False, False)
        frame = ttk.Frame(popup, padding=10)
        frame.pack(fill="both", expand=True)

        selector_frame = ttk.Frame(frame)
        selector_frame.grid(row=0, column=0, sticky="ew")
        year_var = tk.StringVar(value=str(initial.year))
        month_var = tk.StringVar(value=f"{initial.month:02d}")
        ttk.Label(selector_frame, text="Year").grid(row=0, column=0, sticky="w")
        year_box = ttk.Combobox(
            selector_frame,
            textvariable=year_var,
            values=self.date_picker_year_values(initial.year),
            state="readonly",
            width=8,
        )
        year_box.grid(row=0, column=1, sticky="w", padx=(6, 12))
        ttk.Label(selector_frame, text="Month").grid(row=0, column=2, sticky="w")
        month_box = ttk.Combobox(
            selector_frame,
            textvariable=month_var,
            values=[f"{month:02d}" for month in range(1, 13)],
            state="readonly",
            width=6,
        )
        month_box.grid(row=0, column=3, sticky="w", padx=(6, 0))

        days_frame = ttk.Frame(frame)
        days_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        def rerender_days(*_args: object) -> None:
            try:
                year = int(year_var.get())
                month = int(month_var.get())
            except ValueError:
                return
            self.render_calendar_days(days_frame, variable, popup, year, month)

        year_box.bind("<<ComboboxSelected>>", rerender_days)
        month_box.bind("<<ComboboxSelected>>", rerender_days)
        rerender_days()

        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(
            actions,
            text="Today",
            command=lambda: (
                self.set_date_var(variable, date.today()),
                popup.destroy(),
            ),
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            actions,
            text="Clear",
            command=lambda: (
                variable.set(""),
                popup.destroy(),
            ),
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

    def date_picker_year_values(self, initial_year: int | None = None) -> list[str]:
        """Return date picker years from SWOT availability through next year."""
        current_plus_one = date.today().year + 1
        years = set(range(2022, current_plus_one + 1))
        if initial_year is not None:
            years.add(int(initial_year))
        return [str(year) for year in sorted(years)]

    def set_date_from_picker(
        self,
        variable: tk.StringVar,
        popup: tk.Toplevel,
        year: int,
        month: int,
        day: int,
    ) -> None:
        """Set one date field from picker components and close the popup."""
        self.set_date_var(variable, date(int(year), int(month), int(day)))
        popup.destroy()

    def render_calendar_days(
        self,
        parent: ttk.Frame,
        variable: tk.StringVar,
        popup: tk.Toplevel,
        year: int,
        month: int,
    ) -> None:
        """Render clickable day buttons for one date-picker month."""
        for child in parent.winfo_children():
            child.destroy()
        for column, day_name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            ttk.Label(parent, text=day_name, anchor="center", width=5).grid(
                row=0,
                column=column,
                pady=(0, 2),
            )
        month_calendar = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
        for row_index, week in enumerate(month_calendar, start=1):
            for column, day_number in enumerate(week):
                if day_number == 0:
                    ttk.Label(parent, text="", width=5).grid(row=row_index, column=column)
                    continue
                ttk.Button(
                    parent,
                    text=str(day_number),
                    width=5,
                    command=lambda day=day_number: self.set_date_from_picker(
                        variable,
                        popup,
                        year,
                        month,
                        day,
                    ),
                ).grid(row=row_index, column=column, padx=1, pady=1)

    def build_download_tab(self, parent: ttk.Frame) -> None:
        """Create the PIXC preview/download tab."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(5, weight=1)

        form = ttk.LabelFrame(parent, text="Search Parameters", padding=12)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="Collection").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            form,
            textvariable=self.collection_var,
            values=list(COLLECTION_LABELS.keys()),
            state="readonly",
            width=28,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        ttk.Label(form, text="Start date").grid(row=1, column=0, sticky="w", pady=(8, 0))
        start_date_frame = ttk.Frame(form)
        start_date_frame.grid(
            row=1,
            column=1,
            sticky="w",
            padx=(10, 0),
            pady=(8, 0),
        )
        ttk.Entry(start_date_frame, textvariable=self.download_start_var, width=16).grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Button(
            start_date_frame,
            text="Pick",
            command=lambda: self.open_date_picker(self.download_start_var, "Start date"),
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(form, text="End date").grid(row=1, column=2, sticky="w", padx=(14, 0), pady=(8, 0))
        end_date_frame = ttk.Frame(form)
        end_date_frame.grid(
            row=1,
            column=3,
            sticky="w",
            padx=(10, 0),
            pady=(8, 0),
        )
        ttk.Entry(end_date_frame, textvariable=self.download_end_var, width=16).grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Button(
            end_date_frame,
            text="Pick",
            command=lambda: self.open_date_picker(self.download_end_var, "End date"),
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(form, text="Spatial mode").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            form,
            textvariable=self.download_spatial_mode_var,
            values=[SPATIAL_MODE_BBOX, SPATIAL_MODE_UTM, SPATIAL_MODE_REFERENCE, SPATIAL_MODE_NONE],
            state="readonly",
            width=24,
        ).grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        ttk.Label(form, text="Max granules").grid(row=2, column=2, sticky="w", padx=(14, 0), pady=(8, 0))
        ttk.Entry(form, textvariable=self.download_max_granules_var, width=10).grid(
            row=2,
            column=3,
            sticky="w",
            padx=(10, 0),
            pady=(8, 0),
        )

        bbox_frame = ttk.Frame(form)
        bbox_frame.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        for column in range(8):
            bbox_frame.columnconfigure(column, weight=0)
        ttk.Label(bbox_frame, text="West").grid(row=0, column=0, sticky="w")
        ttk.Entry(bbox_frame, textvariable=self.download_west_var, width=12).grid(
            row=0,
            column=1,
            sticky="w",
            padx=(6, 12),
        )
        ttk.Label(bbox_frame, text="South").grid(row=0, column=2, sticky="w")
        ttk.Entry(bbox_frame, textvariable=self.download_south_var, width=12).grid(
            row=0,
            column=3,
            sticky="w",
            padx=(6, 12),
        )
        ttk.Label(bbox_frame, text="East").grid(row=0, column=4, sticky="w")
        ttk.Entry(bbox_frame, textvariable=self.download_east_var, width=12).grid(
            row=0,
            column=5,
            sticky="w",
            padx=(6, 12),
        )
        ttk.Label(bbox_frame, text="North").grid(row=0, column=6, sticky="w")
        ttk.Entry(bbox_frame, textvariable=self.download_north_var, width=12).grid(
            row=0,
            column=7,
            sticky="w",
            padx=(6, 0),
        )

        ttk.Label(form, text="UTM tile").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(form, textvariable=self.download_utm_tile_var, width=16).grid(
            row=4,
            column=1,
            sticky="w",
            padx=(10, 0),
            pady=(8, 0),
        )
        ttk.Button(
            form,
            text="Apply UTM BBox",
            command=self.apply_download_utm_bbox,
        ).grid(row=4, column=2, sticky="w", padx=(14, 0), pady=(8, 0))
        ttk.Button(
            form,
            text="Open AOI Map",
            command=self.open_download_aoi_map,
        ).grid(row=4, column=3, sticky="w", padx=(8, 0), pady=(8, 0))

        reference_frame = ttk.Frame(form)
        reference_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        reference_frame.columnconfigure(1, weight=1)
        ttk.Label(reference_frame, text="SWOT tiles").grid(row=0, column=0, sticky="w")
        ttk.Entry(reference_frame, textvariable=self.download_reference_tiles_var).grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(10, 0),
        )
        ttk.Button(
            reference_frame,
            text="Open SWOT Tile Map",
            command=self.open_reference_tile_map,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        track_frame = ttk.Frame(form)
        track_frame.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(track_frame, text="Cycle").grid(row=0, column=0, sticky="w")
        ttk.Entry(track_frame, textvariable=self.download_cycle_var, width=10).grid(
            row=0,
            column=1,
            sticky="w",
            padx=(6, 12),
        )
        ttk.Label(track_frame, text="Pass").grid(row=0, column=2, sticky="w")
        ttk.Entry(track_frame, textvariable=self.download_pass_var, width=10).grid(
            row=0,
            column=3,
            sticky="w",
            padx=(6, 12),
        )
        ttk.Label(track_frame, text="Tile(s)").grid(row=0, column=4, sticky="w")
        ttk.Entry(track_frame, textvariable=self.download_tiles_var, width=22).grid(
            row=0,
            column=5,
            sticky="w",
            padx=(6, 0),
        )

        ttk.Label(form, text="Output folder").grid(row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(form, textvariable=self.download_output_var).grid(
            row=7,
            column=1,
            columnspan=2,
            sticky="ew",
            padx=(10, 0),
            pady=(8, 0),
        )
        self.download_output_browse_button = ttk.Button(
            form,
            text="Browse Folder",
            command=self.browse_download_output_folder,
            state="disabled",
        )
        self.download_output_browse_button.grid(row=7, column=3, sticky="w", padx=(8, 0), pady=(8, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        self.download_preview_button = ttk.Button(
            actions,
            text="Preview Search",
            command=self.preview_pixc_download_search,
            state="disabled",
        )
        self.download_preview_button.grid(row=0, column=0, sticky="w")
        self.download_missing_button = ttk.Button(
            actions,
            text="Download Selected Files",
            command=self.download_pixc_missing,
            state="disabled",
        )
        self.download_missing_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.download_preview_map_button = ttk.Button(
            actions,
            text="Show Preview On Map",
            command=self.open_preview_footprint_map,
            state="disabled",
        )
        self.download_preview_map_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.download_stop_button = ttk.Button(
            actions,
            text="Stop Download",
            command=self.stop_pixc_download,
            state="disabled",
        )
        self.download_stop_button.grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Label(
            parent,
            textvariable=self.download_status_var,
            foreground="#184a8b",
            wraplength=820,
            justify="left",
        ).grid(row=2, column=0, sticky="ew", pady=(0, 8))

        progress_frame = ttk.Frame(parent)
        progress_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        progress_frame.columnconfigure(0, weight=1)
        self.download_progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.download_progress_var,
            mode="determinate",
            maximum=100,
        )
        self.download_progress_bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            progress_frame,
            textvariable=self.download_progress_text_var,
            width=34,
            anchor="e",
        ).grid(row=0, column=1, sticky="e", padx=(10, 0))

        filter_frame = ttk.Frame(parent)
        filter_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        filter_frame.columnconfigure(1, weight=1)
        ttk.Label(filter_frame, text="Status filter").grid(row=0, column=0, sticky="w")
        status_filter = ttk.Combobox(
            filter_frame,
            textvariable=self.download_status_filter_var,
            values=list(DOWNLOAD_STATUS_FILTER_OPTIONS),
            state="readonly",
            width=26,
        )
        status_filter.grid(row=0, column=1, sticky="w", padx=(8, 0))
        status_filter.bind("<<ComboboxSelected>>", lambda event: self.apply_download_status_filter())
        ttk.Button(
            filter_frame,
            text="Select Matched",
            command=self.select_matched_download_rows,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Button(
            filter_frame,
            text="Show All",
            command=self.show_all_download_preview_rows,
        ).grid(row=0, column=3, sticky="w", padx=(8, 0))

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=5, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            table_frame,
            columns=("status", "cycle", "pass", "tile", "side", "file_name", "start_time", "end_time", "size_mb"),
            show="headings",
            height=15,
            selectmode="extended",
        )
        tree.heading("status", text="Status")
        tree.heading("cycle", text="Cycle")
        tree.heading("pass", text="Pass")
        tree.heading("tile", text="Tile")
        tree.heading("side", text="Side")
        tree.heading("file_name", text="File")
        tree.heading("start_time", text="Start")
        tree.heading("end_time", text="End")
        tree.heading("size_mb", text="Size MB")
        tree.column("status", width=140, stretch=False)
        tree.column("cycle", width=55, stretch=False)
        tree.column("pass", width=55, stretch=False)
        tree.column("tile", width=65, stretch=False)
        tree.column("side", width=45, stretch=False)
        tree.column("file_name", width=280, stretch=True)
        tree.column("start_time", width=145, stretch=False)
        tree.column("end_time", width=145, stretch=False)
        tree.column("size_mb", width=75, stretch=False)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        self.download_tree = tree

    def browse_download_output_folder(self) -> None:
        """Choose the folder where PIXC NetCDF files are downloaded."""
        if self.has_active_pixc_project():
            messagebox.showinfo(
                "Project-managed folder",
                "PIXC downloads are stored in the active project's 01_raw_downloads folder.",
            )
            self.set_project_paths(self.current_project_root_var.get())
            return
        selected = filedialog.askdirectory(
            title="Choose PIXC raw download folder",
            initialdir=self.download_output_var.get() or str(DEFAULT_RAW_DOWNLOADS_DIR),
        )
        if selected:
            self.download_output_var.set(selected)

    def apply_download_utm_bbox(self) -> None:
        """Convert one UTM tile token to an approximate WGS84 bbox."""
        try:
            from .download import utm_tile_to_bbox

            bbox = utm_tile_to_bbox(self.download_utm_tile_var.get())
        except Exception as exc:
            messagebox.showerror("Invalid UTM tile", str(exc))
            return
        west, south, east, north = bbox
        self.download_spatial_mode_var.set(SPATIAL_MODE_UTM)
        self.download_west_var.set(f"{west:.6f}")
        self.download_south_var.set(f"{south:.6f}")
        self.download_east_var.set(f"{east:.6f}")
        self.download_north_var.set(f"{north:.6f}")
        self.download_status_var.set(
            f"Applied approximate bbox for {self.download_utm_tile_var.get().strip().upper()}."
        )

    def current_download_bbox_or_none(self) -> tuple[float, float, float, float] | None:
        """Return the current bbox fields when all four are valid."""
        values = [
            self.download_west_var.get().strip(),
            self.download_south_var.get().strip(),
            self.download_east_var.get().strip(),
            self.download_north_var.get().strip(),
        ]
        if not any(values):
            return None
        if not all(values):
            return None
        try:
            from .download import parse_bbox_text

            return parse_bbox_text(*values)
        except Exception:
            return None

    def current_reference_tile_names(self) -> list[str]:
        """Return normalized selected SWOT reference tile names."""
        from .reference_tiles import normalize_reference_tile_names

        return normalize_reference_tile_names(self.download_reference_tiles_var.get())

    def set_reference_tile_names(self, tile_names: Sequence[str]) -> None:
        """Set selected SWOT reference tile names in display form."""
        from .reference_tiles import normalize_reference_tile_names

        self.download_reference_tiles_var.set(", ".join(normalize_reference_tile_names(tile_names)))

    def reference_tiles_search_bbox(self, tile_names: Sequence[str]) -> tuple[float, float, float, float] | None:
        """Return a simple union bbox for selected reference tiles when safe."""
        if not tile_names:
            return None
        from .reference_tiles import load_pixc_reference_tiles

        tiles = load_pixc_reference_tiles().require_tiles(tile_names)
        if not tiles or any(tile.crosses_antimeridian for tile in tiles):
            return None
        west = min(tile.bbox[0] for tile in tiles)
        south = min(tile.bbox[1] for tile in tiles)
        east = max(tile.bbox[2] for tile in tiles)
        north = max(tile.bbox[3] for tile in tiles)
        if east - west > 180.0:
            return None
        return west, south, east, north

    def open_download_aoi_map(self) -> None:
        """Open a browser AOI picker with satellite imagery basemap support."""
        try:
            from .aoi_picker import AoiPickerSession

            self.close_aoi_picker_session()
            session = AoiPickerSession(initial_bbox=self.current_download_bbox_or_none())
            url = session.start()
            self.aoi_picker_session = session
            webbrowser.open(url)
            self.download_status_var.set(
                "AOI map opened in the browser. Save one rectangle there to fill the bbox fields."
            )
            self.poll_aoi_picker_selection()
        except Exception as exc:
            messagebox.showerror("AOI map failed", str(exc))

    def open_reference_tile_map(self) -> None:
        """Open the AOI picker in SWOT reference tile selection mode."""
        try:
            from .aoi_picker import AoiPickerSession

            self.close_aoi_picker_session()
            self.download_spatial_mode_var.set(SPATIAL_MODE_REFERENCE)
            session = AoiPickerSession(
                initial_bbox=self.current_download_bbox_or_none(),
                enable_reference_tiles=True,
                selected_reference_tile_names=self.current_reference_tile_names(),
            )
            url = session.start()
            self.aoi_picker_session = session
            webbrowser.open(url)
            self.download_status_var.set(
                "SWOT tile map opened. Draw an AOI, click intersecting tiles, then save the selection."
            )
            self.poll_aoi_picker_selection()
        except Exception as exc:
            messagebox.showerror("SWOT tile map failed", str(exc))

    def open_preview_footprint_map(self) -> None:
        """Open the AOI map with preview granule footprints for selection."""
        if self.pixc_latest_preview is None:
            messagebox.showwarning("No PIXC preview", "Run Preview Search before opening preview footprints.")
            return
        try:
            from .aoi_picker import AoiPickerSession
            from .download import granule_preview_feature

            features = [
                feature
                for granule in getattr(self.pixc_latest_preview, "granules", []) or []
                for feature in [granule_preview_feature(granule)]
                if feature is not None
            ]
            if not features:
                messagebox.showinfo(
                    "No preview footprints",
                    "The current CMR preview did not include usable footprint geometry.",
                )
                return
            self.close_aoi_picker_session()
            session = AoiPickerSession(
                initial_bbox=self.current_download_bbox_or_none(),
                preview_features=features,
            )
            url = session.start()
            self.aoi_picker_session = session
            webbrowser.open(url)
            self.download_status_var.set(
                "Preview footprints opened in the browser. Click granules there and save the selection."
            )
            self.poll_aoi_picker_selection()
        except Exception as exc:
            messagebox.showerror("Preview map failed", str(exc))

    def poll_aoi_picker_selection(self) -> None:
        """Poll the local AOI picker for a saved browser selection."""
        session = self.aoi_picker_session
        if session is None:
            self.aoi_poll_after_id = None
            return
        selection = session.get_selection()
        if selection is not None:
            if getattr(selection, "selected_reference_tile_names", None):
                self.apply_reference_tile_selection(selection.selected_reference_tile_names)
                self.close_aoi_picker_session()
                return
            if getattr(selection, "selected_granule_ids", None):
                self.apply_preview_granule_selection(selection.selected_granule_ids)
                self.close_aoi_picker_session()
                return
            if selection.bbox is not None:
                west, south, east, north = selection.bbox
                self.download_spatial_mode_var.set(SPATIAL_MODE_BBOX)
                self.download_west_var.set(f"{west:.6f}")
                self.download_south_var.set(f"{south:.6f}")
                self.download_east_var.set(f"{east:.6f}")
                self.download_north_var.set(f"{north:.6f}")
                basemap = f" using {selection.basemap}" if selection.basemap else ""
                self.download_status_var.set(f"AOI map bbox applied{basemap}. Preview before downloading.")
            self.close_aoi_picker_session()
            return
        self.aoi_poll_after_id = self.root.after(1000, self.poll_aoi_picker_selection)

    def apply_reference_tile_selection(self, tile_names: list[str]) -> None:
        """Apply selected SWOT reference tiles returned from the browser map."""
        try:
            self.set_reference_tile_names(tile_names)
            selected = self.current_reference_tile_names()
        except Exception as exc:
            messagebox.showerror("Invalid SWOT reference tiles", str(exc))
            return
        self.download_spatial_mode_var.set(SPATIAL_MODE_REFERENCE)
        bbox = self.reference_tiles_search_bbox(selected)
        if bbox is not None:
            west, south, east, north = bbox
            self.download_west_var.set(f"{west:.6f}")
            self.download_south_var.set(f"{south:.6f}")
            self.download_east_var.set(f"{east:.6f}")
            self.download_north_var.set(f"{north:.6f}")
            bbox_text = " Search bbox updated from selected tiles."
        else:
            bbox_text = " No simple bbox was applied; use Cycle for the most efficient exact CMR query."
        self.download_status_var.set(f"Applied {len(selected)} SWOT reference tile(s).{bbox_text}")

    def apply_preview_granule_selection(self, selected_ids: list[str]) -> None:
        """Limit the current preview to granules selected from the map."""
        if self.pixc_latest_preview is None:
            return
        try:
            from .download import apply_preview_granule_selection, preview_statuses_from_existing, write_download_report

            selected_count = apply_preview_granule_selection(self.pixc_latest_preview, selected_ids)
            if self.pixc_latest_config is not None:
                statuses = preview_statuses_from_existing(self.pixc_latest_config, self.pixc_latest_preview)
                write_download_report(self.pixc_latest_config, self.pixc_latest_preview, statuses)
        except Exception as exc:
            messagebox.showerror("Granule selection failed", str(exc))
            return
        self.render_download_preview(self.pixc_latest_preview)
        self.set_download_running(False)
        self.download_status_var.set(
            f"Map selection applied: {selected_count} preview granule(s) selected for download."
        )

    def close_aoi_picker_session(self) -> None:
        """Close any active AOI picker server and polling callback."""
        if self.aoi_poll_after_id is not None:
            try:
                self.root.after_cancel(self.aoi_poll_after_id)
            except Exception:
                pass
            self.aoi_poll_after_id = None
        session = self.aoi_picker_session
        self.aoi_picker_session = None
        if session is not None:
            try:
                session.stop()
            except Exception:
                pass

    def build_pixc_download_config(self) -> object:
        """Build and validate a PIXC download config from GUI fields."""
        from .download import (
            PixcDownloadConfig,
            PixcTrackFilter,
            parse_bbox_text,
            utm_tile_to_bbox,
        )
        from .project import pixc_project_paths

        label = self.collection_var.get()
        collection_short_name = COLLECTION_LABELS.get(label, label)
        try:
            max_granules = int(self.download_max_granules_var.get().strip())
        except ValueError as exc:
            raise ValueError("Max granules must be an integer.") from exc

        if not self.has_active_pixc_project():
            raise ValueError("Create or open a PIXC project before previewing or downloading.")
        project_paths = pixc_project_paths(self.current_project_root_var.get())
        output_folder = project_paths["raw_downloads"]
        report_csv = project_paths["download_report"]
        manifest_csv = project_paths["download_manifest"]
        events_csv = project_paths["download_events"]
        self.download_output_var.set(str(output_folder))

        mode = self.download_spatial_mode_var.get()
        bbox = None
        utm_tile = ""
        reference_tiles: tuple[str, ...] = ()
        if mode == SPATIAL_MODE_BBOX:
            bbox = parse_bbox_text(
                self.download_west_var.get().strip(),
                self.download_south_var.get().strip(),
                self.download_east_var.get().strip(),
                self.download_north_var.get().strip(),
            )
        elif mode == SPATIAL_MODE_UTM:
            utm_tile = self.download_utm_tile_var.get().strip()
            bbox = utm_tile_to_bbox(utm_tile)
            west, south, east, north = bbox
            self.download_west_var.set(f"{west:.6f}")
            self.download_south_var.set(f"{south:.6f}")
            self.download_east_var.set(f"{east:.6f}")
            self.download_north_var.set(f"{north:.6f}")
        elif mode == SPATIAL_MODE_REFERENCE:
            reference_tiles = tuple(self.current_reference_tile_names())
            if not reference_tiles:
                raise ValueError("Select one or more SWOT reference tiles before previewing.")
            bbox = self.current_download_bbox_or_none() or self.reference_tiles_search_bbox(reference_tiles)

        if reference_tiles:
            track_filter = PixcTrackFilter.from_text(cycle=self.download_cycle_var.get().strip())
        else:
            track_filter = PixcTrackFilter.from_text(
                cycle=self.download_cycle_var.get().strip(),
                pass_id=self.download_pass_var.get().strip(),
                tiles=self.download_tiles_var.get().strip(),
            )

        return PixcDownloadConfig(
            collection_short_name=collection_short_name,
            collection_version_label=label,
            output_folder=output_folder,
            start_date=self.download_start_var.get().strip(),
            end_date=self.download_end_var.get().strip(),
            bbox=bbox,
            utm_tile=utm_tile,
            track_filter=track_filter,
            reference_tiles=reference_tiles,
            max_granules=max_granules,
            report_csv=report_csv,
            manifest_csv=manifest_csv,
            events_csv=events_csv,
        )

    def preview_pixc_download_search(self) -> None:
        """Run a PIXC CMR preview search in a background thread."""
        if not self.require_active_pixc_project("preview PIXC downloads"):
            return
        try:
            from .download import validate_config

            config = self.build_pixc_download_config()
            query = validate_config(config)
        except Exception as exc:
            messagebox.showerror("Invalid PIXC search", str(exc))
            return

        if (
            query.bbox is None
            and getattr(query, "track_filter", None) is None
            and not getattr(query, "reference_tiles", ())
        ):
            confirmed = messagebox.askyesno(
                "No spatial filter",
                "No spatial filter is active. Preview will be limited by max granules, but the CMR match count may be very large. Continue?",
            )
            if not confirmed:
                return

        self.pixc_latest_config = config
        self.pixc_latest_preview = None
        self.save_active_pixc_project_quietly()
        self.start_pixc_background_task(
            "Previewing PIXC granules...",
            lambda stop_event: self.run_pixc_preview_worker(config, stop_event),
            self.finish_pixc_preview,
        )

    def run_pixc_preview_worker(self, config: object, stop_event: threading.Event) -> object:
        """Worker body for PIXC preview."""
        from .download import preview_pixc_granules

        return preview_pixc_granules(
            config,
            progress_callback=self.threadsafe_download_status,
            stop_event=stop_event,
        )

    def download_pixc_missing(self) -> None:
        """Download selected files from the current PIXC preview."""
        if not self.require_active_pixc_project("download PIXC files"):
            return
        if self.pixc_latest_preview is None or self.pixc_latest_config is None:
            messagebox.showwarning("No PIXC preview", "Run Preview Search before downloading.")
            return
        self.sync_download_selection_from_table()
        if not list(getattr(self.pixc_latest_preview, "selected_granules", []) or []):
            messagebox.showwarning("No selected granules", "The current preview has no granules selected for download.")
            return
        self.save_active_pixc_project_quietly()
        self.start_pixc_background_task(
            "Downloading selected PIXC files...",
            lambda stop_event: self.run_pixc_download_worker(
                self.pixc_latest_config,
                self.pixc_latest_preview,
                stop_event,
            ),
            self.finish_pixc_download,
        )

    def run_pixc_download_worker(
        self,
        config: object,
        preview: object,
        stop_event: threading.Event,
    ) -> object:
        """Worker body for PIXC download."""
        from .download import download_pixc_granules

        return download_pixc_granules(
            config,
            preview=preview,
            progress_callback=self.threadsafe_download_status,
            stop_event=stop_event,
        )

    def start_pixc_background_task(
        self,
        message: str,
        worker: Callable[[threading.Event], object],
        on_success: Callable[[object], None],
    ) -> None:
        """Run a PIXC download task without blocking Tkinter."""
        stop_event = threading.Event()
        self.download_stop_event = stop_event
        self.set_download_running(True)
        self.download_status_var.set(message)
        self.reset_pixc_download_progress(message, indeterminate=True)

        def run() -> None:
            try:
                result = worker(stop_event)
            except Exception as exc:
                error = str(exc)
                self.root.after(0, lambda: self.finish_pixc_background_error(error))
            else:
                self.root.after(0, lambda: on_success(result))

        threading.Thread(target=run, daemon=True).start()

    def stop_pixc_download(self) -> None:
        """Request cancellation for the active PIXC background task."""
        if self.download_stop_event is not None:
            self.download_stop_event.set()
            self.download_status_var.set("Stop requested. Waiting for the active PIXC step to finish.")
            self.download_progress_text_var.set("Progress: stop requested")

    def threadsafe_download_status(self, current: int, total: int, message: str) -> None:
        """Schedule download status updates from a worker thread."""
        self.root.after(0, self.update_pixc_download_progress, current, total, message)

    def reset_pixc_download_progress(self, message: str = "Progress: idle", indeterminate: bool = False) -> None:
        """Reset the PIXC progress bar at the beginning of a background task."""
        self.download_progress_var.set(0.0)
        self.download_progress_text_var.set(message if message.startswith("Progress:") else f"Progress: {message}")
        if indeterminate:
            self.set_pixc_download_progress_indeterminate(True)
        else:
            self.set_pixc_download_progress_indeterminate(False)
            if self.download_progress_bar is not None:
                self.download_progress_bar.configure(maximum=100)

    def set_pixc_download_progress_indeterminate(self, active: bool) -> None:
        """Switch the PIXC progress bar between indeterminate and determinate modes."""
        if self.download_progress_bar is None:
            self.download_progress_indeterminate = active
            return
        if active:
            if not self.download_progress_indeterminate:
                self.download_progress_bar.configure(mode="indeterminate", maximum=100)
                self.download_progress_bar.start(12)
                self.download_progress_indeterminate = True
            return
        if self.download_progress_indeterminate:
            self.download_progress_bar.stop()
            self.download_progress_indeterminate = False
        self.download_progress_bar.configure(mode="determinate")

    def update_pixc_download_progress(self, current: int, total: int, message: str) -> None:
        """Update the PIXC progress bar and status text from a backend callback."""
        try:
            current_value = max(0, int(current or 0))
        except (TypeError, ValueError):
            current_value = 0
        try:
            total_value = max(0, int(total or 0))
        except (TypeError, ValueError):
            total_value = 0
        message_text = str(message or "").strip()
        if total_value > 0:
            current_value = min(current_value, total_value)
            self.set_pixc_download_progress_indeterminate(False)
            if self.download_progress_bar is not None:
                self.download_progress_bar.configure(maximum=total_value)
            self.download_progress_var.set(float(current_value))
            progress_text = f"Progress: {current_value}/{total_value}"
            status_text = f"{current_value}/{total_value} {message_text}".strip()
        else:
            self.set_pixc_download_progress_indeterminate(True)
            progress_text = "Progress: working"
            status_text = message_text
        if message_text:
            progress_text = f"{progress_text} - {message_text}"
        self.download_progress_text_var.set(progress_text)
        if status_text:
            self.download_status_var.set(status_text)

    def complete_pixc_download_progress(self, message: str) -> None:
        """Mark the PIXC progress bar complete."""
        self.set_pixc_download_progress_indeterminate(False)
        if self.download_progress_bar is not None:
            self.download_progress_bar.configure(maximum=100)
        self.download_progress_var.set(100.0)
        self.download_progress_text_var.set(f"Progress: {message}")

    def fail_pixc_download_progress(self, message: str) -> None:
        """Mark the PIXC progress bar as stopped after a failure."""
        self.set_pixc_download_progress_indeterminate(False)
        self.download_progress_var.set(0.0)
        self.download_progress_text_var.set(f"Progress: {message}")

    def finish_pixc_background_error(self, error: str) -> None:
        """Handle a failed PIXC background task."""
        self.set_download_running(False)
        self.fail_pixc_download_progress("failed")
        try:
            from .download import append_download_event

            if self.pixc_latest_config is not None:
                append_download_event(self.pixc_latest_config, "ERROR", "operation_failed", error)
        except Exception:
            pass
        self.download_status_var.set(f"PIXC operation failed: {error}")
        messagebox.showerror("PIXC operation failed", error)

    def finish_pixc_preview(self, preview: object) -> None:
        """Render a completed PIXC preview."""
        self.pixc_latest_preview = preview
        self.set_download_running(False)
        self.render_download_preview(preview)
        self.complete_pixc_download_progress("preview complete")
        self.download_status_var.set(self.summarize_download_preview(preview))

    def finish_pixc_download(self, result: object) -> None:
        """Render a completed PIXC download run."""
        preview = getattr(result, "preview", None)
        if preview is not None:
            self.pixc_latest_preview = preview
            self.render_download_preview(preview)
        self.set_download_running(False)
        self.complete_pixc_download_progress("download complete")
        downloaded_count = len(getattr(result, "downloaded_files", []) or [])
        skipped_count = len(getattr(result, "skipped_existing", []) or []) + len(
            getattr(result, "skipped_manifest", []) or []
        )
        missing_count = len(getattr(result, "missing_granules", []) or [])
        report_csv = getattr(result, "report_csv", None)
        self.download_status_var.set(
            f"Download finished: {downloaded_count} downloaded, {skipped_count} already complete/skipped, "
            f"{missing_count} not complete. Report: {report_csv}"
        )
        self.refresh_visualize_downloaded_files(show_status=False)

    def set_download_running(self, running: bool) -> None:
        """Enable/disable PIXC download controls for a background task."""
        if self.download_preview_button is not None:
            can_preview = self.has_active_pixc_project()
            self.download_preview_button.configure(
                state="disabled" if running or not can_preview else "normal"
            )
        if self.download_missing_button is not None:
            has_preview = self.pixc_latest_preview is not None and self.has_active_pixc_project()
            state = "disabled" if running or not has_preview else "normal"
            self.download_missing_button.configure(state=state)
        if self.download_preview_map_button is not None:
            has_preview = self.pixc_latest_preview is not None and self.has_active_pixc_project()
            self.download_preview_map_button.configure(
                state="disabled" if running or not has_preview else "normal"
            )
        if self.download_stop_button is not None:
            self.download_stop_button.configure(state="normal" if running else "disabled")
        if self.download_output_browse_button is not None:
            self.download_output_browse_button.configure(state="disabled")
        if not running:
            self.download_stop_event = None

    def download_granule_status(self, granule: object) -> str:
        """Return the preview status used by the download table."""
        return str(getattr(granule, "local_status", "") or "").strip().upper()

    def is_download_table_status_eligible(self, granule: object) -> bool:
        """Return True when a table row can be selected for a download attempt."""
        return self.download_granule_status(granule) in DOWNLOAD_TABLE_ELIGIBLE_STATUSES

    def is_download_granule_visible(self, granule: object) -> bool:
        """Return True when a preview row should be rendered for the active status filter."""
        status = self.download_granule_status(granule)
        filter_value = self.download_status_filter_var.get()
        if filter_value == DOWNLOAD_STATUS_FILTER_MATCHED:
            return status == "MATCHED"
        if filter_value == DOWNLOAD_STATUS_FILTER_COMPLETE:
            return status in DOWNLOAD_TABLE_COMPLETE_STATUSES
        if filter_value == DOWNLOAD_STATUS_FILTER_EXCLUDED:
            return status.startswith("EXCLUDED") or not bool(getattr(granule, "selected_for_download", True))
        return True

    def show_all_download_preview_rows(self) -> None:
        """Reset the preview table status filter."""
        self.download_status_filter_var.set(DOWNLOAD_STATUS_FILTER_ALL)
        self.apply_download_status_filter()

    def apply_download_status_filter(self) -> None:
        """Re-render the preview table with the selected status filter."""
        if self.pixc_latest_preview is None:
            return
        total_rows = len(list(getattr(self.pixc_latest_preview, "granules", []) or []))
        self.render_download_preview(self.pixc_latest_preview)
        visible_rows = 0 if self.download_tree is None else len(self.download_tree.get_children())
        self.download_status_var.set(
            f"Showing {visible_rows} of {total_rows} preview row(s) with filter: "
            f"{self.download_status_filter_var.get()}."
        )

    def set_download_preview_selection(self, predicate: Callable[[object], bool]) -> int:
        """Apply a predicate to the current preview and mark matching rows for download."""
        if self.pixc_latest_preview is None:
            return 0
        selected_count = 0
        for granule in getattr(self.pixc_latest_preview, "granules", []) or []:
            excluded_older = getattr(granule, "duplicate_filter_status", "") == "excluded_older_version"
            selected = bool(predicate(granule)) and not excluded_older
            setattr(granule, "selected_for_download", selected)
            if selected:
                selected_count += 1
        self.render_download_preview(self.pixc_latest_preview)
        self.select_rendered_download_granules(
            lambda granule: bool(getattr(granule, "selected_for_download", False))
        )
        return selected_count

    def select_rendered_download_granules(self, predicate: Callable[[object], bool]) -> int:
        """Highlight rendered preview rows that match a predicate."""
        if self.download_tree is None:
            return 0
        selected_items: list[str] = []
        for item_id, granule in self.download_preview_item_granules.items():
            if predicate(granule):
                selected_items.append(item_id)
        if selected_items:
            self.download_tree.selection_set(selected_items)
        else:
            self.download_tree.selection_remove(self.download_tree.selection())
        if selected_items:
            self.download_tree.focus(selected_items[0])
            self.download_tree.see(selected_items[0])
        return len(selected_items)

    def select_matched_download_rows(self) -> None:
        """Filter to MATCHED preview rows and select them for download."""
        if self.pixc_latest_preview is None:
            messagebox.showwarning("No PIXC preview", "Run Preview Search before selecting matched rows.")
            return
        self.download_status_filter_var.set(DOWNLOAD_STATUS_FILTER_MATCHED)
        selected_count = self.set_download_preview_selection(
            lambda granule: self.download_granule_status(granule) == "MATCHED"
        )
        self.set_download_running(False)
        self.download_status_var.set(
            f"Selected {selected_count} MATCHED preview row(s) for download. "
            "Already downloaded and excluded rows are not selected."
        )

    def sync_download_selection_from_table(self) -> int:
        """Use highlighted preview table rows as the current backend download selection."""
        if self.download_tree is None or self.pixc_latest_preview is None:
            return 0
        selected_item_ids = set(self.download_tree.selection())
        if not selected_item_ids:
            return len(list(getattr(self.pixc_latest_preview, "selected_granules", []) or []))
        selected_granule_ids = {
            id(self.download_preview_item_granules[item_id])
            for item_id in selected_item_ids
            if item_id in self.download_preview_item_granules
        }
        selected_count = self.set_download_preview_selection(
            lambda granule: id(granule) in selected_granule_ids
            and self.is_download_table_status_eligible(granule)
        )
        return selected_count

    def render_download_preview(self, preview: object) -> None:
        """Render PIXC preview rows in the download table."""
        if self.download_tree is None:
            return
        tree = self.download_tree
        self.download_preview_item_granules = {}
        for item in tree.get_children():
            tree.delete(item)
        for granule in getattr(preview, "granules", []) or []:
            if not self.is_download_granule_visible(granule):
                continue
            size_mb = getattr(granule, "size_mb", None)
            size_text = "" if size_mb is None else f"{float(size_mb):.3f}"
            item_id = tree.insert(
                "",
                "end",
                values=(
                    getattr(granule, "local_status", ""),
                    "" if getattr(granule, "cycle_id", None) is None else str(getattr(granule, "cycle_id", "")),
                    "" if getattr(granule, "pass_id", None) is None else str(getattr(granule, "pass_id", "")),
                    getattr(granule, "tile_id", ""),
                    getattr(granule, "swath_side", ""),
                    getattr(granule, "file_name", ""),
                    getattr(granule, "start_time", ""),
                    getattr(granule, "end_time", ""),
                    size_text,
                ),
            )
            self.download_preview_item_granules[item_id] = granule

    def summarize_download_preview(self, preview: object) -> str:
        """Return a compact status line for a PIXC preview."""
        from .download import format_size

        granules = list(getattr(preview, "granules", []) or [])
        selected = list(getattr(preview, "selected_granules", []) or [])
        total_hits = getattr(preview, "total_hits", None)
        report_csv = getattr(preview, "report_csv", None)
        known_size = getattr(preview, "selected_known_size_mb", 0.0)
        missing_sizes = getattr(preview, "selected_missing_size_count", 0)
        hit_text = f"{len(granules)} previewed"
        if total_hits is not None:
            hit_text = f"{len(granules)} previewed from {total_hits} CMR match(es)"
        warnings = " ".join(getattr(preview, "warnings", []) or [])
        return (
            f"Preview complete: {hit_text}; {len(selected)} selected; "
            f"known selected size {format_size(float(known_size), int(missing_sizes))}. "
            f"Report: {report_csv}. {warnings}"
        ).strip()

    def set_visualize_file_paths(self, paths: Sequence[str | Path]) -> None:
        """Set one or more visualizer input files and update the display field."""
        normalized: list[Path] = []
        seen: set[str] = set()
        for value in paths:
            text = str(value or "").strip()
            if not text:
                continue
            path = Path(text)
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(path)
        self.visualize_file_paths = normalized
        if not normalized:
            self.visualize_file_var.set("")
        elif len(normalized) == 1:
            self.visualize_file_var.set(str(normalized[0]))
        else:
            self.visualize_file_var.set(f"{len(normalized)} selected PIXC files")

    def current_visualize_file_paths(self) -> list[Path]:
        """Return the visualizer input file paths."""
        if self.visualize_file_paths:
            return list(self.visualize_file_paths)
        text = self.visualize_file_var.get().strip()
        if not text:
            return []
        if " selected PIXC files" in text:
            return []
        return [Path(text)]

    def build_visualize_tab(self, parent: ttk.Frame) -> None:
        """Create the PIXC point visualization tab."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        files_frame = ttk.LabelFrame(parent, text="Project Downloads", padding=8)
        files_frame.grid(row=0, column=0, sticky="nsew")
        files_frame.columnconfigure(0, weight=1)
        files_frame.rowconfigure(0, weight=1)
        files_tree = ttk.Treeview(
            files_frame,
            columns=("status", "size_mb", "last_downloaded", "path"),
            show="tree headings",
            height=6,
            selectmode="extended",
        )
        files_tree.heading("#0", text="File")
        files_tree.heading("status", text="Status")
        files_tree.heading("size_mb", text="Size MB")
        files_tree.heading("last_downloaded", text="Last Downloaded")
        files_tree.heading("path", text="")
        files_tree.column("#0", width=330, stretch=True)
        files_tree.column("status", width=110, stretch=False)
        files_tree.column("size_mb", width=75, stretch=False)
        files_tree.column("last_downloaded", width=150, stretch=False)
        files_tree.column("path", width=0, minwidth=0, stretch=False)
        files_tree.grid(row=0, column=0, columnspan=4, sticky="nsew")
        files_tree.bind("<Double-1>", lambda event: self.use_selected_visualize_downloads())
        files_scrollbar = ttk.Scrollbar(files_frame, orient="vertical", command=files_tree.yview)
        files_scrollbar.grid(row=0, column=4, sticky="ns")
        files_tree.configure(yscrollcommand=files_scrollbar.set)
        self.visualize_files_tree = files_tree
        ttk.Button(
            files_frame,
            text="Refresh Files",
            command=self.refresh_visualize_downloaded_files,
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(
            files_frame,
            text="Use Selected Files",
            command=self.use_selected_visualize_downloads,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(
            files_frame,
            text="Browse Point Files",
            command=self.browse_visualize_file,
        ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0))

        form = ttk.LabelFrame(parent, text="Point Map", padding=12)
        form.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text="Selected file(s)").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.visualize_file_var, state="readonly").grid(
            row=0,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(10, 0),
        )

        ttk.Label(form, text="Attribute").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.visualize_attribute_combo = ttk.Combobox(
            form,
            textvariable=self.visualize_attribute_var,
            values=[],
            state="normal",
        )
        self.visualize_attribute_combo.bind("<<ComboboxSelected>>", lambda event: self.on_visualize_attribute_changed())
        self.visualize_attribute_combo.grid(
            row=1,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(10, 0),
            pady=(8, 0),
        )
        ttk.Button(
            form,
            text="Load Variables",
            command=self.load_visualize_variables,
        ).grid(row=1, column=4, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Label(form, text="Latitude").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.visualize_latitude_combo = ttk.Combobox(
            form,
            textvariable=self.visualize_latitude_var,
            values=[],
            state="normal",
            width=34,
        )
        self.visualize_latitude_combo.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=(8, 0))
        ttk.Label(form, text="Longitude").grid(row=2, column=2, sticky="w", padx=(14, 0), pady=(8, 0))
        self.visualize_longitude_combo = ttk.Combobox(
            form,
            textvariable=self.visualize_longitude_var,
            values=[],
            state="normal",
            width=34,
        )
        self.visualize_longitude_combo.grid(row=2, column=3, sticky="ew", padx=(10, 0), pady=(8, 0))

        ttk.Label(form, text="Max points/file").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(form, textvariable=self.visualize_max_points_var, width=12).grid(
            row=3,
            column=1,
            sticky="w",
            padx=(10, 0),
            pady=(8, 0),
        )
        values_frame = ttk.LabelFrame(parent, text="Attribute Values Filter", padding=8)
        values_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        values_frame.columnconfigure(0, weight=1)
        values_tree = ttk.Treeview(
            values_frame,
            columns=("meaning", "count"),
            show="tree headings",
            height=5,
            selectmode="extended",
        )
        values_tree.heading("#0", text="Value")
        values_tree.heading("meaning", text="Meaning")
        values_tree.heading("count", text="Count")
        values_tree.column("#0", width=180, stretch=False)
        values_tree.column("meaning", width=320, stretch=True)
        values_tree.column("count", width=90, stretch=False)
        values_tree.grid(row=0, column=0, columnspan=4, sticky="ew")
        values_scrollbar = ttk.Scrollbar(values_frame, orient="vertical", command=values_tree.yview)
        values_scrollbar.grid(row=0, column=4, sticky="ns")
        values_tree.configure(yscrollcommand=values_scrollbar.set)
        self.visualize_values_tree = values_tree
        ttk.Button(
            values_frame,
            text="Load Values",
            command=self.load_visualize_attribute_values,
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(
            values_frame,
            text="Select All Values",
            command=self.select_all_visualize_attribute_values,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(
            values_frame,
            text="Clear Values",
            command=self.clear_visualize_attribute_values,
        ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(
            values_frame,
            text="Open Point Map",
            command=self.open_visualize_point_map,
        ).grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(8, 0))

        table_frame = ttk.LabelFrame(parent, text="Variable Details", padding=8)
        table_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            table_frame,
            columns=("dtype", "shape", "size", "units"),
            show="tree headings",
            height=9,
        )
        tree.heading("#0", text="Variable")
        tree.heading("dtype", text="DType")
        tree.heading("shape", text="Shape")
        tree.heading("size", text="Size")
        tree.heading("units", text="Units")
        tree.column("#0", width=360, stretch=True)
        tree.column("dtype", width=100, stretch=False)
        tree.column("shape", width=120, stretch=False)
        tree.column("size", width=90, stretch=False)
        tree.column("units", width=120, stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        self.visualize_tree = tree

        ttk.Label(
            parent,
            textvariable=self.visualize_status_var,
            foreground="#184a8b",
            wraplength=820,
            justify="left",
        ).grid(row=4, column=0, sticky="ew", pady=(10, 0))

    def project_download_inventory(self) -> list[object]:
        """Return local/downloaded files known to the active PIXC project."""
        if not self.has_active_pixc_project():
            return []
        from .download import list_downloaded_pixc_files
        from .project import pixc_project_paths

        paths = pixc_project_paths(self.current_project_root_var.get())
        return list_downloaded_pixc_files(paths["raw_downloads"], paths["download_manifest"])

    def refresh_visualize_downloaded_files(self, show_status: bool = True) -> None:
        """Refresh the visualizer's project download inventory table."""
        if self.visualize_files_tree is None:
            return
        tree = self.visualize_files_tree
        for item in tree.get_children():
            tree.delete(item)
        try:
            files = self.project_download_inventory()
        except Exception as exc:
            if show_status:
                self.visualize_status_var.set(f"Could not read project downloads: {exc}")
            return
        local_files = [row for row in files if bool(getattr(row, "raw_exists", False))]
        for row in local_files:
            tree.insert(
                "",
                "end",
                text=str(getattr(row, "file_name", "")),
                values=(
                    "Downloaded",
                    str(getattr(row, "size_mb", "")),
                    str(getattr(row, "last_downloaded", "")),
                    str(getattr(row, "local_path", "")),
                ),
            )
        if local_files:
            first = tree.get_children()[0]
            tree.selection_set(first)
            tree.focus(first)
        if show_status:
            self.visualize_status_var.set(f"Found {len(local_files)} downloaded PIXC file(s) in this project.")

    def use_selected_visualize_downloads(self) -> None:
        """Use selected project downloads as visualizer input files."""
        if self.visualize_files_tree is None:
            return
        selection = self.visualize_files_tree.selection()
        if not selection:
            messagebox.showwarning("No file selected", "Select one or more project downloads first.")
            return
        paths: list[Path] = []
        missing: list[str] = []
        for item in selection:
            values = self.visualize_files_tree.item(item, "values")
            local_path = str(values[3] if len(values) >= 4 else "").strip()
            status = str(values[0] if values else "").strip()
            if not local_path:
                continue
            path = Path(local_path)
            if path.exists():
                paths.append(path)
            else:
                missing.append(f"{path} ({status})")
        if missing:
            messagebox.showwarning(
                "Local file missing",
                "These manifest-tracked files are not present locally:\n" + "\n".join(missing[:5]),
            )
        if not paths:
            self.visualize_status_var.set("No selected project downloads are present locally.")
            return
        self.set_visualize_file_paths(paths)
        self.visualize_attribute_var.set("")
        self.visualize_latitude_var.set("")
        self.visualize_longitude_var.set("")
        self.load_visualize_variables()

    def visualize_initial_dir(self) -> str:
        """Return the initial folder for PIXC point file dialogs."""
        if self.has_active_pixc_project():
            try:
                from .project import pixc_project_paths

                raw = pixc_project_paths(self.current_project_root_var.get())["raw_downloads"]
                if raw.exists():
                    return str(raw)
            except Exception:
                pass
        return self.current_project_root_var.get() or str(DEFAULT_RAW_DOWNLOADS_DIR)

    def browse_visualize_file(self) -> None:
        """Choose one or more PIXC NetCDF files for point visualization."""
        selected = filedialog.askopenfilenames(
            title="Choose PIXC NetCDF files",
            initialdir=self.visualize_initial_dir(),
            filetypes=[("NetCDF files", "*.nc *.nc4"), ("All files", "*.*")],
        )
        if selected:
            self.set_visualize_file_paths(selected)
            self.load_visualize_variables()

    def load_visualize_variables(self) -> bool:
        """Discover variables for the selected PIXC point file."""
        if not self.require_active_pixc_project("visualize PIXC files"):
            return False
        input_paths = self.current_visualize_file_paths()
        if not input_paths:
            messagebox.showerror("Missing PIXC NetCDF", "Choose one or more PIXC NetCDF files to visualize.")
            return False
        input_path = input_paths[0]
        try:
            from .visualize import discover_pixc_point_variables

            catalog = discover_pixc_point_variables(input_path)
        except Exception as exc:
            self.visualize_status_var.set(f"Variable discovery failed: {exc}")
            messagebox.showerror("PIXC variable discovery failed", str(exc))
            return False

        self.visualize_catalog = catalog
        paths = [str(variable.path) for variable in getattr(catalog, "variables", [])]
        attribute_paths = list(getattr(catalog, "point_attribute_paths", []) or paths)
        coordinate_paths = paths
        if self.visualize_attribute_combo is not None:
            self.visualize_attribute_combo.configure(values=attribute_paths)
        if self.visualize_latitude_combo is not None:
            self.visualize_latitude_combo.configure(values=coordinate_paths)
        if self.visualize_longitude_combo is not None:
            self.visualize_longitude_combo.configure(values=coordinate_paths)
        if not self.visualize_latitude_var.get().strip() and getattr(catalog, "latitude_path", ""):
            self.visualize_latitude_var.set(str(catalog.latitude_path))
        if not self.visualize_longitude_var.get().strip() and getattr(catalog, "longitude_path", ""):
            self.visualize_longitude_var.set(str(catalog.longitude_path))
        if not self.visualize_attribute_var.get().strip() and getattr(catalog, "default_attribute_path", ""):
            self.visualize_attribute_var.set(str(catalog.default_attribute_path))
        self.render_visualize_variables(catalog)
        self.load_visualize_attribute_values(show_errors=False, update_status=False)
        suffix = f" Schema read from {input_path.name}." if len(input_paths) > 1 else ""
        self.visualize_status_var.set(
            f"Loaded {len(paths)} variable(s) for {len(input_paths)} file(s). Choose an attribute, then open the point map.{suffix}"
        )
        self.save_active_pixc_project_quietly()
        return True

    def render_visualize_variables(self, catalog: object) -> None:
        """Render discovered variables in the visualizer table."""
        if self.visualize_tree is None:
            return
        tree = self.visualize_tree
        for item in tree.get_children():
            tree.delete(item)
        for variable in getattr(catalog, "variables", []) or []:
            shape = "x".join(str(part) for part in getattr(variable, "shape", ()) or ())
            tree.insert(
                "",
                "end",
                text=str(getattr(variable, "path", "")),
                values=(
                    str(getattr(variable, "dtype", "")),
                    shape,
                    str(getattr(variable, "size", "")),
                    str(getattr(variable, "units", "")),
                ),
            )

    def clear_visualize_attribute_value_rows(self) -> None:
        """Clear loaded attribute-value rows."""
        self.visualize_values_attribute_path = ""
        if self.visualize_values_tree is None:
            return
        for item in self.visualize_values_tree.get_children():
            self.visualize_values_tree.delete(item)

    def on_visualize_attribute_changed(self) -> None:
        """Refresh value filters after the selected attribute changes."""
        self.clear_visualize_attribute_value_rows()
        self.load_visualize_attribute_values(show_errors=False)

    def load_visualize_attribute_values(
        self,
        *,
        show_errors: bool = True,
        update_status: bool = True,
    ) -> bool:
        """Load distinct values for the currently selected point attribute."""
        if self.visualize_values_tree is None:
            return False
        input_paths = self.current_visualize_file_paths()
        attribute_path = self.visualize_attribute_var.get().strip()
        self.clear_visualize_attribute_value_rows()
        if not input_paths or not attribute_path:
            if update_status:
                self.visualize_status_var.set("Choose file(s) and an attribute before loading values.")
            return False
        try:
            from .visualize import summarize_pixc_attribute_values

            rows = summarize_pixc_attribute_values(input_paths, attribute_path)
        except Exception as exc:
            if update_status:
                self.visualize_status_var.set(f"Could not load attribute values: {exc}")
            if show_errors:
                messagebox.showwarning("Attribute values unavailable", str(exc))
            return False
        tree = self.visualize_values_tree
        inserted: list[str] = []
        for row in rows:
            item_id = tree.insert(
                "",
                "end",
                text=str(getattr(row, "key", "")),
                values=(
                    str(getattr(row, "meaning", "")),
                    str(getattr(row, "count", "")),
                ),
            )
            inserted.append(item_id)
        if inserted:
            tree.selection_set(inserted)
            tree.focus(inserted[0])
        self.visualize_values_attribute_path = attribute_path
        if update_status:
            self.visualize_status_var.set(
                f"Loaded {len(inserted)} distinct value(s) for {attribute_path}. "
                "Select one or more values, or clear the selection to map all values."
            )
        return True

    def select_all_visualize_attribute_values(self) -> None:
        """Select all loaded attribute values for filtering."""
        if self.visualize_values_tree is None:
            return
        children = self.visualize_values_tree.get_children()
        if not children:
            self.load_visualize_attribute_values()
            return
        self.visualize_values_tree.selection_set(children)
        self.visualize_status_var.set(f"Selected all {len(children)} attribute value(s) for the point map.")

    def clear_visualize_attribute_values(self) -> None:
        """Clear the current attribute-value selection."""
        if self.visualize_values_tree is None:
            return
        self.visualize_values_tree.selection_remove(self.visualize_values_tree.selection())
        self.visualize_status_var.set("Attribute value filter cleared. The point map will use all values.")

    def current_visualize_value_filter_keys(self) -> tuple[str, ...]:
        """Return selected attribute value keys for point-map filtering."""
        if self.visualize_values_tree is None:
            return ()
        if self.visualize_values_attribute_path != self.visualize_attribute_var.get().strip():
            return ()
        selected = self.visualize_values_tree.selection()
        if not selected:
            return ()
        return tuple(str(self.visualize_values_tree.item(item, "text")) for item in selected)

    def build_visualize_config(self) -> object:
        """Build point map config from the visualizer fields."""
        from .visualize import PixcMultiPointMapConfig

        input_paths = self.current_visualize_file_paths()
        if not input_paths:
            raise ValueError("Choose one or more PIXC NetCDF files to visualize.")
        try:
            max_points = int(self.visualize_max_points_var.get().strip())
        except ValueError as exc:
            raise ValueError("Max points per file must be an integer.") from exc
        if max_points <= 0:
            raise ValueError("Max points per file must be greater than zero.")
        return PixcMultiPointMapConfig(
            file_paths=tuple(input_paths),
            attribute_path=self.visualize_attribute_var.get().strip(),
            latitude_path=self.visualize_latitude_var.get().strip(),
            longitude_path=self.visualize_longitude_var.get().strip(),
            max_points_per_file=max_points,
            allowed_value_keys=self.current_visualize_value_filter_keys(),
        )

    def open_visualize_point_map(self) -> None:
        """Build a sampled point layer and open it on a satellite-backed browser map."""
        if not self.require_active_pixc_project("visualize PIXC files"):
            return
        if not self.visualize_attribute_var.get().strip():
            if not self.load_visualize_variables():
                return
        try:
            config = self.build_visualize_config()
        except Exception as exc:
            messagebox.showerror("Invalid PIXC point map", str(exc))
            return
        self.visualize_status_var.set("Building point map sample...")

        def run() -> None:
            try:
                from .visualize import build_pixc_multi_point_map

                map_data = build_pixc_multi_point_map(config)
            except Exception as exc:
                error = str(exc)
                self.root.after(0, lambda: self.finish_visualize_point_map_error(error))
            else:
                self.root.after(0, lambda: self.finish_visualize_point_map(map_data))

        threading.Thread(target=run, daemon=True).start()

    def finish_visualize_point_map_error(self, error: str) -> None:
        """Show point map build errors."""
        self.visualize_status_var.set(f"Point map failed: {error}")
        messagebox.showerror("PIXC point map failed", error)

    def finish_visualize_point_map(self, map_data: object) -> None:
        """Open the browser point map for sampled PIXC data."""
        try:
            from .point_viewer import PointMapSession
            from .project import pixc_project_paths

            if self.point_viewer_session is not None:
                try:
                    self.point_viewer_session.stop()
                except Exception:
                    pass
            project_paths = pixc_project_paths(self.current_project_root_var.get())
            session = PointMapSession(
                map_data,
                reference_log_csv=project_paths["reference_imagery_log"],
            )
            url = session.start()
            self.point_viewer_session = session
            webbrowser.open(url)
        except Exception as exc:
            self.visualize_status_var.set(f"Point map failed: {exc}")
            messagebox.showerror("PIXC point map failed", str(exc))
            return
        file_count = len(getattr(map_data, "file_paths", []) or [getattr(map_data, "file_path", "")])
        self.visualize_status_var.set(
            f"Opened point map with {getattr(map_data, 'rendered_points', 0)} point(s) from {file_count} file(s) "
            f"colored by {getattr(map_data, 'attribute_path', '')}. Use the map viewer to add dated reference imagery."
        )
        self.save_active_pixc_project_quietly()

    def build_inspect_tab(self, parent: ttk.Frame) -> None:
        """Create the initial PIXC inspection tab shell."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        form = ttk.LabelFrame(parent, text="Input And Reports", padding=12)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Input NetCDF").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.inspect_file_var).grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(10, 0),
        )
        ttk.Button(
            form,
            text="Browse NetCDF",
            command=self.browse_inspect_file,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(form, text="Report folder").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(form, textvariable=self.inspect_output_var).grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(10, 0),
            pady=(8, 0),
        )
        ttk.Button(
            form,
            text="Browse Folder",
            command=self.browse_inspect_output_folder,
        ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Button(
            actions,
            text="Inspect File",
            command=self.inspect_selected_file,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            actions,
            textvariable=self.inspect_status_var,
            foreground="#184a8b",
            wraplength=680,
            justify="left",
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        tree = ttk.Treeview(
            parent,
            columns=("value", "status"),
            show="tree headings",
            height=16,
        )
        tree.heading("#0", text="Item")
        tree.heading("value", text="Value")
        tree.heading("status", text="Status")
        tree.column("#0", width=340, stretch=True)
        tree.column("value", width=240, stretch=True)
        tree.column("status", width=160, stretch=True)
        tree.grid(row=2, column=0, sticky="nsew")
        self.inspect_tree = tree

    def browse_inspect_file(self) -> None:
        """Choose one PIXC NetCDF file for inspection."""
        selected = filedialog.askopenfilename(
            title="Choose a PIXC NetCDF file",
            filetypes=[("NetCDF files", "*.nc *.nc4"), ("All files", "*.*")],
        )
        if selected:
            self.inspect_file_var.set(selected)

    def browse_inspect_output_folder(self) -> None:
        """Choose the folder where PIXC inspection reports are written."""
        if self.has_active_pixc_project():
            messagebox.showinfo(
                "Project-managed folder",
                "PIXC inspection reports are stored in the active project's 00_logs\\inspection folder.",
            )
            self.set_project_paths(self.current_project_root_var.get())
            return
        selected = filedialog.askdirectory(
            title="Choose PIXC inspection report folder",
            initialdir=self.inspect_output_var.get() or str(DEFAULT_INSPECTION_OUTPUT_DIR),
        )
        if selected:
            self.inspect_output_var.set(selected)

    def inspect_selected_file(self) -> None:
        """Inspect the selected PIXC NetCDF file and write reports."""
        if not self.require_active_pixc_project("inspect PIXC files"):
            return
        self.set_project_paths(self.current_project_root_var.get())
        input_path = Path(self.inspect_file_var.get().strip())
        output_dir = Path(self.inspect_output_var.get().strip() or DEFAULT_INSPECTION_OUTPUT_DIR)
        if not str(input_path).strip():
            messagebox.showerror("Missing PIXC NetCDF", "Choose a PIXC NetCDF file to inspect.")
            return
        try:
            from .inspect import inspect_netcdf, summarize_for_status, write_inspection_reports

            summary = inspect_netcdf(input_path)
            reports = write_inspection_reports(summary, output_dir)
        except Exception as exc:
            self.inspect_status_var.set(f"Inspection failed: {exc}")
            messagebox.showerror("PIXC inspection failed", str(exc))
            return

        self.render_inspection_summary(summary)
        self.inspect_status_var.set(
            f"{summarize_for_status(summary)} Reports written to {reports.summary_json.parent}."
        )
        self.save_active_pixc_project_quietly()

    def render_inspection_summary(self, summary: object) -> None:
        """Render a compact inspection summary table."""
        if self.inspect_tree is None:
            return
        tree = self.inspect_tree
        for item in tree.get_children():
            tree.delete(item)

        groups = list(getattr(summary, "groups", []) or [])
        dimensions = list(getattr(summary, "dimensions", []) or [])
        variables = list(getattr(summary, "variables", []) or [])
        stats = list(getattr(summary, "variable_stats", []) or [])

        tree.insert("", "end", text="Groups", values=(len(groups), ""))
        tree.insert("", "end", text="Dimensions", values=(len(dimensions), ""))
        tree.insert("", "end", text="Variables", values=(len(variables), ""))
        summarized = [row for row in stats if row.get("status") == "summarized"]
        tree.insert("", "end", text="Summarized key variables", values=(len(summarized), ""))

        for row in variables[:40]:
            shape = "x".join(str(part) for part in row.get("shape", []))
            label = str(row.get("path", ""))
            value = f"{row.get('dtype', '')} {shape}".strip()
            tree.insert("", "end", text=label, values=(value, row.get("stat_kind", "")))

        if len(variables) > 40:
            tree.insert("", "end", text="Additional variables", values=(len(variables) - 40, "not shown"))

    def build_summary_tab(self, parent: ttk.Frame) -> None:
        """Create the initial PIXC summary/QA tab shell."""
        parent.columnconfigure(0, weight=1)
        ttk.Label(
            parent,
            text="PIXC statistics and QA reports will be added after the first file-inspection tools are implemented.",
            foreground="#555555",
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="w")


def launch(root: tk.Tk, back_command: Callable[[], None] | None = None) -> PixcApp:
    """Mount the PIXC workflow shell in the given Tk root."""
    return PixcApp(root, back_command=back_command)
