import csv
import subprocess
import tkinter as tk
from tkinter import ttk
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from swot_download_tool import DownloadConfig
from swotflow_automation import (
    AutomationConfig,
    AutomationRunState,
    AutomationStageResult,
    AutomationTilePlan,
    write_run_state,
)
from swotflow_gui import LauncherApp
from swotflow_project import create_project, load_project_tile_profiles
from utm_map_selector import DisplayTile, UTMDisplayGeometry


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def find_notebook(widget: tk.Widget) -> ttk.Notebook:
    for child in widget.winfo_children():
        if isinstance(child, ttk.Notebook):
            return child
        try:
            return find_notebook(child)
        except LookupError:
            continue
    raise LookupError("No ttk.Notebook found")


def collect_label_texts(widget: tk.Widget) -> set[str]:
    texts: set[str] = set()
    for child in widget.winfo_children():
        if isinstance(child, (ttk.Label, ttk.Checkbutton, ttk.LabelFrame)):
            text = child.cget("text")
            if isinstance(text, str):
                texts.add(text)
        texts.update(collect_label_texts(child))
    return texts


def collect_widgets(widget: tk.Widget, widget_type: type[tk.Widget]) -> list[tk.Widget]:
    matches: list[tk.Widget] = []
    for child in widget.winfo_children():
        if isinstance(child, widget_type):
            matches.append(child)
        matches.extend(collect_widgets(child, widget_type))
    return matches


def collect_button_texts(widget: tk.Widget) -> set[str]:
    texts: set[str] = set()
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            text = child.cget("text")
            if isinstance(text, str):
                texts.add(text)
        texts.update(collect_button_texts(child))
    return texts


def listbox_values(listbox: tk.Listbox) -> list[str]:
    return [str(listbox.get(index)) for index in range(listbox.size())]


def swot_tif_name(tile: str) -> str:
    return (
        f"SWOT_L2_HR_Raster_100m_{tile}_N_x_x_x_"
        "034_266_MOSA_20260102T000000_20260102T010000_PGD0_01.tif"
    )


