import tkinter as tk
from tkinter import ttk
import unittest

from ee_uploader_gui import LauncherApp


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


class GuiLayoutTests(unittest.TestCase):
    def test_tab_order_and_gdal_field_placement(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            app = LauncherApp(root)
            notebook = find_notebook(app.root)
            tab_ids = notebook.tabs()
            tab_texts = [notebook.tab(tab_id, "text") for tab_id in tab_ids]

            self.assertEqual(tab_texts, ["Duplicate Removal", "Extraction", "Mosaic", "Upload"])
            duplicate_tab, extract_tab, mosaic_tab, upload_tab = notebook.winfo_children()
            self.assertIn("Input folder", collect_label_texts(duplicate_tab))
            self.assertIn("Input NetCDF folder", collect_label_texts(extract_tab))
            self.assertIn("Output GeoTIFF folder", collect_label_texts(extract_tab))
            self.assertIn("CRS mode", collect_label_texts(extract_tab))
            self.assertIn("GDAL Python", collect_label_texts(mosaic_tab))
            self.assertIn("Grouping mode", collect_label_texts(mosaic_tab))
            self.assertIn("Target CRS label", collect_label_texts(mosaic_tab))
            self.assertIn("Write .tfw world files beside mosaic GeoTIFFs", collect_label_texts(mosaic_tab))
            self.assertNotIn("Compression", collect_label_texts(mosaic_tab))
            self.assertNotIn("GDAL Python", collect_label_texts(upload_tab))
            self.assertGreaterEqual(len(collect_widgets(extract_tab, ttk.Progressbar)), 1)
            self.assertGreaterEqual(len(collect_widgets(mosaic_tab, ttk.Progressbar)), 1)
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

            self.assertEqual(parsed, ("extract", 5, 20, "Processing file.nc"))
            app.update_extraction_progress(5, 20, "Processing file.nc")
            app.update_mosaic_progress(2, 4, "MOSAIC_CREATED: file.tif")

            self.assertEqual(app.extract_progress_var.get(), 25.0)
            self.assertEqual(app.mosaic_progress_var.get(), 50.0)
            self.assertIn("5/20", app.extract_progress_text_var.get())
            self.assertIn("2/4", app.mosaic_progress_text_var.get())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
