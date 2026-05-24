import csv
import json
import tempfile
import unittest
from pathlib import Path

from project_insights import (
    collect_project_insights,
    delete_cleanup_candidates,
    format_bytes,
    load_project_insights_snapshot,
    plan_cleanup_candidates,
    write_project_insights_snapshot,
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


def swot_name(
    *,
    tile: str = "UTM34M",
    crid: str = "PGD0",
    counter: str = "01",
    suffix: str = ".nc",
) -> str:
    return (
        f"SWOT_L2_HR_Raster_100m_{tile}_N_x_x_x_"
        f"034_266_000F_20260102T000000_20260102T010000_{crid}_{counter}{suffix}"
    )


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
                "ee_asset_inventory_csv": str(logs / "ee_asset_inventory.csv"),
            },
        }

    def test_collect_project_insights_counts_files_tiles_dates_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            raw = root / "01_raw_downloads" / swot_name(crid="PGD0", counter="02")
            moved = root / "01_raw_downloads" / "moved" / "old.nc"
            extracted = root / "02_extracted_geotiffs" / swot_name(crid="PGD0", counter="02", suffix=".tif")
            mosaic = root / "03_mosaics" / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_034_266_MOSA_"
                "20260102T000000_20260102T010000_PGD0_02.tif"
            )
            for path in (raw, moved, extracted, mosaic):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"12345")
            extracted.with_suffix(".tfw").write_bytes(b"tfw")
            extracted.with_suffix(".tif.aux.xml").write_bytes(b"aux")
            mosaic.with_suffix(".tfw").write_bytes(b"tfw")
            mosaic.with_suffix(".tif.aux.xml").write_bytes(b"aux")

            logs = root / "00_logs"
            write_csv(
                logs / "download_manifest.csv",
                [
                    {
                        "granule_id": "G1",
                        "file_name": swot_name(crid="PGD0", counter="02"),
                        "utm_tile": "UTM34M",
                        "start_time": "2026-01-02T00:00:00Z",
                        "size_mb": "1.5",
                        "downloaded": "yes",
                        "raw_exists": "yes",
                        "last_status": "DOWNLOADED",
                    },
                    {
                        "granule_id": "G0",
                        "file_name": swot_name(crid="PGD0", counter="01"),
                        "utm_tile": "UTM34M",
                        "start_time": "2026-01-02T00:00:00Z",
                        "size_mb": "0.5",
                        "downloaded": "no",
                        "raw_exists": "no",
                        "last_status": "EXCLUDED_OLDER_VERSION",
                        "selected_for_download": "no",
                        "duplicate_filter_status": "excluded_older_version",
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
                        "output_grid": "UTM34M",
                        "source_utm_tiles": json.dumps(["UTM34M"]),
                    },
                    {
                        "local_file": str(mosaic),
                        "asset_id": "projects/example/assets/mosaic_existing",
                        "final_status": "EE_VERIFIED_EXISTS",
                        "output_grid": "UTM34M",
                        "source_utm_tiles": json.dumps(["UTM34M"]),
                    },
                    {
                        "local_file": str(mosaic),
                        "asset_id": "projects/example/assets/mosaic_filtered",
                        "final_status": "FILTERED_UTM_TILE",
                        "upload_selected": "no",
                    },
                    {
                        "local_file": str(mosaic.with_name("missing.tif")),
                        "asset_id": "projects/example/assets/mosaic_failed",
                        "final_status": "ERROR",
                        "error_message": "browser closed",
                    }
                ],
            )

            insights = collect_project_insights(config)
            candidates = plan_cleanup_candidates(config)

            self.assertEqual(insights.metrics["Raw NetCDF files on disk"], "1")
            self.assertEqual(insights.metrics["Duplicate files moved"], "1")
            self.assertEqual(insights.metrics["Date coverage"], "2026-01-02 to 2026-01-02")
            self.assertEqual(insights.metrics["Known cumulative download size"], "2.0 MB")
            self.assertEqual(insights.metrics["Remote matches excluded as older versions"], "1")
            self.assertEqual(insights.metrics["EE-verified existing assets recorded"], "1")
            self.assertEqual(insights.metrics["Upload rows filtered by UTM selection"], "1")
            self.assertEqual(insights.metrics["Upload failures/errors recorded"], "1")
            self.assertEqual(insights.metrics["Upload-ready mosaics not uploaded/verified"], "0")
            self.assertEqual(insights.metrics["Unique processing levels observed"], "2")
            self.assertIn(("PGD0_02", 1, 1, 1, 1, 1, 2), insights.processing_level_counts)
            self.assertIn(("PGD0_01", 1, 0, 0, 0, 0, 0), insights.processing_level_counts)
            self.assertIn(("UTM34M", "PGD0_02", 1, 1, 1, 1), insights.processing_level_tile_counts)
            self.assertIn(("COMPLETED", 1), insights.upload_status_counts)
            self.assertIn(("EE_VERIFIED_EXISTS", 1), insights.upload_status_counts)
            self.assertIn(("UTM34M", 2), insights.uploaded_tile_counts)
            self.assertIn(("PGD0_02", 2), insights.uploaded_processing_level_counts)
            self.assertIn(("ERROR", "browser closed", 1), insights.upload_error_counts)
            self.assertIn(("UTM34M", 1, 1, 1, 2, 0), insights.upload_qa_tile_rows)
            self.assertIn(("UTM34M", 3), insights.tile_counts)
            self.assertIn(("UTM34M", 1), insights.mosaic_output_grid_counts)
            self.assertIn(("UTM34M", 1), insights.mosaic_source_tile_counts)
            self.assertEqual(
                [candidate.stage for candidate in candidates],
                ["extracted", "extracted", "extracted", "mosaic", "mosaic", "mosaic", "raw"],
            )
            self.assertIn(extracted.with_suffix(".tfw"), [candidate.path for candidate in candidates])
            self.assertIn(mosaic.with_suffix(".tif.aux.xml"), [candidate.path for candidate in candidates])

    def test_ee_inventory_recovers_filtered_upload_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            mosaic = root / "03_mosaics" / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_034_266_MOSA_"
                "20260102T000000_20260102T010000_PGD0_02.tif"
            )
            mosaic.parent.mkdir(parents=True, exist_ok=True)
            mosaic.write_bytes(b"12345")
            mosaic.with_suffix(".tfw").write_bytes(b"tfw")
            mosaic.with_suffix(".tif.aux.xml").write_bytes(b"aux")
            asset_id = "projects/example/assets/SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_034_266_MOSA_20260102T000000_20260102T010000_PGD0_02"
            logs = root / "00_logs"
            write_csv(
                logs / "upload_report.csv",
                [
                    {
                        "local_file": str(mosaic),
                        "asset_id": asset_id,
                        "final_status": "FILTERED_UTM_TILE",
                        "upload_selected": "no",
                        "output_grid": "UTM34M",
                    }
                ],
            )
            write_csv(
                logs / "ee_asset_inventory.csv",
                [
                    {
                        "asset_id": asset_id,
                        "asset_name": asset_id.rsplit("/", 1)[-1],
                        "asset_type": "IMAGE",
                        "listed_at": "2026-05-20T10:00:00+02:00",
                    }
                ],
            )

            insights = collect_project_insights(config)
            candidates = plan_cleanup_candidates(config)

            self.assertIn(("EE_VERIFIED_EXISTS", 1), insights.upload_status_counts)
            self.assertIn(("UTM34M", 1), insights.uploaded_tile_counts)
            self.assertEqual(insights.metrics["Uploaded/already-existing assets recorded"], "1")
            self.assertEqual([candidate.stage for candidate in candidates], ["mosaic", "mosaic", "mosaic"])

    def test_orphan_uploaded_mosaic_sidecars_are_cleanup_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            mosaic = root / "03_mosaics" / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_034_266_MOSA_"
                "20260102T000000_20260102T010000_PGD0_02.tif"
            )
            mosaic.parent.mkdir(parents=True, exist_ok=True)
            mosaic.with_suffix(".tfw").write_bytes(b"tfw")
            mosaic.with_suffix(".tif.aux.xml").write_bytes(b"aux")
            write_csv(
                root / "00_logs" / "upload_report.csv",
                [
                    {
                        "local_file": str(mosaic),
                        "asset_id": "projects/example/assets/mosaic",
                        "final_status": "EE_VERIFIED_EXISTS",
                    }
                ],
            )

            candidates = plan_cleanup_candidates(config)
            deleted, bytes_deleted, errors = delete_cleanup_candidates(candidates)

            self.assertCountEqual([candidate.path.name for candidate in candidates], [
                mosaic.with_suffix(".tif.aux.xml").name,
                mosaic.with_suffix(".tfw").name,
            ])
            self.assertEqual(deleted, 2)
            self.assertEqual(bytes_deleted, 6)
            self.assertEqual(errors, [])
            self.assertFalse(mosaic.with_suffix(".tfw").exists())
            self.assertFalse(mosaic.with_suffix(".tif.aux.xml").exists())

    def test_common_crs_mosaic_reports_source_tile_participation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            src1 = (
                root
                / "02_extracted_geotiffs"
                / "SWOT_L2_HR_Raster_100m_UTM34M_001_002_0001_20260102T000000_20260102T010000_PGC0_01.tif"
            )
            src2 = (
                root
                / "02_extracted_geotiffs"
                / "SWOT_L2_HR_Raster_100m_UTM35M_001_002_0001_20260102T000000_20260102T010000_PGC0_01.tif"
            )
            mosaic = (
                root
                / "03_mosaics"
                / "SWOT_L2_HR_Raster_100m_LAEA_001_002_MOSA_20260102T000000_20260102T010000_PGC0_01.tif"
            )
            for path in (src1, src2, mosaic):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"12345")
            write_csv(
                root / "00_logs" / "mosaic_manifest.csv",
                [
                    {
                        "status": "MOSAIC_CREATED",
                        "output_file": str(mosaic),
                        "input_count": "2",
                        "start_date": "2026-01-02",
                        "coordinate_system": "LAEA",
                        "input_files": json.dumps([str(src1), str(src2)]),
                        "output_exists": "yes",
                        "stale": "false",
                    }
                ],
            )

            insights = collect_project_insights(config)

            self.assertEqual(insights.metrics["Completed common-CRS/non-UTM mosaics"], "1")
            self.assertEqual(insights.metrics["Upload-ready mosaics not uploaded/verified"], "1")
            self.assertIn(("LAEA", 1), insights.mosaic_output_grid_counts)
            self.assertIn(("UTM34M", 1), insights.mosaic_source_tile_counts)
            self.assertIn(("UTM35M", 1), insights.mosaic_source_tile_counts)
            self.assertEqual(insights.ready_not_uploaded_rows[0][1], "UTM34M,UTM35M")

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

    def test_statistics_snapshot_round_trip_and_csv_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            write_csv(
                root / "00_logs" / "download_manifest.csv",
                [
                    {
                        "granule_id": "G1",
                        "file_name": swot_name(crid="PGD0", counter="01"),
                        "utm_tile": "UTM34M",
                        "start_time": "2026-01-02T00:00:00Z",
                        "size_mb": "1.5",
                        "downloaded": "yes",
                        "raw_exists": "no",
                        "selected_for_download": "yes",
                        "last_status": "SKIPPED_MANIFEST",
                    }
                ],
            )
            insights = collect_project_insights(config)

            snapshot_path = write_project_insights_snapshot(config, insights)
            loaded = load_project_insights_snapshot(config)

            self.assertTrue(snapshot_path.exists())
            self.assertTrue((root / "00_logs" / "statistics" / "project_statistics_metrics.csv").exists())
            self.assertTrue((root / "00_logs" / "statistics" / "project_statistics_processing_levels.csv").exists())
            self.assertIsNotNone(loaded)
            loaded_insights, generated_at = loaded
            self.assertTrue(generated_at)
            self.assertEqual(
                loaded_insights.metrics["Downloaded granules recorded"],
                insights.metrics["Downloaded granules recorded"],
            )
            self.assertEqual(loaded_insights.tile_counts, insights.tile_counts)
            self.assertEqual(loaded_insights.processing_level_counts, insights.processing_level_counts)
            self.assertEqual(loaded_insights.upload_status_counts, insights.upload_status_counts)
            self.assertEqual(loaded_insights.upload_qa_tile_rows, insights.upload_qa_tile_rows)


if __name__ == "__main__":
    unittest.main()
