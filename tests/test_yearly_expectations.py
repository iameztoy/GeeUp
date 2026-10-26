"""Authoritative annual selections replace only their parent tile/window."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

from swotflow.catalog.history import CatalogHistory
from swotflow.project.database import ProjectDatabase, database_path_for
from swotflow.project.updates import (campaign_rows, expected_rows, record_update_expected_rows,
                                     synchronize_yearly_expectations, update_campaign_id)


def name(year=2024, tile="UTM31S", crid="PGD0"):
    return f"SWOT_L2_HR_Raster_100m_{tile}_N_x_x_x_018_126_083F_{year}0721T100000_{year}0721T100020_{crid}_01.nc"


class YearlyExpectationTests(unittest.TestCase):
    def configs(self, root):
        logs = root / "00_logs"
        parent = {"processing": {"logs": str(logs)}, "download": {
            "collection_short_name": "SWOT_L2_HR_Raster_100m_D", "product_version_filter": "best",
            "start_date": "2023-01-01", "end_date": "2025-12-31", "utm_tiles": ["UTM31S", "UTM34H"],
            "manifest_csv": str(logs / "download_manifest.csv"),
            "acquisition_start_date": "2023-01-01", "acquisition_end_date": "2025-12-31"}}
        child = copy.deepcopy(parent)
        child["download"].update(start_date="2024-01-01", end_date="2024-12-31", utm_tiles=["UTM31S"],
                                  acquisition_start_date="2024-01-01", acquisition_end_date="2024-12-31")
        return parent, child

    def record(self, config, names):
        return record_update_expected_rows(config, [
            {"file_name": n, "utm_tile": n.split("_")[5], "start_time": n.split("_")[14][:4] + "-07-21",
             "selected_for_download": "yes"} for n in names], source="preflight",
             campaign_tiles=config["download"]["utm_tiles"])

    def snapshot(self, child, names, status="complete"):
        d = child["download"]
        ledger = CatalogHistory(Path(d["manifest_csv"]).parent / "nasa_catalog_history.sqlite3")
        scan = ledger.begin({"start": d["start_date"], "end": d["end_date"], "tiles": d["utm_tiles"],
                             "collections": [d["collection_short_name"]], "filter": d["product_version_filter"],
                             "acquisition_start_date": d["acquisition_start_date"],
                             "acquisition_end_date": d["acquisition_end_date"]})
        candidates = [{"filename": n, "identity": n, "concept_id": f"C{i}"} for i, n in enumerate(names)]
        ledger.page(scan, d["collection_short_name"], 1, {"items": names}, {"hits": len(names)}, candidates)
        ledger.finish(scan, [SimpleNamespace(file_name=n, selected_for_download=True,
                      preferred_file_name=n, duplicate_filter_status="selected", duplicate_reason="") for n in names], status)
        return ledger, scan

    def test_current_selection_replaces_old_version_preserves_other_windows_and_history(self):
        with TemporaryDirectory() as tmp:
            parent, child = self.configs(Path(tmp))
            unchanged = [name(2023), name(2025), name(tile="UTM34H")]
            self.record(parent, [name(), *unchanged])
            other_id = self.record(child, [name(crid="PGD0")])
            ledger, scan = self.snapshot(child, [name(crid="PIC0")])
            before = ledger.snapshot(scan)
            result = synchronize_yearly_expectations(parent, child)
            rows = expected_rows(parent)
            parent_id = update_campaign_id(parent)
            actual = {r["file_name"] for r in rows if r["campaign_id"] == parent_id}
            self.assertEqual(actual, {name(crid="PIC0"), *unchanged})
            self.assertEqual({r["file_name"] for r in rows if r["campaign_id"] == other_id}, {name()})
            self.assertEqual(result["replaced_count"], 1)
            self.assertEqual(result["expected_count"], 4)
            changed = next(r for r in rows if r["file_name"] == name(crid="PIC0"))
            self.assertEqual(changed["origin_scan_id"], scan)
            database = ProjectDatabase(database_path_for(Path(parent["processing"]["logs"]) / "update_expected.csv"))
            history = database.read_rows("yearly_expectation_history")
            self.assertEqual(history[0]["previous_rows"][0]["file_name"], name())
            self.assertEqual(history[0]["selected_rows"][0]["file_name"], name(crid="PIC0"))
            self.assertEqual(ledger.snapshot(scan), before)
            self.assertEqual(next(r for r in campaign_rows(parent) if r["campaign_id"] == parent_id)["expected_granules"], "4")

    def test_empty_authoritative_scope_clears_only_its_previous_expectations(self):
        with TemporaryDirectory() as tmp:
            parent, child = self.configs(Path(tmp))
            self.record(parent, [name(), name(2023), name(tile="UTM34H")])
            self.snapshot(child, [])
            result = synchronize_yearly_expectations(parent, child)
            self.assertEqual(result["selected_count"], 0)
            self.assertEqual({r["file_name"] for r in expected_rows(parent)}, {name(2023), name(tile="UTM34H")})

    def test_missing_or_newest_failed_scan_cannot_clear_expectations(self):
        with TemporaryDirectory() as tmp:
            parent, child = self.configs(Path(tmp))
            self.record(parent, [name()])
            before = expected_rows(parent)
            with self.assertRaisesRegex(ValueError, "No complete"):
                synchronize_yearly_expectations(parent, child)
            self.snapshot(child, [])
            self.snapshot(child, [], status="failed")
            with self.assertRaisesRegex(ValueError, "No complete"):
                synchronize_yearly_expectations(parent, child)
            self.assertEqual(expected_rows(parent), before)

    def test_legacy_preview_needs_success_and_rejects_wrong_scope(self):
        with TemporaryDirectory() as tmp:
            parent, child = self.configs(Path(tmp))
            self.record(parent, [name()])
            with self.assertRaises(ValueError):
                synchronize_yearly_expectations(parent, child, preview_rows=[])
            with self.assertRaisesRegex(ValueError, "out-of-scope"):
                synchronize_yearly_expectations(parent, child, preview_rows=[{"file_name": name(2023)}], download_complete=True)
            result = synchronize_yearly_expectations(parent, child, preview_rows=[
                {"file_name": name(crid="PIC0"), "selected_for_download": "yes"},
                {"file_name": name(2023), "selected_for_download": "no", "status": "EXCLUDED_ACQUISITION_WINDOW"}],
                download_complete=True)
            self.assertEqual(result["origin_kind"], "completed_download_preview")
            self.assertEqual({r["file_name"] for r in expected_rows(parent)}, {name(crid="PIC0")})

    def test_update_and_history_roll_back_together_on_database_failure(self):
        with TemporaryDirectory() as tmp:
            parent, child = self.configs(Path(tmp))
            self.record(parent, [name()])
            self.snapshot(child, [name(crid="PIC0")])
            before = expected_rows(parent)
            with mock.patch.object(ProjectDatabase, "_bump_revision", side_effect=RuntimeError("test interruption")):
                with self.assertRaisesRegex(RuntimeError, "test interruption"):
                    synchronize_yearly_expectations(parent, child)
            self.assertEqual(expected_rows(parent), before)
            database = ProjectDatabase(database_path_for(Path(parent["processing"]["logs"]) / "update_expected.csv"))
            self.assertEqual(database.read_rows("yearly_expectation_history"), [])


if __name__ == "__main__":
    unittest.main()
