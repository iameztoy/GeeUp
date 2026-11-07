"""Long-run catalogue freshness must not erase work or bypass new-input checks."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from swotflow.catalog import history as h
from swotflow.catalog.ui import verification_view_text
from swotflow.processing import automation as auto
from swotflow.processing import download as d
from swotflow.processing import reconciliation_catalogue as refresh
from swotflow.processing.automation_years import child_outcome
from swotflow.verification import pipeline as p
from tests import test_pipeline_verification as fixtures
from tests.test_download_tool import FakeGranule, FakePagedEarthaccess


class CatalogueBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        fixtures.ProofTests.setUp(self)
        logs = self.store.logs
        self.base = {
            "processing": {"logs": str(logs), "raw_downloads": str(self.root / "raw"),
                           "acquisition_start_date": "2026-01-01", "acquisition_end_date": "2026-01-31"},
            "download": {"manifest_csv": str(logs / "download_manifest.csv"),
                         "report_csv": str(logs / "download_preview.csv"), "verified_downloads": True,
                         "utm_tiles": ["UTM30R"], "start_date": "2026-01-01", "end_date": "2026-01-31"},
            "destination_parent": self.asset["id"].rsplit("/", 1)[0],
        }
        self.cfg = auto.AutomationConfig(self.root, self.base, ["UTM30R"], "2026-01-01", "2026-01-31",
                                         include_upload=True, prevent_system_sleep=False)
        self.state = auto.AutomationRunState("boundary", self.root / "run", self.cfg, preflight_ok=True)
        self.state.stage_results = [auto.AutomationStageResult("boundary", "UTM30R", stage, "success")
                                    for stage in ("download", "duplicates", "extract", "raw_cleanup", "mosaic",
                                                  "extracted_cleanup", "upload", "ee_sync", "mosaic_cleanup")]
        self.download_config = d.parse_config(self.base, self.root)
        self.ledger = h.CatalogHistory(logs / "nasa_catalog_history.sqlite3")
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with mock.patch.object(h, "utc_now", return_value=old):
            preview = d.build_download_preview(self.download_config, FakePagedEarthaccess([self.g.source]))
        self.old_id = preview.history_scan_id
        self.old_snapshot = self.ledger.snapshot(self.old_id)
        receipt = self.store.prepare_ingestion(self.asset["id"], self.mosaic_proof)
        self.store.record_ingestion({**receipt, "phase": "submitted", "task_id": "already-finished-task"})
        self.asset["properties"] = p.ingestion_properties(receipt)
        p.save_inventory(self.store, self.inventory)
        self.original_bytes = {path: path.read_bytes() for path in (self.raw, self.extract, self.mosaic)}

    def reconcile(self, granules=None, *, effect=None) -> mock.Mock:
        def search(config):
            self.assertEqual(config.utm_tiles, ["UTM30R"])
            self.assertEqual((config.acquisition_start_date, config.acquisition_end_date),
                             ("2026-01-01", "2026-01-31"))
            return d.build_download_preview(config, FakePagedEarthaccess(granules if granules is not None else [self.g.source]))

        def report(root, store, scan_id, inventory):
            return p.reconcile(self.ledger.snapshot(scan_id), {"mosaic_manifest": []}, inventory, store)

        with mock.patch.object(refresh, "build_download_preview", side_effect=effect or search) as searched, \
             mock.patch.object(h, "represented_inputs", return_value=([{"filename": self.name, "asset_id": self.asset["id"]}], [])), \
             mock.patch.object(p, "project_report", side_effect=report), \
             mock.patch.object(auto, "run_download", side_effect=AssertionError("No raster download at reconciliation")):
            auto.reconcile_verified_run(self.state)
        for path, data in self.original_bytes.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.ledger.snapshot(self.old_id), self.old_snapshot)
        return searched

    def latest_report(self) -> dict:
        return json.loads((self.store.logs / "verification_reports/latest.json").read_text(encoding="utf-8"))

    def test_old_catalogue_refreshes_metadata_and_preserves_history_and_counts(self) -> None:
        searched = self.reconcile()
        searched.assert_called_once()
        self.assertFalse(self.state.errors)
        self.assertEqual(child_outcome(self.state), "complete")
        self.assertEqual(len(self.ledger.scans()), 2)
        report = self.latest_report()
        self.assertTrue(report["input_accounting_complete"])
        self.assertFalse(report["pixel_verification_complete"])
        self.assertEqual(report["expected_preferred_inputs"], 1)
        self.assertEqual(report["counts"], {"ee_revision_bound_pixels_pending": 1})
        self.assertEqual(report["catalogue_refresh"]["previous_scan_id"], self.old_id)
        self.assertTrue(report["catalogue_refresh"]["performed"])
        self.assertIn("automatically refreshed", self.state.stage_results[-1].message)
        summary, status = verification_view_text(report)
        self.assertIn("1 preferred inputs", summary)
        self.assertNotIn("refresh needed", status)
        self.assertFalse((self.root / "raw").exists())

    def test_recent_scope_avoids_network_refresh(self) -> None:
        preview = d.build_download_preview(self.download_config, FakePagedEarthaccess([self.g.source]))
        searched = self.reconcile()
        searched.assert_not_called()
        self.assertEqual(self.latest_report()["scan_id"], preview.history_scan_id)
        self.assertFalse(self.latest_report()["catalogue_refresh"]["performed"])

    def test_new_preferred_version_keeps_next_year_blocked(self) -> None:
        name = self.name.replace("_01.nc", "_02.nc")
        newer = FakeGranule(name, concept_id="G2", size_mb=None)
        newer["umm"]["DataGranule"] = copy.deepcopy(self.g.source["umm"]["DataGranule"])
        newer["umm"]["DataGranule"]["ArchiveAndDistributionInformation"][0]["Name"] = name
        self.reconcile([self.g.source, newer])
        self.assertTrue(self.state.errors)
        self.assertEqual(child_outcome(self.state), "incomplete")
        self.assertFalse(self.latest_report()["input_accounting_complete"])
        self.assertEqual(self.latest_report()["rows"][0]["filename"], name)

    def test_changed_checksum_under_same_name_keeps_next_year_blocked(self) -> None:
        changed = copy.deepcopy(self.g.source)
        changed["umm"]["DataGranule"]["ArchiveAndDistributionInformation"][0]["Checksum"]["Value"] = "a" * 32
        self.reconcile([changed])
        self.assertTrue(self.state.errors)
        self.assertFalse(self.latest_report()["input_accounting_complete"])
        self.assertEqual(child_outcome(self.state), "incomplete")

    def test_new_acquisition_is_counted_separately_from_completed_work(self) -> None:
        name = self.name.replace("001F", "002F")
        self.assertNotEqual(name, self.name)
        extra = FakeGranule(name, concept_id="G2", size_mb=None)
        extra["umm"]["DataGranule"] = copy.deepcopy(self.g.source["umm"]["DataGranule"])
        extra["umm"]["DataGranule"]["ArchiveAndDistributionInformation"][0]["Name"] = name
        self.reconcile([self.g.source, extra])
        report = self.latest_report()
        self.assertEqual(report["expected_preferred_inputs"], 2)
        self.assertEqual(report["counts"]["ee_revision_bound_pixels_pending"], 1)
        self.assertEqual(report["counts"]["download_missing_or_unverified"], 1)
        self.assertFalse(report["input_accounting_complete"])
        self.assertEqual(child_outcome(self.state), "incomplete")

    def test_disappeared_catalogue_entry_requires_review_not_empty_success(self) -> None:
        self.reconcile([])
        self.assertTrue(self.state.errors)
        self.assertFalse(self.latest_report()["input_accounting_complete"])
        self.assertTrue(self.latest_report()["version_review"])

    def test_failed_metadata_refresh_retains_completion_history_and_blocks_gate(self) -> None:
        self.reconcile(effect=TimeoutError("private response details"))
        self.assertEqual(child_outcome(self.state), "incomplete")
        self.assertEqual(self.state.stage_results[-1].return_code, 2)
        self.assertIn("Completed files and histories are retained", self.state.errors[-1])
        self.assertNotIn("private response", self.state.errors[-1])
        self.assertEqual(len(self.ledger.scans()), 1)

    def test_partial_search_is_not_accepted_as_current(self) -> None:
        def partial(config):
            scan = self.ledger.begin(self.old_snapshot["scope"])
            self.ledger.finish(scan, [], "limited")
            return d.DownloadPreview(d.build_download_query(config), [], scan)
        self.reconcile(effect=partial)
        self.assertEqual(child_outcome(self.state), "incomplete")
        self.assertIn("incomplete or outside", self.state.errors[-1])

    def test_wrong_scope_cannot_supply_the_freshness_receipt(self) -> None:
        def wrong_scope(config):
            scope = dict(self.old_snapshot["scope"], tiles=["UTM31S"])
            scan = self.ledger.begin(scope)
            self.ledger.finish(scan, [], "complete")
            return d.DownloadPreview(d.build_download_query(config), [], scan)
        self.reconcile(effect=wrong_scope)
        self.assertEqual(child_outcome(self.state), "incomplete")
        self.assertIn("outside this tile/date scope", self.state.errors[-1])

    def test_local_only_completion_does_not_request_remote_freshness(self) -> None:
        self.cfg.include_upload = False
        searched = self.reconcile()
        searched.assert_not_called()
        self.assertFalse(self.state.errors)
        self.assertIn("Local input receipts accounted for", self.state.stage_results[-1].message)


if __name__ == "__main__":
    unittest.main()
