import tempfile
import unittest
from pathlib import Path

import yaml

from products.pixc.project import (
    PRODUCT_FAMILY,
    create_pixc_project,
    load_pixc_project,
    pixc_project_paths,
    save_pixc_project,
)


class PixcProjectTests(unittest.TestCase):
    def test_create_project_writes_yaml_and_expected_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "PIXC_Test"
            project = create_pixc_project(
                root,
                "PIXC Test",
                {"download": {"start_date": "2026-01-01"}},
            )
            paths = pixc_project_paths(root)

            self.assertEqual(project.name, "PIXC Test")
            self.assertTrue(paths["project_file"].exists())
            self.assertTrue(paths["raw_downloads"].is_dir())
            self.assertTrue(paths["logs"].is_dir())
            self.assertTrue(paths["inspection"].is_dir())
            self.assertTrue(paths["processed_points"].is_dir())
            self.assertTrue(paths["qa"].is_dir())
            self.assertEqual(paths["download_report"], root / "00_logs" / "pixc_download_preview.csv")
            self.assertEqual(paths["download_manifest"], root / "00_logs" / "pixc_download_manifest.csv")
            self.assertEqual(paths["download_events"], root / "00_logs" / "pixc_download_events.csv")

            document = yaml.safe_load(paths["project_file"].read_text(encoding="utf-8"))
            self.assertEqual(document["product_family"], PRODUCT_FAMILY)
            self.assertEqual(document["project"]["product_family"], PRODUCT_FAMILY)
            self.assertEqual(document["settings"]["download"]["start_date"], "2026-01-01")

    def test_save_and_reopen_project_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "PIXC_Test"
            project = create_pixc_project(root, "PIXC Test", {"download": {"max_granules": "10"}})
            project.settings["download"]["max_granules"] = "25"
            save_pixc_project(project)

            reopened = load_pixc_project(root)

            self.assertEqual(reopened.name, "PIXC Test")
            self.assertEqual(reopened.settings["download"]["max_granules"], "25")
            self.assertTrue(reopened.created_at)

    def test_rejects_non_pixc_project_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_yaml = root / "project.yaml"
            project_yaml.write_text(
                "schema_version: 2\nproject:\n  name: Raster\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_pixc_project(project_yaml)


if __name__ == "__main__":
    unittest.main()
