"""Scope and uncertainty in the Statistics evidence display."""
import copy
import unittest
from unittest import mock

from nasa_catalog_ui import STAGE_EVIDENCE, stage_evidence_label, verification_view_text


class StatisticsEvidenceTests(unittest.TestCase):
    def report(self) -> dict:
        return {
            "scope": {"start": "2024-07-01", "end": "2024-07-31", "tiles": ["UTM31S"]},
            "collection": "projects/example/assets/source",
            "utc": "2026-10-25T12:00:00Z",
            "catalogue_finished_utc": "2026-10-25T11:00:00Z",
            "inventory_finished_utc": "2026-10-25T11:30:00Z",
            "expected_preferred_inputs": 3,
            "counts": {"download_missing_or_unverified": 1, "verified_active": 2},
            "input_accounting_complete": False,
            "pixel_verification_complete": False,
            "blockers": [],
            "gap_note": "Input-accounting gaps are not measured spatial holes.",
        }

    def test_scope_counts_and_missing_receipt_are_not_collection_gaps(self) -> None:
        report = self.report()
        before = copy.deepcopy(report)
        with mock.patch("nasa_catalog_ui.history.recent", return_value=True):
            summary, status = verification_view_text(report)
        self.assertIn("Selected search only: 2024-07-01 to 2024-07-31 UTC | UTM31S", summary)
        self.assertIn("3 preferred inputs (not mosaic assets)", summary)
        self.assertIn("No verified download receipt: 1", summary)
        self.assertIn("Full local-to-EE evidence recorded: 2", summary)
        self.assertIn("not measured spatial holes", status)
        self.assertIn("does not prove", STAGE_EVIDENCE["download_missing_or_unverified"][1])
        self.assertEqual(report, before)

    def test_stale_evidence_never_displays_a_current_completeness_certificate(self) -> None:
        report = self.report()
        report.update(input_accounting_complete=True, pixel_verification_complete=True)
        with mock.patch("nasa_catalog_ui.history.recent", return_value=False):
            _, status = verification_view_text(report)
        self.assertEqual(status.count("refresh needed"), 2)
        self.assertIn("not a guarantee of a complete or duplicate-free collection", status)
        self.assertIn("original-source checks are separate", status)

    def test_legacy_report_missing_dates_is_unknown_not_fresh(self) -> None:
        report = self.report()
        del report["catalogue_finished_utc"], report["inventory_finished_utc"]
        _, status = verification_view_text(report)
        self.assertEqual(status.count("not recorded — reconcile again"), 2)
        self.assertNotIn("(recent)", status)

    def test_empty_and_future_state_are_explicit_without_inventing_zero_gaps(self) -> None:
        report = self.report()
        report.update(expected_preferred_inputs=0, counts={}, blockers=["Search incomplete."])
        summary, status = verification_view_text(report)
        self.assertIn("0 preferred inputs", summary)
        self.assertIn("Search incomplete.", status)
        self.assertEqual(stage_evidence_label("future_state"), "future state")
