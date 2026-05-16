from pathlib import Path
import unittest

from swot_metadata import crid_rank, parse_swot_l2_hr_raster_metadata, swot_product_rank


EXTRA_PROPERTIES = {
    "swot_descriptor": "descriptor",
    "swot_grid_resolution": "grid_resolution",
    "swot_coordinate_system": "coordinate_system",
    "swot_granule_overlap": "granule_overlap",
    "swot_cycle_id": "cycle_id",
    "swot_pass_id": "pass_id",
    "swot_scene_id": "scene_id",
    "swot_crid": "crid",
    "swot_product_counter": "product_counter",
}


class SwotMetadataParserTests(unittest.TestCase):
    def test_parses_swot_filename_without_extension(self) -> None:
        metadata = parse_swot_l2_hr_raster_metadata(
            "SWOT_L2_HR_Raster_100m_UTM01C_N_x_x_x_034_287_002F_"
            "20250618T233535_20250618T233556_PID0_01",
            EXTRA_PROPERTIES,
        )

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.start_time, "2025-06-18 23:35:35")
        self.assertEqual(metadata.end_time, "2025-06-18 23:35:56")
        self.assertEqual(metadata.properties["swot_cycle_id"], "034")
        self.assertEqual(metadata.properties["swot_pass_id"], "287")
        self.assertEqual(metadata.properties["swot_scene_id"], "002F")
        self.assertEqual(metadata.properties["swot_crid"], "PID0")
        self.assertEqual(metadata.properties["swot_product_counter"], "01")

    def test_parses_swot_filename_with_extension_and_suffix(self) -> None:
        metadata = parse_swot_l2_hr_raster_metadata(
            Path(
                "SWOT_L2_HR_Raster_100m_UTM36M_N_x_x_x_025_193_077F_"
                "20241209T203857_20241209T203918_PIC2_01_wse_laea.tif"
            ),
            EXTRA_PROPERTIES,
        )

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.start_time, "2024-12-09 20:38:57")
        self.assertEqual(metadata.end_time, "2024-12-09 20:39:18")
        self.assertEqual(metadata.properties["swot_grid_resolution"], "100m")
        self.assertEqual(metadata.properties["swot_coordinate_system"], "UTM36M")
        self.assertEqual(metadata.properties["swot_granule_overlap"], "N")

    def test_invalid_filename_returns_none(self) -> None:
        metadata = parse_swot_l2_hr_raster_metadata(
            "not_a_swot_raster_file.tif",
            EXTRA_PROPERTIES,
        )

        self.assertIsNone(metadata)

    def test_invalid_timestamp_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_swot_l2_hr_raster_metadata(
                "SWOT_L2_HR_Raster_100m_UTM01C_N_x_x_x_034_287_002F_"
                "20259918T233535_20250618T233556_PID0_01.tif",
                EXTRA_PROPERTIES,
            )

    def test_crid_rank_prefers_version_d_over_version_c(self) -> None:
        self.assertGreater(crid_rank("PID0"), crid_rank("PIC2"))

    def test_crid_rank_prefers_higher_minor_within_same_major(self) -> None:
        self.assertGreater(crid_rank("PIC2"), crid_rank("PIC0"))

    def test_crid_rank_prefers_reprocessed_within_same_major_minor(self) -> None:
        self.assertGreater(crid_rank("PGD0"), crid_rank("PID0"))
        self.assertGreater(crid_rank("PGC0"), crid_rank("PIC0"))

    def test_product_rank_uses_counter_last(self) -> None:
        self.assertGreater(swot_product_rank("PID0", "02"), swot_product_rank("PID0", "01"))

    def test_unknown_fidelity_crid_ranks_below_supported_public_crid(self) -> None:
        self.assertGreater(crid_rank("PID0"), crid_rank("PXD0"))


if __name__ == "__main__":
    unittest.main()
