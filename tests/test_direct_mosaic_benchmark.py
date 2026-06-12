import tempfile
import unittest
from pathlib import Path

from direct_mosaic_benchmark import (
    BenchmarkGroup,
    paths_overlap,
    select_representative_groups,
    validate_output_location,
)
from ee_mosaic_tool import MosaicGroup, MosaicGroupKey, MosaicSource
from swot_metadata import ParsedMetadata


def fake_group(name: str, source_count: int) -> MosaicGroup:
    key = MosaicGroupKey(
        descriptor="100m_UTM36M_N_x_x_x",
        cycle_id="035",
        pass_id="225",
        start_date="20250707",
        coordinate_system="UTM36M",
    )
    metadata = ParsedMetadata(fields={"coordinate_system": "UTM36M"})
    sources = [
        MosaicSource(path=Path(f"{name}_{index}.nc"), metadata=metadata)
        for index in range(source_count)
    ]
    return MosaicGroup(key=key, sources=sources, output_file=Path(f"{name}.tif"))


class DirectMosaicBenchmarkPlanningTests(unittest.TestCase):
    def test_representative_selection_covers_observed_group_sizes(self) -> None:
        groups = [
            fake_group("one_a", 1),
            fake_group("one_b", 1),
            fake_group("two_a", 2),
            fake_group("two_b", 2),
            fake_group("three_a", 3),
            fake_group("three_b", 3),
        ]

        selected = select_representative_groups(groups, limit=3, seed=7)

        self.assertEqual(sorted(len(group.sources) for group in selected), [1, 2, 3])

    def test_representative_selection_is_deterministic(self) -> None:
        groups = [fake_group(f"group_{index}", (index % 4) + 1) for index in range(20)]

        first = select_representative_groups(groups, limit=8, seed=17)
        second = select_representative_groups(groups, limit=8, seed=17)

        self.assertEqual(
            [group.output_file.name for group in first],
            [group.output_file.name for group in second],
        )

    def test_path_overlap_detects_parent_and_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child = root / "raw"

            self.assertTrue(paths_overlap(root, child))
            self.assertTrue(paths_overlap(child, root))
            self.assertFalse(paths_overlap(child, root.parent / "separate"))

    def test_output_location_rejects_raw_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            raw.mkdir()

            with self.assertRaisesRegex(ValueError, "must not contain"):
                validate_output_location(raw, raw / "benchmark")

    def test_benchmark_group_is_json_friendly(self) -> None:
        payload = BenchmarkGroup(
            key={
                "descriptor": "100m_UTM36M_N_x_x_x",
                "cycle_id": "035",
                "pass_id": "225",
                "start_date": "20250707",
                "coordinate_system": "UTM36M",
            },
            output_name="mosaic.tif",
            sources=["one.nc", "two.nc"],
        )

        self.assertEqual(payload.output_name, "mosaic.tif")
        self.assertEqual(len(payload.sources), 2)


if __name__ == "__main__":
    unittest.main()
