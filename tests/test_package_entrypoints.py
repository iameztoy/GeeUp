"""Packaging must preserve external projects, workers and historical storage."""
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

from swotflow.paths import APP_ROOT, module_path, script_command
from swotflow.processing.automation import run_subprocess_stage, stage_command
from swotflow.verification.pipeline import VerificationStore, file_hash


class PackageEntrypointTests(unittest.TestCase):
    def test_both_launchers_dispatch_to_the_same_app(self):
        with mock.patch("swotflow.ui.main.main", return_value=0) as launch:
            for entry in (APP_ROOT / "swotflow_gui.py", APP_ROOT / "swotflow/__main__.py"):
                with self.subTest(entry=entry), self.assertRaises(SystemExit) as result:
                    runpy.run_path(str(entry), run_name="__main__")
                self.assertEqual(result.exception.code, 0)
            self.assertEqual(launch.call_count, 2)

    def test_worker_launch_from_external_project_preserves_explicit_config(self):
        with tempfile.TemporaryDirectory(prefix="swotflow project ") as temp:
            project = Path(temp)
            raw = project / "raw"
            raw.mkdir()
            config = project / "project settings.yaml"
            config.write_text(json.dumps({"duplicates": {"input_folder": str(raw)}}), encoding="utf-8")
            command, env = stage_command("duplicates", config, Path(sys.executable))
            command.append("--dry-run")
            previous = Path.cwd()
            try:
                os.chdir(project)
                code, tail = run_subprocess_stage(command, project / "worker.log", env=env)
            finally:
                os.chdir(previous)
            self.assertEqual(code, 0, tail)
            self.assertIn(str(raw.resolve()), tail)
            self.assertEqual(list(raw.iterdir()), [])

    def test_history_names_and_bundled_resources_keep_their_locations(self):
        from swotflow.core.storage import LEGACY_CONSOLIDATION_ROOT
        from swotflow.project.model import BUILTIN_PRESET_PATH
        from swotflow.ui.utm_map import DISPLAY_GEOMETRY_PATH
        from swotflow.ui.main import CONFIG_PATH, CONFIG_EXAMPLE_PATH, README_PATH, ASSETS_DIR
        self.assertEqual(LEGACY_CONSOLIDATION_ROOT, APP_ROOT / "outputs/ee_consolidation")
        self.assertEqual(CONFIG_PATH, APP_ROOT / "config.yaml")
        for path in (CONFIG_EXAMPLE_PATH, README_PATH, BUILTIN_PRESET_PATH, DISPLAY_GEOMETRY_PATH):
            self.assertTrue(path.is_file(), path)
        self.assertTrue((ASSETS_DIR / "swotflow_home_banner.png").is_file())

    def test_stage_receipts_hash_the_packaged_code(self):
        with tempfile.TemporaryDirectory() as temp:
            store = VerificationStore(Path(temp))
            run_id = store.begin_run("extract", {"workers": 2})
            with store.connection() as connection:
                payload = json.loads(connection.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            fingerprints = payload["code_sha256"]
            self.assertIn("swotflow/processing/extract.py", fingerprints)
            self.assertIn("swotflow/processing/mosaic.py", fingerprints)
            self.assertTrue(all((APP_ROOT / name).is_file() and file_hash(APP_ROOT / name) == value
                                for name, value in fingerprints.items()))

    def test_worker_paths_reject_other_packages_and_external_scripts(self):
        with self.assertRaises(ValueError):
            module_path("other_package.worker")
        with self.assertRaises(ValueError):
            script_command(sys.executable, APP_ROOT.parent / "external.py")


if __name__ == "__main__":
    unittest.main()
