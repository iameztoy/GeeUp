"""Upload-name limits, compact metadata and retained provenance regressions."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swotflow.core.metadata import (
    compact_mosaic_asset_stem, index_mosaic_lineage, parse_swot_l2_hr_raster_metadata,
    product_identity_key, validate_ee_asset_path, REPLACED_INVALID_ASSET_ID_STATUS,
)
from swotflow.earthengine.uploader import UploadItem, compact_upload_properties
from swotflow.project.database import read_project_rows
from swotflow.project.insights import merge_upload_rows_with_ee_inventory
from tests import test_uploader_regressions as fixtures
from tests.test_ee_consolidation import asset, manifest, COLLECTION
from swotflow.consolidation.core import read_lineage, source_record
from swotflow.catalog.history import represented_inputs

STEM = "SWOT_L2_HR_Raster_100m_UTM35L_N_x_x_x_001_152_MOSA_20230726T153018_20230726T153239_PGD0_01"
LONG = STEM + "_PVPGD0-01_to_PGD0-01"


class UploadAssetNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="swotflow-upload-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture = fixtures.UploaderDialogRegressionTests()
        self.uploader = fixture.uploader(fixture.uploader_config(self.root))

    def test_rejected_real_mosaic_becomes_parseable_legacy_cloud_name(self):
        self.assertEqual(len(LONG), 111)
        self.assertEqual(self.uploader.build_asset_name(LONG), STEM)
        self.assertEqual(len(STEM), 90)
        self.assertEqual(product_identity_key(LONG + ".tif"), product_identity_key(STEM + ".tif"))
        self.assertEqual(self.uploader.build_asset_name(STEM), STEM)
        self.assertEqual(self.uploader.build_asset_name("x" * 100), "x" * 100)

    def test_invalid_components_and_unrecognized_overlength_fail_early(self):
        for value in ("projects//assets/a", "projects/p/assets/", "projects/p/assets/" + "x" * 101):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "1–100"):
                validate_ee_asset_path(value)
        with self.assertRaisesRegex(ValueError, "1–100"):
            self.uploader.build_asset_name("x" * 101)
        self.uploader.config.upload.prefix = "x" * 20
        with self.assertRaisesRegex(ValueError, "1–100"):
            self.uploader.build_asset_name(LONG)

    def test_plan_detects_compaction_collisions_before_browser(self):
        for name in (LONG, STEM):
            (self.uploader.config.input_folder / (name + ".tif")).write_bytes(b"fixture")
        with mock.patch.object(self.uploader, "sync_earth_engine_inventory"), \
             mock.patch.object(self.uploader, "create_driver") as browser:
            self.assertEqual(self.uploader._run(), 1)
            browser.assert_not_called()

    def test_dialog_metadata_is_compact_but_local_history_is_complete(self):
        parsed = parse_swot_l2_hr_raster_metadata(LONG + ".tif")
        parsed.properties["swot_processing_levels"] = '["PGD0_01"]'
        parsed.properties["custom_note"] = "retain"
        before = dict(parsed.properties)
        compact = compact_upload_properties(parsed)
        self.assertEqual(parsed.properties, before)
        self.assertEqual(len(before) - len(compact), 6)
        self.assertEqual(compact["swot_processing_min"], "PGD0_01")
        self.assertEqual(compact["swot_processing_max"], "PGD0_01")
        self.assertEqual(compact["custom_note"], "retain")
        self.assertEqual(parsed.start_time, "2023-07-26 15:30:18")
        parsed.properties["swot_descriptor"] = "custom description"
        self.assertEqual(compact_upload_properties(parsed)["swot_descriptor"], "custom description")

    def test_verified_upload_still_sends_all_three_binding_properties(self):
        from swotflow.verification.pipeline import ingestion_properties
        item = UploadItem(self.root / (LONG + ".tif"), STEM, "projects/example/assets/" + STEM)
        item.ingestion_revision = {"token": "revision", "local_sha256": "bytes", "sources": {"source.nc": {"hash": "nasa"}}}
        self.uploader.config.verified_workflow = True
        self.uploader._mosaic_version_lookup = {item.local_file.name: {
            "processing_min": "PGD0_01", "processing_max": "PGD0_01", "processing_levels": '["PGD0_01"]'}}
        with mock.patch.object(self.uploader, "is_current_upload_dialog_open", return_value=True), \
             mock.patch.object(self.uploader, "_apply_current_ui_metadata_properties") as applied:
            self.uploader.apply_metadata_properties(item)
        properties = applied.call_args.args[0].properties
        for key, value in ingestion_properties(item.ingestion_revision).items():
            self.assertEqual(properties[key], value)
        report = self.uploader.make_report_row(item, "", "", "PLANNED_UPLOAD", "")
        self.assertIn("swot_processing_levels", json.loads(report["metadata_properties"]))

    def seed_invalid_attempt(self):
        path = self.uploader.config.input_folder / (LONG + ".tif")
        path.write_bytes(b"fixture")
        parent = self.uploader.config.destination_parent
        old_id = parent + "/" + LONG
        item = UploadItem(path, STEM, parent + "/" + STEM)
        self.uploader.report.upsert({"asset_id": old_id, "local_file": str(path),
                                    "final_status": "ERROR", "error_message": "disabled upload"})
        self.uploader.config.execution.dry_run = False
        self.uploader.ee_inventory_synced = True
        return old_id, item

    def test_retry_retains_old_attempt_but_statistics_count_one_current_item(self):
        old_id, item = self.seed_invalid_attempt()
        self.uploader.archive_invalid_name_attempts([item])
        self.uploader.archive_invalid_name_attempts([item])  # Idempotent after interruption.
        self.assertEqual(self.uploader.report.get_status(old_id), REPLACED_INVALID_ASSET_ID_STATUS)
        self.uploader.report.upsert(self.uploader.make_report_row(item, "", "", "PLANNED_UPLOAD", ""))
        rows = read_project_rows(self.uploader.report.report_path, "upload_report")
        self.assertEqual(len(rows), 2)
        current = merge_upload_rows_with_ee_inventory(rows, [])
        self.assertEqual([row["asset_id"] for row in current], [item.asset_id])
        events = read_project_rows(self.uploader.report.events_path, "upload_events")
        self.assertEqual(sum(row["new_status"] == REPLACED_INVALID_ASSET_ID_STATUS for row in events), 1)
        self.assertTrue(any(row["new_status"] == "ERROR" for row in events))

    def test_archiving_requires_current_inventory_and_never_hides_a_task(self):
        old_id, item = self.seed_invalid_attempt()
        self.uploader.ee_inventory_synced = False
        self.uploader.archive_invalid_name_attempts([item])
        self.assertEqual(self.uploader.report.get_status(old_id), "ERROR")
        self.uploader.ee_inventory_synced = True
        self.uploader.report.rows[old_id]["detected_task_name"] = "task"
        with self.assertRaisesRegex(ValueError, "contradictory"):
            self.uploader.archive_invalid_name_attempts([item])

    def test_nasa_accounting_and_consolidation_resolve_shortened_cloud_name(self):
        a = asset()
        stem = a["id"].rsplit("/", 1)[-1]
        row = next(iter(manifest(a).values()))
        row["output_file"] = str(self.root / (stem + "_PVPGD0-01_to_PGD0-01.tif"))
        path = self.root / "mosaic_manifest.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        source = source_record(a, read_lineage(path))
        self.assertTrue(source["lineage_complete"])
        represented, unresolved = represented_inputs({"collection": COLLECTION, "assets": [a]}, [row])
        self.assertFalse(unresolved)
        self.assertEqual(len(represented), 1)
        conflicting = dict(row, output_file=row["output_file"].replace("_to_PGD0-01", "_to_PGD0-02"))
        self.assertNotIn(stem, index_mosaic_lineage([row, conflicting]))


if __name__ == "__main__":
    unittest.main()
