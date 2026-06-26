import unittest
from pathlib import Path

import numpy as np

from products.pixc.point_viewer import PointMapSession
from products.pixc.visualize import (
    PixcPointMapConfig,
    build_pixc_point_map_from_dataset,
    discover_dataset_point_variables,
    map_data_from_samples,
    sample_pixc_points_from_dataset,
    summarize_attribute_values_from_dataset,
)


class FakeVariable:
    def __init__(
        self,
        data,
        *,
        dimensions: tuple[str, ...] = ("points",),
        attrs: dict[str, object] | None = None,
    ) -> None:
        self.data = np.array(data)
        self.dimensions = dimensions
        self.shape = self.data.shape
        self.dtype = self.data.dtype
        self.size = self.data.size
        self.attrs = attrs or {}

    def __getitem__(self, key):
        return self.data

    def ncattrs(self):
        return list(self.attrs)

    def getncattr(self, name):
        return self.attrs[name]


class FakeGroup:
    def __init__(
        self,
        *,
        variables: dict[str, FakeVariable] | None = None,
        groups: dict[str, "FakeGroup"] | None = None,
    ) -> None:
        self.variables = variables or {}
        self.groups = groups or {}

    def ncattrs(self):
        return []


def fake_pixc_dataset() -> FakeGroup:
    return FakeGroup(
        groups={
            "pixel_cloud": FakeGroup(
                variables={
                    "latitude": FakeVariable(
                        [10.0, 11.0, 12.0, np.nan, -9999.0, 13.0],
                        attrs={"units": "degrees_north", "_FillValue": -9999.0},
                    ),
                    "longitude": FakeVariable(
                        [20.0, 21.0, 22.0, 23.0, 24.0, 25.0],
                        attrs={"units": "degrees_east"},
                    ),
                    "classification": FakeVariable(
                        [1, 2, 2, 3, -999, 4],
                        attrs={
                            "_FillValue": -999,
                            "flag_values": [1, 2, 3, 4],
                            "flag_meanings": "land land_near_water water_near_land open_water",
                        },
                    ),
                    "height": FakeVariable([100.0, 101.0, 102.0, 103.0, 104.0, 105.0]),
                }
            )
        }
    )


