from pathlib import Path
from types import SimpleNamespace
import unittest

from products.pixc.earth_engine_imagery import (
    EarthEngineImageSearchConfig,
    EarthEngineReferenceConfig,
    EarthEngineTileLayerRequest,
    build_reference_tile_layer,
    build_reference_imagery_layers,
    infer_reference_date_from_paths,
    reference_imagery_options,
    search_reference_images,
)


class FakeInfo:
    def __init__(self, value):
        self.value = value

    def getInfo(self):
        return self.value


class FakeImageDate:
    def __init__(self, value: str):
        self.value = value

    def format(self, pattern: str):
        return FakeInfo(self.value)


class FakeImage:
    def __init__(self, ee, source_name: str, *, image_id: str = "IMG_001", date_value: str = "2026-01-17"):
        self.ee = ee
        self.source_name = source_name
        self.image_id = image_id
        self.date_value = date_value
        self.map_vis_params = []

    def set(self, *args):
        self.ee.operations.append(("image.set", self.source_name, args))
        return self

    def date(self):
        return FakeImageDate(self.date_value)

    def get(self, key: str):
        if key == "system:index":
            return FakeInfo(self.image_id)
        return FakeInfo("")

    def getMapId(self, vis_params):
        self.map_vis_params.append(vis_params)
        self.ee.map_requests.append((self.source_name, vis_params))
        return {
            "tile_fetcher": SimpleNamespace(
                url_format=f"https://example.test/{self.source_name}/{{z}}/{{x}}/{{y}}.png"
            )
        }

    def select(self, bands):
        self.ee.operations.append(("image.select", self.source_name, bands))
        return self

    def normalizedDifference(self, bands):
        self.ee.operations.append(("image.normalizedDifference", self.source_name, bands))
        return self

    def rename(self, name):
        self.ee.operations.append(("image.rename", self.source_name, name))
        return self


class FakeCollection:
    def __init__(self, ee, name: str, *, count: int = 2):
        self.ee = ee
        self.name = name
        self.count = count
        self.operations = []

    def _record(self, operation: str, *args):
        self.operations.append((operation, args))
        self.ee.operations.append((self.name, operation, args))
        return self

    def filterBounds(self, geometry):
        return self._record("filterBounds", geometry)

    def filterDate(self, start, end):
        return self._record("filterDate", start, end)

    def filter(self, filter_value):
        return self._record("filter", filter_value)

    def select(self, bands):
        return self._record("select", bands)

    def merge(self, other):
        merged = FakeCollection(self.ee, f"{self.name}+{other.name}", count=max(self.count, other.count))
        self.ee.operations.append((self.name, "merge", other.name))
        return merged

    def size(self):
        return FakeInfo(self.count)

    def map(self, callback):
        return self._record("map", "callback")

    def sort(self, key):
        return self._record("sort", key)

    def limit(self, limit):
        self.limit_value = int(limit)
        return self._record("limit", limit)

    def getInfo(self):
        count = min(self.count, getattr(self, "limit_value", self.count))
        features = []
        for index in range(count):
            features.append(
                {
                    "id": f"{self.name}/IMG_{index + 1:03d}",
                    "properties": {
                        "system:time_start": 1768608000000 + index * 86400000,
                        "system:index": f"IMG_{index + 1:03d}",
                        "CLOUDY_PIXEL_PERCENTAGE": 12.5,
                        "CLOUD_COVER": 18.0,
                        "SPACECRAFT_NAME": "FAKE",
                        "orbitProperties_pass": "ASCENDING",
                        "transmitterReceiverPolarisation": ["VV", "VH"],
                    },
                }
            )
        return {"features": features}

    def first(self):
        self.ee.operations.append((self.name, "first", ()))
        return FakeImage(self.ee, self.name)

    def median(self):
        self.ee.operations.append((self.name, "median", ()))
        return FakeImage(self.ee, self.name, image_id="COMPOSITE", date_value="")


class FakeFilter:
    @staticmethod
    def lte(name, value):
        return ("lte", name, value)

    @staticmethod
    def eq(name, value):
        return ("eq", name, value)

    @staticmethod
    def listContains(name, value):
        return ("listContains", name, value)


class FakeGeometry:
    @staticmethod
    def Rectangle(coords):
        return ("Rectangle", tuple(coords))


class FakeEe:
    Filter = FakeFilter
    Geometry = FakeGeometry

    def __init__(self, *, count: int = 2, fail_initialize: bool = False):
        self.count = count
        self.fail_initialize = fail_initialize
        self.initialized_with = None
        self.collections = []
        self.operations = []
        self.map_requests = []

    def Initialize(self, project=None):
        if self.fail_initialize:
            raise RuntimeError("auth missing")
        self.initialized_with = project

    def ImageCollection(self, name):
        self.collections.append(name)
        return FakeCollection(self, name, count=self.count)

    def Image(self, image_id):
        self.operations.append(("Image", image_id))
        return FakeImage(self, f"Image:{image_id}", image_id=image_id)

    def Date(self, value):
        return value

    def Number(self, value):
        return value


