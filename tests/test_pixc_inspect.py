import tempfile
import unittest
from pathlib import Path

import numpy as np

from products.pixc.inspect import (
    inspect_dataset,
    summarize_for_status,
    write_inspection_reports,
)


class FakeDimension:
    def __init__(self, size: int, unlimited: bool = False) -> None:
        self.size = size
        self.unlimited = unlimited

    def __len__(self) -> int:
        return self.size

    def isunlimited(self) -> bool:
        return self.unlimited


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
        dimensions: dict[str, FakeDimension] | None = None,
        variables: dict[str, FakeVariable] | None = None,
        groups: dict[str, "FakeGroup"] | None = None,
        attrs: dict[str, object] | None = None,
    ) -> None:
        self.dimensions = dimensions or {}
        self.variables = variables or {}
        self.groups = groups or {}
        self.attrs = attrs or {}

    def ncattrs(self):
        return list(self.attrs)

    def getncattr(self, name):
        return self.attrs[name]


class PixcInspectionTests(unittest.TestCase):
    def test_inspect_dataset_reports_groups_variables_ranges_and_counts(self) -> None:
        dataset = FakeGroup(
            attrs={"title": "fake pixc"},
            groups={
                "pixel_cloud": FakeGroup(
                    dimensions={"points": FakeDimension(5)},
                    variables={
                        "latitude": FakeVariable(
                            [10.0, 11.5, 12.0, np.nan, -9999.0],
                            attrs={"units": "degrees_north", "_FillValue": -9999.0},
                        ),
                        "longitude": FakeVariable(
                            [20.0, 21.0, 22.0, 23.0, 24.0],
                            attrs={"units": "degrees_east"},
                        ),
                        "classification": FakeVariable(
                            [1, 2, 2, 3, -999],
                            attrs={"_FillValue": -999},
                        ),
                        "ancillary_text": FakeVariable(["a", "b", "c", "d", "e"]),
                    },
                )
            },
        )

        summary = inspect_dataset(dataset, file_path="fake_pixc.nc", file_size_bytes=1234)

        self.assertEqual(summary.file_path, "fake_pixc.nc")
        self.assertEqual(summary.file_size_bytes, 1234)
        self.assertEqual([group["path"] for group in summary.groups], ["/", "/pixel_cloud"])
        variable_paths = [row["path"] for row in summary.variables]
        self.assertIn("/pixel_cloud/latitude", variable_paths)
        self.assertIn("/pixel_cloud/classification", variable_paths)

        latitude_stats = next(
            row for row in summary.variable_stats if row["path"] == "/pixel_cloud/latitude"
        )
        self.assertEqual(latitude_stats["status"], "summarized")
        self.assertEqual(latitude_stats["missing_count"], 2)
        self.assertEqual(latitude_stats["min"], 10.0)
        self.assertEqual(latitude_stats["max"], 12.0)

        classification_stats = next(
            row for row in summary.variable_stats if row["path"] == "/pixel_cloud/classification"
        )
        self.assertEqual(classification_stats["status"], "summarized")
        self.assertEqual(classification_stats["missing_count"], 1)
        self.assertEqual(
            classification_stats["value_counts"],
            [
                {"value": 2, "count": 2},
                {"value": 1, "count": 1},
                {"value": 3, "count": 1},
            ],
        )
        self.assertIn("4 variable(s)", summarize_for_status(summary))

    def test_write_inspection_reports_outputs_json_and_csv(self) -> None:
        dataset = FakeGroup(
            groups={
                "pixel_cloud": FakeGroup(
                    dimensions={"points": FakeDimension(3)},
                    variables={
                        "classification": FakeVariable([1, 1, 2]),
                    },
                )
            }
        )
        summary = inspect_dataset(dataset, file_path="fake_pixc.nc")

        with tempfile.TemporaryDirectory() as temp:
            reports = write_inspection_reports(summary, Path(temp))

            self.assertTrue(reports.summary_json.exists())
            self.assertTrue(reports.variables_csv.exists())
            self.assertTrue(reports.value_counts_csv.exists())
            self.assertIn(
                "/pixel_cloud/classification",
                reports.value_counts_csv.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()