class GuiLayoutTests(unittest.TestCase):
    def sample_config(self, root: Path) -> dict:
        return {
            "processing": {
                "root": str(root),
                "raw_downloads": str(root / "01_raw_downloads"),
                "extracted_geotiffs": str(root / "02_extracted_geotiffs"),
                "mosaics": str(root / "03_mosaics"),
                "logs": str(root / "00_logs"),
            },
            "input_folder": str(root / "03_mosaics"),
            "destination_parent": "projects/example/assets/collection",
            "download": {
                "start_date": "",
                "end_date": "",
                "utm_tiles": [],
            },
        }

    def test_tab_order_and_gdal_field_placement(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            notebook = find_notebook(app.root)
            tab_ids = notebook.tabs()
            tab_texts = [notebook.tab(tab_id, "text") for tab_id in tab_ids]

            self.assertEqual(
                tab_texts,
                [
                    "Home",
                    "Automation",
                    "Download",
                    "Duplicate Removal",
                    "Extraction",
                    "Mosaic",
                    "Upload",
                    "Statistics",
                    "Cleanup",
                ],
            )
            self.assertIn("New Project", collect_button_texts(app.root))
            self.assertIn("Open Project", collect_button_texts(app.root))
            self.assertIn("Save Project", collect_button_texts(app.root))
            self.assertIn("Prepare Update", collect_button_texts(app.root))
            self.assertIn("Download Data", collect_button_texts(app.root))
            self.assertIn("View Statistics", collect_button_texts(app.root))
            self.assertIn("README", collect_button_texts(app.root))
            self.assertIn("Getting Started", collect_button_texts(app.root))
            self.assertIn("Processing Guide", collect_button_texts(app.root))
            self.assertIn("GitHub", collect_button_texts(app.root))
            self.assertNotIn("Save Config", collect_button_texts(app.root))
            (
                home_tab,
                automation_tab,
                download_tab,
                duplicate_tab,
                extract_tab,
                mosaic_tab,
                upload_tab,
                statistics_tab,
                cleanup_tab,
            ) = notebook.winfo_children()
            self.assertIn("Current Project", collect_label_texts(home_tab))
            self.assertIn("Project And Documentation", collect_label_texts(home_tab))
            self.assertIn("Workflow Shortcuts", collect_label_texts(home_tab))
            self.assertIn("Selected Download Tiles", collect_label_texts(home_tab))
            self.assertIn("Project Folders", collect_label_texts(home_tab))
            self.assertIn("Automation Settings", collect_label_texts(automation_tab))
            self.assertIn("Automation UTM Tiles", collect_label_texts(automation_tab))
            self.assertIn("Automation Queue And Results", collect_label_texts(automation_tab))
            self.assertIn("Authenticate Earthdata", collect_button_texts(automation_tab))
            self.assertIn("Copy Download Date Range", collect_button_texts(automation_tab))
            self.assertIn("Run Preflight", collect_button_texts(automation_tab))
            self.assertIn("Start Automation", collect_button_texts(automation_tab))
            self.assertIn("Stop After Current Stage", collect_button_texts(automation_tab))
            self.assertIn(
                "Start automatically after successful preflight",
                collect_label_texts(automation_tab),
            )
            self.assertGreaterEqual(len(collect_widgets(automation_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(automation_tab, ttk.Treeview)), 1)
            self.assertIn("Collection", collect_label_texts(download_tab))
            self.assertIn("Product version filter", collect_label_texts(download_tab))
            self.assertIn("Start date", collect_label_texts(download_tab))
            self.assertIn("End date", collect_label_texts(download_tab))
            self.assertIn("Batch size", collect_label_texts(download_tab))
            self.assertIn("Download manifest CSV", collect_label_texts(download_tab))
            self.assertIn("Selected tiles", collect_label_texts(download_tab))
            self.assertIn("Tile preset", collect_label_texts(download_tab))
            self.assertIn("Skip files that already exist in the output folder", collect_label_texts(download_tab))
            self.assertIn("Skip granules already recorded in the project download manifest", collect_label_texts(download_tab))
            self.assertIn("Apply Preset", collect_button_texts(download_tab))
            self.assertIn("Save Selected As Preset", collect_button_texts(download_tab))
            self.assertIn("Open UTM Map Selector", collect_button_texts(download_tab))
            self.assertIn("Stop Download", collect_button_texts(download_tab))
            self.assertGreaterEqual(len(collect_widgets(download_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(download_tab, ttk.Treeview)), 1)
            self.assertIn("Input folder", collect_label_texts(duplicate_tab))
            self.assertIn("Input NetCDF folder", collect_label_texts(extract_tab))
            self.assertIn("Output GeoTIFF folder", collect_label_texts(extract_tab))
            self.assertIn("CRS mode", collect_label_texts(extract_tab))
            self.assertIn("Parallel workers", collect_label_texts(extract_tab))
            self.assertIn("Skip NetCDFs already recorded in the extraction manifest", collect_label_texts(extract_tab))
            self.assertIn("GDAL Python", collect_label_texts(mosaic_tab))
            self.assertIn("Grouping mode", collect_label_texts(mosaic_tab))
            self.assertIn("Target CRS label", collect_label_texts(mosaic_tab))
            self.assertIn("Parallel workers", collect_label_texts(mosaic_tab))
            self.assertIn("Mosaic manifest CSV", collect_label_texts(mosaic_tab))
            self.assertIn("Skip mosaics already recorded with the same source set", collect_label_texts(mosaic_tab))
            self.assertIn("Write .tfw world files beside mosaic GeoTIFFs", collect_label_texts(mosaic_tab))
            self.assertNotIn("Compression", collect_label_texts(mosaic_tab))
            self.assertNotIn("GDAL Python", collect_label_texts(upload_tab))
            self.assertIn("Upload scope", collect_label_texts(upload_tab))
            self.assertIn("Selected upload tiles", collect_label_texts(upload_tab))
            self.assertIn("Validate Typed Tiles", collect_button_texts(upload_tab))
            self.assertIn("Clear Upload Tiles", collect_button_texts(upload_tab))
            self.assertIn("Refresh Available Tiles", collect_button_texts(upload_tab))
            self.assertIn("Execution", collect_label_texts(upload_tab))
            self.assertIn("Sync EE Assets", collect_button_texts(upload_tab))
            self.assertIn("Run Dry Run", collect_button_texts(upload_tab))
            self.assertIn("Run Real Upload", collect_button_texts(upload_tab))
            self.assertGreaterEqual(len(collect_widgets(upload_tab, ttk.Progressbar)), 1)
            self.assertIn("Refresh Statistics", collect_button_texts(statistics_tab))
            self.assertNotIn("Preview Cleanup", collect_button_texts(statistics_tab))
            self.assertIn("Sync EE Assets + Preview Cleanup", collect_button_texts(cleanup_tab))
            self.assertIn("Preview Cleanup", collect_button_texts(cleanup_tab))
            self.assertIn("Delete Selected Cleanup Files", collect_button_texts(cleanup_tab))
            self.assertIn("Delete All Cleanup Candidates", collect_button_texts(cleanup_tab))
            self.assertGreaterEqual(len(collect_widgets(extract_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(mosaic_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(statistics_tab, ttk.Treeview)), 3)
            self.assertGreaterEqual(len(collect_widgets(cleanup_tab, ttk.Treeview)), 1)
            self.assertIn("Processing Levels Across Stages", collect_label_texts(statistics_tab))
            self.assertIn("Upload Status Counts", collect_label_texts(statistics_tab))
            self.assertIn("Pipeline Completeness By UTM Tile", collect_label_texts(statistics_tab))
            self.assertIn("UTM Pipeline Status Map", collect_label_texts(statistics_tab))
        finally:
            root.destroy()

    def test_automation_date_status_and_copy_from_download(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.download_start_date_var.set("2024-01-01")
            app.download_end_date_var.set("2024-12-31")
            app.automation_start_date_var.set("2024-01-01")
            app.automation_end_date_var.set("2024-06-30")

            app.update_automation_date_status()

            self.assertIn("differ", app.automation_date_status_var.get())
            self.assertFalse(app.automation_dates_match_download())

            app.copy_download_dates_to_automation()

            self.assertTrue(app.automation_dates_match_download())
            self.assertEqual(app.automation_start_date_var.get(), "2024-01-01")
            self.assertEqual(app.automation_end_date_var.get(), "2024-12-31")
            self.assertIn("matches", app.automation_date_status_var.get())
        finally:
            root.destroy()

    def test_automation_map_uses_cached_statistics_overlay(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            status_rows = [
                ("UTM34M", 2, 2, 2, 2, 0),
                ("UTM35M", 0, 1, 1, 0, 1),
            ]
            app.latest_project_insights = SimpleNamespace(upload_qa_tile_rows=status_rows)
            app.set_automation_tiles(["UTM34M"])
            with mock.patch("swotflow_gui.load_display_geometry", return_value=object()):
                with mock.patch("swotflow_gui.collect_project_insights") as collect:
                    with mock.patch("swotflow_gui.UTMMapSelectorDialog") as dialog:
                        app.open_automation_map_selector()

            collect.assert_not_called()
            _args, kwargs = dialog.call_args
            self.assertEqual(kwargs["status_rows"], status_rows)
            self.assertEqual(kwargs["coverage_tiles"], ["UTM34M"])
        finally:
            root.destroy()

    def test_gui_auto_opens_project_from_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp) / "AutoProject"
            project = create_project(project_root, "Auto Project", self.sample_config(project_root))
            root = tk.Tk()
            root.withdraw()
            try:
                with mock.patch("swotflow_gui.load_config", return_value=project.config):
                    app = LauncherApp(root)

                self.assertEqual(app.current_project_name_var.get(), "Auto Project")
                self.assertEqual(app.current_project_root_var.get(), str(project_root))
                self.assertIn("auto-opened", app.project_status_var.get())
            finally:
                root.destroy()

    def test_save_config_requires_open_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.sample_config(Path(temp) / "NoProject")
            root = tk.Tk()
            root.withdraw()
            try:
                with mock.patch("swotflow_gui.load_config", return_value=config):
                    app = LauncherApp(root)
                with mock.patch("swotflow_gui.messagebox.showwarning") as warning:
                    saved = app.save_config(notify=False, validate_upload=False)

                self.assertFalse(saved)
                self.assertEqual(app.current_project_root_var.get(), "")
                warning.assert_called_once()
            finally:
                root.destroy()

    def test_download_matches_warns_when_earthdata_not_authenticated(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            config = DownloadConfig(
                output_folder=Path("raw"),
                start_date="2026-01-01",
                end_date="2026-01-02",
                utm_tiles=["UTM30R"],
                report_csv=Path("report.csv"),
            )
            app.download_authenticated = False
            with mock.patch.object(app, "save_config", return_value=True):
                with mock.patch.object(app, "download_config_from_ui", return_value=config):
                    with mock.patch("swotflow_gui.messagebox.askyesno", return_value=False) as ask:
                        with mock.patch("swotflow_gui.threading.Thread") as thread:
                            app.start_download()

            ask.assert_called_once()
            thread.assert_not_called()
            self.assertIn("authentication", app.download_status_var.get().lower())
        finally:
            root.destroy()

    def test_automation_start_requires_earthdata_auth_for_pending_downloads(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.download_authenticated = False
            app.automation_preflight_state = SimpleNamespace(
                preflight_ok=True,
                tile_plans=[SimpleNamespace(pending_downloads=2)],
            )
            with mock.patch("swotflow_gui.messagebox.showwarning") as warning:
                app.start_automation()

            warning.assert_called_once()
            self.assertIn("Authenticate Earthdata", app.automation_status_var.get())
            self.assertFalse(app.automation_running)
        finally:
            root.destroy()

    def test_successful_preflight_can_start_automation_automatically(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.automation_auto_start_after_preflight_var.set(True)
            state = AutomationRunState(
                run_id="run",
                run_dir=Path("run"),
                config=AutomationConfig(
                    project_root=Path("."),
                    base_config={},
                    utm_tiles=["UTM34M"],
                    start_date="2026-01-01",
                    end_date="2026-01-31",
                ),
                preflight_ok=True,
            )
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="new", message="pending")
            ]
            with mock.patch.object(app, "start_automation") as start:
                with mock.patch.object(app.root, "after", side_effect=lambda _delay, callback: callback()):
                    with mock.patch("swotflow_gui.messagebox.showinfo") as info:
                        app.finish_automation_preflight(state)

            start.assert_called_once()
            info.assert_not_called()
            self.assertIn("Starting automation automatically", app.automation_status_var.get())
        finally:
            root.destroy()

    def test_automation_progress_reports_percentage_and_tile_position(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            config = AutomationConfig(
                project_root=Path("."),
                base_config={},
                utm_tiles=["UTM34M", "UTM35M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                include_upload=False,
            )
            state = AutomationRunState(
                run_id="run",
                run_dir=Path("run"),
                config=config,
                preflight_ok=True,
                stage_results=[
                    AutomationStageResult(
                        run_id="run",
                        tile="UTM34M",
                        stage="download",
                        status="success",
                    )
                ],
            )
            app.initialize_automation_progress_tracker(state)
            app.automation_running = True

            app.update_automation_progress("UTM34M", "duplicates", "success", "done")

            self.assertAlmostEqual(app.automation_progress_var.get(), 2 / 12 * 100)
            self.assertIn("16.7%", app.automation_progress_text_var.get())
            self.assertIn("2/12 stages", app.automation_progress_text_var.get())
            self.assertIn("tile 1/2", app.automation_progress_text_var.get())
        finally:
            root.destroy()

    def test_open_project_loads_latest_automation_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp) / "Project"
            config_data = self.sample_config(project_root)
            project = create_project(project_root, "Project", config_data)
            run_config = AutomationConfig(
                project_root=project_root,
                base_config=config_data,
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                run_id="run",
                run_dir=project_root / "00_logs" / "automation_runs" / "run",
            )
            state = AutomationRunState(
                run_id="run",
                run_dir=run_config.run_dir,
                config=run_config,
                preflight_ok=True,
            )
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="new", message="pending", pending_downloads=1)
            ]
            state.stage_results = [
                AutomationStageResult(
                    run_id="run",
                    tile="UTM34M",
                    stage="download",
                    status="success",
                    message="ok",
                )
            ]
            write_run_state(state)
            root = tk.Tk()
            root.withdraw()
            try:
                app = LauncherApp(root)
                app.apply_project(project)

                self.assertIsNotNone(app.automation_preflight_state)
                assert app.automation_preflight_state is not None
                self.assertEqual(app.automation_preflight_state.run_id, "run")
                self.assertEqual(app.automation_selected_tiles_var.get(), "UTM34M")
                self.assertGreater(len(app.automation_tree.get_children()), 0)
                self.assertIn("Loaded automation run run", app.automation_status_var.get())
            finally:
                root.destroy()

    def test_open_utm_map_selector_passes_manifest_coverage(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.set_download_tiles(["UTM34M", "UTM35M"])
            with mock.patch("swotflow_gui.load_display_geometry", return_value=object()):
                with mock.patch("swotflow_gui.manifest_downloaded_tiles", return_value=["UTM33M"]):
                    with mock.patch("swotflow_gui.UTMMapSelectorDialog") as dialog:
                        app.open_utm_map_selector()

            args, kwargs = dialog.call_args
            self.assertIs(args[0], root)
            self.assertEqual(args[2], ["UTM34M", "UTM35M"])
            self.assertEqual(kwargs["coverage_tiles"], ["UTM33M"])
        finally:
            root.destroy()

    def test_open_utm_map_selector_uses_current_selection(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.set_download_tiles(["UTM34M", "UTM35M"])
            with mock.patch("swotflow_gui.load_display_geometry", return_value=object()):
                with mock.patch("swotflow_gui.UTMMapSelectorDialog") as dialog:
                    app.open_utm_map_selector()

            args, kwargs = dialog.call_args
            self.assertIs(args[0], root)
            self.assertEqual(args[2], ["UTM34M", "UTM35M"])
            self.assertIn("preset_choices", kwargs)
        finally:
            root.destroy()

    def test_map_selector_apply_updates_manual_utm_selection(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)

            app.apply_utm_map_selection(["UTM34M", "UTM35M"])

            self.assertEqual(app.current_download_tiles(), ["UTM34M", "UTM35M"])
            self.assertIn("UTM34M", app.download_selected_tiles_var.get())
        finally:
            root.destroy()

    def test_finished_real_duplicate_removal_refreshes_statistics(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            result = subprocess.CompletedProcess(
                args=["duplicate"],
                returncode=0,
                stdout="done",
                stderr="",
            )
            with mock.patch("swotflow_gui.messagebox.showinfo"):
                with mock.patch.object(app, "refresh_project_statistics_if_active") as refresh:
                    app.finish_duplicate_process(result, dry_run=False)

            refresh.assert_called_once_with("duplicate removal")
        finally:
            root.destroy()

    def test_finished_dry_run_duplicate_removal_does_not_refresh_statistics(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            result = subprocess.CompletedProcess(
                args=["duplicate"],
                returncode=0,
                stdout="done",
                stderr="",
            )
            with mock.patch("swotflow_gui.messagebox.showinfo"):
                with mock.patch.object(app, "refresh_project_statistics_if_active") as refresh:
                    app.finish_duplicate_process(result, dry_run=True)

            refresh.assert_not_called()
        finally:
            root.destroy()

    def test_upload_report_poll_refreshes_after_updates_are_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "upload_report.csv"
            report.write_text("final_status\nCOMPLETED\n", encoding="utf-8")
            root = tk.Tk()
            root.withdraw()
            try:
                app = LauncherApp(root)
                with mock.patch.object(app, "refresh_project_statistics_if_active") as refresh:
                    app.poll_upload_statistics_report(str(report), None, 0)
                    refresh.assert_not_called()
                    app.upload_statistics_last_change_at -= 20
                    revision = app.upload_report_revision(report)
                    app.poll_upload_statistics_report(str(report), revision, 0)

                refresh.assert_called_once_with("completed upload report updates")
            finally:
                root.destroy()

    def test_upload_report_poll_runs_one_shot_cleanup_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "upload_report.csv"
            report.write_text("final_status\nCOMPLETED\n", encoding="utf-8")
            root = tk.Tk()
            root.withdraw()
            try:
                app = LauncherApp(root)
                callback = mock.Mock()
                app.upload_report_update_callback = callback

                app.poll_upload_statistics_report(str(report), None, 0)

                callback.assert_called_once()
                self.assertIsNone(app.upload_report_update_callback)
            finally:
                root.destroy()

    def test_cleanup_sync_starts_sync_only_with_preview_callback(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            with mock.patch.object(app, "save_config", return_value=True) as save:
                with mock.patch.object(app, "launch_uploader") as launch:
                    app.sync_ee_assets_for_cleanup()

            save.assert_called_once()
            _args, kwargs = launch.call_args
            self.assertFalse(kwargs["dry_run"])
            self.assertTrue(kwargs["sync_only"])
            self.assertEqual(kwargs["after_report_update"], app.preview_cleanup_after_ee_sync)
        finally:
            root.destroy()

    def test_upload_report_progress_counts_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "upload_report.csv"
            write_csv(
                report,
                [
                    {"final_status": "PLANNED_UPLOAD"},
                    {"final_status": "SUBMITTED"},
                    {"final_status": "COMPLETED"},
                    {"final_status": "FILTERED_UTM_TILE"},
                ],
            )
            root = tk.Tk()
            root.withdraw()
            try:
                app = LauncherApp(root)

                app.update_upload_progress_from_report(report)

                self.assertAlmostEqual(app.upload_progress_var.get(), 66.666, places=2)
                self.assertIn("planned pending 1", app.upload_progress_text_var.get())
                self.assertIn("filtered 1", app.upload_progress_text_var.get())
            finally:
                root.destroy()

    def test_upload_tile_selection_is_saved_to_config(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.upload_scope_var.set("Selected UTM/source tiles only")
            app.upload_selected_tiles_var.set("UTM34M, UTM35M")

            config = app.build_config()

            self.assertEqual(config["upload"]["scope"], "selected_utm")
            self.assertEqual(config["upload"]["utm_tiles"], ["UTM34M", "UTM35M"])
            self.assertTrue(config["upload"]["ee_sync_before_upload"])
            self.assertTrue(config["artifacts"]["ee_asset_inventory_csv"].endswith("ee_asset_inventory.csv"))
            self.assertEqual(config["extract"]["workers"], 1)
            self.assertEqual(config["mosaic"]["workers"], 1)
        finally:
            root.destroy()

    def test_upload_tile_list_prefers_available_not_uploaded_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp)
            logs = project_root / "00_logs"
            origin = project_root / "03_mosaics"
            origin.mkdir(parents=True)
            tile_34 = origin / swot_tif_name("UTM34M")
            tile_35 = origin / swot_tif_name("UTM35M")
            tile_34.write_bytes(b"tif")
            tile_35.write_bytes(b"tif")
            write_csv(
                logs / "upload_report.csv",
                [
                    {
                        "local_file": str(tile_35),
                        "final_status": "COMPLETED",
                    }
                ],
            )
            root = tk.Tk()
            root.withdraw()
            try:
                app = LauncherApp(root)
                app.processing_logs_var.set(str(logs))
                app.folder_var.set(str(origin))
                app.refresh_upload_tile_list(async_scan=False)

                values = listbox_values(app.upload_tile_listbox)

                self.assertEqual(values, ["UTM34M"])
                self.assertIn("current origin folder", app.upload_tile_availability_var.get())
                self.assertIn("UTM35M", app.upload_tile_status_var.get())
            finally:
                root.destroy()

    def test_listbox_adds_to_existing_map_selection(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.apply_utm_map_selection(["UTM34M"])
            app.download_tile_filter_var.set("UTM35M")
            app.refresh_download_tile_list()
            app.download_tile_listbox.selection_set(0)

            app.on_download_tile_select()

            self.assertEqual(app.current_download_tiles(), ["UTM34M", "UTM35M"])
        finally:
            root.destroy()

    def test_africa_preset_is_reflected_when_opening_map_selector(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.tile_preset_var.set("Continent: Africa")
            app.apply_tile_preset()
            with mock.patch("swotflow_gui.load_display_geometry", return_value=object()):
                with mock.patch("swotflow_gui.UTMMapSelectorDialog") as dialog:
                    app.open_utm_map_selector()

            args, _kwargs = dialog.call_args
            self.assertIn("UTM34M", args[2])
            self.assertGreater(len(args[2]), 50)
        finally:
            root.destroy()

    def test_project_load_updates_workflow_paths(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            with tempfile.TemporaryDirectory() as temp:
                project_root = Path(temp) / "Africa"
                project = create_project(project_root, "Africa Full", app.build_config())

                app.apply_project(project, write_config=False)

                self.assertEqual(app.current_project_name_var.get(), "Africa Full")
                self.assertEqual(app.download_output_var.get(), str(project_root / "01_raw_downloads"))
                self.assertEqual(app.duplicate_input_var.get(), str(project_root / "01_raw_downloads"))
                self.assertEqual(app.extract_output_var.get(), str(project_root / "02_extracted_geotiffs"))
                self.assertEqual(app.mosaic_output_var.get(), str(project_root / "03_mosaics"))
                self.assertEqual(app.folder_var.get(), str(project_root / "03_mosaics"))
        finally:
            root.destroy()

    def test_statistics_refresh_populates_metrics_and_cleanup(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            with tempfile.TemporaryDirectory() as temp:
                project_root = Path(temp) / "Stats"
                project = create_project(project_root, "Stats", app.build_config())
                app.apply_project(project, write_config=False)
                raw = project_root / "01_raw_downloads" / "raw.nc"
                extracted = project_root / "02_extracted_geotiffs" / "raw.tif"
                for path in (raw, extracted):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"123")
                write_csv(
                    project_root / "00_logs" / "extract_manifest.csv",
                    [
                        {
                            "status": "written",
                            "input_nc": str(raw),
                            "output_tif": str(extracted),
                        }
                    ],
                )

                app.refresh_project_statistics()

                metrics = [
                    app.stats_metrics_tree.item(item, "values")
                    for item in app.stats_metrics_tree.get_children()
                ]
                cleanup_rows = app.cleanup_tree.get_children()
                self.assertIn(("Raw NetCDF files on disk", "1"), metrics)
                self.assertEqual(len(cleanup_rows), 1)
                self.assertIn("Project statistics refreshed", app.statistics_status_var.get())
                self.assertTrue(
                    (project_root / "00_logs" / "statistics" / "project_statistics_snapshot.json").exists()
                )

                app.clear_project_statistics_display()
                app.apply_project(project, write_config=False)
                reloaded_metrics = [
                    app.stats_metrics_tree.item(item, "values")
                    for item in app.stats_metrics_tree.get_children()
                ]

                self.assertIn(("Raw NetCDF files on disk", "1"), reloaded_metrics)
                self.assertIn("Loaded saved project statistics", app.statistics_status_var.get())
        finally:
            root.destroy()

    def test_statistics_display_updates_status_map(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            geometry = UTMDisplayGeometry(
                bounds=(0.0, 0.0, 10.0, 10.0),
                tiles={
                    "UTM34M": DisplayTile(
                        token="UTM34M",
                        bounds=(0.0, 0.0, 10.0, 10.0),
                        polygons=[
                            [
                                (0.0, 0.0),
                                (10.0, 0.0),
                                (10.0, 10.0),
                                (0.0, 10.0),
                                (0.0, 0.0),
                            ]
                        ],
                    )
                },
                continents=[],
            )
            insights = SimpleNamespace(
                metrics={"Date coverage": "2026-01-01 to 2026-01-02"},
                stage_status_counts=[],
                tile_counts=[],
                date_counts=[],
                processing_level_counts=[],
                processing_level_tile_counts=[],
                mosaic_output_grid_counts=[],
                mosaic_source_tile_counts=[],
                upload_status_counts=[],
                uploaded_tile_counts=[],
                uploaded_date_counts=[],
                uploaded_processing_level_counts=[],
                uploaded_grid_counts=[],
                upload_error_counts=[],
                upload_qa_tile_rows=[("UTM34M", 2, 2, 2, 2, 0)],
                ready_not_uploaded_rows=[],
                cleanup_candidates=[],
            )

            with mock.patch("swotflow_gui.load_display_geometry", return_value=geometry):
                app.display_project_statistics(
                    insights,
                    status_text="Project statistics refreshed.",
                    include_cleanup=False,
                )

            self.assertEqual(app.stats_status_map.tile_status("UTM34M").status, "uploaded")
            self.assertIn("Uploaded/EE verified: 1", app.stats_status_map.status_var.get())
        finally:
            root.destroy()

    def test_builtin_preset_updates_manual_utm_selection(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.tile_preset_var.set("Continent: Africa")

            app.apply_tile_preset()

            selected = app.current_download_tiles()
            self.assertIn("UTM34M", selected)
            self.assertGreater(len(selected), 50)
        finally:
            root.destroy()

    def test_save_selected_tiles_as_project_preset(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            with tempfile.TemporaryDirectory() as temp:
                project = create_project(Path(temp) / "Okavango", "Okavango", app.build_config())
                app.apply_project(project, write_config=False)
                app.set_download_tiles(["UTM34K", "UTM34L"])

                with mock.patch("swotflow_gui.simpledialog.askstring", return_value="Okavango Delta"):
                    with mock.patch("swotflow_gui.messagebox.showinfo"):
                        app.save_selected_tiles_as_preset()

                presets = load_project_tile_profiles(project.root)
                self.assertIn("Okavango Delta", presets)
                self.assertEqual(presets["Okavango Delta"].tiles, ["UTM34K", "UTM34L"])
        finally:
            root.destroy()

    def test_prepare_update_uses_project_history_start_date(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            with tempfile.TemporaryDirectory() as temp:
                project = create_project(Path(temp) / "Africa", "Africa", app.build_config())
                app.apply_project(project, write_config=False)
                app.project_download_history = [
                    {"status": "success", "end_date": "2026-03-01", "failed_count": 0}
                ]

                app.prepare_project_update()

                self.assertEqual(app.download_start_date_var.get(), "2026-03-01")
                self.assertRegex(app.download_end_date_var.get(), r"^\d{4}-\d{2}-\d{2}$")
        finally:
            root.destroy()

    def test_download_handoff_sets_raw_processing_inputs(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.download_output_var.set("./SWOT_Processing/01_raw_downloads")

            app.apply_download_handoff_to_processing()

            self.assertEqual(app.duplicate_input_var.get(), "./SWOT_Processing/01_raw_downloads")
            self.assertEqual(app.extract_input_var.get(), "./SWOT_Processing/01_raw_downloads")
        finally:
            root.destroy()

    def test_stop_download_sets_event_and_status(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.download_stop_event = threading.Event()

            app.stop_download()

            self.assertTrue(app.download_stop_event.is_set())
            self.assertIn("Stop requested", app.download_status_var.get())
        finally:
            root.destroy()

    def test_extraction_handoff_sets_mosaic_grouping(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.extract_output_var.set("./SWOT_Processing/02_extracted_geotiffs")

            app.extract_crs_mode_var.set("Original projection")
            app.apply_extraction_handoff_to_mosaic()
            self.assertEqual(app.mosaic_input_var.get(), "./SWOT_Processing/02_extracted_geotiffs")
            self.assertEqual(app.mosaic_grouping_mode_var.get(), "Original projection / split by UTM zone")
            self.assertEqual(app.mosaic_target_crs_label_var.get(), "")

            app.extract_crs_mode_var.set("Africa LAEA")
            app.apply_extraction_handoff_to_mosaic()
            self.assertEqual(app.mosaic_grouping_mode_var.get(), "Reprojected common CRS / whole pass-date mosaic")
            self.assertEqual(app.mosaic_target_crs_label_var.get(), "LAEA")

            app.extract_crs_mode_var.set("WGS84")
            app.apply_extraction_handoff_to_mosaic()
            self.assertEqual(app.mosaic_grouping_mode_var.get(), "Reprojected common CRS / whole pass-date mosaic")
            self.assertEqual(app.mosaic_target_crs_label_var.get(), "WGS84")
        finally:
            root.destroy()

    def test_progress_line_parsing_and_widget_updates(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            parsed = app.parse_progress_line(
                "GEEUP_PROGRESS\textract\t5\t20\tProcessing file.nc\n"
            )
            download_parsed = app.parse_progress_line(
                "GEEUP_PROGRESS\tdownload\t1\t3\tDownloading file.nc\n"
            )

            self.assertEqual(parsed, ("extract", 5, 20, "Processing file.nc"))
            self.assertEqual(download_parsed, ("download", 1, 3, "Downloading file.nc"))
            app.update_download_progress(1, 3, "Downloading file.nc")
            app.update_extraction_progress(5, 20, "Processing file.nc")
            app.update_mosaic_progress(2, 4, "MOSAIC_CREATED: file.tif")

            self.assertAlmostEqual(app.download_progress_var.get(), 33.3333333333)
            self.assertEqual(app.extract_progress_var.get(), 25.0)
            self.assertEqual(app.mosaic_progress_var.get(), 50.0)
            self.assertIn("1/3", app.download_progress_text_var.get())
            self.assertIn("5/20", app.extract_progress_text_var.get())
            self.assertIn("2/4", app.mosaic_progress_text_var.get())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