class PixcVisualizeTests(unittest.TestCase):
    def test_discover_dataset_point_variables_finds_coordinates_and_default_attribute(self) -> None:
        catalog = discover_dataset_point_variables(fake_pixc_dataset(), file_path="fake_pixc.nc")

        self.assertEqual(catalog.latitude_path, "/pixel_cloud/latitude")
        self.assertEqual(catalog.longitude_path, "/pixel_cloud/longitude")
        self.assertEqual(catalog.default_attribute_path, "/pixel_cloud/classification")
        self.assertIn("/pixel_cloud/height", catalog.point_attribute_paths)

    def test_build_point_map_samples_and_colors_categorical_attribute(self) -> None:
        config = PixcPointMapConfig(
            file_path=Path("fake_pixc.nc"),
            attribute_path="/pixel_cloud/classification",
            max_points=3,
        )

        map_data = build_pixc_point_map_from_dataset(fake_pixc_dataset(), config)

        self.assertEqual(map_data.total_points, 6)
        self.assertEqual(map_data.valid_points, 4)
        self.assertEqual(map_data.rendered_points, 3)
        self.assertTrue(map_data.sampled)
        self.assertEqual(map_data.color_mode, "categorical")
        self.assertEqual(map_data.bbox, (20.0, 10.0, 25.0, 13.0))
        self.assertEqual(map_data.reference_bbox, (20.0, 10.0, 25.0, 13.0))
        self.assertEqual(map_data.features[0]["geometry"]["coordinates"], [20.0, 10.0])
        self.assertIn("color", map_data.features[0]["properties"])
        self.assertEqual(map_data.features[0]["properties"]["display_value"], "1 - land")
        legend_labels = [row["label"] for row in map_data.legend]
        self.assertIn("1 - land", legend_labels)
        self.assertIn("2 - land near water", legend_labels)

    def test_attribute_values_are_summarized_for_filter_selection(self) -> None:
        rows, _ = summarize_attribute_values_from_dataset(
            fake_pixc_dataset(),
            "/pixel_cloud/classification",
        )

        by_key = {row.key: row for row in rows}

        self.assertEqual(by_key["1"].label, "1 - land")
        self.assertEqual(by_key["2"].count, 2)
        self.assertEqual(by_key["4"].meaning, "open water")

    def test_point_map_filters_selected_attribute_values(self) -> None:
        config = PixcPointMapConfig(
            file_path=Path("fake_pixc.nc"),
            attribute_path="/pixel_cloud/classification",
            max_points=10,
            allowed_value_keys=("2",),
        )

        map_data = build_pixc_point_map_from_dataset(fake_pixc_dataset(), config)

        self.assertEqual(map_data.valid_points, 2)
        self.assertEqual(map_data.rendered_points, 2)
        self.assertEqual(map_data.bbox, (21.0, 11.0, 22.0, 12.0))
        self.assertEqual(map_data.reference_bbox, (20.0, 10.0, 25.0, 13.0))
        self.assertEqual({feature["properties"]["value_label"] for feature in map_data.features}, {"2"})

    def test_multi_file_point_map_uses_shared_layers_and_legend(self) -> None:
        first, labels = sample_pixc_points_from_dataset(
            fake_pixc_dataset(),
            PixcPointMapConfig(
                file_path=Path("first.nc"),
                attribute_path="/pixel_cloud/classification",
                max_points=10,
            ),
        )
        second, _ = sample_pixc_points_from_dataset(
            fake_pixc_dataset(),
            PixcPointMapConfig(
                file_path=Path("second.nc"),
                attribute_path="/pixel_cloud/classification",
                max_points=2,
            ),
        )

        map_data = map_data_from_samples([first, second], value_labels=labels)

        self.assertEqual(len(map_data.layers), 2)
        self.assertEqual(map_data.layers[0]["file_name"], "first.nc")
        self.assertEqual(map_data.layers[1]["file_name"], "second.nc")
        self.assertEqual(map_data.rendered_points, 6)
        self.assertTrue(map_data.sampled)
        self.assertEqual(map_data.reference_bbox, (20.0, 10.0, 25.0, 13.0))
        self.assertIn("4 - open water", [row["label"] for row in map_data.legend])

    def test_point_viewer_serves_satellite_map_and_point_payload(self) -> None:
        config = PixcPointMapConfig(
            file_path=Path("fake_pixc.nc"),
            attribute_path="/pixel_cloud/classification",
            max_points=10,
        )
        map_data = build_pixc_point_map_from_dataset(fake_pixc_dataset(), config)
        session = PointMapSession(
            map_data,
            reference_layers=[
                {
                    "name": "Sentinel-2 closest (2026-01-17)",
                    "tile_url": "https://example.test/s2/{z}/{x}/{y}.png",
                    "attribution": "Google Earth Engine / Copernicus Sentinel-2",
                    "opacity": 0.82,
                    "default_visible": True,
                }
            ],
        )

        html = session.render_html()
        payload = session.point_payload()

        self.assertIn("World_Imagery", html)
        self.assertIn("OpenStreetMap", html)
        self.assertIn("overlayLayers", html)
        self.assertIn("reference_layers", html)
        self.assertIn("Earth Engine Reference", html)
        self.assertIn("/reference_search", html)
        self.assertIn("/reference_layer", html)
        self.assertIn("Landsat 9", html)
        self.assertIn("Measure Distance", html)
        self.assertIn("map.distance", html)
        self.assertEqual(payload["rendered_points"], 4)
        self.assertEqual(payload["attribute_path"], "/pixel_cloud/classification")
        self.assertEqual(len(payload["layers"]), 1)
        self.assertEqual(len(payload["reference_layers"]), 1)
        self.assertIn("reference_bbox", payload)
        self.assertIn("swot_reference_date", payload)


if __name__ == "__main__":
    unittest.main()
