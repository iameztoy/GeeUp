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

from swotflow.processing.download import DownloadConfig
from swotflow.processing.automation import (
    AutomationConfig,
    AutomationRunState,
    AutomationStageResult,
    AutomationTilePlan,
    write_run_state,
)
from swotflow.ui.main import LauncherApp
from swotflow.version import __version__
from swotflow.project.model import create_project, load_project_tile_profiles
from swotflow.ui.utm_map import DisplayTile, UTMDisplayGeometry


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
    def test_statistics_evidence_shortcuts_only_navigate_existing_panels(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            notebook = app.statistics_notebook
            buttons = {button.cget("text"): button for button in collect_widgets(notebook, ttk.Button)}
            with mock.patch.object(app.nasa_history_panel, "work") as nasa_work,\
                 mock.patch.object(app.consolidation_coverage_panel, "work") as register_work:
                buttons["Input verification / NASA History"].invoke()
                self.assertEqual(notebook.tab(notebook.select(), "text"), "NASA History")
                buttons["Consolidation coverage"].invoke()
                self.assertEqual(notebook.tab(notebook.select(), "text"), "Consolidation")
                nasa_work.assert_not_called()
                register_work.assert_not_called()
            self.assertIn("Verification and consolidation", collect_label_texts(notebook))
        finally:
            root.update_idletasks()
            root.destroy()

    def test_nasa_history_preview_compare_and_future_run_setting_are_isolated(self) -> None:
        import json
        from tests.test_download_tool import FakeGranule, FakePagedEarthaccess, granule_name
        from tests.test_ee_consolidation import asset, manifest, COLLECTION
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                app.current_project_root_var.set(str(project))
                app.download_manifest_var.set(str(project / "00_logs/download_manifest.csv"))
                app.download_output_var.set(str(project / "raw"))
                app.destination_var.set(COLLECTION)
                app.download_start_date_var.set("2026-10-01")
                app.download_end_date_var.set("2026-10-24")
                panel = app.nasa_history_panel
                panel.start.set("2026-01-01")
                panel.end.set("2026-01-31")
                panel.tiles.set("UTM30R")
                panel.collections.set("SWOT_L2_HR_Raster_100m_D, SWOT_L2_HR_Raster_D")
                a = asset(tile="UTM30R", crs="EPSG:32630", cycle="044", pass_id="387",
                          start="20260117T052508", end="20260117T052529")
                rows = [{"output_file": name + ".tif", **row} for name, row in manifest(a).items()]
                write_csv(project / "00_logs/mosaic_manifest.csv", rows)
                old = Path(json.loads(rows[0]["input_files"])[0]).stem + ".nc"
                new = old.replace("_PGD0_01.nc", "_PGD0_02.nc")
                fake = FakePagedEarthaccess([FakeGranule(old, concept_id="old"), FakeGranule(new, concept_id="new")])
                with mock.patch.object(panel, "work", side_effect=lambda label, job, done: done(job())),\
                     mock.patch("swotflow.processing.download.import_earthaccess", return_value=fake):
                    panel.scan()
                    self.assertEqual(len(panel.scan_rows), 1)
                    from swotflow.catalog.history import utc_now
                    panel.inventory_ready({"collection": COLLECTION, "assets": [a], "finished_utc": utc_now()})
                    panel.compare()
                    self.assertEqual(panel.report["summary"], {"newer_version_available": 1})
                    self.assertEqual(len(panel.versions.get_children()), 1)
                    self.assertFalse((project / "swotflow.sqlite3").exists())
                    self.assertFalse((project / "raw").exists())
                    self.assertEqual(app.download_start_date_var.get(), "2026-10-01")
                    self.assertEqual(app.download_end_date_var.get(), "2026-10-24")
                    self.assertFalse(app.catalog_history_enabled_var.get())
                    app.catalog_history_enabled_var.set(True)
                    self.assertTrue(app.build_config()["download"]["catalog_history_enabled"])
                    self.assertTrue(app.download_config_from_ui().catalog_history_enabled)
                    panel.reconcile_stages()
                    self.assertEqual(len(panel.stage_table.get_children()),1)
                    self.assertFalse(panel.verification_report["complete"])
                    self.assertEqual(panel.verification_report["rows"][0]["state"],"download_missing_or_unverified")
                    self.assertIn("No verified download receipt", panel.stage_table.item("0", "values"))
                    self.assertIn("Selected search only: 2026-01-01 to 2026-01-31", panel.summary.get())
                    self.assertIn("EE inventory:", panel.status.get())
                    panel.verification_report["rows"][0]["asset_ids"] = [a["id"]]
                    panel.stage_table.selection_set("0")
                    reference = project/"local_name_different_from_asset.tif"
                    with mock.patch("swotflow.verification.pipeline.VerificationStore.artifacts",return_value={str(reference):{"stage":"mosaic","path":str(reference)}}),\
                         mock.patch("swotflow.project.database.read_project_rows",return_value=[{"asset_id":a["id"],"local_file":str(reference)}]),\
                         mock.patch("swotflow.verification.native.check_native_pixels",return_value={"passed":True}) as pixel_check,\
                         mock.patch.object(panel,"show_details"):
                        panel.check_pixels()
                        self.assertEqual(pixel_check.call_args.args[1],reference)
                panel.clear()
                self.assertFalse(panel.inventory)
                self.assertFalse(panel.report)
                self.assertFalse(panel.searches.get_children())
                self.assertFalse(panel.stage_table.get_children())
                self.assertFalse(panel.verification_report)
        finally:
            root.update_idletasks()
            root.destroy()

    def test_consolidation_register_to_statistics_map_and_local_approval(self) -> None:
        from tests.test_ee_replacement_register import inventory, receipt
        from tests.test_ee_consolidation import asset, manifest, COLLECTION, DESTINATION, local_temp
        from tests.test_ee_consolidation_update import stored
        import swotflow.consolidation.core as c
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            tab = app.consolidation_tab
            panel = app.consolidation_coverage_panel
            a, b = asset(), asset(tile="UTM31Q")
            lineage = {**manifest(a), **manifest(b)}
            plan = c.plan_assets([a, b], COLLECTION, c.Options(), lineage)
            output = stored(plan["groups"][0], plan)
            with local_temp() as temp:
                path = Path(temp) / "coverage.sqlite3"
                tab.source.set(COLLECTION)
                tab.destination.set(DESTINATION)
                tab.existing_collection.set(DESTINATION)
                tab.previous_plans.set(str(Path(temp) / "plans"))
                tab.inventory = inventory(COLLECTION, [a, b])
                tab.existing_inventory = inventory(DESTINATION, [output])
                tab.plan = plan
                tab.plan_settings = tab.settings()
                with mock.patch.object(tab, "work", side_effect=lambda label, job, done: done(job())),\
                     mock.patch.object(panel, "work", side_effect=lambda job, done: done(job())),\
                     mock.patch("swotflow.consolidation.tab.registry.default_path", return_value=path),\
                     mock.patch("swotflow.consolidation.tab.engine.read_lineage", return_value=lineage),\
                     mock.patch("swotflow.consolidation.tab.engine.connect", side_effect=AssertionError("No EE calls allowed")):
                    tab.update_register()
                    self.assertEqual(panel.report["summary"]["awaiting"], 2)
                    self.assertEqual(len(panel.tiles.get_children()), 2)
                    self.assertEqual(app.stats_status_map.consolidation_rows["UTM31Q"]["awaiting"], 1)
                    from swotflow.consolidation.register import ReplacementRegister
                    register = ReplacementRegister(path, COLLECTION)
                    register.record_verification(receipt(output, plan))
                    panel.refresh()
                    row = next(i for i in panel.products.get_children() if panel.products.item(i, "values")[0] == output["id"])
                    panel.products.selection_set(row)
                    with mock.patch("swotflow.consolidation.coverage_ui.messagebox.askyesno", return_value=True):
                        panel.approve()
                    self.assertEqual(panel.report["summary"]["active"], 2)
                    self.assertEqual(app.stats_status_map.consolidation_rows["UTM31P"]["status"], "complete")
                    panel.start.set("2024-01-02")
                    panel.refresh()
                    self.assertFalse(app.stats_status_map.consolidation_rows)
                    self.assertFalse(panel.tiles.get_children())
        finally:
            root.update_idletasks()
            root.destroy()

    def setUp(self) -> None:
        # GUI constructors auto-open config.yaml; tests must never touch a user's project.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        config_patch = mock.patch("swotflow.ui.main.load_config", return_value=self.sample_config(root / "NoProject"))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        path_patch = mock.patch("swotflow.ui.main.CONFIG_PATH", root / "config.yaml")
        path_patch.start()
        self.addCleanup(path_patch.stop)

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

    def test_incremental_consolidation_preview_without_cloud_or_project_writes(self) -> None:
        from tests.test_ee_consolidation import asset, manifest, COLLECTION, DESTINATION, local_temp
        from tests.test_ee_consolidation_update import stored
        import swotflow.consolidation.core as consolidation
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            tab = app.consolidation_tab
            a, b, new = asset(), asset(tile="UTM31Q"), asset(tile="UTM31R")
            lineage = {**manifest(a), **manifest(b), **manifest(new)}
            old = consolidation.plan_assets([a, b], COLLECTION, consolidation.Options(), lineage)
            previous = stored(old["groups"][0], old)
            with local_temp() as temp:
                consolidation.save_plan(Path(temp) / "old", old)
                tab.operation.set("Update existing mosaics")
                tab.previous_plans.set(temp)
                tab.inventory_ready({"collection": COLLECTION, "assets": [new],
                                     "existing_inventory": {"collection": DESTINATION, "assets": [previous]}})
                with mock.patch.object(tab, "work", side_effect=lambda label, job, done: done(job())),\
                     mock.patch("swotflow.consolidation.tab.engine.read_lineage", return_value=lineage):
                    tab.preview()
                self.assertEqual(tab.plan["summary"]["extended_groups"], 1)
                self.assertEqual(len(tab.tree.get_children()), 1)
                self.assertIn("Extend 1 groups", tab.comparison.get())
                self.assertEqual(tab.current_plan(), tab.plan)
                tab.quality.set("1")
                with self.assertRaises(ValueError):
                    tab.current_plan()
        finally:
            root.destroy()

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
                    "EE Consolidation",
                    "Statistics",
                    "Cleanup",
                ],
            )
            self.assertIn("New Project", collect_button_texts(app.root))
            self.assertIn("Open Project", collect_button_texts(app.root))
            self.assertIn("Save Project", collect_button_texts(app.root))
            self.assertIn("Relocate / Validate Project", collect_button_texts(app.root))
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
                consolidation_tab,
                statistics_tab,
                cleanup_tab,
            ) = notebook.winfo_children()
            self.assertIn("Current Project", collect_label_texts(home_tab))
            self.assertIn(f"v{__version__}", root.title())
            self.assertIn(f"v{__version__}", {app.home_banner_canvas.itemcget(item, "text") for item in app.home_banner_canvas.find_all()
                                      if app.home_banner_canvas.type(item) == "text"})
            self.assertIn("Project And Documentation", collect_label_texts(home_tab))
            self.assertIn("Workflow Shortcuts", collect_label_texts(home_tab))
            self.assertIn("Selected Download Tiles", collect_label_texts(home_tab))
            self.assertIn("Project Folders", collect_label_texts(home_tab))
            self.assertIn("Automation Settings", collect_label_texts(automation_tab))
            self.assertIn("Automation UTM Tiles", collect_label_texts(automation_tab))
            self.assertIn("Automation Queue And Results", collect_label_texts(automation_tab))
            self.assertIn("Upload completion mode", collect_label_texts(automation_tab))
            self.assertIn("Authenticate Earthdata", collect_button_texts(automation_tab))
            self.assertIn("Copy Download Date Range", collect_button_texts(automation_tab))
            self.assertIn("Run Preflight", collect_button_texts(automation_tab))
            self.assertIn("Start Automation", collect_button_texts(automation_tab))
            self.assertIn("Stop After Current Stage", collect_button_texts(automation_tab))
            self.assertIn(
                "Start automatically after successful preflight",
                collect_label_texts(automation_tab),
            )
            self.assertIn(
                "Prevent computer sleep while automation runs",
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
            self.assertIn("Select Retryable Uploads", collect_button_texts(upload_tab))
            self.assertIn("Execution", collect_label_texts(upload_tab))
            self.assertIn("Sync EE Assets", collect_button_texts(upload_tab))
            self.assertIn("Run Dry Run", collect_button_texts(upload_tab))
            self.assertIn("Run Real Upload", collect_button_texts(upload_tab))
            self.assertGreaterEqual(len(collect_widgets(upload_tab, ttk.Progressbar)), 1)
            self.assertIn("Refresh Statistics", collect_button_texts(statistics_tab))
            self.assertIn("Calculate preview", collect_button_texts(consolidation_tab))
            self.assertIn("Export selected (max 10)", collect_button_texts(consolidation_tab))
            self.assertIn("Estimated asset reduction", collect_label_texts(consolidation_tab))
            self.assertIn("Existing mosaic collection", collect_label_texts(consolidation_tab))
            self.assertIn("Previous plans folder", collect_label_texts(consolidation_tab))
            self.assertIn("Preview accounting / blocked groups", collect_button_texts(consolidation_tab))
            self.assertIn("Update coverage register", collect_button_texts(consolidation_tab))
            self.assertIn("Read consolidated inventory", collect_button_texts(consolidation_tab))
            self.assertIn("Refresh register", collect_button_texts(statistics_tab))
            self.assertIn("Approve selected revision locally", collect_button_texts(statistics_tab))
            self.assertTrue(any("Update existing mosaics" in widget.cget("values") for widget in collect_widgets(consolidation_tab, ttk.Combobox)))
            self.assertNotIn("Preview Cleanup", collect_button_texts(statistics_tab))
            self.assertIn("Sync EE Assets + Preview Cleanup", collect_button_texts(cleanup_tab))
            self.assertIn("Preview Cleanup", collect_button_texts(cleanup_tab))
            self.assertIn("Delete Selected Cleanup Files", collect_button_texts(cleanup_tab))
            self.assertIn("Delete All Cleanup Candidates", collect_button_texts(cleanup_tab))
            self.assertIn("Diagnostic Cleanup...", collect_button_texts(cleanup_tab))
            self.assertGreaterEqual(len(collect_widgets(extract_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(mosaic_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(statistics_tab, ttk.Treeview)), 3)
            self.assertGreaterEqual(len(collect_widgets(cleanup_tab, ttk.Treeview)), 1)
            self.assertIn("Processing Levels Across Stages", collect_label_texts(statistics_tab))
            self.assertIn("Product Version Audit", collect_label_texts(statistics_tab))
            self.assertIn("Recorded Update Runs", collect_label_texts(statistics_tab))
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

    def test_yearly_automation_is_optional_and_survives_project_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp) / "Project"
            config_data = self.sample_config(project_root)
            root = tk.Tk()
            root.withdraw()
            try:
                with mock.patch("swotflow.ui.main.load_config", return_value=config_data):
                    app = LauncherApp(root)
                self.assertFalse(app.automation_process_by_year_var.get())
                self.assertFalse(app.build_config()["automation"]["process_by_year"])
                self.assertIn("Process each tile year by year", collect_label_texts(root))
                controls = {widget.cget("text"): widget for widget in collect_widgets(root, ttk.Checkbutton)}
                yearly = controls["Process each tile year by year"]
                refresh = controls["Refresh remote catalogue during preflight"]
                self.assertEqual(yearly.master, refresh.master)
                def occupied_cells(widget):
                    grid = widget.grid_info()
                    return {(r, c) for r in range(grid['row'], grid['row'] + grid['rowspan'])
                            for c in range(grid['column'], grid['column'] + grid['columnspan'])}
                self.assertTrue(occupied_cells(yearly).isdisjoint(occupied_cells(refresh)),
                                "Catalogue refresh must not cover the yearly-processing checkbox")
                app.automation_refresh_cmr_var.set(False)
                yearly.invoke()
                self.assertTrue(app.automation_process_by_year_var.get())
                self.assertFalse(app.automation_refresh_cmr_var.get())
                refresh.invoke()
                self.assertTrue(app.automation_refresh_cmr_var.get())
                self.assertTrue(app.automation_process_by_year_var.get())
                config_data["automation"] = {
                    "process_by_year": True, "utm_tiles": ["UTM34M"],
                    "start_date": "2023-07-01", "end_date": "2026-09-30",
                }
                project = create_project(project_root, "Project", config_data)
                app.apply_project(project)
                self.assertTrue(app.automation_process_by_year_var.get())
                self.assertTrue(app.build_config()["automation"]["process_by_year"])
                self.assertTrue(app.automation_config_from_ui().process_by_year)
                saved = app.build_config()
                app.automation_process_by_year_var.set(False)
                app.apply_config_to_ui(saved)
                self.assertTrue(app.automation_process_by_year_var.get())
            finally:
                root.destroy()

    def test_changing_yearly_mode_invalidates_preflight(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.automation_preflight_state = SimpleNamespace(preflight_ok=True)
            app.automation_start_button.configure(state=tk.NORMAL)
            app.automation_process_by_year_var.set(True)
            self.assertIsNone(app.automation_preflight_state)
            self.assertTrue(app.automation_start_button.instate(["disabled"]))
            self.assertIn("Run preflight", app.automation_status_var.get())
        finally:
            root.destroy()

    def test_start_rejects_yearly_mode_changed_during_preflight(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            original = AutomationConfig(Path("."), {}, ["UTM34M"], "2023-01-01", "2024-12-31")
            state = AutomationRunState("run", Path("run"), original, preflight_ok=True)
            app.automation_preflight_state = state
            changed = AutomationConfig(Path("."), {}, ["UTM34M"], "2023-01-01", "2024-12-31",
                                       process_by_year=True)
            with mock.patch.object(app, "automation_config_from_ui", return_value=changed), \
                 mock.patch("swotflow.ui.main.messagebox.showwarning") as warning, \
                 mock.patch("swotflow.ui.main.threading.Thread") as thread:
                app.start_automation()
            warning.assert_called_once()
            self.assertIn("processing order", warning.call_args.args[1])
            thread.assert_not_called()
            self.assertFalse(app.automation_running)
        finally:
            root.destroy()

    def test_yearly_progress_keeps_years_distinct_and_tiles_unique(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            config = AutomationConfig(Path("."), {}, ["UTM34M", "UTM35M"],
                                      "2023-07-01", "2024-09-30", process_by_year=True)
            stages = app.automation_stage_names(False)
            results = [AutomationStageResult("run", "UTM34M", stage, "success",
                        period_start="2023-07-01", period_end="2023-12-31") for stage in stages]
            state = AutomationRunState("run", Path("run"), config, stage_results=results,
                                       preflight_ok=True)
            app.apply_automation_run_state(state)
            self.assertTrue(app.automation_process_by_year_var.get())
            self.assertEqual(app.automation_progress_total_units, 24)
            self.assertAlmostEqual(app.automation_progress_var.get(), 25)
            self.assertEqual(app.completed_automation_tile_count(), 0)
            self.assertTrue(all("2023 / " in app.automation_tree.item(item, "values")[1]
                                for item in app.automation_tree.get_children()))
            app.automation_running = True
            for stage in stages:
                app.update_automation_progress("UTM35M", f"2023/{stage}", "success", "done")
            self.assertAlmostEqual(app.automation_progress_var.get(), 50)
            self.assertIn("0/2 tiles through all years", app.automation_progress_text_var.get())
            self.assertIn("2/4 tile/year batches through stages", app.automation_progress_text_var.get())
            app.update_automation_progress("UTM34M", "2024/download", "success", "done")
            self.assertEqual(len(app.automation_progress_completed_keys), 13)
            self.assertIn("2024/download", app.automation_activity_var.get())
            app.update_automation_progress("UTM34M", "2024/download", "failed", "retry failed")
            self.assertEqual(len(app.automation_progress_completed_keys), 12)
            app.automation_running = False
            state.stage_results.append(AutomationStageResult("run", "UTM34M", "download", "failed",
                                       period_start="2023-07-01", period_end="2023-12-31"))
            app.initialize_automation_progress_tracker(state)
            self.assertNotIn(("UTM34M", "2023/download"), app.automation_progress_completed_keys)
            state.tile_plans = [AutomationTilePlan("UTM34M", "already complete", "old preflight")]
            app.initialize_automation_progress_tracker(state)
            self.assertNotIn(("UTM34M", "2023/download"), app.automation_progress_completed_keys)
            self.assertLess(app.automation_progress_var.get(), 100)
        finally:
            root.destroy()

    def test_yearly_progress_counts_completed_tile_year_batches(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            config = AutomationConfig(Path("."), {}, ["UTM34M", "UTM35M"],
                                      "2023-07-01", "2024-09-30", process_by_year=True)
            results = [AutomationStageResult("run", "UTM34M", stage, "success",
                       period_start=start, period_end=end)
                       for start, end in [("2023-07-01", "2023-12-31"), ("2024-01-01", "2024-09-30")]
                       for stage in app.automation_stage_names(False)]
            state = AutomationRunState("run", Path("run"), config, tile_plans=[
                AutomationTilePlan(tile, "already complete", "full-range preflight")
                for tile in config.utm_tiles
            ])
            app.apply_automation_run_state(state)
            self.assertEqual(app.automation_progress_var.get(), 0)
            self.assertEqual(app.completed_automation_tile_count(), 0)
            self.assertIn("0/4 tile/year batches through stages", app.automation_progress_text_var.get())
            state.stage_results = results
            app.apply_automation_run_state(state)
            self.assertAlmostEqual(app.automation_progress_var.get(), 50)
            self.assertIn("1/2 tiles through all years", app.automation_progress_text_var.get())
            self.assertIn("2/4 tile/year batches through stages", app.automation_progress_text_var.get())
        finally:
            root.destroy()

    def test_yearly_finish_uses_latest_attempt_and_reports_run_errors(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            config = AutomationConfig(Path("."), {}, ["UTM34M"], "2023-01-01", "2024-12-31",
                                      process_by_year=True)
            state = AutomationRunState("run", Path("run"), config, stage_results=[
                AutomationStageResult("run", "UTM34M", "download", status,
                                      period_start="2023-01-01", period_end="2023-12-31")
                for status in ("failed", "success")
            ])
            app.apply_automation_run_state(state)
            self.assertIn("failed stages 0", app.automation_status_var.get())
            with mock.patch("swotflow.ui.main.messagebox.showinfo"), \
                 mock.patch.object(app, "refresh_project_statistics_if_active"):
                app.finish_automation(state)
                self.assertIn("Failed stages: 0", app.automation_status_var.get())
                self.assertNotIn("First failure", app.automation_status_var.get())
                state.errors = ["2024 boundary has unresolved uploads"]
                app.finish_automation(state)
                self.assertIn("2024 boundary has unresolved uploads", app.automation_status_var.get())
                self.assertIn("outstanding work", app.automation_activity_var.get())
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
            with mock.patch("swotflow.ui.main.load_display_geometry", return_value=object()):
                with mock.patch("swotflow.ui.main.collect_project_insights") as collect:
                    with mock.patch("swotflow.ui.main.UTMMapSelectorDialog") as dialog:
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
                with mock.patch("swotflow.ui.main.load_config", return_value=project.config):
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
                with mock.patch("swotflow.ui.main.load_config", return_value=config):
                    app = LauncherApp(root)
                with mock.patch("swotflow.ui.main.messagebox.showwarning") as warning:
                    saved = app.save_config(notify=False, validate_upload=False)

                self.assertFalse(saved)
                self.assertEqual(app.current_project_root_var.get(), "")
                warning.assert_called_once()
            finally:
                root.destroy()

    def test_opening_moved_project_routes_to_relocation_dialog(self) -> None:
        from swotflow.project.relocation import RelocationRequired
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            with mock.patch("swotflow.ui.main.load_project", side_effect=RelocationRequired("moved")), mock.patch("swotflow.ui.main.messagebox.showwarning"), mock.patch.object(app, "show_project_transfer") as dialog:
                app.open_project_path(Path("copied/project.yaml"))
            dialog.assert_called_once_with(str(Path("copied/project.yaml")))
        finally:
            root.destroy()

    def test_project_load_keeps_explicit_external_data_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            project = create_project(folder / "project", "Mapped project", self.sample_config(folder / "project"))
            external = str(folder / "external_mosaics")
            project.config["mosaic"]["output_folder"] = external
            project.config["input_folder"] = external
            root = tk.Tk()
            root.withdraw()
            try:
                app = LauncherApp(root)
                app.apply_project(project)
                self.assertEqual(app.mosaic_output_var.get(), external)
                self.assertEqual(app.folder_var.get(), external)
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
                    with mock.patch("swotflow.ui.main.messagebox.askyesno", return_value=False) as ask:
                        with mock.patch("swotflow.ui.main.threading.Thread") as thread:
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
            with mock.patch("swotflow.ui.main.messagebox.showwarning") as warning:
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
                    with mock.patch("swotflow.ui.main.messagebox.showinfo") as info:
                        app.finish_automation_preflight(state)

            start.assert_called_once()
            info.assert_not_called()
            self.assertIn("Starting automation automatically", app.automation_status_var.get())
        finally:
            root.destroy()

    def test_auto_start_still_runs_with_windows_reboot_warning(self) -> None:
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
                warnings=["Windows Update reports a pending reboot."],
            )
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="new", message="pending")
            ]
            with mock.patch.object(app, "start_automation") as start:
                with mock.patch.object(app.root, "after", side_effect=lambda _delay, callback: callback()):
                    with mock.patch("swotflow.ui.main.messagebox.showinfo") as info:
                        app.finish_automation_preflight(state)

            start.assert_called_once()
            info.assert_not_called()
            self.assertIn("reboot/update warning", app.automation_status_var.get())
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
            self.assertIn("0/2 tiles complete", app.automation_progress_text_var.get())
            self.assertIn("current tile 1/2", app.automation_progress_text_var.get())

            for stage in app.automation_progress_stages:
                app.automation_progress_completed_keys.add(("UTM34M", stage))
            app.set_automation_progress_text("after first tile")
            self.assertIn("1/2 tiles complete", app.automation_progress_text_var.get())

            long_message = (
                "Extracting SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_044_070_"
                "20260105T211603_20260105T211628_PID0_01_really_long_file_name.tif"
            )
            compact = app.compact_live_status_message(long_message, limit=80)
            self.assertLessEqual(len(compact), 80)
            self.assertIn("...", compact)

            cmr_message = (
                "Download failed: CMR search failed for selected UTM tile(s). "
                "HTTPSConnectionPool(host='cmr.earthdata.nasa.gov', port=443): "
                "Read timed out after 60 seconds. Log: D:/SWOT/very/long/download.log"
            )
            self.assertEqual(
                app.compact_automation_tree_message(cmr_message),
                "CMR search timed out. Usually temporary; automation will continue/retry. See tile log.",
            )
            plan = AutomationTilePlan(
                tile="UTM34M",
                classification="cmr retry later",
                message="CMR search unavailable during preflight.",
            )
            self.assertEqual(
                app.compact_automation_plan_counts(plan),
                "CMR counts pending; download will retry",
            )
        finally:
            root.destroy()

    def test_automation_progress_counts_preflight_complete_tiles(self) -> None:
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
                include_upload=True,
            )
            state = AutomationRunState(
                run_id="run",
                run_dir=Path("run"),
                config=config,
                preflight_ok=True,
            )
            state.tile_plans = [
                AutomationTilePlan(
                    tile="UTM34M",
                    classification="already complete",
                    message="done",
                    pending_downloads=0,
                ),
                AutomationTilePlan(
                    tile="UTM35M",
                    classification="needs update",
                    message="pending",
                    pending_downloads=1,
                ),
            ]

            app.initialize_automation_progress_tracker(state)
            app.set_automation_progress_text("loaded")

            self.assertAlmostEqual(app.automation_progress_var.get(), 50.0)
            self.assertIn("9/18 stages", app.automation_progress_text_var.get())
            self.assertIn("1/2 tiles complete", app.automation_progress_text_var.get())
        finally:
            root.destroy()

    def test_automation_finish_does_not_force_incomplete_run_to_full_progress(self) -> None:
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

            with mock.patch("swotflow.ui.main.messagebox.showinfo"):
                with mock.patch.object(app, "refresh_project_statistics_if_active"):
                    app.finish_automation(state)

            self.assertAlmostEqual(app.automation_progress_var.get(), 1 / 12 * 100)
            self.assertIn("1/12 stages", app.automation_progress_text_var.get())
            self.assertIn("0/2 tiles complete", app.automation_progress_text_var.get())
            self.assertIn("pending", app.automation_status_var.get())
        finally:
            root.destroy()

    def test_open_project_loads_latest_automation_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp) / "Project"
            config_data = self.sample_config(project_root)
            config_data["automation"] = {"utm_tiles": ["UTM34M"], "start_date": "2026-01-01", "end_date": "2026-01-31"}
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
            with mock.patch("swotflow.ui.main.load_display_geometry", return_value=object()):
                with mock.patch("swotflow.ui.main.manifest_downloaded_tiles", return_value=["UTM33M"]):
                    with mock.patch("swotflow.ui.main.UTMMapSelectorDialog") as dialog:
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
            with mock.patch("swotflow.ui.main.load_display_geometry", return_value=object()):
                with mock.patch("swotflow.ui.main.UTMMapSelectorDialog") as dialog:
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
            with mock.patch("swotflow.ui.main.messagebox.showinfo"):
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
            with mock.patch("swotflow.ui.main.messagebox.showinfo"):
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

    def test_upload_progress_uses_only_rows_changed_in_current_run(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.upload_progress_started_at = "2026-08-11T20:12:08+02:00"
            with mock.patch(
                "swotflow.ui.main.project_status_counts_since",
                return_value={"PLANNED_UPLOAD": 108},
            ) as counts_since:
                app.update_upload_progress_from_report(Path("upload_report.csv"))

            self.assertEqual(app.upload_progress_var.get(), 0.0)
            self.assertIn("planning complete", app.upload_progress_text_var.get())
            self.assertIn("planned pending 108", app.upload_progress_text_var.get())
            counts_since.assert_called_once_with(
                Path("upload_report.csv"),
                "2026-08-11T20:12:08+02:00",
                "upload_report",
            )
        finally:
            root.destroy()

    def test_upload_tile_selection_is_saved_to_config(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.upload_scope_var.set("Selected UTM/source tiles only")
            app.upload_completion_mode_var.set("Submit and continue; verify later")
            app.upload_selected_tiles_var.set("UTM34M, UTM35M")

            config = app.build_config()

            self.assertEqual(config["upload"]["scope"], "selected_utm")
            self.assertEqual(config["upload"]["completion_mode"], "submit_and_continue")
            self.assertEqual(config["upload"]["utm_tiles"], ["UTM34M", "UTM35M"])
            self.assertEqual(config["upload"]["max_pending_browser_uploads"], 3)
            self.assertTrue(config["upload"]["ee_sync_before_upload"])
            self.assertTrue(config["artifacts"]["ee_asset_inventory_csv"].endswith("ee_asset_inventory.csv"))
            self.assertEqual(config["extract"]["workers"], 1)
            self.assertEqual(config["mosaic"]["workers"], 1)
        finally:
            root.destroy()

    def test_real_upload_is_confirmed_in_gui_before_launch(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            with mock.patch.object(app, "save_config", return_value=True):
                with mock.patch("swotflow.ui.main.messagebox.askyesno", return_value=True):
                    with mock.patch.object(app, "launch_uploader") as launch:
                        app.save_and_run_real()

            launch.assert_called_once_with(dry_run=False)
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
            with mock.patch("swotflow.ui.main.load_display_geometry", return_value=object()):
                with mock.patch("swotflow.ui.main.UTMMapSelectorDialog") as dialog:
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
                # This legacy statistics fixture has only manifest flags, no byte
                # receipts. Verified mode must withhold these cleanup proposals.
                project.config["download"]["verified_downloads"] = False
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
                app.verified_downloads_var.set(True)
                app.refresh_project_statistics()
                self.assertFalse(app.cleanup_tree.get_children())
                app.verified_downloads_var.set(False)
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
                upload_qa_tile_rows=[("UTM34M", 2, 2, 2, 0, 2, 0)],
                ready_not_uploaded_rows=[],
                cleanup_candidates=[],
            )

            with mock.patch("swotflow.ui.main.load_display_geometry", return_value=geometry):
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

                with mock.patch("swotflow.ui.main.simpledialog.askstring", return_value="Okavango Delta"):
                    with mock.patch("swotflow.ui.main.messagebox.showinfo"):
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
