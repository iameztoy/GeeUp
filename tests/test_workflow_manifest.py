import csv
import tempfile
import unittest
from pathlib import Path

from workflow_manifest import (
    read_workflow_manifest,
    source_signature,
    upsert_workflow_manifest,
)


class WorkflowManifestTests(unittest.TestCase):
    def test_upsert_replaces_stage_record_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workflow_manifest.csv"

            upsert_workflow_manifest(
                path,
                [
                    {
                        "stage": "download",
                        "record_id": "G1",
                        "status": "DOWNLOADED",
                    }
                ],
            )
            upsert_workflow_manifest(
                path,
                [
                    {
                        "stage": "download",
                        "record_id": "G1",
                        "status": "SKIPPED_EXISTING",
                    },
                    {
                        "stage": "extract",
                        "record_id": "G1|original",
                        "status": "written",
                    },
                ],
            )

            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            loaded = read_workflow_manifest(path)

            self.assertEqual(len(rows), 2)
            self.assertEqual(loaded["download\tG1"]["status"], "SKIPPED_EXISTING")
            self.assertEqual(loaded["extract\tG1|original"]["status"], "written")

    def test_source_signature_is_order_independent(self) -> None:
        first = source_signature([Path("b.tif"), Path("a.tif")])
        second = source_signature([Path("a.tif"), Path("b.tif")])
        changed = source_signature([Path("a.tif"), Path("c.tif")])

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)


if __name__ == "__main__":
    unittest.main()
