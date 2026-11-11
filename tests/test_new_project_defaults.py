"""New-project defaults must never migrate existing settings or history."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from swotflow.project.model import (
    NEW_PROJECT_DEFAULTS, config_for_project, create_project, load_project,
    new_project_defaults, save_project,
)


class NewProjectDefaultsTests(unittest.TestCase):
    def test_template_and_first_launch_use_the_same_preset(self) -> None:
        from swotflow.ui.main import load_config
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch("swotflow.ui.main.CONFIG_PATH", Path(temp) / "absent.yaml"):
                config = load_config()
        for section, values in NEW_PROJECT_DEFAULTS.items():
            for key, value in values.items():
                with self.subTest(section=section, key=key):
                    self.assertEqual(config[section][key], value)
        self.assertTrue(config["execution"]["dry_run"])
        self.assertTrue(config["execution"]["require_confirmation"])
        self.assertFalse(config["automation"]["auto_start_after_preflight"])

    def test_new_project_resets_workflow_but_keeps_scope_and_machine_choices(self) -> None:
        base = {
            "destination_parent": "projects/example/assets/collection",
            "download": {"start_date": "2024-01-01", "end_date": "2025-12-31",
                         "utm_tiles": ["UTM36P"], "verified_downloads": False,
                         "max_granules": 2, "product_version_filter": "all"},
            "extract": {"target_crs_mode": "africa_laea", "year_selection": "2023", "limit_files": 1},
            "mosaic": {"grouping_mode": "pass_date_common_crs", "target_crs_label": "LAEA"},
            "automation": {"process_by_year": False, "cleanup_enabled": False},
            "upload": {"pyramiding_policy": {"default": "SAMPLE"}},
            "gdal": {"python": "configured-runtime"},
            "chrome": {"user_data_dir": "configured-browser"},
        }
        before = deepcopy(base)
        with tempfile.TemporaryDirectory() as temp:
            project = create_project(Path(temp) / "project", "New", base)
            config = load_project(project.root).config
            for section, values in NEW_PROJECT_DEFAULTS.items():
                for key, value in values.items():
                    self.assertEqual(config[section][key], value, (section, key))
            self.assertEqual(config["download"]["utm_tiles"], ["UTM36P"])
            self.assertEqual(config["download"]["start_date"], "2024-01-01")
            self.assertEqual(config["download"]["end_date"], "2025-12-31")
            for section in ("destination_parent", "gdal", "chrome"):
                self.assertEqual(config[section], base[section])
            self.assertEqual(config["upload"]["pyramiding_policy"], base["upload"]["pyramiding_policy"])
            self.assertEqual(config["input_folder"], str(project.root / "03_mosaics"))
        self.assertEqual(base, before)

    def test_saved_custom_values_survive_open_and_path_repointing(self) -> None:
        from swotflow.ui.main import load_config
        with tempfile.TemporaryDirectory() as temp:
            project = create_project(Path(temp) / "project", "Existing", {})
            project.config.update({
                "download": {"verified_downloads": False, "catalog_history_enabled": False,
                             "threads": 2, "max_granules": 3},
                "automation": {"process_by_year": False, "cleanup_enabled": False,
                               "refresh_cmr": False, "include_upload": False,
                               "continue_on_tile_failure": False},
                "cleanup": {"mosaics_enabled": False, "mosaic_policy": "full_pixels"},
                "upload": {"completion_mode": "wait_for_ee", "max_pending_browser_uploads": 1},
            })
            project.download_history = [{"status": "success", "downloaded_count": 7}]
            save_project(project)
            expected = deepcopy(project.config)
            before = project.project_file.read_bytes()
            loaded = load_project(project.root)
            self.assertEqual(loaded.config, expected)
            self.assertEqual(loaded.download_history, project.download_history)
            self.assertEqual(project.project_file.read_bytes(), before)
            repointed = config_for_project(loaded.config, Path(temp) / "copy")
            for section in ("automation", "cleanup", "upload"):
                self.assertEqual(repointed[section], expected[section])
            mirror = Path(temp) / "config.yaml"
            mirror.write_text(yaml.safe_dump(expected), encoding="utf-8")
            with mock.patch("swotflow.ui.main.CONFIG_PATH", mirror):
                self.assertEqual(load_config(), expected)

    def test_legacy_missing_options_are_not_backfilled_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = create_project(Path(temp) / "project", "Legacy", {})
            project.config = {"processing": project.config["processing"], "download": {}}
            save_project(project)
            self.assertEqual(load_project(project.root).config, project.config)

    def test_new_project_cannot_overwrite_existing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = create_project(Path(temp) / "project", "Existing", {})
            before = project.project_file.read_bytes()
            with self.assertRaises(FileExistsError):
                create_project(project.root, "Replacement", {})
            self.assertEqual(project.project_file.read_bytes(), before)

    def test_preset_copies_nested_settings_without_mutating_caller(self) -> None:
        base = {"download": {"utm_tiles": ["UTM35L"]}}
        result = new_project_defaults(base)
        result["download"]["utm_tiles"].append("UTM35M")
        self.assertEqual(base, {"download": {"utm_tiles": ["UTM35L"]}})


if __name__ == "__main__":
    unittest.main()
