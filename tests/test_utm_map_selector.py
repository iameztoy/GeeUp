import tkinter as tk
import unittest

from geeup_project import TilePreset
from utm_map_selector import (
    CanvasTransform,
    UTMMapSelectorDialog,
    hit_test_tile,
    load_display_geometry,
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
