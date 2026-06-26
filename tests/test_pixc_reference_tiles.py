import unittest

from products.pixc.reference_tiles import (
    find_tiles_intersecting_bbox,
    find_tiles_intersecting_polygon,
    load_pixc_reference_tiles,
    selected_reference_tiles_to_track_filters,
)


class PixcReferenceTileTests(unittest.TestCase):
    def test_loads_generated_reference_cache_and_known_tile(self) -> None:
        index = load_pixc_reference_tiles()

        self.assertEqual(len(index.tiles), 90249)
        tile = index.get("001_164L")
        self.assertIsNotNone(tile)
        self.assertEqual(tile.pass_num, 1)
        self.assertEqual(tile.tile_num, 164)
        self.assertEqual(tile.tile_side, "L")
        self.assertEqual(tile.scene_id, "001_082F")
        self.assertEqual(len(tile.ring), 5)

    def test_bbox_and_polygon_intersection_find_expected_tiles(self) -> None:
        bbox_tiles = [tile.name for tile in find_tiles_intersecting_bbox((0.3, 5.1, 1.1, 5.9), limit=20)]
        polygon_tiles = [
            tile.name
            for tile in find_tiles_intersecting_polygon(
                [(0.3, 5.1), (1.1, 5.1), (1.1, 5.9), (0.3, 5.9), (0.3, 5.1)],
                limit=20,
            )
        ]

        self.assertIn("001_164L", bbox_tiles)
        self.assertIn("001_164L", polygon_tiles)
        self.assertEqual(find_tiles_intersecting_bbox((-20.0, -80.0, -19.0, -79.0), limit=5), [])

    def test_dateline_tile_intersection_uses_geometry_not_world_bbox(self) -> None:
        index = load_pixc_reference_tiles()
        dateline_tile = index.get("017_275R")

        self.assertIsNotNone(dateline_tile)
        self.assertTrue(dateline_tile.crosses_antimeridian)
        positive_matches = [
            tile.name for tile in find_tiles_intersecting_bbox((179.7, 66.5, 179.95, 67.2), limit=20)
        ]
        unrelated_matches = [
            tile.name for tile in find_tiles_intersecting_bbox((0.0, 66.5, 1.0, 67.2), limit=20)
        ]
        self.assertIn("017_275R", positive_matches)
        self.assertNotIn("017_275R", unrelated_matches)

    def test_selected_reference_tiles_to_track_filters_groups_by_pass(self) -> None:
        filters = selected_reference_tiles_to_track_filters(["001_164L", "001_164R", "002_007L"], cycle=44)

        self.assertEqual([item.pass_id for item in filters], [1, 2])
        self.assertEqual(filters[0].cycle_id, 44)
        self.assertEqual([tile.token for tile in filters[0].tile_ids], ["164L", "164R"])


if __name__ == "__main__":
    unittest.main()
