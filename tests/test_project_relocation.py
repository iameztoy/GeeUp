import csv
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import yaml

from project_database import DATABASE_FILE_NAME, ProjectDatabase
from project_relocation import (
    LOCK_FILE, PENDING_FILE, VALIDATION_FILE, PathMapper, RelocationRequired,
    _backup, relocate_project, restore_relocation, save_validation_report,
    validate_project,
)
from swotflow_project import create_project, load_project
from swotflow_automation import (
    AutomationConfig, AutomationRunState, AutomationStageResult, AutomationTilePlan,
    completed_stage_keys, load_automation_run_state, write_run_state,
    latest_automation_run_dir,
)
from workflow_manifest import source_signature
from swot_download_tool import read_download_manifest, manifest_row_downloaded
from swot_extract_tool import read_extract_manifest, manifest_row_completed
from ee_mosaic_tool import read_mosaic_manifest, mosaic_manifest_completed


class ProjectRelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "original"
        self.target = self.base / "copied"
        self.project = create_project(self.source, "Africa", {
            "gdal": {"python": sys.executable},
            "destination_parent": "projects/example/assets/SWOT",
            "download": {"start_date": "2026-01-01", "end_date": "2026-08-10", "utm_tiles": ["UTM32T"]},
        })
        self.db = ProjectDatabase(self.source / DATABASE_FILE_NAME)
        self.raw = self.source / "01_raw_downloads" / "observation.nc"
        self.extract = self.source / "02_extracted_geotiffs" / "observation.tif"
        self.mosaic = self.source / "03_mosaics" / "mosaic.tif"
        self.asset = "projects/example/assets/SWOT/mosaic"
        self.db.upsert_rows("download_manifest", [{"granule_id": "G1", "file_name": self.raw.name, "local_path": str(self.raw), "downloaded": "yes", "last_status": "DOWNLOADED"}])
        self.db.upsert_rows("extract_manifest", [{"record_id": "observation.nc|original", "input_nc": str(self.raw), "output_tif": str(self.extract), "status": "written", "updated_at": "2026-08-10T00:00:00"}])
        self.db.upsert_rows("mosaic_manifest", [{"output_file": str(self.mosaic), "input_files": json.dumps([str(self.extract)]), "source_signature": source_signature([self.extract]), "status": "MOSAIC_CREATED", "stale": "false"}])
        self.db.upsert_rows("upload_report", [{"asset_id": self.asset, "local_file": str(self.mosaic), "final_status": "EE_VERIFIED_EXISTS"}])
        self.db.upsert_rows("update_campaigns", [{"campaign_id": "campaign-1", "start_date": "2026-01-01", "end_date": "2026-08-10", "tiles": '["UTM32T"]'}])
        self.db.upsert_rows("update_expected", [{"record_id": "campaign-1:G1", "campaign_id": "campaign-1", "granule_id": "G1", "status": "downloaded"}])
        self.db.upsert_rows("update_runs", [{"run_id": "update-1", "campaign_id": "campaign-1", "run_type": "overlap_refresh"}])
        self.db.upsert_rows("workflow_manifest", [{"stage": "mosaic", "record_id": "mosaic.tif", "source_path": json.dumps([str(self.extract)]), "output_path": str(self.mosaic), "status": "MOSAIC_CREATED"}])
        self.db.upsert_rows("upload_events", [{"event_id": "event-1", "asset_id": self.asset, "local_file": str(self.mosaic), "new_status": "EE_VERIFIED_EXISTS"}])
        run_dir = self.source / "00_logs" / "automation_runs" / "run-1"
        state = AutomationRunState(
            run_id="run-1", run_dir=run_dir,
            config=AutomationConfig(project_root=self.source, base_config=self.project.config, utm_tiles=["UTM32T"], start_date="2026-01-01", end_date="2026-08-10", run_dir=run_dir),
            tile_plans=[AutomationTilePlan(tile="UTM32T", classification="partial/resumable", message="Test")],
            stage_results=[AutomationStageResult(run_id="run-1", tile="UTM32T", stage="extract", status="success", start_time="a", end_time="b", config_path=str(run_dir / "UTM32T" / "automation_config.yaml"), log_path=str(run_dir / "UTM32T" / "extract.log"))],
            preflight_ok=True, stopped=True,
        )
        write_run_state(state)
        config_dir = run_dir / "UTM32T"
        config_dir.mkdir()
        (config_dir / "automation_config.yaml").write_text(yaml.safe_dump(self.project.config), encoding="utf-8")
        self.db.export_csv("upload_report", self.source / "00_logs" / "upload_report.csv")

    def copy(self):
        shutil.copytree(self.source, self.target)

    def relocate(self):
        return relocate_project(self.target, confirmed_stopped=True)

    def test_preserves_history_keys_campaigns_and_resume_with_cleaned_files(self):
        self.copy()
        source_before = (self.source / "project.yaml").read_bytes()
        report = self.relocate()
        self.assertTrue(report.valid, report.summary())
        self.assertEqual(report.absent_with_downstream_proof, 3)
        self.assertEqual((self.source / "project.yaml").read_bytes(), source_before)
        project = load_project(self.target)
        self.assertEqual(project.root, self.target)
        target_db = ProjectDatabase(self.target / DATABASE_FILE_NAME)
        for dataset, count in report.dataset_counts.items():
            self.assertEqual(target_db.dataset_count(dataset), count)
            self.assertEqual(self.db.dataset_count(dataset), count)
        mosaic = target_db.read_rows("mosaic_manifest")[0]
        self.assertEqual(mosaic["output_file"], str(self.target / "03_mosaics" / "mosaic.tif"))
        self.assertEqual(mosaic["source_signature"], source_signature([self.extract]))
        self.assertEqual(target_db.read_rows("upload_report")[0]["asset_id"], self.asset)
        self.assertEqual(target_db.read_rows("update_expected"), self.db.read_rows("update_expected"))
        self.assertEqual(target_db.read_rows("update_runs"), self.db.read_rows("update_runs"))
        self.assertEqual(target_db.read_rows("extract_manifest")[0]["updated_at"], "2026-08-10T00:00:00")
        with target_db.session() as connection:
            key = connection.execute("SELECT record_key FROM records WHERE dataset='mosaic_manifest'").fetchone()[0]
        self.assertEqual(key, mosaic["output_file"])
        # A subsequent normal upsert updates the same record, rather than duplicating it.
        target_db.upsert_rows("mosaic_manifest", [mosaic])
        self.assertEqual(target_db.dataset_count("mosaic_manifest"), 1)
        run = load_automation_run_state(self.target / "00_logs" / "automation_runs" / "run-1")
        self.assertFalse(run.preflight_ok)
        self.assertEqual(run.run_id, "run-1")
        self.assertIn(("UTM32T", "extract"), completed_stage_keys(run))
        self.assertEqual(run.config.project_root, self.target)
        self.assertEqual(run.config.base_config["download"]["start_date"], "2026-01-01")
        self.assertTrue(run.stage_results[0].config_path.startswith(str(self.target)))
        logs = self.target / "00_logs"
        self.assertTrue(manifest_row_downloaded(read_download_manifest(logs / "download_manifest.csv")["G1"]))
        self.assertTrue(manifest_row_completed(read_extract_manifest(logs / "extract_manifest.csv")["observation.nc|original"]))
        self.assertTrue(mosaic_manifest_completed(read_mosaic_manifest(logs / "mosaic_manifest.csv")["mosaic.tif"]))
        second = self.relocate()
        self.assertEqual(second.dataset_counts, report.dataset_counts)

    def test_loader_never_opens_original_when_copied_root_exists(self):
        self.copy()
        with self.assertRaises(RelocationRequired):
            load_project(self.target)
        self.assertEqual(self.db.read_rows("download_manifest")[0]["local_path"], str(self.raw))

    def test_unverified_missing_mosaic_blocks_open_until_restored_and_validated(self):
        self.db.upsert_rows("upload_report", [{"asset_id": self.asset, "local_file": str(self.mosaic), "final_status": "SUBMITTED"}])
        self.copy()
        report = self.relocate()
        self.assertFalse(report.valid)
        self.assertEqual(report.missing_unresolved_files, 1)
        with self.assertRaises(RelocationRequired):
            load_project(self.target)
        (self.target / "03_mosaics" / "mosaic.tif").write_bytes(b"restored raster")
        report = validate_project(self.target)
        save_validation_report(report)
        self.assertTrue(report.valid)
        self.assertEqual(load_project(self.target).name, "Africa")

    def test_inventory_can_prove_upload_without_completed_report_status(self):
        self.db.upsert_rows("upload_report", [{"asset_id": self.asset, "local_file": str(self.mosaic), "final_status": "SUBMITTED"}])
        self.db.upsert_rows("ee_asset_inventory", [{"asset_id": self.asset, "asset_type": "IMAGE"}])
        self.copy()
        self.assertTrue(self.relocate().valid)

    def test_path_mapping_windows_to_native_boundary_and_nested_json(self):
        mapper = PathMapper([(r"D:\SWOT_e2e", str(self.target))])
        self.assertEqual(mapper.text(r"d:\swot_E2E\03_mosaics\x.tif"), str(self.target / "03_mosaics" / "x.tif"))
        self.assertEqual(mapper.text("D:/SWOT_e2e/../other/x.tif"), "D:/SWOT_e2e/../other/x.tif")
        self.assertEqual(mapper.text("D:/SWOT_e2e_other/x.tif"), "D:/SWOT_e2e_other/x.tif")
        self.assertEqual(mapper.text(self.asset), self.asset)
        result = json.loads(mapper.value(json.dumps([r"D:\SWOT_e2e\x.nc"])))
        self.assertEqual(result, [str(self.target / "x.nc")])

    def test_unmapped_external_data_is_rejected_without_changing_target(self):
        self.db.upsert_rows("extract_errors", [{"input_nc": str(self.base / "external" / "x.nc"), "error": "invalid"}])
        self.copy()
        before = (self.target / "project.yaml").read_bytes()
        with self.assertRaisesRegex(ValueError, "Unmapped historical"):
            self.relocate()
        self.assertEqual((self.target / "project.yaml").read_bytes(), before)
        self.assertFalse((self.target / PENDING_FILE).exists())
        report = relocate_project(self.target, [(str(self.base / "external"), str(self.target / "external"))], confirmed_stopped=True)
        self.assertTrue(report.valid)

    def test_backup_includes_committed_wal_rows(self):
        path = self.base / "wal.sqlite3"
        backup = self.base / "backup.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE value(x)")
            connection.execute("INSERT INTO value VALUES(42)")
            connection.commit()
            self.assertTrue(Path(str(path) + "-wal").exists())
            _backup(path, backup)
            with closing(sqlite3.connect(backup)) as copied:
                self.assertEqual(copied.execute("SELECT x FROM value").fetchone()[0], 42)

    def test_stale_csv_export_is_regenerated_from_database(self):
        # Disk snapshot predates successful verification.
        export = self.source / "00_logs" / "upload_report.csv"
        export.write_text("asset_id,local_file,final_status\n" + self.asset + "," + str(self.mosaic) + ",SUBMITTED\n", encoding="utf-8")
        self.copy()
        self.relocate()
        with (self.target / "00_logs" / "upload_report.csv").open(encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["final_status"], "EE_VERIFIED_EXISTS")
        self.assertEqual(row["local_file"], str(self.target / "03_mosaics" / "mosaic.tif"))

    def test_install_failure_rolls_back_and_keeps_backup(self):
        self.copy()
        before = (self.target / "project.yaml").read_bytes()
        real_replace = __import__("os").replace
        failed = False

        def fail_once(source, destination):
            nonlocal failed
            if Path(destination) == self.target / "project.yaml" and not failed:
                failed = True
                raise OSError("simulated interrupted install")
            return real_replace(source, destination)

        with mock.patch("project_relocation.os.replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.relocate()
        self.assertEqual((self.target / "project.yaml").read_bytes(), before)
        db = ProjectDatabase(self.target / DATABASE_FILE_NAME)
        self.assertEqual(db.read_rows("mosaic_manifest"), self.db.read_rows("mosaic_manifest"))
        self.assertFalse((self.target / PENDING_FILE).exists())
        self.assertTrue(list((self.target / ".swotflow-relocation").glob("*/before/swotflow.sqlite3")))

    def test_interrupted_relocation_recovery_and_lock_guard(self):
        self.copy()
        real_replace = __import__("os").replace
        def interrupt_install(source, destination):
            if Path(destination) == self.target / "project.yaml":
                raise KeyboardInterrupt
            return real_replace(source, destination)
        with mock.patch("project_relocation.os.replace", side_effect=interrupt_install):
            with self.assertRaises(KeyboardInterrupt):
                self.relocate()
        self.assertTrue((self.target / PENDING_FILE).exists())
        with self.assertRaises(RelocationRequired):
            load_project(self.target)
        restore_relocation(self.target)
        self.assertFalse((self.target / LOCK_FILE).exists())
        (self.target / LOCK_FILE).write_text("test", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProjectDatabase(self.target / DATABASE_FILE_NAME)
        restore_relocation(self.target)
        self.assertTrue(self.relocate().valid)

    def test_refuses_nested_target_missing_db_and_unconfirmed_changes(self):
        self.copy()
        with self.assertRaisesRegex(ValueError, "Stop all"):
            relocate_project(self.target)
        (self.target / DATABASE_FILE_NAME).unlink()
        with self.assertRaisesRegex(ValueError, "authoritative database"):
            self.relocate()

    def test_relocation_rejects_key_collision_without_losing_history(self):
        # Two histories would map to the same path; they must never be merged silently.
        self.db.upsert_rows("mosaic_manifest", [{"output_file": str(self.target / "03_mosaics" / "mosaic.tif"), "status": "ERROR"}])
        self.copy()
        with self.assertRaisesRegex(ValueError, "merge distinct history"):
            self.relocate()
        target_db = ProjectDatabase(self.target / DATABASE_FILE_NAME)
        self.assertEqual(target_db.dataset_count("mosaic_manifest"), 2)

    def test_legacy_database_without_schema_marker_is_supported(self):
        with self.db.session() as connection:
            connection.execute("DELETE FROM metadata WHERE key='schema_version'")
            connection.commit()
        self.copy()
        self.assertTrue(self.relocate().valid)

    def test_relocation_preserves_latest_run_order(self):
        runs = self.source / "00_logs" / "automation_runs"
        second = runs / "zzz-older-run"
        shutil.copytree(runs / "run-1", second)
        os.utime(second, (1000, 1000))
        os.utime(runs / "run-1", (2000, 2000))
        self.copy()
        self.relocate()
        project = load_project(self.target)
        self.assertEqual(latest_automation_run_dir(project.config, self.target).name, "run-1")

    def test_empty_files_and_duplicate_history_are_reported(self):
        self.raw.touch()
        report = validate_project(self.source)
        self.assertFalse(report.valid)
        self.assertEqual(report.issue_counts["empty_file"], 1)
        self.raw.unlink()
        self.db.upsert_rows("download_manifest", [{"granule_id": "old", "downloaded": "yes", "local_path": str(self.raw.with_name("old.nc"))}])
        (self.source / "00_logs" / "swot_duplicate_removal_old.txt").write_text("MOVED: D:\\old\\old.nc\n", encoding="utf-8")
        self.assertTrue(validate_project(self.source).valid)


if __name__ == "__main__":
    unittest.main()
