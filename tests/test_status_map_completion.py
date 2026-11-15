"""Completion is coverage of the full requested period, not a finished child year."""
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from swotflow.ui.main import LauncherApp
from swotflow.ui.utm_map import (
    PIPELINE_STATUS_STYLES, DisplayTile, UTMDisplayGeometry, UTMPipelineStatusMap,
    UTMMapSelectorDialog, pipeline_completion_notes, pipeline_status_key,
)


def coverage(tile="UTM36P", expected=4029, verified=3053, status="pending_upload"):
    return (tile, expected, expected, expected, expected, verified,
            "2026-10-30", "2026-10-30", "2026-10-30", "2026-10-30", "2025-12-31", status)


def yearly():
    return {"tiles": ["UTM36P"], "start_date": "2023-01-01", "end_date": "2026-10-31",
            "parent_campaign_id": "full", "year_batches": [
                {"tile": "UTM36P", "year": y, "status": "complete" if y < 2026 else "running"}
                for y in range(2023, 2027)]}


def controller():
    app = LauncherApp.__new__(LauncherApp)
    app.automation_running = True
    app.automation_progress_tiles = ["UTM36P"]
    app.automation_progress_stages = [f"{y}/upload" for y in range(2023, 2027)]
    app.automation_progress_completed_keys = {("UTM36P", f"{y}/upload") for y in range(2023, 2026)}
    app.automation_statistics_timer = None
    app.automation_map_dialog = None
    app.stats_status_map = Mock()
    app.root = Mock()
    app.notebook = Mock()
    app.notebook.tab.return_value = "Statistics"
    app.statistics_refresh_running = False
    app.refresh_project_statistics_if_active = Mock()
    return app


class FullRangeCompletionTests(unittest.TestCase):
    def test_previous_years_do_not_complete_requested_range(self):
        notes = pipeline_completion_notes([coverage()], yearly())
        self.assertIn("3053/4029", notes["UTM36P"])
        self.assertIn("2023-01-01 to 2026-10-31", notes["UTM36P"])

    def test_unprocessed_and_deferred_inputs_remain_incomplete(self):
        for status in ("pending_download", "pending_extract", "pending_mosaic", "pending_upload", "attention"):
            with self.subTest(status=status):
                self.assertIn("UTM36P", pipeline_completion_notes([coverage(status=status)], yearly()))

    def test_only_verified_full_range_has_no_completion_block(self):
        self.assertFalse(pipeline_completion_notes([coverage(verified=4029, status="complete")], yearly()))
        # Even a malformed complete label cannot overrule missing identities.
        self.assertTrue(pipeline_completion_notes([coverage(status="complete")], yearly()))

    def test_missing_and_empty_catalogue_are_not_proof(self):
        self.assertIn("UTM36P", pipeline_completion_notes([], yearly()))
        self.assertIn("UTM36P", pipeline_completion_notes([coverage(expected=0, verified=0, status="no_expected")], yearly()))

    def test_missing_output_counter_cannot_hide_unverified_mosaics(self):
        self.assertEqual(pipeline_status_key(976, 976, 173, 172, 0), "attention")
        self.assertEqual(pipeline_status_key(976, 976, 173, 173, 0), "uploaded")

    def test_controller_uses_parent_even_if_current_coverage_is_completed_year(self):
        app = controller()
        info = SimpleNamespace(yearly_automation=yearly(), update_coverage_tile_rows=[coverage(verified=4029, status="complete")],
                               update_coverage_campaign_rows={"full": [coverage()]})
        self.assertIn("3053/4029", app.status_map_completion_notes(info)["UTM36P"])
        info.update_coverage_campaign_rows = {}
        self.assertIn("not established", app.status_map_completion_notes(info)["UTM36P"])

    def test_ordinary_full_window_also_needs_verified_sources(self):
        app = controller()
        info = SimpleNamespace(update_coverage_tile_rows=[coverage()])
        self.assertIn("UTM36P", app.status_map_completion_notes(info))

    def test_live_overlay_survives_stale_completed_snapshot(self):
        app = controller()
        info = SimpleNamespace(update_coverage_tile_rows=[coverage(verified=4029, status="complete")])
        self.assertIn("UTM36P", app.status_map_completion_notes(info, live=True))
        app.automation_progress_completed_keys.add(("UTM36P", "2026/upload"))
        self.assertFalse(app.status_map_completion_notes(info, live=True))

    def test_visible_refresh_has_one_timer_and_no_duplicate_scan(self):
        app = controller()
        app.poll_automation_statistics()
        app.refresh_project_statistics_if_active.assert_called_once_with("live Automation coverage")
        app.root.after.assert_called_once_with(60_000, app.poll_automation_statistics)
        app.statistics_refresh_running = True
        app.poll_automation_statistics()
        self.assertEqual(app.refresh_project_statistics_if_active.call_count, 1)

    def test_hidden_map_avoids_history_scans_and_stopped_run_ends_timer(self):
        app = controller()
        app.notebook.tab.return_value = "Automation"
        app.poll_automation_statistics()
        app.refresh_project_statistics_if_active.assert_not_called()
        app.automation_running = False
        app.root.after.reset_mock()
        app.poll_automation_statistics()
        app.root.after.assert_not_called()

    def test_open_selector_refreshes_even_when_statistics_hidden(self):
        app = controller()
        app.notebook.tab.return_value = "Automation"
        app.automation_map_dialog = Mock()
        app.automation_map_dialog.winfo_exists.return_value = True
        app.poll_automation_statistics()
        app.refresh_project_statistics_if_active.assert_called_once()

    def test_repeated_upload_log_lines_do_not_recompute_range_overlay(self):
        app = controller()
        app.automation_running = False
        app.automation_map_progress_key = None
        app.automation_progress_text_var = Mock()
        app.automation_status_var = Mock()
        app.update_live_map_pending_tiles = Mock()
        for index in range(100):
            app.update_automation_progress("UTM36P", "2026/upload", "running", f"log {index}")
        app.update_live_map_pending_tiles.assert_called_once()
        app.update_automation_progress("UTM36P", "2026/upload", "success", "done")
        self.assertEqual(app.update_live_map_pending_tiles.call_count, 2)

    def test_resume_replaces_old_timer_instead_of_adding_another(self):
        app = controller()
        app.automation_statistics_timer = "previous"
        app.schedule_automation_statistics_refresh()
        app.root.after_cancel.assert_called_once_with("previous")
        app.root.after.assert_called_once()


class MapRenderingCompletionTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        tiles = {}
        for i, token in enumerate(("UTM35L", "UTM36P", "UTM36L")):
            x = i * 2
            tiles[token] = DisplayTile(token, (x, 0, x+1, 1), [[(x, 0), (x+1, 0), (x+1, 1), (x, 1), (x, 0)]])
        self.geometry = UTMDisplayGeometry((0, 0, 6, 1), tiles, [])
        self.widget = UTMPipelineStatusMap(self.root, self.geometry)
        self.qa = [("UTM35L", 3711, 3711, 662, 662, 662, 0),
                   ("UTM36P", 3053, 3053, 586, 586, 586, 0),
                   ("UTM36L", 3, 3, 2, 2, 1, 1)]
        self.widget.set_tile_statuses(self.qa)

    def tearDown(self):
        self.root.destroy()

    def test_three_map_features_and_counts_stay_consistent(self):
        self.widget.set_completion_notes(pipeline_completion_notes([coverage()], yearly()))
        expected = {"UTM35L": "uploaded", "UTM36P": "pending_range", "UTM36L": "attention"}
        for tile, state in expected.items():
            with self.subTest(tile=tile):
                self.assertEqual(self.widget.tile_status(tile).status, state)
        fills = [self.widget.canvas.itemcget(i, "fill") for i in self.widget.canvas.find_all()
                 if self.widget.canvas.type(i) == "polygon"]
        self.assertCountEqual(fills, [PIPELINE_STATUS_STYLES[s]["fill"] for s in expected.values()])
        self.assertIn("awaiting verification: 1", self.widget.status_var.get())
        self.widget.update_tile_status("UTM36P")
        self.assertIn("3053/4029", self.widget.status_var.get())
        self.assertEqual(self.widget.tile_status("UTM36P").uploaded, 586)

    def test_completed_historical_window_does_not_change_pipeline_colour(self):
        self.widget.set_completion_notes(pipeline_completion_notes([coverage()], yearly()))
        self.widget.set_update_campaigns(
            [("old", "2023 only", "2023-01-01", "2023-12-31", "", 3053, 1)],
            {"old": [coverage(expected=3053, verified=3053, status="complete")]}, "old")
        self.widget.map_mode_var.set("Update Coverage")
        self.widget.on_mode_changed()
        self.assertEqual(self.widget.tile_update_coverage_status("UTM36P").status, "complete")
        self.widget.map_mode_var.set("Pipeline Status")
        self.widget.on_mode_changed()
        self.assertEqual(self.widget.tile_status("UTM36P").status, "pending_range")

    def test_final_verified_coverage_can_turn_green_without_changing_counts(self):
        self.widget.set_completion_notes(pipeline_completion_notes([coverage()], yearly()))
        self.widget.set_tile_statuses([("UTM36P", 4029, 4029, 759, 759, 759, 0)])
        self.widget.set_completion_notes(pipeline_completion_notes([coverage(verified=4029, status="complete")], yearly()))
        self.assertEqual(self.widget.tile_status("UTM36P").status, "uploaded")

    def test_async_saved_counts_cannot_clear_live_overlay(self):
        self.widget.set_live_pending_tiles(["UTM36P"])
        self.widget.set_tile_statuses(self.qa)
        self.widget.set_completion_notes({})
        self.assertEqual(self.widget.tile_status("UTM36P").status, "pending_range")
        self.widget.clear_statuses()
        self.assertFalse(self.widget.live_pending_tiles)
        self.assertFalse(self.widget.completion_notes)

    def test_selector_uses_same_range_rules_and_keeps_selection_on_refresh(self):
        dialog = UTMMapSelectorDialog(self.root, self.geometry, ["UTM36P"], Mock(), status_rows=self.qa,
                                     completion_notes=pipeline_completion_notes([coverage()], yearly()))
        self.assertEqual(dialog.tile_statuses["UTM36P"].status, "pending_range")
        dialog.set_pipeline_statuses(self.qa, {})
        self.assertEqual(dialog.selected_tiles, {"UTM36P"})
        self.assertEqual(dialog.tile_statuses["UTM36P"].uploaded, 586)
        self.assertNotIn("green = covered", dialog.status_var.get())
        dialog.destroy()


if __name__ == "__main__":
    unittest.main()
