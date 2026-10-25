"""Current evidence, exact-input accounting and stale-state recovery regressions."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import download_integrity
import nasa_catalog_history as history
import pipeline_verification as p
from native_pixel_verification import compare_arrays, windows
from swot_download_tool import normalize_granule
from tests.test_download_tool import FakeGranule, granule_name


class ProofTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = p.VerificationStore(self.root / "00_logs")
        self.name = granule_name()
        self.raw = self.root / self.name
        self.raw.write_bytes(b"nasa")
        g = FakeGranule(self.name, concept_id="G1")
        g["umm"]["DataGranule"] = {"ArchiveAndDistributionInformation": [{"Name": self.name, "SizeInBytes": 4,
            "Checksum": {"Algorithm": "MD5", "Value": hashlib.md5(b"nasa").hexdigest()}}]}
        self.g = normalize_granule(g)
        self.config = SimpleNamespace(workers=2, target_crs_mode="original", resampling_alg="near")
        self.extract = self.root / self.raw.with_suffix(".tif").name
        self.extract.write_bytes(b"extracted")
        download_integrity.verified_file(self.raw, self.g, self.store.logs)
        self.proof = self.store.record_artifact("extract", self.extract, [self.raw], self.config, "run1", {})
        self.mosaic = self.root / "mosaic.tif"
        self.mosaic.write_bytes(b"mosaic")
        self.mosaic_proof = self.store.record_artifact("mosaic", self.mosaic, [self.extract], self.config, "run2", {})
        self.candidate = history.product_record(g, "parent")
        self.snapshot = {"id": "scan1", "scope": {"tiles": ["UTM30R"], "start": "2026-01-01", "end": "2026-01-31"},
                         "status": "complete", "finished": p.utc_now(), "started": p.utc_now(),
                         "candidates": [self.candidate], "selections": [{"filename": self.name, "selected": 1}]}
        self.asset = {"id": "projects/example/assets/source/test", "type": "IMAGE", "updateTime": "one"}
        self.inventory = {"collection": "projects/example/assets/source", "finished_utc": p.utc_now(), "assets": [self.asset]}
        self.receipt = {"asset_id": self.asset["id"], "asset_fingerprint": p.digest(self.asset),
                        "local_sha256": self.mosaic_proof["sha256"], "sources": self.mosaic_proof["sources"],
                        "mode": "full", "passed": True, "utc": p.utc_now()}

    def report(self, represented=None) -> dict:
        represented = represented if represented is not None else [{"filename": self.name, "asset_id": self.asset["id"]}]
        with mock.patch.object(history, "represented_inputs", return_value=(represented, [])):
            return p.reconcile(self.snapshot, {"mosaic_manifest": []}, self.inventory, self.store)

    def test_full_chain_passes_but_presence_is_not_pixel_proof(self) -> None:
        report = self.report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["catalogue_finished_utc"], self.snapshot["finished"])
        self.assertEqual(report["inventory_finished_utc"], self.inventory["finished_utc"])
        self.store.record_cloud(self.receipt)
        self.assertTrue(self.report()["complete"])

    def test_local_completion_rejects_changed_source_or_missing_output(self) -> None:
        self.assertEqual(p.pending_local_inputs(self.store,self.snapshot),[])
        revised = copy.deepcopy(self.snapshot)
        revised["candidates"][0]["data_granule"]["ArchiveAndDistributionInformation"][0]["Checksum"]["Value"] = "a"*32
        self.assertEqual(p.pending_local_inputs(self.store,revised),[self.name])
        self.mosaic.unlink()
        self.assertEqual(p.pending_local_inputs(self.store,self.snapshot),[self.name])

    def test_mask_sidecar_change_invalidates_extraction_and_mosaic_reuse(self) -> None:
        sidecar = Path(str(self.extract)+".aux.xml")
        sidecar.write_text("original mask")
        self.proof = self.store.record_artifact("extract",self.extract,[self.raw],self.config,"run",{})
        self.store.record_artifact("mosaic",self.mosaic,[self.extract],self.config,"run",{})
        self.assertTrue(self.store.reusable("mosaic",self.mosaic,[self.extract],self.config))
        sidecar.write_text("changed mask")
        self.assertFalse(p.artifact_current(self.proof))
        self.assertFalse(self.store.reusable("mosaic",self.mosaic,[self.extract],self.config))

    def test_sampled_check_never_certifies_full_pixels(self) -> None:
        self.store.record_cloud({**self.receipt, "mode": "sampled"})
        self.assertFalse(self.report()["pixel_verification_complete"])

    def test_later_failure_invalidates_previous_full_pass(self) -> None:
        self.store.record_cloud(self.receipt)
        self.store.record_cloud({**self.receipt, "passed": False})
        self.assertFalse(self.report()["complete"])

    def test_cloud_change_invalidates_previous_pass(self) -> None:
        self.store.record_cloud(self.receipt)
        self.inventory["assets"][0]["updateTime"] = "changed"
        self.assertFalse(self.report()["complete"])

    def test_changed_provider_bytes_under_same_name_require_repair(self) -> None:
        self.store.record_cloud(self.receipt)
        self.snapshot["candidates"][0]["data_granule"]["ArchiveAndDistributionInformation"][0]["Checksum"]["Value"] = "a"*32
        result = self.report()
        self.assertFalse(result["complete"])
        self.assertFalse(result["rows"][0]["download_verified"])
        self.assertFalse(result["rows"][0]["mosaic_recorded"])

    def test_duplicate_active_memberships_are_not_complete(self) -> None:
        self.store.record_cloud(self.receipt)
        result = self.report([{"filename": self.name, "asset_id": self.asset["id"]}, {"filename": self.name, "asset_id": "other"}])
        self.assertEqual(result["rows"][0]["state"], "duplicate_active_inputs")
        self.assertFalse(result["complete"])

    def test_stale_or_partial_snapshot_blocks_current_completeness(self) -> None:
        self.store.record_cloud(self.receipt)
        self.snapshot["status"] = "limited"
        self.assertFalse(self.report()["complete"])
        self.snapshot["status"] = "complete"
        self.inventory["finished_utc"] = "2020-01-01T00:00:00Z"
        self.assertFalse(self.report()["complete"])

    def test_superseded_version_does_not_inflate_expected_count(self) -> None:
        old = self.name.replace("PID0", "PIC0")
        self.snapshot["selections"].append({"filename": old, "selected": 0})
        self.assertEqual(self.report()["expected_preferred_inputs"], 1)

    def test_missing_ee_input_reported_as_upload_gap_not_nasa_absence(self) -> None:
        result = self.report([])
        self.assertEqual(result["rows"][0]["state"], "mosaic_pending_upload")
        self.assertEqual(len(result["repair_queue"]), 1)

    def test_receipts_survive_raw_and_intermediate_cleanup(self) -> None:
        self.store.record_cloud(self.receipt)
        self.raw.unlink()
        self.extract.unlink()
        self.mosaic.unlink()
        self.assertTrue(self.report()["complete"])
        self.assertFalse(self.store.reusable("extract", self.extract, [self.raw], self.config))

    def test_same_size_changed_output_cannot_resume(self) -> None:
        self.assertTrue(self.store.reusable("extract", self.extract, [self.raw], self.config))
        stat = self.extract.stat()
        self.extract.write_bytes(b"different")
        os.utime(self.extract, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertFalse(self.store.reusable("extract", self.extract, [self.raw], self.config))

    def test_changed_source_cannot_resume_or_create_mosaic_receipt(self) -> None:
        self.raw.write_bytes(b"bad!")
        self.assertFalse(self.store.reusable("extract", self.extract, [self.raw], self.config))
        self.extract.write_bytes(b"corrupted")
        with self.assertRaises(ValueError):
            self.store.record_artifact("mosaic", self.mosaic, [self.extract], self.config, "run3", {})

    def test_pixel_settings_change_invalidates_reuse(self) -> None:
        self.config.resampling_alg = "bilinear"
        self.assertFalse(self.store.reusable("extract", self.extract, [self.raw], self.config))

    def test_whole_group_rebuilt_when_new_input_arrives(self) -> None:
        wanted = self.proof["sources"][self.name]
        with mock.patch.object(p, "active_proofs", return_value={self.name: {"checksum": wanted}}):
            self.assertEqual(p.reusable_download_groups([self.g], self.store, "collection"), {self.name})
            second = copy.deepcopy(self.g)
            second.file_name = granule_name(scene="002F")
            second.source = copy.deepcopy(self.g.source)
            second.source["umm"]["DataGranule"]["ArchiveAndDistributionInformation"][0]["Name"] = second.file_name
            self.assertEqual(p.reusable_download_groups([self.g, second], self.store, "collection"), set())

    def test_cleanup_rechecks_downstream_bytes_and_retains_metadata(self) -> None:
        from project_insights import CleanupCandidate, delete_cleanup_candidates
        config = {"download": {"manifest_csv": str(self.store.logs / "download_manifest.csv")}}
        candidate = CleanupCandidate("raw", self.raw, "legacy flag", "old manifest")
        allowed = p.cleanup_guards(config, [candidate])
        self.assertEqual(len(allowed), 1)
        self.extract.write_bytes(b"changed")
        deleted, _, errors = delete_cleanup_candidates(allowed)
        self.assertEqual(deleted, 0)
        self.assertTrue(errors)
        self.assertTrue(self.raw.exists())
        self.assertTrue(self.store.path.exists())

    def test_no_cleanup_from_legacy_manifest_alone(self) -> None:
        from project_insights import CleanupCandidate
        config = {"download": {"manifest_csv": str(self.root / "other" / "download_manifest.csv")}}
        self.assertEqual(p.cleanup_guards(config, [CleanupCandidate("raw", self.raw, "done", "manifest")]), [])

    def test_wrong_collection_cannot_supply_reuse(self) -> None:
        p.save_inventory(self.store, self.inventory)
        self.assertEqual(p.load_inventory(self.store, "projects/other/assets/data"), {})

    def test_run_receipt_does_not_persist_credentials(self) -> None:
        self.store.begin_run("download", {"threads": 3, "password": "secret-for-test", "token": "private-token"})
        with self.store.connection() as db:
            row = db.execute("SELECT payload FROM runs").fetchone()[0]
        self.assertNotIn("secret-for-test", row)
        self.assertNotIn("private-token", row)
        self.assertIn('"threads": 3', row)


class PixelTests(unittest.TestCase):
    def test_full_windows_cover_grid_exactly_once(self) -> None:
        self.assertEqual(sum(w*h for x,y,w,h in windows(1027, 715, "full")), 1027*715)
        self.assertLess(sum(w*h for x,y,w,h in windows(1027, 715, "sampled")), 1027*715)

    def test_shift_quality_and_mask_differences_are_separate(self) -> None:
        import numpy as np
        local = {"b1": np.array([[1., 2.], [3., 4.]]), "b2": np.zeros((2,2)), "m1": np.ones((2,2),bool), "m2": np.ones((2,2),bool)}
        remote = {k: v.copy() for k, v in local.items()}
        self.assertEqual(compare_arrays(local, remote)["wse_mismatch"], 0)
        remote["b1"][0,0] = 101
        remote["b2"][0,1] = 1
        remote["m1"][1,0] = False
        result = compare_arrays(local, remote)
        self.assertEqual((result["wse_mismatch"], result["quality_mismatch"], result["mask_mismatch"]), (1,1,1))
        self.assertEqual(result["max_abs_wse_m"], 100)

    def test_nodata_values_do_not_appear_as_elevation_disagreements(self) -> None:
        import numpy as np
        local = {"b1": np.array([[np.nan]]), "b2": np.array([[255]]), "m1": np.array([[False]]), "m2": np.array([[False]])}
        remote = {"b1": np.array([[0.]]), "b2": np.array([[0.]]), "m1": np.array([[0]]), "m2": np.array([[0]])}
        result = compare_arrays(local, remote)
        self.assertEqual((result["wse_mismatch"], result["quality_mismatch"], result["mask_mismatch"]), (0,0,0))


if __name__ == "__main__":
    unittest.main()
