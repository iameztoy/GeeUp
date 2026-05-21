import tkinter as tk
import unittest

from geeup_project import TilePreset
from utm_map_selector import (
    CanvasTransform,
    UTMPipelineStatusMap,
    UTMMapSelectorDialog,
    hit_test_tile,
    load_display_geometry,
    pipeline_status_from_qa_row,
    pipeline_status_key,
)


class UTMMapSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.geometry = load_display_geometry()

    def test_display_asset_loads_tiles_and_continents(self) -> None:
        self.assertIn("UTM34M", self.geometry.tiles)
        self.assertGreaterEqual(len(self.geometry.tiles), 1100)
        self.assertIn("Africa", {continent.name for continent in self.geometry.continents})

    def test_hit_test_identifies_tile_from_bounds_center(self) -> None:
        tile = self.geometry.tiles["UTM34M"]
        minx, miny, maxx, maxy = tile.bounds

        token = hit_test_tile(
            self.geometry,
            ((minx + maxx) / 2.0, (miny + maxy) / 2.0),
        )

        self.assertEqual(token, "UTM34M")

    def test_canvas_transform_round_trip(self) -> None:
        transform = CanvasTransform(self.geometry.bounds, 940, 520)
        tile = self.geometry.tiles["UTM34M"]
        minx, miny, maxx, maxy = tile.bounds
        point = ((minx + maxx) / 2.0, (miny + maxy) / 2.0)

        canvas_point = transform.world_to_canvas(point)
        round_tripped = transform.canvas_to_world(canvas_point)

        self.assertAlmostEqual(point[0], round_tripped[0])
        self.assertAlmostEqual(point[1], round_tripped[1])

    def test_pipeline_status_classification(self) -> None:
        self.assertEqual(pipeline_status_key(0, 0, 0, 0, 0), "none")
        self.assertEqual(pipeline_status_key(3, 0, 0, 0, 0), "downloaded")
        self.assertEqual(pipeline_status_key(3, 2, 0, 0, 0), "extracted")
        self.assertEqual(pipeline_status_key(3, 2, 1, 0, 1), "mosaicked")
        self.assertEqual(pipeline_status_key(3, 2, 2, 1, 1), "attention")
        self.assertEqual(pipeline_status_key(3, 2, 2, 2, 0), "uploaded")

        status = pipeline_status_from_qa_row(("UTM34M", 3, 2, 2, 1, 1))

        self.assertEqual(status.token, "UTM34M")
        self.assertEqual(status.status, "attention")
        self.assertEqual(status.label, "Partially uploaded; missing files")

    def test_status_map_updates_from_qa_rows(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            widget = UTMPipelineStatusMap(root, self.geometry)
            widget.set_tile_statuses(
                [
                    ("UTM34M", 3, 0, 0, 0, 0),
                    ("UTM35M", 3, 3, 2, 2, 0),
                ]
            )

            self.assertEqual(widget.tile_status("UTM34M").status, "downloaded")
            self.assertEqual(widget.tile_status("UTM35M").status, "uploaded")
            self.assertIn("Uploaded/EE verified: 1", widget.status_var.get())
        finally:
            root.destroy()

    def test_status_map_reports_missing_upload_file_for_tile(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            widget = UTMPipelineStatusMap(root, self.geometry)
            widget.set_tile_statuses([("UTM34M", 3, 3, 2, 1, 1)])
            widget.set_missing_upload_rows(
                [
                    (
                        "C:/Project/03_mosaics/SWOT_L2_HR_Raster_100m_UTM34M_example.tif",
                        "UTM34M",
                        "2026-01-02",
                        "UTM34M",
                    )
                ]
            )

            widget.update_tile_status("UTM34M")

            self.assertIn("First missing mosaic", widget.status_var.get())
            self.assertIn("SWOT_L2_HR_Raster_100m_UTM34M_example.tif", widget.status_var.get())
        finally:
            root.destroy()

    def test_dialog_apply_returns_selected_tiles(self) -> None:
        root = tk.Tk()
        root.withdraw()
        applied: list[list[str]] = []
        try:
            dialog = UTMMapSelectorDialog(
                root,
                self.geometry,
                ["UTM34M"],
                applied.append,
                preset_choices={
                    "Project: Okavango": TilePreset(
                        name="Okavango",
                        tiles=["UTM34K", "UTM34L"],
                    )
                },
                coverage_tiles=["UTM35M"],
            )
            dialog.withdraw()
            self.assertTrue(dialog.show_labels_var.get())
            self.assertEqual(dialog.coverage_tiles, {"UTM35M"})
            dialog.selected_tiles.add("UTM35M")

            dialog.apply_selection()

            self.assertEqual(applied, [["UTM34M", "UTM35M"]])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
