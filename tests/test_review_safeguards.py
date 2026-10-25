"""Regression coverage for version, revision, history and cleanup review findings."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import swotflow.verification.pipeline as p
import tests.test_pipeline_verification as fixtures


class SafeguardTests(unittest.TestCase):
    setUp = fixtures.ProofTests.setUp
    report = fixtures.ProofTests.report

    def test_stale_extracted_version_cannot_supply_a_mosaic(self):
        from swotflow.processing.mosaic import MosaicConfig, build_mosaic_plan
        folder = self.root / "extracted"
        folder.mkdir()
        preferred = folder / Path(self.name).with_suffix(".tif")
        older = folder / preferred.name.replace("PID0", "PIC0")
        for path in (preferred, older):
            path.write_bytes(b"fixture")
        config = MosaicConfig(folder, self.root / "outputs")
        plan = build_mosaic_plan(config)
        self.assertEqual([s.path for s in plan.groups[0].sources], [preferred])
        self.assertEqual(plan.report_rows[0]["status"], "FILTERED_PROCESSING_VERSION")
        self.assertTrue(older.exists())  # Selection is independent of deletion.
        preferred.unlink()
        config.expected_sources = {self.name: self.mosaic_proof["sources"][self.name]}
        with self.assertRaisesRegex(ValueError, "Preferred catalogue version is missing"):
            build_mosaic_plan(config)

    def test_existing_name_does_not_certify_changed_local_revision(self):
        self.assertFalse(self.report()["input_accounting_complete"])
        receipt = self.store.prepare_ingestion(self.asset["id"], self.mosaic_proof)
        self.store.record_ingestion({**receipt, "phase": "submitted", "task_id": "operation-1"})
        self.asset["properties"] = p.ingestion_properties(receipt)
        p.save_inventory(self.store, self.inventory)
        self.assertTrue(p.revision_matches(self.store, self.mosaic_proof, self.asset))
        self.assertTrue(self.report()["input_accounting_complete"])
        self.assertFalse(self.report()["pixel_verification_complete"])
        self.mosaic.write_bytes(b"new mosaic with an interior input")
        changed = self.store.record_artifact("mosaic", self.mosaic, [self.extract], self.config, "update", {})
        self.assertFalse(p.revision_matches(self.store, changed, self.asset))
        self.assertFalse(self.report()["input_accounting_complete"])
        self.asset["updateTime"] = "replaced"
        self.assertFalse(p.revision_matches(self.store, self.mosaic_proof, self.asset))

    def test_prepared_but_not_submitted_revision_is_not_accounted(self):
        receipt = self.store.prepare_ingestion(self.asset["id"], self.mosaic_proof)
        self.asset["properties"] = p.ingestion_properties(receipt)
        p.save_inventory(self.store, self.inventory)
        self.assertFalse(self.report()["input_accounting_complete"])

    def test_project_summary_cannot_override_a_changed_local_revision(self):
        self.store.record_cloud(self.receipt)
        self.mosaic.write_bytes(b"different revision")
        self.store.record_artifact("mosaic", self.mosaic, [self.extract], self.config, "new", {})
        member = {"filename": self.name, "asset_id": self.asset["id"]}
        with mock.patch("swotflow.catalog.history.CatalogHistory.snapshot", return_value=self.snapshot),\
             mock.patch("swotflow.catalog.history.represented_inputs", return_value=([member], [])),\
             mock.patch.object(p, "active_proofs", return_value={self.name: {"checksum": self.mosaic_proof["sources"][self.name], "asset_id": self.asset["id"]}}):
            result = p.project_report(self.root, self.store, "scan1", self.inventory)
            self.assertFalse(result["input_accounting_complete"])

    def test_versions_in_different_assets_or_one_asset_are_duplicates(self):
        self.store.record_cloud(self.receipt)
        older = self.name.replace("PID0", "PIC0")
        for asset_id in (self.asset["id"], "other-asset"):
            result = self.report([{"filename": self.name, "asset_id": self.asset["id"]},
                                  {"filename": older, "asset_id": asset_id}])
            self.assertEqual(result["rows"][0]["state"], "duplicate_active_inputs")
            self.assertFalse(result["input_accounting_complete"])
            self.assertFalse(result["complete"])

    def test_unverified_old_owner_blocks_automatic_download_reuse(self):
        self.store.record_cloud(self.receipt)
        p.save_inventory(self.store, self.inventory)
        members = [{"filename": self.name, "asset_id": self.asset["id"]},
                   {"filename": self.name.replace("PID0", "PIC0"), "asset_id": "older"}]
        with mock.patch("swotflow.catalog.history.represented_inputs", return_value=(members, [])):
            self.assertEqual(p.active_proofs(self.store, self.inventory["collection"]), {})

    def test_single_receipt_lookup_uses_index_at_large_history_size(self):
        path = self.store.logs / "download_verification.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executemany("INSERT INTO checks(filename,file_state,valid) VALUES (?,?,?)",
                           ((f"other-{i}", "[]", 1) for i in range(100000)))
            plan = db.execute("EXPLAIN QUERY PLAN SELECT file_state,valid,actual_hash FROM checks WHERE filename=? ORDER BY id DESC LIMIT 1", (self.name,)).fetchall()
        self.assertIn("checks_filename_id", str(plan))
        with mock.patch.object(p.json, "loads", wraps=json.loads) as loads:
            self.assertTrue(p.download_proof(self.store.logs, self.name)["valid"])
            self.assertEqual(loads.call_count, 1)
        with mock.patch.object(p, "download_proofs", side_effect=AssertionError("Bulk read in per-file path")):
            self.assertTrue(self.store.reusable("extract", self.extract, [self.raw], self.config))
            self.store.record_artifact("extract", self.extract, [self.raw], self.config, "again", {})

    def test_combined_cleanup_orders_dependencies_even_if_selection_is_reversed(self):
        from swotflow.project.insights import CleanupCandidate, delete_cleanup_candidates
        config = {"download": {"manifest_csv": str(self.store.logs / "download_manifest.csv")}}
        candidates = [CleanupCandidate("extracted", self.extract, "done", "manifest"),
                      CleanupCandidate("raw", self.raw, "done", "manifest")]
        guarded = p.cleanup_guards(config, candidates)
        self.assertEqual(len(guarded), 2)
        deleted, _, errors = delete_cleanup_candidates(guarded)
        self.assertEqual((deleted, errors), (2, []))
        self.assertTrue(self.mosaic.exists())

    def test_relocation_preserves_immutable_payload_and_maps_operational_proofs(self):
        import swotflow.verification.download as download_integrity
        from swotflow.project.model import create_project
        from swotflow.project.relocation import relocate_project
        project = create_project(self.root / "original", "test", {})
        raw = project.root / "01_raw_downloads" / self.raw.name
        extracted = project.root / "02_extracted_geotiffs" / self.extract.name
        raw.write_bytes(self.raw.read_bytes())
        extracted.write_bytes(self.extract.read_bytes())
        Path(str(extracted) + ".aux.xml").write_text("mask evidence", encoding="utf-8")
        store = p.VerificationStore(project.root / "00_logs")
        download_integrity.verified_file(raw, self.g, store.logs)
        store.record_artifact("extract", extracted, [raw], self.config, "run", {})
        with store.connection() as db:
            payload = db.execute("SELECT payload FROM artifacts").fetchone()[0]
        target = self.root / "relocated"
        shutil.copytree(project.root, target)
        report = relocate_project(target, confirmed_stopped=True)
        self.assertTrue(report.valid, report.summary())
        moved = p.VerificationStore(target / "00_logs")
        new_extract = target / "02_extracted_geotiffs" / extracted.name
        new_raw = target / "01_raw_downloads" / raw.name
        self.assertTrue(moved.reusable("extract", new_extract, [new_raw], self.config))
        self.assertIsNone(moved.artifact(extracted))
        self.assertTrue(p.artifact_current(store.artifact(extracted)))
        self.assertEqual(p.download_proof(moved.logs, raw.name)["state"][0], str(new_raw.resolve()))
        with moved.connection() as db:
            self.assertEqual(db.execute("SELECT payload FROM artifacts").fetchone()[0], payload)
        # Fail after the staged verification databases have been installed.
        # Recovery must restore their original operational indexes too.
        failed_target = self.root / "failed-copy"
        shutil.copytree(project.root, failed_target)
        original_replace = os.replace
        injected = False
        def fail_once(source, destination):
            nonlocal injected
            if Path(destination) == failed_target / "project.yaml" and not injected:
                injected = True
                raise OSError("simulated metadata installation interruption")
            return original_replace(source, destination)
        with mock.patch("swotflow.project.relocation.os.replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated metadata"):
                relocate_project(failed_target, confirmed_stopped=True)
        recovered = p.VerificationStore(failed_target / "00_logs")
        self.assertIsNotNone(recovered.artifact(extracted))
        self.assertIsNone(recovered.artifact(failed_target / "02_extracted_geotiffs" / extracted.name))


if __name__ == "__main__":
    unittest.main()
