import unittest
from pathlib import Path

import numpy as np

from products.pixc.point_viewer import PointMapSession
from products.pixc.three_d_viewer import ThreeDViewerSession
from products.pixc.visualize import (
    PIXC_CLASSIFICATION_COLORS,
    Pixc3DViewConfig,
    SAMPLING_MODE_BALANCED_CLASSES,
    SAMPLING_MODE_WATER_PRIORITY,
    PixcPointMapConfig,
    build_pixc_3d_view_from_dataset,
    build_pixc_point_map_from_dataset,
    choose_default_3d_variables,
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
                    "phase_noise_std": FakeVariable(
                        [0.01, 0.02, 0.5, 0.7, 0.9, np.nan],
                        attrs={"long_name": "Phase noise std"},
                    ),
                    "sig0": FakeVariable(
                        [0.20, 0.13, 0.05, 0.01, -9999.0, 0.03],
                        attrs={"long_name": "Sigma0", "_FillValue": -9999.0},
                    ),
                    "coherent_power": FakeVariable(
                        [0.85, 0.62, 0.15, 0.02, 0.01, 0.04],
                        attrs={"long_name": "Coherent power"},
                    ),
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
        self.assertEqual(map_data.features[0]["properties"]["color"], PIXC_CLASSIFICATION_COLORS["1"])
        legend_labels = [row["label"] for row in map_data.legend]
        self.assertIn("1 - land", legend_labels)
        self.assertIn("2 - land near water", legend_labels)
        legend_colors = {row["value"]: row["color"] for row in map_data.legend}
        self.assertEqual(legend_colors["4"], PIXC_CLASSIFICATION_COLORS["4"])

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

    def test_point_map_can_prioritize_water_class_points(self) -> None:
        config = PixcPointMapConfig(
            file_path=Path("fake_pixc.nc"),
            attribute_path="/pixel_cloud/classification",
            max_points=2,
            sampling_mode=SAMPLING_MODE_WATER_PRIORITY,
        )

        map_data = build_pixc_point_map_from_dataset(fake_pixc_dataset(), config)

        self.assertEqual(map_data.rendered_points, 2)
        self.assertEqual(map_data.sampling_mode, SAMPLING_MODE_WATER_PRIORITY)
        self.assertIn("water-like point", map_data.sampling_summary)
        display_values = {feature["properties"]["display_value"] for feature in map_data.features}
        self.assertIn("4 - open water", display_values)

    def test_point_map_can_balance_sample_by_class(self) -> None:
        config = PixcPointMapConfig(
            file_path=Path("fake_pixc.nc"),
            attribute_path="/pixel_cloud/classification",
            max_points=3,
            sampling_mode=SAMPLING_MODE_BALANCED_CLASSES,
        )

        map_data = build_pixc_point_map_from_dataset(fake_pixc_dataset(), config)

        self.assertEqual(map_data.rendered_points, 3)
        self.assertEqual(map_data.sampling_mode, SAMPLING_MODE_BALANCED_CLASSES)
        self.assertIn("Balanced sampling", map_data.sampling_summary)

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
        self.assertIn("Reset View", html)
        self.assertIn("resetMapView", html)
        self.assertIn("Show All Values", html)
        self.assertIn("Hide All Values", html)
        self.assertIn("setValueVisibility", html)
        self.assertIn("Measure Distance", html)
        self.assertIn("map.distance", html)
        self.assertEqual(payload["rendered_points"], 4)
        self.assertEqual(payload["attribute_path"], "/pixel_cloud/classification")
        self.assertIn("sampling_summary", payload)
        self.assertEqual(len(payload["layers"]), 1)
        self.assertEqual(len(payload["reference_layers"]), 1)
        self.assertIn("reference_bbox", payload)
        self.assertIn("swot_reference_date", payload)

    def test_3d_defaults_pick_phase_sig0_power_and_classification(self) -> None:
        catalog = discover_dataset_point_variables(fake_pixc_dataset(), file_path="fake_pixc.nc")

        defaults = choose_default_3d_variables(catalog)

        self.assertEqual(defaults["x"], "/pixel_cloud/phase_noise_std")
        self.assertEqual(defaults["y"], "/pixel_cloud/sig0")
        self.assertEqual(defaults["z"], "/pixel_cloud/coherent_power")
        self.assertEqual(defaults["color"], "/pixel_cloud/classification")

    def test_build_3d_view_samples_attribute_space_and_colors_classes(self) -> None:
        config = Pixc3DViewConfig(
            file_path=Path("fake_pixc.nc"),
            max_points=3,
        )

        view_data = build_pixc_3d_view_from_dataset(fake_pixc_dataset(), config)

        self.assertEqual(view_data.total_points, 6)
        self.assertEqual(view_data.valid_points, 4)
        self.assertEqual(view_data.rendered_points, 3)
        self.assertTrue(view_data.sampled)
        self.assertEqual(view_data.x_path, "/pixel_cloud/phase_noise_std")
        self.assertEqual(view_data.y_path, "/pixel_cloud/sig0")
        self.assertEqual(view_data.z_path, "/pixel_cloud/coherent_power")
        self.assertEqual(view_data.color_attribute_path, "/pixel_cloud/classification")
        self.assertEqual(view_data.color_mode, "categorical")
        self.assertIn("1 - land", [row["label"] for row in view_data.legend])
        self.assertEqual(view_data.points[0]["display_color_value"], "1 - land")
        self.assertEqual(view_data.points[0]["color"], PIXC_CLASSIFICATION_COLORS["1"])
        legend_colors = {row["value"]: row["color"] for row in view_data.legend}
        self.assertEqual(legend_colors["3"], PIXC_CLASSIFICATION_COLORS["3"])
        self.assertIn("x", view_data.points[0])
        self.assertIn("y", view_data.points[0])
        self.assertIn("z", view_data.points[0])

    def test_3d_view_can_prioritize_water_class_points(self) -> None:
        config = Pixc3DViewConfig(
            file_path=Path("fake_pixc.nc"),
            max_points=2,
            sampling_mode=SAMPLING_MODE_WATER_PRIORITY,
        )

        view_data = build_pixc_3d_view_from_dataset(fake_pixc_dataset(), config)

        self.assertEqual(view_data.rendered_points, 2)
        self.assertEqual(view_data.sampling_mode, SAMPLING_MODE_WATER_PRIORITY)
        self.assertIn("water-like point", view_data.sampling_summary)
        self.assertIn("3 - water near land", [point["display_color_value"] for point in view_data.points])

    def test_3d_view_can_balance_sample_by_class(self) -> None:
        config = Pixc3DViewConfig(
            file_path=Path("fake_pixc.nc"),
            max_points=3,
            sampling_mode=SAMPLING_MODE_BALANCED_CLASSES,
        )

        view_data = build_pixc_3d_view_from_dataset(fake_pixc_dataset(), config)

        self.assertEqual(view_data.rendered_points, 3)
        self.assertEqual(view_data.sampling_mode, SAMPLING_MODE_BALANCED_CLASSES)
        self.assertIn("Balanced sampling", view_data.sampling_summary)

    def test_3d_viewer_serves_plotly_html_and_payload(self) -> None:
        view_data = build_pixc_3d_view_from_dataset(
            fake_pixc_dataset(),
            Pixc3DViewConfig(file_path=Path("fake_pixc.nc"), max_points=10),
        )
        session = ThreeDViewerSession(view_data)

        html = session.render_html()
        payload = session.data_payload()

        self.assertIn("Plotly.newPlot('plot3d'", html)
        self.assertIn("Reset View", html)
        self.assertIn("resetPlots", html)
        self.assertIn("Plotly.relayout('plot3d'", html)
        self.assertIn("Show All Values", html)
        self.assertIn("Apply Variables", html)
        self.assertIn("/rebuild", html)
        self.assertIn("setColorVisibility", html)
        self.assertIn("Plotly.restyle", html)
        self.assertIn("traceIndexByColorKey", html)
        self.assertIn("plotly_click", html)
        self.assertIn("Point Query", html)
        self.assertEqual(payload["rendered_points"], 4)
        self.assertEqual(payload["x_path"], "/pixel_cloud/phase_noise_std")
        self.assertIn("sampling_summary", payload)
        self.assertIn("variable_options", payload)
        self.assertIn("/pixel_cloud/classification", [row["path"] for row in payload["variable_options"]])
        self.assertEqual(len(payload["points"]), 4)


if __name__ == "__main__":
    unittest.main()
