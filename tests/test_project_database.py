import csv
import tempfile
import unittest
from pathlib import Path

from project_database import (
    ProjectDatabase,
    database_path_for,
    migrate_project_csvs,
    read_project_rows,
    upsert_project_rows,
)


class ProjectDatabaseTests(unittest.TestCase):
    def test_csv_is_imported_once_and_sqlite_becomes_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp) / "00_logs"
            logs.mkdir()
            report = logs / "upload_report.csv"
            with report.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["asset_id", "final_status"])
                writer.writeheader()
                writer.writerow({"asset_id": "asset/a", "final_status": "SUBMITTED"})

            first = read_project_rows(report, "upload_report")
            report.unlink()
            second = read_project_rows(report, "upload_report")

            self.assertEqual(first, second)
            self.assertEqual(database_path_for(report), Path(temp) / "swotflow.sqlite3")

    def test_upsert_changes_one_record_without_rewriting_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "upload_report.csv"
            upsert_project_rows(
                report,
                [{"asset_id": "asset/a", "final_status": "PLANNED_UPLOAD"}],
                dataset="upload_report",
            )
            self.assertFalse(report.exists())

            upsert_project_rows(
                report,
                [{"asset_id": "asset/a", "final_status": "SUBMITTED"}],
                dataset="upload_report",
            )
            rows = read_project_rows(report, "upload_report")
            self.assertEqual(rows[0]["final_status"], "SUBMITTED")

    def test_project_migration_preserves_csv_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "00_logs"
            logs.mkdir()
            manifest = logs / "download_manifest.csv"
            with manifest.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["granule_id", "last_status"])
                writer.writeheader()
                writer.writerow({"granule_id": "G1", "last_status": "DOWNLOADED"})

            result = migrate_project_csvs(root)
            database = ProjectDatabase(result.database_path)

            self.assertTrue(manifest.exists())
            self.assertEqual(result.imported_rows["download_manifest"], 1)
            self.assertEqual(database.dataset_count("download_manifest"), 1)
            self.assertEqual(database.status_counts("download_manifest"), {"DOWNLOADED": 1})


if __name__ == "__main__":
    unittest.main()
