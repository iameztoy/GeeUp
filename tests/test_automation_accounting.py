"""Annual progress must be justified by retained inputs, including after cleanup."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from swotflow.processing import automation as engine
from swotflow.processing.automation_accounting import read_window_previews, verify_window_inputs
from swotflow.project.database import upsert_project_rows
from tests.test_automation import base_config, fake_granule


class AnnualInputAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tile = "UTM34M"
        self.start, self.end = "2026-01-01", "2026-01-31"
        config = base_config(self.root)
        scope = {"acquisition_start_date": self.start, "acquisition_end_date": self.end}
        config["processing"].update(scope)
        config["download"].update(scope, utm_tiles=[self.tile])
        config["automation"] = {"yearly_batch": True}
        run_dir = self.root / "00_logs/automation_runs/annual/2026"
        settings = engine.AutomationConfig(self.root, config, [self.tile], self.start, self.end,
                                           run_id="annual-2026", run_dir=run_dir)
        self.state = engine.AutomationRunState("annual-2026", run_dir, settings, preflight_ok=True)
        self.name = fake_granule(self.tile).file_name
        self.raw = self.root / "01_raw_downloads" / self.name
        self.tif = self.root / "02_extracted_geotiffs" / Path(self.name).with_suffix(".tif")
        self.mosaic = self.root / "03_mosaics" / Path(self.name).with_suffix(".tif")
        self.preview = {"file_name": self.name, "utm_tile": self.tile, "start_time": "2026-01-02 00:00:00",
                        "status": "DOWNLOADED", "selected_for_download": "yes", "downloaded": "yes"}
        self.tile_config = engine.build_tile_config_dict(config, self.tile, run_dir,
            start_date=self.start, end_date=self.end, include_upload=False)
        self.config_path = engine.write_tile_config(self.tile_config, run_dir / self.tile / "automation_config.yaml")

    def csv(self, path, rows, columns=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns or list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def successful_download(self, rows=None):
        self.csv(self.tile_config["download"]["report_csv"],
                 [self.preview] if rows is None else rows, list(self.preview))
        self.state.stage_results.append(engine.AutomationStageResult(
            self.state.run_id, self.tile, "download", "success", config_path=str(self.config_path)))

    def lineage(self, *, extract=True, mosaic=True, upload=False, source=None):
        name = source or self.name
        raw = self.raw.with_name(name)
        tif = self.tif.with_name(Path(name).with_suffix(".tif").name)
        config = self.state.config.base_config
        upsert_project_rows(config["download"]["manifest_csv"], [dict(self.preview, file_name=name, downloaded="yes")])
        if extract:
            upsert_project_rows(config["extract"]["manifest_csv"], [{"status": "written", "input_nc": str(raw),
                "output_tif": str(tif), "utm_zone": "34", "mgrs_band": "M", "date": "2026-01-02"}])
        if mosaic:
            upsert_project_rows(config["mosaic"]["manifest_csv"], [{"status": "MOSAIC_CREATED", "output_file": str(self.mosaic),
                "input_files": json.dumps([str(tif)]), "stale": "false"}])
        if upload:
            upsert_project_rows(config["artifacts"]["report_csv"], [{"final_status": "EE_VERIFIED_EXISTS", "local_file": str(self.mosaic)}])

    def test_retained_lineage_accounts_for_cleaned_up_files(self):
        self.successful_download()
        self.lineage()
        self.assertFalse(self.raw.exists())
        self.assertFalse(self.tif.exists())
        self.assertFalse(self.mosaic.exists())
        verify_window_inputs(self.state)
        self.assertEqual(self.state.errors, [])
        receipt = self.state.stage_results[-1]
        self.assertEqual(receipt.status, "warning")
        self.assertIn("Input accounting complete for 1", receipt.message)
        self.assertIn("Pixel correctness is not certified", receipt.message)

    def test_zero_output_success_does_not_mask_missing_source_lineage(self):
        self.successful_download()
        self.lineage(extract=False, mosaic=False)
        self.state.stage_results.extend(engine.AutomationStageResult(self.state.run_id, self.tile, stage, "success")
                                        for stage in ("extract", "mosaic"))
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "failed")
        self.assertIn("0 extracted", self.state.errors[-1])

    def test_missing_upload_blocks_only_upload_enabled_batch(self):
        self.successful_download()
        self.lineage()
        self.state.config.include_upload = True
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "failed")
        self.lineage(upload=True)
        self.state.errors.clear()
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "warning")
        self.assertFalse(self.state.errors)

    def test_empty_confirmed_search_differs_from_unknown_or_failed(self):
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "failed")
        self.state.errors.clear()
        self.successful_download(rows=[])
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "warning")
        self.assertIn("0 selected sources", self.state.stage_results[-1].message)
        self.state.stage_results.append(engine.AutomationStageResult(self.state.run_id, self.tile, "download", "failed"))
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "failed")

    def test_missing_header_duplicate_and_wrong_year_reports_fail_closed(self):
        self.successful_download()
        report = Path(self.tile_config["download"]["report_csv"])
        report.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "header"):
            read_window_previews(self.state)
        self.successful_download(rows=[self.preview, self.preview])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            read_window_previews(self.state)
        self.successful_download(rows=[dict(self.preview, file_name=self.name.replace("20260102", "20250102"))])
        with self.assertRaisesRegex(ValueError, "outside"):
            read_window_previews(self.state)

    def test_skipped_stage_cannot_hide_a_failed_later_download(self):
        self.successful_download(rows=[])
        self.state.stage_results.extend(engine.AutomationStageResult(self.state.run_id, self.tile, "download", status)
                                        for status in ("failed", "skipped"))
        with self.assertRaisesRegex(ValueError, "skipped download"):
            read_window_previews(self.state)

    def test_conflicting_preferred_versions_and_stale_config_fail_closed(self):
        newer = dict(self.preview, file_name=self.name.replace("_01.nc", "_02.nc"))
        self.successful_download(rows=[self.preview, newer])
        with self.assertRaisesRegex(ValueError, "conflicting preferred"):
            read_window_previews(self.state)
        self.successful_download(rows=[])
        self.tile_config["download"]["end_date"] = "2026-12-31"
        engine.write_tile_config(self.tile_config, self.config_path)
        with self.assertRaisesRegex(ValueError, "differs"):
            read_window_previews(self.state)

    def test_preferred_version_requires_at_least_the_selected_version(self):
        self.successful_download()
        self.lineage(source=self.name.replace("_PGD0_01", "_PIC2_01"))
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "failed")

    def test_verified_mode_requires_exact_existing_reconciliation(self):
        self.state.config.base_config["download"]["verified_downloads"] = True
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "failed")
        self.state.errors.clear()
        self.state.stage_results.append(engine.AutomationStageResult(self.state.run_id, self.tile,
            "reconciliation", "warning", period_start=self.start, period_end="2026-12-31"))
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results[-1].status, "failed")
        self.state.errors.clear()
        self.state.stage_results.append(engine.AutomationStageResult(self.state.run_id, self.tile,
            "reconciliation", "warning", period_start=self.start, period_end=self.end))
        count = len(self.state.stage_results)
        verify_window_inputs(self.state)
        self.assertEqual(len(self.state.stage_results), count)
        self.assertEqual(self.state.errors, [])

    def test_normal_mode_is_unchanged(self):
        self.state.config.base_config["automation"]["yearly_batch"] = False
        verify_window_inputs(self.state)
        self.assertEqual(self.state.stage_results, [])
        self.assertEqual(self.state.errors, [])


if __name__ == "__main__":
    unittest.main()
