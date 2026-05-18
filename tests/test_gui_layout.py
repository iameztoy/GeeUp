import csv
import tkinter as tk
from tkinter import ttk
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ee_uploader_gui import LauncherApp
from geeup_project import create_project, load_project_tile_profiles


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
        if isinstance(child, (ttk.Label, ttk.Checkbutton)):
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
                ["Download", "Duplicate Removal", "Extraction", "Mosaic", "Upload", "Statistics"],
            )
            self.assertIn("New Project", collect_button_texts(app.root))
            self.assertIn("Open Project", collect_button_texts(app.root))
            self.assertIn("Save Project", collect_button_texts(app.root))
            self.assertIn("Prepare Update", collect_button_texts(app.root))
            (
                download_tab,
                duplicate_tab,
                extract_tab,
                mosaic_tab,
                upload_tab,
                statistics_tab,
            ) = notebook.winfo_children()
            self.assertIn("Collection", collect_label_texts(download_tab))
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
            self.assertIn("Skip NetCDFs already recorded in the extraction manifest", collect_label_texts(extract_tab))
            self.assertIn("GDAL Python", collect_label_texts(mosaic_tab))
            self.assertIn("Grouping mode", collect_label_texts(mosaic_tab))
            self.assertIn("Target CRS label", collect_label_texts(mosaic_tab))
            self.assertIn("Mosaic manifest CSV", collect_label_texts(mosaic_tab))
            self.assertIn("Skip mosaics already recorded with the same source set", collect_label_texts(mosaic_tab))
            self.assertIn("Write .tfw world files beside mosaic GeoTIFFs", collect_label_texts(mosaic_tab))
            self.assertNotIn("Compression", collect_label_texts(mosaic_tab))
            self.assertNotIn("GDAL Python", collect_label_texts(upload_tab))
            self.assertIn("Refresh Statistics", collect_button_texts(statistics_tab))
            self.assertIn("Preview Cleanup", collect_button_texts(statistics_tab))
            self.assertIn("Delete Selected Cleanup Files", collect_button_texts(statistics_tab))
            self.assertIn("Delete All Cleanup Candidates", collect_button_texts(statistics_tab))
            self.assertGreaterEqual(len(collect_widgets(extract_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(mosaic_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(statistics_tab, ttk.Treeview)), 3)
        finally:
            root.destroy()

    def test_gui_auto_opens_project_from_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project_root = Path(temp) / "AutoProject"
            project = create_project(project_root, "Auto Project", self.sample_config(project_root))
            root = tk.Tk()
            root.withdraw()
            try:
                with mock.patch("ee_uploader_gui.load_config", return_value=project.config):
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
                with mock.patch("ee_uploader_gui.load_config", return_value=config):
                    app = LauncherApp(root)
                with mock.patch("ee_uploader_gui.messagebox.showwarning") as warning:
                    saved = app.save_config(notify=False, validate_upload=False)

                self.assertFalse(saved)
                self.assertEqual(app.current_project_root_var.get(), "")
                warning.assert_called_once()
            finally:
                root.destroy()

    def test_open_utm_map_selector_passes_manifest_coverage(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            app.set_download_tiles(["UTM34M", "UTM35M"])
            with mock.patch("ee_uploader_gui.load_display_geometry", return_value=object()):
                with mock.patch("ee_uploader_gui.manifest_downloaded_tiles", return_value=["UTM33M"]):
                    with mock.patch("ee_uploader_gui.UTMMapSelectorDialog") as dialog:
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
            with mock.patch("ee_uploader_gui.load_display_geometry", return_value=object()):
                with mock.patch("ee_uploader_gui.UTMMapSelectorDialog") as dialog:
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
            with mock.patch("ee_uploader_gui.load_display_geometry", return_value=object()):
                with mock.patch("ee_uploader_gui.UTMMapSelectorDialog") as dialog:
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

                with mock.patch("ee_uploader_gui.simpledialog.askstring", return_value="Okavango Delta"):
                    with mock.patch("ee_uploader_gui.messagebox.showinfo"):
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
