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
from project_updates import record_update_expected_rows


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
    scene: str = "000F",
    crid: str = "PGD0",
    counter: str = "01",
    suffix: str = ".nc",
) -> str:
    return (
        f"SWOT_L2_HR_Raster_100m_{tile}_N_x_x_x_"
        f"034_266_{scene}_20260102T000000_20260102T010000_{crid}_{counter}{suffix}"
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
            self.assertEqual(insights.metrics["Mosaic lineage rows"], "2")
            self.assertEqual(insights.metrics["Lineage used in mosaic"], "1")
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
            self.assertIn("used_in_mosaic", {row["lineage_status"] for row in insights.mosaic_lineage_rows})
            self.assertIn("remote_excluded_older_version", {row["lineage_status"] for row in insights.mosaic_lineage_rows})
            self.assertEqual(
                [candidate.stage for candidate in candidates],
                ["extracted", "extracted", "extracted", "mosaic", "mosaic", "mosaic", "raw"],
            )
            self.assertIn(extracted.with_suffix(".tfw"), [candidate.path for candidate in candidates])
            self.assertIn(mosaic.with_suffix(".tif.aux.xml"), [candidate.path for candidate in candidates])

    def test_update_coverage_rows_follow_preview_to_uploaded_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            logs = root / "00_logs"
            config["download"]["report_csv"] = str(logs / "download_preview.csv")
            config["download"]["start_date"] = "2026-01-01"
            config["download"]["end_date"] = "2026-06-30"
            config["download"]["utm_tiles"] = ["UTM34M", "UTM35M"]

            raw_34_a = swot_name(tile="UTM34M", scene="001F")
            raw_34_b = swot_name(tile="UTM34M", scene="002F")
            raw_35 = swot_name(tile="UTM35M", scene="003F")
            tif_34_a = root / "02_extracted_geotiffs" / swot_name(tile="UTM34M", scene="001F", suffix=".tif")
            tif_35 = root / "02_extracted_geotiffs" / swot_name(tile="UTM35M", scene="003F", suffix=".tif")
            mosaic_34 = root / "03_mosaics" / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_034_266_MOSA_"
                "20260102T000000_20260102T010000_PGD0_01.tif"
            )
            mosaic_35 = root / "03_mosaics" / (
                "SWOT_L2_HR_Raster_100m_UTM35M_N_x_x_x_034_266_MOSA_"
                "20260102T000000_20260102T010000_PGD0_01.tif"
            )
            for path in (tif_34_a, tif_35, mosaic_34, mosaic_35):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"12345")

            write_csv(
                logs / "download_preview.csv",
                [
                    {
                        "file_name": raw_34_a,
                        "utm_tile": "UTM34M",
                        "start_time": "2026-01-02T00:00:00Z",
                        "selected_for_download": "yes",
                        "downloaded": "yes",
                        "status": "SKIPPED_MANIFEST",
                    },
                    {
                        "file_name": raw_34_b,
                        "utm_tile": "UTM34M",
                        "start_time": "2026-01-02T02:00:00Z",
                        "selected_for_download": "yes",
                        "downloaded": "yes",
                        "status": "SKIPPED_MANIFEST",
                    },
                    {
                        "file_name": raw_35,
                        "utm_tile": "UTM35M",
                        "start_time": "2026-01-02T00:00:00Z",
                        "selected_for_download": "yes",
                        "downloaded": "yes",
                        "status": "SKIPPED_MANIFEST",
                    },
                ],
            )
            record_update_expected_rows(
                config,
                [
                    {
                        "file_name": raw_34_a,
                        "utm_tile": "UTM34M",
                        "start_time": "2026-01-02T00:00:00Z",
                        "downloaded": "yes",
                    },
                    {
                        "file_name": raw_34_b,
                        "utm_tile": "UTM34M",
                        "start_time": "2026-01-02T02:00:00Z",
                        "downloaded": "yes",
                    },
                    {
                        "file_name": raw_35,
                        "utm_tile": "UTM35M",
                        "start_time": "2026-01-02T00:00:00Z",
                        "downloaded": "yes",
                    },
                ],
                source="test",
                campaign_tiles=["UTM34M", "UTM35M"],
            )
            write_csv(
                logs / "extract_manifest.csv",
                [
                    {
                        "record_id": "34a|original",
                        "status": "written",
                        "input_nc": str(root / "01_raw_downloads" / raw_34_a),
                        "output_tif": str(tif_34_a),
                        "utm_zone": "34",
                        "mgrs_band": "M",
                        "date": "2026-01-02",
                    },
                    {
                        "record_id": "35|original",
                        "status": "written",
                        "input_nc": str(root / "01_raw_downloads" / raw_35),
                        "output_tif": str(tif_35),
                        "utm_zone": "35",
                        "mgrs_band": "M",
                        "date": "2026-01-02",
                    },
                ],
            )
            write_csv(
                logs / "mosaic_manifest.csv",
                [
                    {
                        "status": "MOSAIC_CREATED",
                        "output_file": str(mosaic_34),
                        "input_files": json.dumps([str(tif_34_a)]),
                        "start_date": "2026-01-02",
                        "coordinate_system": "UTM34M",
                    },
                    {
                        "status": "MOSAIC_CREATED",
                        "output_file": str(mosaic_35),
                        "input_files": json.dumps([str(tif_35)]),
                        "start_date": "2026-01-02",
                        "coordinate_system": "UTM35M",
                    },
                ],
            )
            write_csv(
                logs / "upload_report.csv",
                [
                    {
                        "local_file": str(mosaic_34),
                        "asset_id": "projects/example/assets/mosaic34",
                        "final_status": "COMPLETED",
                        "output_grid": "UTM34M",
                        "source_utm_tiles": json.dumps(["UTM34M"]),
                    },
                    {
                        "local_file": str(mosaic_35),
                        "asset_id": "projects/example/assets/mosaic35",
                        "final_status": "EE_VERIFIED_EXISTS",
                        "output_grid": "UTM35M",
                        "source_utm_tiles": json.dumps(["UTM35M"]),
                    },
                ],
            )

            insights = collect_project_insights(config)
            rows = {row[0]: row for row in insights.update_coverage_tile_rows}

            self.assertEqual(rows["UTM34M"][1:6], (2, 2, 1, 1, 1))
            self.assertEqual(rows["UTM34M"][-1], "pending_extract")
            self.assertEqual(rows["UTM35M"][1:6], (1, 1, 1, 1, 1))
            self.assertEqual(rows["UTM35M"][-1], "complete")
            self.assertEqual(len(insights.update_campaigns), 1)
            self.assertIn("UTM35M", rows)

    def test_update_campaigns_keep_multiple_date_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            config["download"]["start_date"] = "2026-01-01"
            config["download"]["end_date"] = "2026-05-31"
            first = swot_name(tile="UTM34M", scene="001F")
            record_update_expected_rows(
                config,
                [{"file_name": first, "utm_tile": "UTM34M", "start_time": "2026-01-02T00:00:00Z"}],
                source="test",
                campaign_tiles=["UTM34M"],
            )
            config["download"]["start_date"] = "2026-06-01"
            config["download"]["end_date"] = "2026-12-31"
            second = swot_name(tile="UTM35M", scene="002F").replace("20260102", "20260602")
            record_update_expected_rows(
                config,
                [{"file_name": second, "utm_tile": "UTM35M", "start_time": "2026-06-02T00:00:00Z"}],
                source="test",
                campaign_tiles=["UTM35M"],
            )

            insights = collect_project_insights(config)

            self.assertEqual(len(insights.update_campaigns), 2)
            self.assertEqual(len(insights.update_coverage_campaign_rows), 2)
            campaign_tiles = {
                campaign_id: {row[0] for row in rows}
                for campaign_id, rows in insights.update_coverage_campaign_rows.items()
            }
            self.assertIn({"UTM34M"}, campaign_tiles.values())
            self.assertIn({"UTM35M"}, campaign_tiles.values())

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

    def test_orphan_temporary_mosaic_sidecar_is_cleanup_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            sidecar = root / "03_mosaics" / "mosaic.part.tif.aux.xml"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_bytes(b"aux")

            candidates = plan_cleanup_candidates(config)

            self.assertEqual([candidate.path for candidate in candidates], [sidecar])
            self.assertEqual(candidates[0].stage, "temporary")
            self.assertIn("Orphaned temporary mosaic sidecar", candidates[0].reason)

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

    def test_incompatible_mosaic_sources_without_raw_are_qa_cleanup_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            extracted = root / "02_extracted_geotiffs" / swot_name(suffix=".tif")
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(b"12345")
            extracted.with_suffix(".tif.aux.xml").write_bytes(b"aux")
            write_csv(
                root / "00_logs" / "mosaic_manifest.csv",
                [
                    {
                        "status": "SKIPPED_INCOMPATIBLE",
                        "input_files": json.dumps([str(extracted)]),
                        "message": "source differs from first file in: res.",
                    }
                ],
            )

            candidates = plan_cleanup_candidates(config)

            self.assertEqual([candidate.stage for candidate in candidates], ["qa", "qa"])
            self.assertCountEqual(
                [candidate.path for candidate in candidates],
                [extracted, extracted.with_suffix(".tif.aux.xml")],
            )
            self.assertIn("no local raw NetCDF left for repair", candidates[0].reason)

    def test_incompatible_mosaic_sources_with_raw_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            raw = root / "01_raw_downloads" / swot_name()
            extracted = root / "02_extracted_geotiffs" / swot_name(suffix=".tif")
            for path in (raw, extracted):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"12345")
            write_csv(
                root / "00_logs" / "mosaic_manifest.csv",
                [
                    {
                        "status": "SKIPPED_INCOMPATIBLE",
                        "input_files": json.dumps([str(extracted)]),
                        "message": "source differs from first file in: res.",
                    }
                ],
            )

            candidates = plan_cleanup_candidates(config)

            self.assertEqual(candidates, [])

    def test_partial_mosaic_exclusions_are_reported_and_cleanable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.sample_project(root)
            used = root / "02_extracted_geotiffs" / swot_name(scene="000F", suffix=".tif")
            excluded = root / "02_extracted_geotiffs" / swot_name(scene="001F", suffix=".tif")
            mosaic = root / "03_mosaics" / (
                "SWOT_L2_HR_Raster_100m_UTM34M_N_x_x_x_034_266_MOSA_"
                "20260102T000000_20260102T010000_PGD0_01.tif"
            )
            for path in (used, excluded, mosaic):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"12345")
            excluded.with_suffix(".tif.aux.xml").write_bytes(b"aux")
            write_csv(
                root / "00_logs" / "mosaic_manifest.csv",
                [
                    {
                        "status": "MOSAIC_CREATED_WITH_EXCLUSIONS",
                        "output_file": str(mosaic),
                        "input_count": "1",
                        "original_input_count": "2",
                        "excluded_input_count": "1",
                        "start_date": "2026-01-02",
                        "coordinate_system": "UTM34M",
                        "input_files": json.dumps([str(used)]),
                        "excluded_input_files": json.dumps([str(excluded)]),
                        "excluded_reasons": json.dumps(["bad geotransform"]),
                        "output_exists": "yes",
                        "stale": "false",
                    }
                ],
            )

            insights = collect_project_insights(config)
            candidates = plan_cleanup_candidates(config)

            self.assertEqual(insights.metrics["Mosaic source files excluded"], "1")
            self.assertEqual(
                insights.mosaic_exclusion_rows,
                [(str(mosaic), str(excluded), "bad geotransform", "2026-01-02", "UTM34M")],
            )
            self.assertEqual(
                [row["lineage_status"] for row in insights.mosaic_lineage_rows],
                ["used_in_mosaic", "excluded_from_partial_mosaic"],
            )
            self.assertCountEqual(
                [(candidate.stage, candidate.path) for candidate in candidates],
                [
                    ("extracted", used),
                    ("qa", excluded),
                    ("qa", excluded.with_suffix(".tif.aux.xml")),
                ],
            )

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
            self.assertEqual(loaded_insights.mosaic_lineage_rows, insights.mosaic_lineage_rows)


if __name__ == "__main__":
    unittest.main()
