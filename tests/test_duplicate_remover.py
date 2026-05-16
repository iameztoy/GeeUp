import tempfile
import unittest
from pathlib import Path

from swot_duplicate_remover import (
    DuplicateConfig,
    build_duplicate_plan,
    move_duplicates,
    split_filename,
    unique_destination,
)


def swot_nc_name(*, crid: str = "PID0", counter: str = "01") -> str:
    return (
        "SWOT_L2_HR_Raster_100m_UTM36P_N_x_x_x_037_527_091F_"
        f"20250829T040753_20250829T040815_{crid}_{counter}.nc"
    )


class DuplicateFilenameTests(unittest.TestCase):
    def test_parses_final_numeric_suffix_and_extension(self) -> None:
        candidate = split_filename(Path("SWOT_L2_HR_Raster_example_035_225_01.nc"))

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.core, "SWOT_L2_HR_Raster_example_035_225")
        self.assertEqual(candidate.version, 1)
        self.assertEqual(candidate.extension, ".nc")

    def test_parses_geotiff_versions(self) -> None:
        candidate = split_filename(Path("tile_05.tiff"))

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.core, "tile")
        self.assertEqual(candidate.version, 5)
        self.assertEqual(candidate.extension, ".tiff")

    def test_parser_is_not_limited_to_pid0(self) -> None:
        candidate = split_filename(
            Path(
                "SWOT_L2_HR_Raster_100m_UTM36P_N_x_x_x_037_527_091F_"
                "20250829T040753_20250829T040815_PIC2_02.nc"
            )
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.version, 2)
        self.assertTrue(candidate.core.endswith("_PIC2"))

    def test_ignores_names_without_final_version(self) -> None:
        self.assertIsNone(split_filename(Path("tile_version_abc.nc")))


class DuplicatePlanningTests(unittest.TestCase):
    def test_groups_by_core_and_keeps_highest_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ["granule_01.nc", "granule_03.nc", "granule_02.nc", "single_01.nc"]:
                (root / name).touch()

            config = DuplicateConfig(input_folder=root, log_folder=root / "logs")
            plan = build_duplicate_plan(config)

            self.assertEqual(plan.duplicate_group_count, 1)
            self.assertEqual(len(plan.actions), 2)
            self.assertEqual({action.source.name for action in plan.actions}, {"granule_01.nc", "granule_02.nc"})
            self.assertEqual(plan.kept_files[0].name, "granule_03.nc")

    def test_dry_run_reports_actions_without_moving(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "granule_01.nc").write_text("old", encoding="utf-8")
            (root / "granule_02.nc").write_text("new", encoding="utf-8")

            config = DuplicateConfig(input_folder=root, log_folder=root / "logs")
            exit_code, plan, log_path = move_duplicates(config, dry_run=True)

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(plan.actions), 1)
            self.assertIsNone(log_path)
            self.assertTrue((root / "granule_01.nc").exists())
            self.assertFalse((root / "moved" / "granule_01.nc").exists())

    def test_swot_duplicate_prefers_higher_product_counter_with_same_crid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_nc_name(crid="PID0", counter="01")).touch()
            (root / swot_nc_name(crid="PID0", counter="02")).touch()

            plan = build_duplicate_plan(DuplicateConfig(input_folder=root, log_folder=root / "logs"))

            self.assertEqual(plan.duplicate_group_count, 1)
            self.assertEqual(len(plan.actions), 1)
            self.assertEqual(plan.actions[0].kept.name, swot_nc_name(crid="PID0", counter="02"))
            self.assertEqual(plan.actions[0].reason, "higher product counter")

    def test_swot_duplicate_prefers_pic2_over_pic0(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_nc_name(crid="PIC0")).touch()
            (root / swot_nc_name(crid="PIC2")).touch()

            plan = build_duplicate_plan(DuplicateConfig(input_folder=root, log_folder=root / "logs"))

            self.assertEqual(plan.duplicate_group_count, 1)
            self.assertEqual(plan.actions[0].kept.name, swot_nc_name(crid="PIC2"))
            self.assertEqual(plan.actions[0].moved_crid, "PIC0")
            self.assertEqual(plan.actions[0].kept_crid, "PIC2")

    def test_swot_duplicate_prefers_pid0_over_pic2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_nc_name(crid="PIC2")).touch()
            (root / swot_nc_name(crid="PID0")).touch()

            plan = build_duplicate_plan(DuplicateConfig(input_folder=root, log_folder=root / "logs"))

            self.assertEqual(plan.duplicate_group_count, 1)
            self.assertEqual(plan.actions[0].kept.name, swot_nc_name(crid="PID0"))
            self.assertEqual(plan.actions[0].reason, "preferred CRID")

    def test_swot_duplicate_prefers_pgd0_over_pid0(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_nc_name(crid="PID0")).touch()
            (root / swot_nc_name(crid="PGD0")).touch()

            plan = build_duplicate_plan(DuplicateConfig(input_folder=root, log_folder=root / "logs"))

            self.assertEqual(plan.duplicate_group_count, 1)
            self.assertEqual(plan.actions[0].kept.name, swot_nc_name(crid="PGD0"))

    def test_swot_duplicate_handles_unknown_fidelity_crid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_nc_name(crid="PXD0", counter="99")).touch()
            (root / swot_nc_name(crid="PID0", counter="01")).touch()

            plan = build_duplicate_plan(DuplicateConfig(input_folder=root, log_folder=root / "logs"))

            self.assertEqual(plan.duplicate_group_count, 1)
            self.assertEqual(plan.actions[0].kept.name, swot_nc_name(crid="PID0", counter="01"))
            self.assertEqual(plan.actions[0].moved_crid, "PXD0")

    def test_unique_destination_avoids_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            moved = root / "moved"
            moved.mkdir()
            existing = moved / "granule_01.nc"
            existing.touch()

            destination = unique_destination(existing)

            self.assertEqual(destination.name, "granule_01__moved1.nc")


class DuplicateFilesystemTests(unittest.TestCase):
    def test_moves_older_versions_and_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            (root / "granule_01.nc").write_text("old", encoding="utf-8")
            (root / "granule_03.nc").write_text("new", encoding="utf-8")

            config = DuplicateConfig(input_folder=root, log_folder=logs)
            exit_code, plan, log_path = move_duplicates(config, dry_run=False)

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(plan.actions), 1)
            self.assertIsNotNone(log_path)
            assert log_path is not None
            self.assertFalse((root / "granule_01.nc").exists())
            self.assertTrue((root / "granule_03.nc").exists())
            self.assertEqual((root / "moved" / "granule_01.nc").read_text(encoding="utf-8"), "old")
            self.assertIn("MOVED:", log_path.read_text(encoding="utf-8"))
            self.assertIn("REASON:", log_path.read_text(encoding="utf-8"))

    def test_log_lists_unmatched_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            unmatched = root / "granule_01 (1).nc"
            unmatched.write_text("copy", encoding="utf-8")

            config = DuplicateConfig(input_folder=root, log_folder=logs)
            exit_code, plan, log_path = move_duplicates(config, dry_run=False)

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(plan.unmatched_files), 1)
            self.assertIsNotNone(log_path)
            assert log_path is not None
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("Unmatched files:", log_text)
            self.assertIn(f"UNMATCHED: {unmatched}", log_text)


if __name__ == "__main__":
    unittest.main()
