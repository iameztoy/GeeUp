import json
import unittest
from urllib.request import Request, urlopen

from products.pixc.aoi_picker import AoiPickerSession


class AoiPickerTests(unittest.TestCase):
    def test_picker_serves_leaflet_satellite_map_and_receives_selection(self) -> None:
        session = AoiPickerSession(initial_bbox=(-1.0, 2.0, 3.0, 4.0))
        url = session.start()
        try:
            html = urlopen(url, timeout=5).read().decode("utf-8")

            self.assertIn("leaflet@1.9.4", html)
            self.assertIn("leaflet-draw@1.0.4", html)
            self.assertIn("World_Imagery/MapServer", html)
            self.assertIn("OpenStreetMap", html)
            self.assertIn("[-1.0, 2.0, 3.0, 4.0]", html)

            request = Request(
                url + "selection",
                data=json.dumps(
                    {
                        "bbox": [-2.0, 1.0, 2.0, 5.0],
                        "basemap": "Satellite",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))

            self.assertTrue(payload["ok"])
            selection = session.get_selection()
            self.assertIsNotNone(selection)
            self.assertEqual(selection.bbox, (-2.0, 1.0, 2.0, 5.0))
            self.assertEqual(selection.basemap, "Satellite")
        finally:
            session.stop()

    def test_picker_serves_preview_features_and_receives_granule_selection(self) -> None:
        feature = {
            "type": "Feature",
            "id": "G123",
            "properties": {"granule_id": "G123", "file_name": "SWOT_L2_HR_PIXC_TEST.nc"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-1.0, 2.0], [3.0, 2.0], [3.0, 4.0], [-1.0, 4.0], [-1.0, 2.0]]],
            },
        }
        session = AoiPickerSession(preview_features=[feature])
        url = session.start()
        try:
            html = urlopen(url, timeout=5).read().decode("utf-8")

            self.assertIn("Use Selected Granules", html)
            self.assertIn("SWOT_L2_HR_PIXC_TEST.nc", html)

            request = Request(
                url + "selection",
                data=json.dumps(
                    {
                        "selectedGranuleIds": ["G123"],
                        "basemap": "Satellite",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["selectedGranuleIds"], ["G123"])
            selection = session.get_selection()
            self.assertIsNotNone(selection)
            self.assertIsNone(selection.bbox)
            self.assertEqual(selection.selected_granule_ids, ["G123"])
        finally:
            session.stop()

    def test_picker_serves_reference_tiles_and_receives_tile_selection(self) -> None:
        session = AoiPickerSession(enable_reference_tiles=True, selected_reference_tile_names=["001_164L"])
        url = session.start()
        try:
            html = urlopen(url, timeout=5).read().decode("utf-8")

            self.assertIn("Use Selected Tiles", html)
            self.assertNotIn("__ENABLE_REFERENCE_TILES__", html)

            query = Request(
                url + "reference_tiles",
                data=json.dumps({"bbox": [0.3, 5.1, 1.1, 5.9]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            payload = json.loads(urlopen(query, timeout=10).read().decode("utf-8"))

            self.assertTrue(payload["ok"])
            names = {feature["properties"]["name"] for feature in payload["features"]}
            self.assertIn("001_164L", names)

            request = Request(
                url + "selection",
                data=json.dumps({"selectedReferenceTileNames": ["001_164L"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            response = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))

            self.assertTrue(response["ok"])
            self.assertEqual(response["selectedReferenceTileNames"], ["001_164L"])
            selection = session.get_selection()
            self.assertIsNotNone(selection)
            self.assertEqual(selection.selected_reference_tile_names, ["001_164L"])
        finally:
            session.stop()

    def test_picker_rejects_invalid_bbox(self) -> None:
        session = AoiPickerSession()
        url = session.start()
        try:
            request = Request(
                url + "selection",
                data=json.dumps({"bbox": [5.0, 1.0, 2.0, 3.0]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(Exception):
                urlopen(request, timeout=5)
            self.assertIsNone(session.get_selection())
        finally:
            session.stop()


if __name__ == "__main__":
    unittest.main()
