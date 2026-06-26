import tkinter as tk
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tkinter import ttk

from swotflow_platform import ProductSelectorApp
from products.pixc.app import (
    DOWNLOAD_STATUS_FILTER_MATCHED,
    PIXC_GETTING_STARTED_PATH,
    PIXC_INSPECTION_GUIDE_PATH,
    PIXC_README_PATH,
    PixcApp,
    SPATIAL_MODE_REFERENCE,
)
from products.pixc.config import COLLECTION_LABELS, DEFAULT_COLLECTION_LABEL
from products.pixc.project import create_pixc_project, pixc_project_paths


def collect_button_texts(widget: tk.Widget) -> set[str]:
    texts: set[str] = set()
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            text = child.cget("text")
            if isinstance(text, str):
                texts.add(text)
        texts.update(collect_button_texts(child))
    return texts


def find_button(widget: tk.Widget, text: str) -> ttk.Button:
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button) and child.cget("text") == text:
            return child
        try:
            return find_button(child, text)
        except LookupError:
            continue
    raise LookupError(f"No ttk.Button found with text {text!r}")


def find_notebook(widget: tk.Widget) -> ttk.Notebook:
    for child in widget.winfo_children():
        if isinstance(child, ttk.Notebook):
            return child
        try:
            return find_notebook(child)
        except LookupError:
            continue
    raise LookupError("No ttk.Notebook found")


