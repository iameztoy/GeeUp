import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from project_diagnostics import (
    AUDIT_NAME, CATEGORIES, DiagnosticPreview, delete_diagnostics, preview_diagnostics,
)
from project_operation_lock import automation_lock


class ProjectDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        (self.root / "00_logs" / "upload_artifacts").mkdir(parents=True)
        (self.root / "project.yaml").write_text(yaml.safe_dump({
            "project": {"root": str(self.root)}, "config": {},
        }), encoding="utf-8")

    def file(self, relative, data=b"diagnostic", age_days=30):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        modified = time.time() - age_days * 86400
        os.utime(path, (modified, modified))
        return path

    def artifact(self, suffix="png", **kwargs):
        return self.file(f"00_logs/upload_artifacts/fatal_error_20260101_120000.{suffix}", **kwargs)

    def test_preview_totals_retention_and_optional_log_category(self):
        self.artifact(data=b"123")
        self.artifact("html", data=b"12345")
        self.file("00_logs/upload_artifacts/recent_20260101_120001.png", age_days=0)
        self.file("00_logs/ee_ui_uploader_20260101_120000.log", data=b"log")
        preview = preview_diagnostics(self.root)
        self.assertEqual(len(preview.candidates), 2)
        self.assertEqual(preview.size_bytes, 8)
        self.assertEqual(preview.recent_files, 1)
        self.assertEqual(len(preview_diagnostics(self.root, categories=CATEGORIES).candidates), 3)
        self.assertEqual(len(preview_diagnostics(self.root, categories=("logs",)).candidates), 1)

    def test_protected_files_and_history_bytes_are_unchanged(self):
        protected = ["00_logs/upload_report.csv", "00_logs/download_manifest.csv",
                     "00_logs/upload_events.csv", "00_logs/statistics/project_statistics.json",
                     "00_logs/automation_runs/run/automation_run.json",
                     "00_logs/automation_runs/run/UTM32T/automation_config.yaml",
                     "00_logs/automation_runs/run/UTM32T/upload.log",
                     "00_logs/automation_runs/run/chrome-profile/LOG",
                     "00_logs/update_recovery/backup.sqlite3", ".swotflow-relocation/backup.sqlite3",
                     "00_logs/upload_artifacts/important.json", "00_logs/upload_artifacts/unknown.png",
                     "00_logs/upload_artifacts/nested/fatal_error_20260101_120000.png",
                     "01_raw_downloads/source.nc", "02_extracted_geotiffs/source.tif",
                     "03_mosaics/mosaic.tif", "profiles/africa.json"]
        for name in protected:
            self.file(name, data=b"must retain")
        database = self.root / "swotflow.sqlite3"
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE history (status TEXT)")
            conn.execute("INSERT INTO history VALUES ('EE_VERIFIED_EXISTS')")
            conn.commit()
        before = {name: (self.root / name).read_bytes() for name in [*protected, "project.yaml", "swotflow.sqlite3"]}
        run_dir = self.root / "00_logs/automation_runs/run"
        run_mtime = run_dir.stat().st_mtime_ns
        self.artifact()
        self.artifact("html")
        self.file("00_logs/ee_ui_uploader_20260101_120000.log")
        preview = preview_diagnostics(self.root, categories=CATEGORIES)
        result = delete_diagnostics(preview, preview.candidates, confirmed_stopped=True)
        self.assertEqual(result.deleted_files, 3)
        self.assertFalse(result.skipped)
        self.assertEqual(run_dir.stat().st_mtime_ns, run_mtime)
        for name, data in before.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)
        audit = [json.loads(line) for line in result.audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(row["status"] == "deleted" for row in audit), 3)

    def test_selected_only_and_changed_file_is_not_deleted(self):
        png = self.artifact()
        html = self.artifact("html")
        preview = preview_diagnostics(self.root)
        selected = [item for item in preview.candidates if item.category == "screenshots"]
        png.write_bytes(b"updated after preview")
        result = delete_diagnostics(preview, selected, confirmed_stopped=True)
        self.assertEqual(result.deleted_files, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertTrue(png.exists())
        preview = preview_diagnostics(self.root)
        result = delete_diagnostics(preview, preview.candidates, confirmed_stopped=True)
        self.assertFalse(html.exists())
        self.assertTrue(png.exists())

    def test_unchanged_file_selection_does_not_delete_other_categories(self):
        png = self.artifact()
        html = self.artifact("html")
        preview = preview_diagnostics(self.root)
        result = delete_diagnostics(preview, [next(item for item in preview.candidates if item.category == "html")], confirmed_stopped=True)
        self.assertEqual(result.deleted_files, 1)
        self.assertTrue(png.exists())
        self.assertFalse(html.exists())

    def test_requires_stopped_confirmation_and_excludes_active_automation(self):
        path = self.artifact()
        preview = preview_diagnostics(self.root)
        with self.assertRaises(ValueError):
            delete_diagnostics(preview, preview.candidates)
        with automation_lock(self.root), self.assertRaises(RuntimeError):
            delete_diagnostics(preview, preview.candidates, confirmed_stopped=True)
        self.assertTrue(path.exists())

    def test_unpreviewed_or_forged_paths_cannot_delete_history(self):
        self.artifact()
        protected = self.file("00_logs/upload_report.csv")
        preview = preview_diagnostics(self.root)
        forged = replace(preview.candidates[0], relative_path="00_logs/upload_report.csv")
        with self.assertRaises(ValueError):
            delete_diagnostics(preview, [forged], confirmed_stopped=True)
        # Even a forged preview is subject to the backend allowlist.
        forged_preview = DiagnosticPreview(self.root, 7, [forged])
        result = delete_diagnostics(forged_preview, [forged], confirmed_stopped=True)
        self.assertEqual(result.deleted_files, 0)
        self.assertTrue(protected.exists())

    def test_invalid_retention_and_missing_or_relocated_project(self):
        for days in [0, -1, True, 0.5]:
            with self.assertRaises(ValueError):
                preview_diagnostics(self.root, keep_days=days)
        with self.assertRaises(ValueError):
            preview_diagnostics(self.root, categories=("backups",))
        (self.root / "project.yaml").write_text(yaml.safe_dump({"project": {"root": "Z:/elsewhere"}, "config": {}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            preview_diagnostics(self.root)

    def test_missing_files_are_reported_not_counted_as_freed_space(self):
        path = self.artifact()
        preview = preview_diagnostics(self.root)
        path.unlink()
        result = delete_diagnostics(preview, preview.candidates, confirmed_stopped=True)
        self.assertEqual((result.deleted_files, result.deleted_bytes), (0, 0))
        self.assertEqual(len(result.skipped), 1)

    def test_locked_files_are_reported_and_preserved(self):
        path = self.artifact()
        preview = preview_diagnostics(self.root)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            result = delete_diagnostics(preview, preview.candidates, confirmed_stopped=True)
        self.assertEqual(result.deleted_files, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertTrue(path.exists())

    def test_hardlinked_files_are_never_candidates(self):
        path = self.artifact()
        try:
            os.link(path, self.root / "important-data")
        except OSError as exc:
            self.skipTest(f"Hard links unavailable: {exc}")
        preview = preview_diagnostics(self.root)
        self.assertFalse(preview.candidates)
        self.assertTrue(preview.warnings)

    def test_symlink_and_symlink_swap_never_delete_external_file(self):
        path = self.artifact()
        preview = preview_diagnostics(self.root)
        external = self.root.parent / "keep.png"
        external.write_bytes(b"outside")
        path.unlink()
        try:
            path.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        result = delete_diagnostics(preview, preview.candidates, confirmed_stopped=True)
        self.assertEqual(result.deleted_files, 0)
        self.assertEqual(external.read_bytes(), b"outside")
        self.assertFalse(preview_diagnostics(self.root).candidates)

    def test_reparse_point_directory_is_not_scanned(self):
        self.artifact()
        from project_diagnostics import _is_link
        def is_link(info):
            return stat_is_dir(info) or _is_link(info)
        # Simulate the artifact directory being a junction, including platforms
        # where creating a real symlink requires elevated privileges.
        import stat
        def stat_is_dir(info):
            return stat.S_ISDIR(info.st_mode) and info.st_ino == artifact_inode
        artifact_inode = (self.root / "00_logs/upload_artifacts").stat().st_ino
        with mock.patch("project_diagnostics._is_link", side_effect=is_link):
            preview = preview_diagnostics(self.root)
        self.assertFalse(preview.candidates)
        self.assertTrue(preview.warnings)

    def test_does_not_create_database_or_import_csv(self):
        self.artifact()
        preview = preview_diagnostics(self.root)
        delete_diagnostics(preview, preview.candidates, confirmed_stopped=True)
        self.assertFalse((self.root / "swotflow.sqlite3").exists())
        self.assertTrue((self.root / "00_logs" / AUDIT_NAME).exists())

    def test_audit_failure_prevents_deletion(self):
        path = self.artifact()
        preview = preview_diagnostics(self.root)
        (self.root / "00_logs" / AUDIT_NAME).mkdir()
        with self.assertRaises(ValueError):
            delete_diagnostics(preview, preview.candidates, confirmed_stopped=True)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
