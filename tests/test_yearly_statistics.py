"""Full-request scope and conservative counts for yearly Automation statistics."""
import copy
import csv
import json
import os
import tempfile
import unittest
from collections import OrderedDict
from dataclasses import fields
from pathlib import Path
from unittest import mock

from swotflow.project.insights import (
    ProjectInsights,
    build_update_campaign_coverage,
    load_project_insights_snapshot,
    read_yearly_automation_overview,
    write_project_insights_snapshot,
    yearly_automation_overview,
)
from swotflow.project.updates import update_campaign_id


class YearlyStatisticsTests(unittest.TestCase):
    def state(self) -> dict:
        return {
            "run_id": "yearly_run",
            "config": {
                "process_by_year": True,
                "start_date": "2023-07-01", "end_date": "2026-09-30",
                "utm_tiles": ["UTM31S", "UTM34H", "UTM31S"],
                "base_config": {"download": {
                    "collection_short_name": "SWOT_L2_HR_Raster_100m_D",
                    "product_version_filter": "best",
                    "start_date": "2023-07-01", "end_date": "2026-09-30",
                    "acquisition_start_date": "2023-07-01", "acquisition_end_date": "2026-09-30",
                }},
            },
            "year_batches": [],
        }

    def batch(self, year: int, status: str, tile: str = "UTM31S") -> dict:
        return {"tile": tile, "year": year, "start_date": "2023-07-01" if year == 2023 else f"{year}-01-01",
                "end_date": "2026-09-30" if year == 2026 else f"{year}-12-31",
                "status": status, "run_dir": f"tiles/{tile}/years/{year}"}

    def test_pending_and_failed_years_do_not_disappear_or_multiply_tiles(self) -> None:
        saved = self.state()
        saved["year_batches"] = [self.batch(2023, "complete"), self.batch(2024, "incomplete")]
        before = copy.deepcopy(saved)
        overview = yearly_automation_overview(saved, Path("example"))
        self.assertEqual(overview["unique_tiles"], 2)
        self.assertEqual(overview["years_total"], 4)
        self.assertEqual(overview["years_complete"], 0)
        self.assertEqual(overview["years_remaining"], 4)
        self.assertEqual(overview["years_pending"], 2)
        self.assertEqual(overview["batches_total"], 8)
        self.assertEqual(overview["batches_complete"], 1)
        self.assertEqual(overview["tiles_complete"], 0)
        self.assertEqual(overview["status"], "incomplete")
        self.assertEqual(saved, before)

    def test_no_receipts_is_pending_and_explicit_empty_search_completion_counts(self) -> None:
        saved = self.state()
        empty = yearly_automation_overview(saved, Path("example"))
        self.assertEqual(empty["status"], "pending")
        self.assertEqual(empty["years_complete"], 0)
        saved["year_batches"] = [self.batch(year, "complete", tile)
                                  for tile in ("UTM31S", "UTM34H") for year in range(2023, 2027)]
        # No observations are necessary to describe a successfully searched empty year.
        complete = yearly_automation_overview(saved, Path("example"))
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["years_complete"], 4)
        self.assertEqual(complete["unique_tiles"], 2)
        self.assertEqual(complete["tiles_complete"], 2)
        self.assertEqual(complete["batches_complete"], 8)
        self.assertIn("pixel verification remain separate", complete["note"])

    def test_duplicate_unknown_and_low_space_receipts_fail_closed(self) -> None:
        saved = self.state()
        saved["year_batches"] = [self.batch(2023, "complete"), self.batch(2023, "complete"),
                                  self.batch(2024, "future_status"), self.batch(2025, "low_space")]
        overview = yearly_automation_overview(saved, Path("example"))
        self.assertEqual(overview["years_complete"], 0)
        self.assertEqual(overview["status"], "low_space")
        self.assertEqual([row["status"] for row in overview["year_batches"]],
                         ["incomplete", "incomplete", "low_space", "pending"] + ["pending"] * 4)

    def test_storage_pause_keeps_reason_when_engine_sets_stopped(self) -> None:
        saved = self.state()
        saved["stopped"] = True
        saved["year_batches"] = [self.batch(2023, "complete"), self.batch(2024, "low_space")]
        overview = yearly_automation_overview(saved, Path("example"))
        self.assertEqual(overview["status"], "low_space")
        self.assertEqual(overview["batches_complete"], 1)
        self.assertEqual(overview["batches_remaining"], 7)

    def test_first_tile_all_three_years_is_not_the_whole_request(self) -> None:
        saved = self.state()
        saved["config"]["end_date"] = "2025-12-31"
        saved["year_batches"] = [self.batch(year, "complete") for year in (2023, 2024, 2025)]
        overview = yearly_automation_overview(saved, Path("example"))
        self.assertEqual(overview["tiles_complete"], 1)
        self.assertEqual(overview["unique_tiles"], 2)
        self.assertEqual(overview["batches_complete"], 3)
        self.assertEqual(overview["batches_total"], 6)
        self.assertEqual(overview["years_complete"], 0)
        self.assertNotEqual(overview["status"], "complete")

    def write_state(self, logs: Path, name: str, state: dict, stamp: int) -> Path:
        folder = logs / "automation_runs" / name
        folder.mkdir(parents=True)
        (folder / "automation_run.json").write_text(json.dumps(state), encoding="utf-8")
        os.utime(folder, (stamp, stamp))
        return folder

    def test_reading_only_latest_parent_does_not_create_database_or_follow_nested_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / "logs"
            config = {"processing": {"logs": str(logs)}}
            self.assertEqual(read_yearly_automation_overview(config), {})
            self.assertFalse(logs.exists())
            parent = self.write_state(logs, "yearly", self.state(), 100)
            child = parent / "tiles" / "UTM31S" / "years" / "2026"
            child.mkdir(parents=True)
            child_state = self.state()
            child_state["config"]["start_date"] = "2026-01-01"
            (child / "automation_run.json").write_text(json.dumps(child_state), encoding="utf-8")
            with mock.patch("swotflow.project.insights.ProjectDatabase", side_effect=AssertionError("No database writes")):
                overview = read_yearly_automation_overview(config)
            self.assertEqual(overview["start_date"], "2023-07-01")
            self.assertEqual(overview["years_total"], 4)
            self.assertEqual(list(logs.rglob("*.sqlite3")), [])

    def test_latest_non_yearly_or_archived_run_does_not_override_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / "logs"
            config = {"processing": {"logs": str(logs)}}
            older = self.write_state(logs, "older", self.state(), 100)
            normal = self.state()
            normal["config"]["process_by_year"] = False
            self.write_state(logs, "newer", normal, 200)
            self.assertEqual(read_yearly_automation_overview(config), {})
            (older / "archived.json").write_text("{}", encoding="utf-8")
            self.assertEqual(read_yearly_automation_overview(config), {})

    def test_full_range_parent_is_default_even_when_annual_campaign_is_newer(self) -> None:
        state = self.state()
        full = state["config"]["base_config"]
        parent_id = update_campaign_id(full)
        annual = copy.deepcopy(full)
        annual["download"].update(start_date="2026-01-01", end_date="2026-09-30",
                                   acquisition_start_date="2026-01-01")
        annual_id = update_campaign_id(annual)
        campaigns = [{"campaign_id": parent_id, **full["download"], "updated_at": "2026-10-01",
                       "tiles": '["UTM31S", "UTM34H"]'},
                     {"campaign_id": annual_id, **annual["download"], "updated_at": "2026-10-26",
                       "tiles": '["UTM31S", "UTM34H"]'}]
        kwargs = dict(config=full, download_rows=[], legacy_preview_rows=[], extract_rows=[],
                      mosaic_rows=[], upload_rows=[])
        with mock.patch("swotflow.project.insights.read_update_campaigns", return_value=campaigns), \
                mock.patch("swotflow.project.insights.read_update_expected", return_value=[]):
            summaries, coverage, active = build_update_campaign_coverage(
                **kwargs, yearly_overview=yearly_automation_overview(state, Path("example")))
            self.assertEqual(active, parent_id)
            self.assertEqual({row[0] for row in summaries}, {parent_id, annual_id})
            self.assertIn(annual_id, coverage)  # Remains available for explicit user selection.
            self.assertEqual(build_update_campaign_coverage(**kwargs, yearly_overview={})[2], annual_id)
            campaigns[0]["archived"] = "yes"
            self.assertEqual(build_update_campaign_coverage(
                **kwargs, yearly_overview=yearly_automation_overview(state, Path("example")))[2], annual_id)

    def test_overview_survives_snapshot_and_exports_annual_windows(self) -> None:
        overview = yearly_automation_overview(self.state(), Path("example"))
        values = {item.name: [] for item in fields(ProjectInsights)}
        values.update(metrics=OrderedDict(), update_coverage_campaign_rows={},
                      active_update_campaign_id="", yearly_automation=overview)
        insights = ProjectInsights(**values)
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / "logs"
            config = {"processing": {"logs": str(logs)}}
            snapshot_path = write_project_insights_snapshot(config, insights)
            loaded, _stamp = load_project_insights_snapshot(config)
            self.assertEqual(loaded.yearly_automation, overview)
            with (snapshot_path.parent / "project_statistics_yearly_automation.csv").open(
                    encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 8)
            self.assertEqual(rows[0]["tile"], "UTM31S")
            self.assertEqual(rows[0]["start_date"], "2023-07-01")
            self.assertEqual(rows[-1]["end_date"], "2026-09-30")
            legacy = json.loads(snapshot_path.read_text(encoding="utf-8"))
            del legacy["yearly_automation"]
            snapshot_path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(load_project_insights_snapshot(config)[0].yearly_automation, {})


if __name__ == "__main__":
    unittest.main()