class PixcReferenceImageryTests(unittest.TestCase):
    def test_infer_reference_date_from_pixc_filename(self) -> None:
        file_name = (
            "SWOT_L2_HR_PIXC_001_514_186R_20260117T052508_"
            "20260117T052529_PGD0_01.nc"
        )

        date_text, warnings = infer_reference_date_from_paths([Path(file_name)])

        self.assertEqual(date_text, "2026-01-17")
        self.assertEqual(warnings, [])

    def test_infer_reference_date_warns_on_multiple_dates(self) -> None:
        first = "SWOT_L2_HR_PIXC_001_514_186R_20260117T052508_20260117T052529_PGD0_01.nc"
        second = "SWOT_L2_HR_PIXC_001_514_187R_20260118T052508_20260118T052529_PGD0_01.nc"

        date_text, warnings = infer_reference_date_from_paths([first, second])

        self.assertEqual(date_text, "2026-01-17")
        self.assertIn("multiple acquisition dates", warnings[0])

    def test_mocked_earth_engine_builds_sentinel2_closest_and_composite_layers(self) -> None:
        fake_ee = FakeEe()
        config = EarthEngineReferenceConfig(
            enabled=True,
            reference_date="2026-01-17",
            window_days=14,
            ee_project="example-project",
            sources=("sentinel2",),
            methods=("closest", "composite"),
        )

        result = build_reference_imagery_layers(
            config,
            (-1.0, 2.0, 3.0, 4.0),
            ee_module=fake_ee,
        )

        self.assertEqual(fake_ee.initialized_with, "example-project")
        self.assertEqual(fake_ee.collections, ["COPERNICUS/S2_SR_HARMONIZED"])
        self.assertEqual(len(result.layers), 2)
        self.assertTrue(result.layers[0].default_visible)
        self.assertEqual(result.layers[0].source, "sentinel2")
        self.assertEqual(result.layers[0].method, "closest")
        self.assertIn("B4", fake_ee.map_requests[0][1]["bands"])
        self.assertIn("filterDate", [operation[1] for operation in fake_ee.operations])
        self.assertIn("map", [operation[1] for operation in fake_ee.operations])
        self.assertIn("median", [operation[1] for operation in fake_ee.operations])

    def test_viewer_options_expose_separate_landsat_sources_and_band_presets(self) -> None:
        options = reference_imagery_options()
        sources = {row["key"] for row in options["sources"]}

        self.assertIn("landsat8", sources)
        self.assertIn("landsat9", sources)
        self.assertIn("sentinel2", options["band_presets"])
        self.assertTrue(any(row["key"] == "s2_false_color" for row in options["band_presets"]["sentinel2"]))
        self.assertTrue(any(row["key"] == "ls_land_water" for row in options["band_presets"]["landsat8"]))

    def test_viewer_search_uses_filter_bounds_and_separate_landsat8_collection(self) -> None:
        fake_ee = FakeEe()
        result = search_reference_images(
            EarthEngineImageSearchConfig(
                source="landsat8",
                start_date="2026-01-17",
                end_date="2026-01-18",
                bbox=(-1.0, 2.0, 3.0, 4.0),
                ee_project="example-project",
                max_images=3,
            ),
            ee_module=fake_ee,
        )

        self.assertEqual(fake_ee.initialized_with, "example-project")
        self.assertEqual(fake_ee.collections, ["LANDSAT/LC08/C02/T1_L2"])
        operations = [operation[1] for operation in fake_ee.operations if isinstance(operation, tuple)]
        self.assertIn("filterBounds", operations)
        self.assertIn("filterDate", operations)
        self.assertIn("limit", operations)
        self.assertEqual(len(result.images), 2)
        self.assertEqual(result.images[0].source, "landsat8")

    def test_viewer_layer_uses_selected_band_preset(self) -> None:
        fake_ee = FakeEe()
        layer = build_reference_tile_layer(
            EarthEngineTileLayerRequest(
                source="sentinel2",
                image_id="COPERNICUS/S2_SR_HARMONIZED/IMG_001",
                band_preset="s2_false_color",
                ee_project="example-project",
            ),
            ee_module=fake_ee,
        )

        self.assertEqual(fake_ee.initialized_with, "example-project")
        self.assertEqual(layer.source, "sentinel2")
        self.assertIn("B8", fake_ee.map_requests[0][1]["bands"])
        self.assertIn("IMG_001", layer.scene_id)

    def test_earth_engine_initialization_failure_is_nonfatal(self) -> None:
        config = EarthEngineReferenceConfig(enabled=True, reference_date="2026-01-17")

        result = build_reference_imagery_layers(
            config,
            (-1.0, 2.0, 3.0, 4.0),
            ee_module=FakeEe(fail_initialize=True),
        )

        self.assertEqual(result.layers, [])
        self.assertIn("Could not initialize Earth Engine", result.warnings[0])

    def test_no_images_in_window_returns_warning_without_layers(self) -> None:
        config = EarthEngineReferenceConfig(
            enabled=True,
            reference_date="2026-01-17",
            sources=("sentinel1",),
            methods=("closest",),
        )

        result = build_reference_imagery_layers(
            config,
            (-1.0, 2.0, 3.0, 4.0),
            ee_module=FakeEe(count=0),
        )

        self.assertEqual(result.layers, [])
        self.assertIn("No Sentinel-1 imagery", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
