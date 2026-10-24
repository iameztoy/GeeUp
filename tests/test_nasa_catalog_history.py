"""Isolated version-history, interrupted-transfer and completeness regression checks."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import nasa_catalog_history as h
import swot_download_tool as d
from tests.test_download_tool import FakeGranule, FakePagedEarthaccess, FakeEarthaccess, FakeCMRResponse, granule_name


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = d.DownloadConfig(output_folder=self.root / "raw", start_date="2026-01-01", end_date="2026-01-31",
            utm_tiles=["UTM30R"], manifest_csv=self.root / "00_logs/download_manifest.csv",
            report_csv=self.root / "00_logs/download_preview.csv", catalog_history_enabled=True, batch_size=1)
        self.ledger = h.CatalogHistory(h.history_path(self.config.manifest_csv))

    @staticmethod
    def granule(counter: str = "01", concept: str = "G1", **kwargs) -> FakeGranule:
        return FakeGranule(granule_name(counter=counter, **kwargs), concept_id=concept, size_mb=None)

    def search(self, granules: list, **kwargs) -> dict:
        preview = h.search_with_history(self.config, FakePagedEarthaccess(granules), **kwargs)
        return self.ledger.snapshot(preview.history_scan_id)

    def test_preview_only_persists_excluded_versions_and_repeated_searches(self) -> None:
        old, new = self.granule(), self.granule("02", "G2")
        first = self.search([old, new])
        self.search([new])
        self.assertEqual(first["status"], "complete")
        self.assertEqual(len(self.ledger.scans()), 2)
        self.assertEqual(len(self.ledger.snapshot(first["id"])["candidates"]), 2)
        loser = next(s for s in first["selections"] if s["filename"] == old.name)
        self.assertEqual(loser["selected"], 0)
        self.assertEqual(loser["preferred"], new.name)
        self.assertFalse(self.config.manifest_csv.exists())
        self.assertFalse((self.root / "swotflow.sqlite3").exists())
        self.assertFalse(self.config.output_folder.exists())

    def test_same_concept_new_revision_and_name_remain_in_history(self) -> None:
        a, b = self.granule(), self.granule("02")
        a["meta"].update({"revision-id": 1, "revision-date": "2026-01-20T00:00:00Z"})
        b["meta"].update({"revision-id": 2, "revision-date": "2026-02-20T00:00:00Z"})
        one, two = self.search([a]), self.search([b])
        self.assertEqual(one["candidates"][0]["filename"], a.name)
        self.assertEqual(two["candidates"][0]["revision_id"], 2)
        self.assertEqual(self.ledger.snapshot(one["id"])["candidates"][0]["revision_id"], 1)

    def test_parent_aliases_do_not_add_required_observations(self) -> None:
        snap = self.search([self.granule()], collections=["SWOT_L2_HR_Raster_100m_D", "SWOT_L2_HR_Raster_D"])
        self.assertEqual(len(snap["candidates"]), 2)
        self.assertEqual(len(snap["selections"]), 1)
        report = h.compare_versions(snap, [], h.utc_now())
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["catalogue_aliases"], 2)

    def test_pagination_saves_each_page_and_matches_total(self) -> None:
        with mock.patch.object(h, "PAGE_SIZE", 1):
            snap = self.search([self.granule(), self.granule("02", "G2")])
        with self.ledger.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 2)
        self.assertEqual(snap["status"], "complete")

    def test_missing_page_token_fails_and_retains_first_page(self) -> None:
        with mock.patch.object(h, "request_page", return_value=FakeCMRResponse([self.granule()], total_hits=2)):
            with self.assertRaisesRegex(ValueError, "reconcile"):
                self.search([])
        scan = self.ledger.scans()[0]
        self.assertEqual(scan["status"], "failed")
        self.assertEqual(len(self.ledger.snapshot(scan["id"])["candidates"]), 1)

    def test_repeated_page_and_changing_hits_fail_closed(self) -> None:
        for responses in ([FakeCMRResponse([self.granule()], 1, 2), FakeCMRResponse([self.granule()], 2, 2)],
                          [FakeCMRResponse([self.granule()], 1, 2), FakeCMRResponse([], None, 1)]):
            with mock.patch.object(h, "request_page", side_effect=responses):
                with self.assertRaises(ValueError):
                    self.search([])
            self.assertEqual(self.ledger.scans()[0]["status"], "failed")

    def test_limited_and_unverified_searches_never_claim_complete(self) -> None:
        preview = h.search_with_history(replace(self.config, max_granules=1), FakePagedEarthaccess([self.granule(), self.granule("02", "G2")]))
        snap = self.ledger.snapshot(preview.history_scan_id)
        self.assertEqual(snap["status"], "limited")
        self.assertFalse(h.compare_versions(snap, [], h.utc_now())["current_complete_metadata"])
        preview = h.search_with_history(self.config, FakeEarthaccess([self.granule()]))
        self.assertEqual(self.ledger.snapshot(preview.history_scan_id)["status"], "unverified")

    def test_interrupted_search_is_not_marked_complete(self) -> None:
        scan = self.ledger.begin({"tiles": ["UTM30R"], "start": "2026-01-01", "end": "2026-01-31"})
        self.assertEqual(self.ledger.scans()[0]["status"], "interrupted/running")
        self.assertFalse(h.compare_versions(self.ledger.snapshot(scan), [], h.utc_now())["current_complete_metadata"])

    def test_newer_version_is_detected_even_if_it_has_been_downloaded(self) -> None:
        old, new = self.granule(), self.granule("02", "G2")
        snap = self.search([old, new])
        report = h.compare_versions(snap, [{"filename": old.name, "asset_id": "old_asset"}], h.utc_now(), [old.name, new.name])
        self.assertEqual(report["summary"], {"newer_version_available": 1})
        self.assertTrue(report["rows"][0]["preferred_downloaded"])
        self.assertEqual(report["rows"][0]["best_recorded_ee"], old.name)
        self.assertEqual(report["rows"][0]["asset_ids"], ["old_asset"])

    def test_current_higher_recorded_missing_and_revised_timestamps(self) -> None:
        old, new = self.granule(), self.granule("02", "G2")
        snap = self.search([old])
        def status(name: str) -> str:
            return h.compare_versions(snap, [{"filename": name, "asset_id": "a"}], h.utc_now())["rows"][0]["status"]
        self.assertEqual(status(old.name), "preferred_version_recorded")
        self.assertEqual(status(new.name), "recorded_version_ranks_higher")
        self.assertEqual(h.compare_versions(snap, [], h.utc_now())["rows"][0]["status"], "not_in_loaded_ee_lineage")
        changed = granule_name(start="20260117T052509", end="20260117T052530")
        report = h.compare_versions(snap, [{"filename": changed, "asset_id": "a"}], h.utc_now())
        self.assertIn("timestamp_variant_review", report["summary"])
        self.assertIn("not_returned_in_search", report["summary"])

    def test_dates_and_tiles_are_scoped_and_stale_evidence_is_visible(self) -> None:
        snap = self.search([self.granule()])
        represented = [{"filename": granule_name(utm="UTM31R"), "asset_id": "outside"},
                       {"filename": granule_name(start="20250117T052508"), "asset_id": "older"}]
        report = h.compare_versions(snap, represented, "2020-01-01T00:00:00Z")
        self.assertEqual(len(report["rows"]), 1)
        self.assertFalse(report["current_complete_metadata"])
        self.assertFalse(h.recent("2026-10-24T00:00:00"))

    def test_raw_pages_are_compressed_hashed_sanitized_and_immutable(self) -> None:
        g = self.granule()
        g["umm"]["RelatedUrls"] = [{"URL": "https://user:pass@server/file?token=secret"}]
        g["password"] = "secret"
        snap = self.search([g])
        with self.ledger.connection() as db:
            row = db.execute("SELECT document,sha256 FROM pages").fetchone()
            raw = gzip.decompress(row[0])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row[1])
            self.assertNotIn(b"secret", raw)
            self.assertNotIn(b"user:pass", raw)
        with self.assertRaises(ValueError):
            self.ledger.page(snap["id"], "more", 1, {}, {}, [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.finish(snap["id"], [], "failed")
        self.assertEqual(self.ledger.snapshot(snap["id"])["status"], "complete")

    def test_disabled_feature_preserves_legacy_path_and_writes_no_ledger(self) -> None:
        d.build_download_preview(replace(self.config, catalog_history_enabled=False), FakePagedEarthaccess([self.granule()]))
        self.assertFalse(self.ledger.path.exists())

    def test_batch_result_is_durable_before_later_batch_crashes(self) -> None:
        fake = FakePagedEarthaccess([self.granule(), self.granule("01", "G2", scene="002F")])
        download = fake.download
        calls = []
        def crashing_download(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt("simulated power interruption")
            return download(*args, **kwargs)
        with mock.patch.object(fake, "download", side_effect=crashing_download):
            with self.assertRaises(KeyboardInterrupt):
                d.run_download(self.config, fake)
        snap = self.ledger.snapshot(self.ledger.scans()[0]["id"])
        results = [json.loads(e["payload"]) for e in snap["transfers"] if e["action"] == "batch_finished"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0]["status"], "DOWNLOADED")
        self.assertFalse(self.config.manifest_csv.exists())
        self.assertEqual(len([e for e in snap["transfers"] if e["action"] == "batch_started"]), 2)

    def test_legacy_import_is_idempotent_and_does_not_fabricate_catalogue_revision(self) -> None:
        name = self.granule().name
        project = {"download_manifest": [{"file_name": name, "downloaded": "yes", "first_seen": "2026-01-20"}],
                   "extract_manifest": [{"input_nc": "D:/raw/" + name}],
                   "mosaic_manifest": [{"input_files": json.dumps([name.replace(".nc", ".tif")])}]}
        one = h.import_legacy(self.ledger, project)
        self.assertEqual(one, h.import_legacy(self.ledger, project))
        snap = self.ledger.snapshot(one)
        self.assertEqual(snap["status"], "legacy")
        self.assertEqual(len(snap["candidates"]), 1)
        self.assertNotIn("revision_id", snap["candidates"][0])
        with self.assertRaises(ValueError):
            h.compare_versions(snap, [], h.utc_now())

    def test_project_snapshot_is_read_only_and_empty_refresh_creates_nothing(self) -> None:
        self.assertEqual(self.ledger.scans(), [])
        self.assertFalse(self.ledger.path.parent.exists())
        dbpath = self.root / "swotflow.sqlite3"
        with sqlite3.connect(dbpath) as db:
            db.execute("CREATE TABLE records(dataset TEXT,payload_json TEXT)")
            db.execute("INSERT INTO records VALUES (?,?)", ("download_manifest", json.dumps({"file_name": self.granule().name})))
        db.close()
        before = dbpath.read_bytes()
        snapshot = h.read_project_snapshot(self.root)
        self.assertEqual(len(snapshot["download_manifest"]), 1)
        self.assertEqual(before, dbpath.read_bytes())

    def test_lineage_join_only_counts_actual_inventory_assets_and_reports_unknowns(self) -> None:
        from tests.test_ee_consolidation import asset, manifest, COLLECTION
        a, b = asset(), asset(tile="UTM31Q")
        rows = [{"output_file": name + ".tif", **row} for name, row in {**manifest(a), **manifest(b)}.items()]
        inventory = {"collection": COLLECTION, "assets": [a], "finished_utc": h.utc_now()}
        represented, unknown = h.represented_inputs(inventory, rows)
        self.assertEqual(len(represented), 1)
        self.assertEqual(represented[0]["asset_id"], a["id"])
        self.assertFalse(unknown)
        represented, unknown = h.represented_inputs(inventory, [])
        self.assertFalse(represented)
        self.assertEqual(len(unknown), 1)
        with self.assertRaises(ValueError):
            h.represented_inputs({**inventory, "partial": True}, rows)

    def test_only_current_approved_consolidations_extend_ee_version_history(self) -> None:
        from tests.test_ee_consolidation import COLLECTION
        name = self.granule().name
        product = {"kind": "consolidated", "role": "active", "availability": "present", "errors": [],
                   "verified": True, "verification": "full_pixels", "asset_id": "consolidated", "observed_utc": h.utc_now(),
                   "details": {"group": {"sources": [{"original_inputs": [{"filename": name}]}]}}}
        with mock.patch("ee_replacement_register.ReplacementRegister.coverage", return_value={"products": [product]}):
            represented, warnings = h.approved_consolidated_inputs(COLLECTION, "2026-01-01", "2026-01-31")
            self.assertEqual(represented[0]["filename"], name)
            self.assertFalse(warnings)
            self.assertEqual(h.compare_versions(self.search([self.granule()]), represented, h.utc_now())["summary"],
                             {"preferred_version_recorded": 1})
            product["availability"] = "unknown/stale"
            represented, warnings = h.approved_consolidated_inputs(COLLECTION, "", "")
            self.assertFalse(represented)
            self.assertEqual(len(warnings), 1)
            product["role"] = "candidate"
            represented, warnings = h.approved_consolidated_inputs(COLLECTION, "", "")
            self.assertFalse(represented)
        self.assertFalse((self.root / "swotflow.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
