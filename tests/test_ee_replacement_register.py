"""Isolated lifecycle tests: no EE API, production files, audit workers or jobs."""
import copy
import hashlib
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path

import ee_consolidation as c
import ee_consolidation_update as u
from ee_replacement_register import ReplacementRegister
from tests.test_ee_consolidation import asset, manifest, COLLECTION, DESTINATION, local_temp
from tests.test_ee_consolidation_update import stored


def inventory(collection, assets, when=None):
    return {"collection": collection, "assets": assets, "finished_utc": when or c.utc_now()}


def receipt(output, plan, result="pass", level="full_pixels"):
    group = plan["groups"][0]
    dims = output["bands"][0]["grid"]["dimensions"]
    return {"schema": 1, "asset_id": output["id"], "asset_fingerprint": c.digest(c.snapshot_asset(output)),
            "record_fingerprint": group["fingerprint"], "result": result, "level": level,
            "method": "Isolated test fixture, not a real scientific approval", "scope": "Full synthetic output grid including masked cells",
            "checked_utc": c.utc_now(), "evidence_sha256": hashlib.sha256(b"synthetic test evidence").hexdigest(),
            "compared_bands": ["b1", "b2", "source_index"], "pixels_compared": dims["width"] * dims["height"],
            "masks_compared": True, "mismatches": 0}


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.temp = local_temp()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "register.sqlite3"
        self.r = ReplacementRegister(self.path, COLLECTION)
        self.a, self.b = asset(), asset(tile="UTM31Q")
        self.new = asset(tile="UTM31R", start="20240101T010030", end="20240101T010040")
        self.lineage = {**manifest(self.a), **manifest(self.b), **manifest(self.new)}
        self.old = c.plan_assets([self.a, self.b], COLLECTION, c.Options(), self.lineage)
        self.m1 = stored(self.old["groups"][0], self.old)
        self.updated = u.plan_updates([self.new], COLLECTION, [self.m1], DESTINATION, [self.old], c.Options(), self.lineage)
        self.m2 = stored(self.updated["groups"][0], self.updated)

    def seed(self):
        self.r.import_plan(self.old, DESTINATION)
        self.r.sync_inventory(inventory(COLLECTION, [self.a, self.b]), self.lineage)
        self.r.sync_inventory(inventory(DESTINATION, [self.m1]))

    def approve_m1(self):
        self.seed()
        self.r.record_verification(receipt(self.m1, self.old))
        self.r.activate(self.m1["id"], [], "Synthetic approval")

    def test_read_only_empty_does_not_create_database(self):
        self.assertFalse(self.r.coverage()["tiles"])
        self.assertFalse(self.path.exists())

    def test_lifecycle_new_tiles_and_atomic_replacement(self):
        self.approve_m1()
        first = self.r.coverage()
        self.assertEqual(first["summary"]["active"], 2)
        self.assertEqual(first["summary"]["represented_copies"], 2)
        self.r.sync_inventory(inventory(COLLECTION, [self.a, self.b, self.new]), self.lineage)
        report = self.r.coverage()
        self.assertEqual(report["summary"]["pending"], 1)
        self.assertEqual({row["tile"]: row["status"] for row in report["tiles"]},
                         {"UTM31P": "complete", "UTM31Q": "complete", "UTM31R": "pending"})
        self.r.import_plan(self.updated, DESTINATION)
        self.r.sync_inventory(inventory(DESTINATION, [self.m1, self.m2]))
        self.assertEqual(self.r.coverage()["summary"]["awaiting"], 1)
        self.r.record_verification(receipt(self.m2, self.updated))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.r.activate(self.m2["id"], [], "Missing replacement list")
        self.assertEqual(self.r.coverage()["summary"]["active"], 2)
        self.r.activate(self.m2["id"], [self.m1["id"]], "Verified additive replacement")
        report = self.r.coverage()
        self.assertEqual(report["summary"]["active"], 3)
        self.assertEqual(report["summary"]["active_assets"], 1)
        self.assertEqual(report["summary"]["stored_images"], 5)
        self.assertEqual(report["summary"]["represented_copies"], 4)
        self.assertEqual(next(p for p in report["products"] if p["asset_id"] == self.m1["id"])["role"], "superseded revision")
        reopened = ReplacementRegister(self.path, COLLECTION).coverage()
        self.assertEqual(reopened["summary"], report["summary"])

    def test_export_presence_alone_and_sample_checks_cannot_approve(self):
        self.seed()
        with self.assertRaisesRegex(ValueError, "full-pixel"):
            self.r.activate(self.m1["id"], [], "No proof")
        self.r.record_verification(receipt(self.m1, self.old, level="sampled_pixels"))
        with self.assertRaisesRegex(ValueError, "full-pixel"):
            self.r.activate(self.m1["id"], [], "Sample only")
        self.assertEqual(self.r.coverage()["summary"]["awaiting"], 2)

    def test_comparison_template_is_unperformed_and_cannot_approve(self):
        self.seed()
        template = self.r.verification_template(self.m1["id"])
        self.assertEqual(template["result"], "not_run")
        self.assertEqual(template["record_fingerprint"], self.old["groups"][0]["fingerprint"])
        with self.assertRaises(ValueError):
            self.r.record_verification(template)

    def test_revised_scene_timestamps_cannot_be_independent_active_observations(self):
        self.approve_m1()
        a = asset(start="20240102T010000", end="20240102T010020")
        b = asset(tile="UTM31Q", start="20240102T010000", end="20240102T010020")
        plan = c.plan_assets([a, b], COLLECTION, c.Options(), manifest(a) | manifest(b))
        output = stored(plan["groups"][0], plan)
        self.r.import_plan(plan, DESTINATION)
        self.r.sync_inventory(inventory(DESTINATION, [self.m1, output]))
        self.r.record_verification(receipt(output, plan))
        with self.assertRaisesRegex(ValueError, "Revised acquisition timestamps"):
            self.r.activate(output["id"], [], "Revised timestamps are not new scenes")

    def test_partial_or_wrong_output_evidence_is_rejected(self):
        self.seed()
        for update in ({"pixels_compared": 1}, {"masks_compared": False}, {"mismatches": 1},
                       {"asset_fingerprint": "wrong"}, {"record_fingerprint": "wrong"}, {"evidence_sha256": "bad"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.r.record_verification(receipt(self.m1, self.old) | update)

    def test_missing_active_asset_is_not_reported_complete_with_originals_present(self):
        self.approve_m1()
        self.r.sync_inventory(inventory(DESTINATION, []))
        self.assertEqual(self.r.coverage()["summary"]["active"], 0)
        self.assertEqual(self.r.coverage()["summary"]["missing"], 2)

    def test_retained_lineage_accounts_for_sources_absent_from_inventory(self):
        self.approve_m1()
        self.r.sync_inventory(inventory(COLLECTION, []))
        report = self.r.coverage()
        self.assertEqual(report["summary"]["active"], 2)
        self.assertEqual(report["summary"]["uploaded"], 0)
        self.assertEqual(report["summary"]["stored_images"], 1)

    def test_stale_or_undated_inventory_never_green(self):
        self.approve_m1()
        future = datetime.now(timezone.utc) + timedelta(days=2)
        report = self.r.coverage(now=future)
        self.assertEqual(report["summary"]["active"], 0)
        self.assertEqual(report["summary"]["unknown"], 2)
        other = ReplacementRegister(Path(self.temp.name) / "undated.sqlite3", COLLECTION)
        other.sync_inventory({"collection": COLLECTION, "assets": [self.a]}, self.lineage)
        self.assertEqual(other.coverage()["summary"]["unknown"], 1)

    def test_snapshot_scope_duplicates_old_and_partial_are_rejected_atomically(self):
        self.seed()
        before = self.r.coverage()["summary"]
        bad = [inventory(COLLECTION, [self.a, self.a]), inventory(COLLECTION, [self.m1]),
               inventory(COLLECTION, [], "2000-01-01T00:00:00Z"), inventory(COLLECTION, []) | {"partial": True},
               inventory(COLLECTION, []) | {"nextPageToken": "more"}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.r.sync_inventory(value)
            self.assertEqual(self.r.coverage()["summary"], before)

    def test_changed_asset_invalidates_verification(self):
        self.approve_m1()
        changed = copy.deepcopy(self.m1)
        changed["updateTime"] = "2026-10-24T19:00:00Z"
        self.r.sync_inventory(inventory(DESTINATION, [changed]))
        self.assertEqual(self.r.coverage()["summary"]["active"], 0)
        with self.assertRaises(ValueError):
            self.r.activate(self.m1["id"], [], "Old proof")

    def test_changed_source_flagged_without_rewriting_history(self):
        self.seed()
        changed = copy.deepcopy(self.a)
        changed["updateTime"] = "2026-10-24T19:00:00Z"
        self.r.sync_inventory(inventory(COLLECTION, [changed, self.b]), self.lineage)
        report = self.r.coverage()
        self.assertEqual(report["summary"]["conflicts"], 1)
        self.assertIn("Source changed", str(report["conflicts"]))

    def test_source_versions_count_as_one_observation_and_mark_old_approval_outdated(self):
        self.approve_m1()
        newer = asset(counter="02")
        self.r.sync_inventory(inventory(COLLECTION, [self.a, self.b, newer]), self.lineage | manifest(newer))
        report = self.r.coverage()
        self.assertEqual(report["summary"]["expected"], 2)
        self.assertEqual(report["summary"]["outdated"], 1)
        self.assertEqual(report["summary"]["active"], 1)
        self.assertIn("Processing-version alternatives", str(report["conflicts"]))

    def test_same_granule_in_two_sources_blocks_approval(self):
        # Keep source asset IDs distinct while intentionally repeating original lineage.
        b_manifest = manifest(self.b)
        b_manifest[next(iter(b_manifest))]["input_files"] = manifest(self.a)[next(iter(manifest(self.a)))]["input_files"]
        plan = c.plan_assets([self.a, self.b], COLLECTION, c.Options(), manifest(self.a) | b_manifest)
        out = stored(plan["groups"][0], plan)
        self.r.import_plan(plan, DESTINATION)
        self.r.sync_inventory(inventory(DESTINATION, [out]))
        self.assertIn("Repeated original", str(self.r.coverage()["conflicts"]))
        with self.assertRaises(ValueError):
            self.r.record_verification(receipt(out, plan))

    def test_unknown_lineage_and_untracked_outputs_remain_explicit(self):
        self.r.sync_inventory(inventory(COLLECTION, [self.a]))
        self.r.sync_inventory(inventory(DESTINATION, [self.m1]))
        report = self.r.coverage()
        self.assertEqual(report["summary"]["untracked_assets"], 1)
        self.assertEqual(report["tiles"][0]["status"], "conflict")

    def test_date_filter_keeps_physical_count_scope_explicit(self):
        self.seed()
        report = self.r.coverage("2024-01-02", "2024-01-03")
        self.assertFalse(report["tiles"])
        self.assertEqual(report["summary"]["stored_images"], 3)
        self.assertIn("whole inventory", report["scope"])

    def test_failed_comparison_is_visible_and_never_approved(self):
        self.seed()
        self.r.record_verification(receipt(self.m1, self.old, result="fail"))
        self.assertEqual(self.r.coverage()["summary"]["conflicts"], 2)
        with self.assertRaises(ValueError):
            self.r.activate(self.m1["id"], [], "Failed comparison")
        self.r.record_verification(receipt(self.m1, self.old))
        self.r.activate(self.m1["id"], [], "Successful repeat comparison")
        self.assertEqual(self.r.coverage()["summary"]["active"], 2)

    def test_replacement_cannot_drop_old_observations(self):
        self.approve_m1()
        alternative = c.plan_assets([self.a, self.new], COLLECTION, c.Options(), self.lineage)
        output = stored(alternative["groups"][0], alternative)
        self.r.import_plan(alternative, DESTINATION)
        self.r.sync_inventory(inventory(DESTINATION, [self.m1, output]))
        self.r.record_verification(receipt(output, alternative))
        with self.assertRaisesRegex(ValueError, "loses"):
            self.r.activate(output["id"], [self.m1["id"]], "Would drop B")
        self.assertEqual(self.r.coverage()["summary"]["active"], 2)

    def test_failed_new_candidate_keeps_previous_approved_coverage(self):
        self.approve_m1()
        self.r.import_plan(self.updated, DESTINATION)
        self.r.sync_inventory(inventory(DESTINATION, [self.m1, self.m2]))
        self.r.record_verification(receipt(self.m2, self.updated, result="fail"))
        report = self.r.coverage()
        self.assertEqual(report["summary"]["active"], 2)
        self.assertEqual(report["summary"]["active_assets"], 1)
        self.assertGreater(report["summary"]["conflicts"], 0)

    def test_parallel_revisions_of_same_group_are_blocked_even_without_shared_inputs(self):
        self.approve_m1()
        later = asset(tile="UTM31S", start="20240101T010050", end="20240101T010100")
        plan = c.plan_assets([self.new, later], COLLECTION, c.Options(), self.lineage | manifest(later))
        output = stored(plan["groups"][0], plan)
        self.r.import_plan(plan, DESTINATION)
        self.r.sync_inventory(inventory(DESTINATION, [self.m1, output]))
        self.r.record_verification(receipt(output, plan))
        with self.assertRaisesRegex(ValueError, "Conflicting active revisions"):
            self.r.activate(output["id"], [], "Same group, disjoint inputs")

    def test_new_date_and_hemisphere_have_separate_tile_coverage(self):
        self.approve_m1()
        other_date = asset(start="20240102T010000", end="20240102T010020")
        south = asset(tile="UTM31M", crs="EPSG:32731")
        self.r.sync_inventory(inventory(COLLECTION, [self.a, self.b, other_date, south]),
                              self.lineage | manifest(other_date) | manifest(south))
        rows = {r["tile"]: r for r in self.r.coverage()["tiles"]}
        self.assertEqual(rows["UTM31P"]["active"], 1)
        self.assertEqual(rows["UTM31P"]["pending"], 1)
        self.assertEqual(rows["UTM31M"]["pending"], 1)
        self.assertEqual(self.r.coverage("2024-01-02", "2024-01-02")["summary"]["expected"], 1)

    def test_replacement_downgrade_is_blocked(self):
        self.seed()
        newer = asset(counter="02")
        high = c.plan_assets([newer, self.b], COLLECTION, c.Options(), self.lineage | manifest(newer))
        out = stored(high["groups"][0], high)
        self.r.import_plan(high, DESTINATION)
        self.r.sync_inventory(inventory(DESTINATION, [self.m1, out]))
        self.r.record_verification(receipt(out, high))
        self.r.activate(out["id"], [], "Higher version")
        self.r.record_verification(receipt(self.m1, self.old))
        with self.assertRaisesRegex(ValueError, "downgrades"):
            self.r.activate(self.m1["id"], [out["id"]], "Lower version")

    def test_transaction_rolls_back_malformed_plan_and_cross_project_open(self):
        self.seed()
        plan = copy.deepcopy(self.updated)
        plan["groups"][0]["fingerprint"] = "bad"
        with self.assertRaises(ValueError):
            self.r.import_plan(plan, DESTINATION)
        self.assertEqual(len(self.r.coverage()["products"]), 3)
        with self.assertRaises(ValueError):
            ReplacementRegister(self.path, "projects/other/assets/source").coverage()

    def test_receipt_file_requires_matching_evidence_and_can_reopen(self):
        self.seed()
        root = Path(self.temp.name)
        (root / "evidence.txt").write_bytes(b"synthetic test evidence")
        path = root / "receipt.json"
        path.write_text(json.dumps(receipt(self.m1, self.old) | {"evidence_file": "evidence.txt"}), encoding="utf-8")
        self.r.import_receipt_file(path)
        (root / "evidence.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Evidence file"):
            self.r.import_receipt_file(path)

    def test_imports_are_idempotent_and_events_survive_reopening(self):
        self.seed()
        self.r.import_plan(self.old, DESTINATION)
        self.assertEqual(len(self.r.coverage()["products"]), 3)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
