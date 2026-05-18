import csv
import json
import tempfile
import unittest
from pathlib import Path

from project_insights import (
    collect_project_insights,
    delete_cleanup_candidates,
    format_bytes,
    plan_cleanup_candidates,
)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class ProjectInsightsTests(unittest.TestCase):
    def sample_project(self, root: Path) -> dict:
        logs = root / "00_logs"
        return {
            "processing": {
                "root": str(root),
                "raw_downloads": str(root / "01_raw_downloads"),
                "extracted_geotiffs": str(root / "02_extracted_geotiffs"),
                "mosaics": str(root / "03_mosaics"),
                "logs": str(logs),
            },
            "download": {
                "manifest_csv": str(logs / "download_manifest.csv"),
            },
            "duplicates": {
                "moved_folder_name": "moved",
            },
            "extract": {
                "manifest_csv": str(logs / "extract_manifest.csv"),
            },
            "mosaic": {
                "manifest_csv": str(logs / "mosaic_manifest.csv"),
            },
            "artifacts": {
                "report_csv": str(logs / "upload_report.csv"),
            },
        }

    def test_collect_project_insights_counts_files_tiles_dates_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            raw = root / "01_raw_downloads" / "raw.nc"
            moved = root / "01_raw_downloads" / "moved" / "old.nc"
            extracted = root / "02_extracted_geotiffs" / "tile.tif"
            mosaic = root / "03_mosaics" / "mosaic.tif"
            for path in (raw, moved, extracted, mosaic):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"12345")

            logs = root / "00_logs"
            write_csv(
                logs / "download_manifest.csv",
                [
                    {
                        "granule_id": "G1",
                        "file_name": "SWOT_L2_HR_Raster_100m_UTM34M_x.nc",
                        "utm_tile": "UTM34M",
                        "start_time": "2026-01-02T00:00:00Z",
                        "size_mb": "1.5",
                        "downloaded": "yes",
                        "raw_exists": "yes",
                        "last_status": "DOWNLOADED",
                    }
                ],
            )
            write_csv(
                logs / "extract_manifest.csv",
                [
                    {
                        "record_id": "raw.nc|original",
                        "status": "written",
                        "input_nc": str(raw),
                        "output_tif": str(extracted),
                        "target_crs_mode": "original",
                        "raw_exists": "yes",
                        "output_exists": "yes",
                        "utm_zone": "34",
                        "mgrs_band": "M",
                        "date": "2026-01-02",
                    }
                ],
            )
            write_csv(
                logs / "mosaic_manifest.csv",
                [
                    {
                        "status": "MOSAIC_CREATED",
                        "output_file": str(mosaic),
                        "input_count": "1",
                        "start_date": "2026-01-02",
                        "coordinate_system": "UTM34M",
                        "input_files": json.dumps([str(extracted)]),
                        "output_exists": "yes",
                        "stale": "false",
                    }
                ],
            )
            write_csv(
                logs / "upload_report.csv",
                [
                    {
                        "local_file": str(mosaic),
                        "asset_id": "projects/example/assets/mosaic",
                        "final_status": "COMPLETED",
                    }
                ],
            )

            insights = collect_project_insights(config)
            candidates = plan_cleanup_candidates(config)

            self.assertEqual(insights.metrics["Raw NetCDF files on disk"], "1")
            self.assertEqual(insights.metrics["Duplicate files moved"], "1")
            self.assertEqual(insights.metrics["Date coverage"], "2026-01-02 to 2026-01-02")
            self.assertEqual(insights.metrics["Known cumulative download size"], "1.5 MB")
            self.assertIn(("UTM34M", 2), insights.tile_counts)
            self.assertEqual([candidate.stage for candidate in candidates], ["extracted", "mosaic", "raw"])

    def test_delete_cleanup_candidates_removes_only_candidate_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            raw = root / "01_raw_downloads" / "raw.nc"
            extracted = root / "02_extracted_geotiffs" / "tile.tif"
            for path in (raw, extracted):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"12345")
            write_csv(
                root / "00_logs" / "extract_manifest.csv",
                [
                    {
                        "status": "written",
                        "input_nc": str(raw),
                        "output_tif": str(extracted),
                    }
                ],
            )

            candidates = plan_cleanup_candidates(config)
            deleted, bytes_deleted, errors = delete_cleanup_candidates(candidates)

            self.assertEqual(deleted, 1)
            self.assertEqual(bytes_deleted, 5)
            self.assertEqual(errors, [])
            self.assertFalse(raw.exists())
            self.assertTrue(extracted.exists())

    def test_stale_mosaic_row_does_not_cleanup_extracted_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            extracted = root / "02_extracted_geotiffs" / "tile.tif"
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(b"12345")
            write_csv(
                root / "00_logs" / "mosaic_manifest.csv",
                [
                    {
                        "status": "MOSAIC_CREATED",
                        "input_files": json.dumps([str(extracted)]),
                        "stale": "true",
                    }
                ],
            )

            candidates = plan_cleanup_candidates(config)

            self.assertEqual(candidates, [])

    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")


if __name__ == "__main__":
    unittest.main()