class ProductSelectorTests(unittest.TestCase):
    def test_selector_shows_product_family_buttons(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            ProductSelectorApp(root)

            buttons = collect_button_texts(root)

            self.assertIn("Process HR Raster 100 m", buttons)
            self.assertIn("Process Pixel Cloud / PIXC", buttons)
        finally:
            root.destroy()

    def test_selector_launches_hr_raster_wrapper(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = ProductSelectorApp(root)
            launch = mock.Mock(return_value="hr-app")
            fake_module = SimpleNamespace(launch=launch)
            with mock.patch.dict("sys.modules", {"products.hr_raster.app": fake_module}):
                app.open_hr_raster()

            launch.assert_called_once_with(root)
            self.assertEqual(app.product_app, "hr-app")
        finally:
            root.destroy()

    def test_selector_launches_pixc_wrapper_with_back_command(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = ProductSelectorApp(root)
            launch = mock.Mock(return_value="pixc-app")
            fake_module = SimpleNamespace(launch=launch)
            with mock.patch.dict("sys.modules", {"products.pixc.app": fake_module}):
                app.open_pixc()

            launch.assert_called_once()
            self.assertIs(launch.call_args.args[0], root)
            self.assertEqual(app.product_app, "pixc-app")
            back_command = launch.call_args.kwargs["back_command"]
            self.assertIs(back_command.__self__, app)
            self.assertIs(back_command.__func__, app.show_selector.__func__)
        finally:
            root.destroy()


class PixcAppTests(unittest.TestCase):
    def test_pixc_shell_has_initial_workflow_tabs_and_collection_choices(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = PixcApp(root)
            notebook = find_notebook(root)
            tab_texts = [notebook.tab(tab_id, "text") for tab_id in notebook.tabs()]

            self.assertEqual(
                tab_texts,
                ["Home", "Download", "Visualize Points", "Summary / QA"],
            )
            self.assertEqual(app.collection_var.get(), DEFAULT_COLLECTION_LABEL)
            self.assertIn(DEFAULT_COLLECTION_LABEL, COLLECTION_LABELS)
            buttons = collect_button_texts(root)
            self.assertIn("New Project", buttons)
            self.assertIn("Open Project", buttons)
            self.assertIn("Save Project", buttons)
            self.assertIn("Save Project As", buttons)
            self.assertIn("PIXC README", buttons)
            self.assertIn("Getting Started", buttons)
            self.assertIn("Pick", buttons)
            self.assertIn("Preview Search", buttons)
            self.assertIn("Download Selected Files", buttons)
            self.assertIn("Show Preview On Map", buttons)
            self.assertIn("Stop Download", buttons)
            self.assertIn("Select Matched", buttons)
            self.assertIn("Show All", buttons)
            self.assertIn("Apply UTM BBox", buttons)
            self.assertIn("Open AOI Map", buttons)
            self.assertIn("Open SWOT Tile Map", buttons)
            self.assertIn("Browse Point Files", buttons)
            self.assertIn("Refresh Files", buttons)
            self.assertIn("Use Selected Files", buttons)
            self.assertIn("Load Variables", buttons)
            self.assertIn("Load Values", buttons)
            self.assertIn("Select All Values", buttons)
            self.assertIn("Clear Values", buttons)
            self.assertIn("Open Point Map", buttons)
            self.assertNotIn("Use SWOT Date", buttons)
            self.assertNotIn("Use Earth Engine imagery", buttons)
            self.assertNotIn("Inspection Guide", buttons)
            self.assertNotIn("Browse NetCDF", buttons)
            self.assertNotIn("Inspect File", buttons)
            self.assertIsNotNone(app.visualize_tree)
            self.assertIsNotNone(app.visualize_values_tree)
            self.assertIsNotNone(app.download_progress_bar)
            self.assertEqual(app.download_progress_text_var.get(), "Progress: idle")
            self.assertTrue(PIXC_README_PATH.exists())
            self.assertTrue(PIXC_GETTING_STARTED_PATH.exists())
            self.assertTrue(PIXC_INSPECTION_GUIDE_PATH.exists())
        finally:
            root.destroy()

    def test_pixc_download_table_filters_and_selects_matched_rows(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = PixcApp(root)

            granules = [
                SimpleNamespace(
                    local_status="SKIPPED_EXISTING",
                    selected_for_download=True,
                    duplicate_filter_status="selected_single_version",
                    cycle_id=1,
                    pass_id=514,
                    tile_id="186",
                    swath_side="R",
                    file_name="existing.nc",
                    start_time="2026-01-01T00:00:00Z",
                    end_time="2026-01-01T00:01:00Z",
                    size_mb=1.0,
                ),
                SimpleNamespace(
                    local_status="EXCLUDED_OLDER_VERSION",
                    selected_for_download=False,
                    duplicate_filter_status="excluded_older_version",
                    cycle_id=1,
                    pass_id=514,
                    tile_id="186",
                    swath_side="R",
                    file_name="old.nc",
                    start_time="2026-01-01T00:00:00Z",
                    end_time="2026-01-01T00:01:00Z",
                    size_mb=1.0,
                ),
                SimpleNamespace(
                    local_status="EXCLUDED_OLDER_VERSION",
                    selected_for_download=False,
                    duplicate_filter_status="excluded_older_version",
                    cycle_id=1,
                    pass_id=514,
                    tile_id="186",
                    swath_side="R",
                    file_name="older.nc",
                    start_time="2026-01-01T00:00:00Z",
                    end_time="2026-01-01T00:01:00Z",
                    size_mb=1.0,
                ),
                SimpleNamespace(
                    local_status="MATCHED",
                    selected_for_download=True,
                    duplicate_filter_status="selected_best_version",
                    cycle_id=1,
                    pass_id=514,
                    tile_id="186",
                    swath_side="R",
                    file_name="matched.nc",
                    start_time="2026-01-01T00:00:00Z",
                    end_time="2026-01-01T00:01:00Z",
                    size_mb=1.0,
                ),
            ]
            app.pixc_latest_preview = SimpleNamespace(granules=granules)
            app.render_download_preview(app.pixc_latest_preview)

            self.assertEqual(len(app.download_tree.get_children()), 4)

            app.download_status_filter_var.set(DOWNLOAD_STATUS_FILTER_MATCHED)
            app.apply_download_status_filter()

            visible_items = app.download_tree.get_children()
            self.assertEqual(len(visible_items), 1)
            self.assertEqual(app.download_tree.item(visible_items[0], "values")[5], "matched.nc")

            app.select_matched_download_rows()

            visible_items = app.download_tree.get_children()
            self.assertEqual(app.download_tree.selection(), visible_items)
            self.assertFalse(granules[0].selected_for_download)
            self.assertFalse(granules[1].selected_for_download)
            self.assertFalse(granules[2].selected_for_download)
            self.assertTrue(granules[3].selected_for_download)
            self.assertIn("Selected 1 MATCHED", app.download_status_var.get())
        finally:
            root.destroy()

    def test_pixc_download_progress_bar_updates_from_backend_callbacks(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = PixcApp(root)

            app.update_pixc_download_progress(2, 5, "Downloading batch of 1 PIXC file(s)")

            self.assertEqual(app.download_progress_var.get(), 2.0)
            self.assertIn("2/5", app.download_progress_text_var.get())
            self.assertIn("Downloading batch", app.download_status_var.get())

            app.complete_pixc_download_progress("download complete")

            self.assertEqual(app.download_progress_var.get(), 100.0)
            self.assertEqual(app.download_progress_text_var.get(), "Progress: download complete")
        finally:
            root.destroy()

    def test_pixc_project_controls_gate_downloads_and_update_project_paths(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = PixcApp(root)
            preview_button = find_button(root, "Preview Search")
            download_button = find_button(root, "Download Selected Files")

            self.assertEqual(str(preview_button.cget("state")), "disabled")
            self.assertEqual(str(download_button.cget("state")), "disabled")

            with tempfile.TemporaryDirectory() as temp:
                project = create_pixc_project(
                    Path(temp) / "PIXC_Test",
                    "PIXC Test",
                    {
                        "download": {
                            "start_date": "2026-01-01",
                            "end_date": "2026-01-02",
                            "spatial_mode": "Bounding box",
                            "west": "-1",
                            "south": "2",
                            "east": "3",
                            "north": "4",
                            "cycle": "44",
                            "pass": "387",
                            "tiles": "001F",
                            "reference_tiles": ["001_164L", "001_164R"],
                            "max_granules": "7",
                        },
                        "visualize": {
                            "input_file": str(Path(temp) / "PIXC_Test" / "01_raw_downloads" / "sample.nc"),
                            "attribute": "/pixel_cloud/classification",
                            "latitude": "/pixel_cloud/latitude",
                            "longitude": "/pixel_cloud/longitude",
                            "max_points": "1234",
                            "reference_imagery": {
                                "enabled": True,
                                "reference_date": "2026-01-17",
                                "window_days": "21",
                                "ee_project": "example-project",
                                "sources": ["sentinel2", "landsat"],
                                "methods": ["closest"],
                            },
                        },
                    },
                )
                paths = pixc_project_paths(project.root)
                paths["raw_downloads"].mkdir(parents=True, exist_ok=True)
                sample_file = paths["raw_downloads"] / "sample.nc"
                sample_file.write_text("downloaded", encoding="utf-8")
                paths["download_manifest"].write_text(
                    "\n".join(
                        [
                            "granule_id,file_name,size_mb,downloaded,raw_exists,local_path,last_status,last_seen,last_downloaded",
                            f"G1,sample.nc,0.001,yes,yes,{sample_file},DOWNLOADED,2026-01-01T00:00:00,2026-01-01T00:00:00",
                        ]
                    ),
                    encoding="utf-8",
                )
                app.apply_pixc_project(project)

                self.assertEqual(app.current_project_name_var.get(), "PIXC Test")
                self.assertEqual(app.download_output_var.get(), str(paths["raw_downloads"]))
                self.assertEqual(app.inspect_output_var.get(), str(paths["inspection"]))
                self.assertEqual(app.download_start_var.get(), "2026-01-01")
                self.assertEqual(app.download_cycle_var.get(), "44")
                self.assertEqual(app.download_pass_var.get(), "387")
                self.assertEqual(app.download_tiles_var.get(), "001F")
                self.assertEqual(app.current_reference_tile_names(), ["001_164L", "001_164R"])
                self.assertEqual(app.download_max_granules_var.get(), "7")
                self.assertEqual(app.visualize_attribute_var.get(), "/pixel_cloud/classification")
                self.assertEqual(app.visualize_latitude_var.get(), "/pixel_cloud/latitude")
                self.assertEqual(app.visualize_longitude_var.get(), "/pixel_cloud/longitude")
                self.assertEqual(app.visualize_max_points_var.get(), "1234")
                self.assertNotIn("reference_imagery", app.current_pixc_settings()["visualize"])
                self.assertIsNotNone(app.visualize_files_tree)
                file_items = app.visualize_files_tree.get_children()
                self.assertEqual(len(file_items), 1)
                self.assertEqual(app.visualize_files_tree.item(file_items[0], "text"), "sample.nc")
                self.assertEqual(app.visualize_files_tree.item(file_items[0], "values")[0], "Downloaded")
                self.assertEqual(str(preview_button.cget("state")), "normal")
                self.assertEqual(str(download_button.cget("state")), "disabled")
        finally:
            root.destroy()

    def test_pixc_date_helper_writes_iso_date_text(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = PixcApp(root)

            app.set_date_var(app.download_start_var, date(2026, 1, 5))

            self.assertEqual(app.download_start_var.get(), "2026-01-05")
            self.assertIn("2023", app.date_picker_year_values())

            popup = tk.Toplevel(root)
            popup.withdraw()
            app.set_date_from_picker(app.download_end_var, popup, 2023, 2, 3)

            self.assertEqual(app.download_end_var.get(), "2023-02-03")
        finally:
            root.destroy()

    def test_pixc_reference_tile_config_wiring(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = PixcApp(root)
            with tempfile.TemporaryDirectory() as temp:
                project = create_pixc_project(Path(temp) / "PIXC_Test", "PIXC Test", {})
                app.apply_pixc_project(project)
                app.download_start_var.set("2026-01-01")
                app.download_end_var.set("2026-01-02")
                app.download_spatial_mode_var.set(SPATIAL_MODE_REFERENCE)
                app.download_cycle_var.set("44")
                app.set_reference_tile_names(["001_164L", "001_164R"])

                config = app.build_pixc_download_config()

                self.assertEqual(config.reference_tiles, ("001_164L", "001_164R"))
                self.assertEqual(config.track_filter.cycle_id, 44)
                self.assertIsNotNone(config.bbox)
        finally:
            root.destroy()

    def test_product_documentation_tree_exists(self) -> None:
        root_docs = Path("docs")

        self.assertTrue((root_docs / "hr_raster" / "README.md").exists())
        self.assertTrue((root_docs / "hr_raster" / "GETTING_STARTED.md").exists())
        self.assertTrue((root_docs / "hr_raster" / "PROCESSING_GUIDE.md").exists())
        self.assertTrue((root_docs / "hr_raster" / "TROUBLESHOOTING.md").exists())
        self.assertTrue((root_docs / "pixc" / "README.md").exists())
        self.assertTrue((root_docs / "pixc" / "GETTING_STARTED.md").exists())
        self.assertTrue((root_docs / "pixc" / "INSPECTION_GUIDE.md").exists())
        self.assertTrue((root_docs / "pixc" / "ROADMAP.md").exists())
        self.assertTrue((root_docs / "pixc" / "TROUBLESHOOTING.md").exists())


if __name__ == "__main__":
    unittest.main()
