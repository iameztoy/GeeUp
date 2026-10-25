"""Offline grouping/provenance and non-destructive export contract tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import swotflow.consolidation.core as c

COLLECTION = "projects/example/assets/source"
DESTINATION = "projects/example/assets/consolidated"


def local_temp():
    """Keep isolated fixtures outside the source checkout."""
    return tempfile.TemporaryDirectory(prefix="swotflow-test-")


def asset(tile="UTM31P", start="20240101T010000", end="20240101T010020", crs="EPSG:32631", x=500050, cycle="010", pass_id="029", counter="01"):
    """Make a realistic two-band native asset metadata fixture."""
    name = f"SWOT_L2_HR_Raster_100m_{tile}_N_x_x_x_{cycle}_{pass_id}_MOSA_{start}_{end}_PGD0_{counter}"
    grid = {"crsCode": crs, "dimensions": {"width": 10, "height": 20},
            "affineTransform": {"scaleX": 100, "scaleY": -100, "translateX": x, "translateY": 900050}}
    return {"id": COLLECTION + "/" + name, "type": "IMAGE", "updateTime": "2026-06-15T01:02:03Z",
            "bands": [{"id": b, "grid": copy.deepcopy(grid), "pyramidingPolicy": "MEAN"} for b in ("b1", "b2")]}


def manifest(a, crids=("PGD0",)):
    """Make original-tile provenance with distinct processing identifiers."""
    name = a["id"].rsplit("/", 1)[-1]
    return {name: {"input_files": json.dumps([name.replace("MOSA", f"{i:03}F").replace("PGD0", crid) + ".tif" for i, crid in enumerate(crids)]), "status": "MOSAIC_CREATED"}}


class PlanTests(unittest.TestCase):
    def test_initial_plan_refuses_two_versions_of_one_acquisition(self):
        a = asset()
        b = asset(counter="02")
        lineage = {**manifest(a), **manifest(b)}
        for separate in (False, True):
            with self.assertRaisesRegex(ValueError, "Duplicate acquisition"):
                c.plan_assets([a, b], COLLECTION, c.Options(separate_versions=separate), lineage)

    def test_grid_zone_hemisphere_alignment_and_tile_modes(self):
        assets = [asset(), asset(tile="UTM31Q", x=500150), asset(tile="UTM31M", crs="EPSG:32731"),
                  asset(tile="UTM32P", crs="EPSG:32632"), asset(tile="UTM31R", x=500000)]
        native = c.plan_assets(assets, COLLECTION, c.Options())
        self.assertEqual(native["summary"]["potential_reduction"], 1)
        self.assertEqual(c.plan_assets(assets, COLLECTION, c.Options(mode="tile"))["summary"]["potential_reduction"], 0)
        self.assertEqual(c.plan_assets(assets, COLLECTION, c.Options(mode="common"))["summary"]["potential_reduction"], 3)
        self.assertEqual(c.plan_assets(assets, COLLECTION, c.Options(mode="common", separate_hemispheres=False))["summary"]["potential_reduction"], 4)
        self.assertEqual(native["groups"][0]["transform"], [100, 0, 500050, 0, -100, 900050])

    def test_cycle_pass_and_utc_start_date_are_never_merged(self):
        assets = [asset(start="20240101T235950", end="20240102T000005"),
                  asset(start="20240102T000001", end="20240102T000020"),
                  asset(cycle="011"), asset(pass_id="030")]
        self.assertFalse(c.plan_assets(assets, COLLECTION, c.Options(mode="common"))["groups"])

    def test_counts_retained_assets_and_date_filter(self):
        assets = [asset(), asset(tile="UTM31Q"), asset(start="20240102T010000", end="20240102T010020")]
        plan = c.plan_assets(assets, COLLECTION, c.Options(end="2024-01-01"))
        self.assertEqual(plan["summary"]["eventual_assets"], 2)
        self.assertEqual(plan["summary"]["temporary_assets_with_originals"], 4)
        self.assertEqual(plan["summary"]["outside_dates"], 1)
        self.assertEqual(plan["summary"]["mergeable_inputs"], 2)

    def test_provenance_mixed_versions_and_strict_policy(self):
        a, b = asset(), asset(tile="UTM31Q", start="20240101T010019", end="20240101T010040")
        lineage = {**manifest(a, ("PGD0", "PIC2")), **manifest(b)}
        plan = c.plan_assets([b, a], COLLECTION, c.Options(), lineage)
        group = plan["groups"][0]
        self.assertEqual(group["versions"], ["PGD0_01", "PIC2_01"])
        self.assertEqual(group["end"], "2024-01-01T01:00:40+00:00")
        self.assertEqual(group["unknown_lineage"], 0)
        self.assertEqual(len(group["sources"][0]["original_inputs"]), 2)
        self.assertEqual(group["sources"][0]["asset"]["id"], a["id"])
        self.assertFalse(c.plan_assets([a, b], COLLECTION, c.Options(separate_versions=True), lineage)["groups"])
        self.assertFalse(c.plan_assets([a, b], COLLECTION, c.Options(separate_versions=True))["groups"])
        same = {**manifest(a), **manifest(b)}
        self.assertTrue(c.plan_assets([a, b], COLLECTION, c.Options(separate_versions=True), same)["groups"])

    def test_unknown_inputs_and_bad_metadata_excluded_visibly(self):
        a = asset()
        bad = asset(tile="UTM31Q")
        bad["bands"][1]["grid"]["affineTransform"]["translateX"] += 100
        timed = asset(tile="UTM31R")
        timed["startTime"] = "2024-01-02T01:00:00Z"
        plan = c.plan_assets([a, bad, timed], COLLECTION, c.Options())
        self.assertEqual(plan["summary"]["excluded"], 2)
        self.assertEqual(plan["summary"]["eventual_assets"], 3)
        with self.assertRaises(ValueError):
            c.plan_assets([a, a], COLLECTION, c.Options())

    def test_lightweight_comparison_agrees_with_detailed_plans(self):
        assets = [asset(), asset(tile="UTM31Q"), asset(tile="UTM31M", crs="EPSG:32731")]
        rows = c.option_comparison(assets, COLLECTION, c.Options(), {})
        for row in rows:
            summary = c.plan_assets(assets, COLLECTION, c.Options(mode=row["mode"]))["summary"]
            self.assertEqual(row["potential_reduction"], summary["potential_reduction"])
            self.assertEqual(row["eventual_assets"], summary["eventual_assets"])

    def test_common_crs_is_explicit_in_plan(self):
        p = c.plan_assets([asset(), asset(tile="UTM31Q")], COLLECTION, c.Options(mode="common"))
        self.assertIn('Cylindrical_Equal_Area', p["groups"][0]["crs_definition"])
        self.assertEqual(p["export_settings"]["pyramidingPolicy"], {".default": "sample"})

    def test_previous_cross_midnight_input_mosaic_requires_split(self):
        a = asset()
        lineage = manifest(a)
        row = next(iter(lineage.values()))
        row["input_files"] = row["input_files"].replace("20240101T010000", "20240102T010000")
        plan = c.plan_assets([a, asset(tile="UTM31Q")], COLLECTION, c.Options(), lineage)
        self.assertEqual(plan["summary"]["excluded"], 1)
        self.assertIn("different UTC dates", plan["excluded"][0]["reason"])

    def test_deterministic_ids_and_immutable_saved_lineage(self):
        a, b = asset(), asset(tile="UTM31Q")
        first = c.plan_assets([a, b], COLLECTION, c.Options())
        second = c.plan_assets([b, a], COLLECTION, c.Options())
        self.assertEqual(first["groups"], second["groups"])
        b["updateTime"] = "2026-06-16T01:02:03Z"
        self.assertNotEqual(first["groups"][0]["id"], c.plan_assets([a, b], COLLECTION, c.Options())["groups"][0]["id"])
        with local_temp() as tmp:
            path = c.save_plan(Path(tmp), first)
            self.assertEqual(json.loads(path.read_text()), first)
            second["notes"].append("changed")
            with self.assertRaises(FileExistsError):
                c.save_plan(Path(tmp), second)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.assets = [asset(), asset(tile="UTM31Q")]
        self.plan = c.plan_assets(self.assets, COLLECTION, c.Options())
        self.ids = [g["id"] for g in self.plan["groups"]]
        self.ee = Mock()
        self.ee.__version__ = "test"
        lookup = {a["id"]: a for a in self.assets}
        self.ee.data.getAsset.side_effect = lambda asset_id: copy.deepcopy(lookup[asset_id])
        self.ee.data.listAssets.return_value = {"assets": []}
        self.ee.data.getAssetRootQuota.return_value = {"asset_count": {"usage": 10, "limit": 50000}}
        self.ee.data.getTaskList.return_value = []
        self.ee.data.newTaskId.return_value = ["request-id"]
        self.ee.data.exportImage.return_value = {"name": "projects/example/operations/task-id"}
        self.ee.batch.Export.image.toAsset.return_value = SimpleNamespace(config={"serialized": "test"})

    def test_source_or_parent_destination_rejected_before_cloud_access(self):
        for destination in (COLLECTION, COLLECTION + "/child", "projects/example/assets"):
            with self.assertRaises(ValueError):
                c.validate_destination(COLLECTION, destination)

    def test_changed_source_blocks_every_cloud_write(self):
        self.ee.data.getAsset.side_effect = lambda aid: {**self.assets[0], "updateTime": "changed"}
        with local_temp() as tmp, self.assertRaises(ValueError):
            c.submit_groups(self.ee, self.plan, self.ids, DESTINATION, Path(tmp))
        self.ee.data.createAsset.assert_not_called()
        self.ee.data.exportImage.assert_not_called()

    def test_quota_and_active_task_guards(self):
        for quota, tasks in (({"usage": 49999, "limit": 50000}, []), ({"usage": 1, "limit": 50000}, [{"state": "READY"}] * 100)):
            self.ee.data.getAssetRootQuota.return_value = {"asset_count": quota}
            self.ee.data.getTaskList.return_value = tasks
            with local_temp() as tmp, self.assertRaises(ValueError):
                c.submit_groups(self.ee, self.plan, self.ids, DESTINATION, Path(tmp))
            self.ee.data.createAsset.assert_not_called()

    def test_submission_records_operation_and_does_not_duplicate(self):
        with local_temp() as tmp, patch.object(c, "export_parameters", return_value={}):
            result = c.submit_groups(self.ee, self.plan, self.ids, DESTINATION, Path(tmp))
            self.assertEqual(result["tasks"][self.ids[0]]["task_id"], "task-id")
            self.assertTrue((Path(tmp) / "plan.json").exists())
            self.ee.data.createAsset.assert_called_once_with({"type": "IMAGE_COLLECTION"}, DESTINATION)
            with self.assertRaises(ValueError):
                c.submit_groups(self.ee, self.plan, self.ids, DESTINATION, Path(tmp))
            self.ee.data.exportImage.assert_called_once_with("request-id", {"serialized": "test"})
            self.ee.data.deleteAsset.assert_not_called()
            self.ee.data.setAssetProperties.assert_not_called()

    def test_uncertain_submission_is_durable_and_never_blindly_retried(self):
        self.ee.data.exportImage.side_effect = TimeoutError("network response lost")
        with local_temp() as tmp, patch.object(c, "export_parameters", return_value={}):
            with self.assertRaises(RuntimeError):
                c.submit_groups(self.ee, self.plan, self.ids, DESTINATION, Path(tmp))
            saved = json.loads((Path(tmp) / "tasks.json").read_text())
            self.assertEqual(saved["tasks"][self.ids[0]]["state"], "SUBMISSION_UNCERTAIN")
            with self.assertRaises(ValueError):
                c.submit_groups(self.ee, self.plan, self.ids, DESTINATION, Path(tmp))
            self.ee.data.exportImage.assert_called_once()

    def test_active_identical_export_is_not_duplicated(self):
        self.ee.data.getTaskList.return_value = [{"state": "READY", "description": self.ids[0], "destination_uris": [DESTINATION + "/" + self.ids[0]]}]
        with local_temp() as tmp, self.assertRaises(ValueError):
            c.submit_groups(self.ee, self.plan, self.ids, DESTINATION, Path(tmp))
        self.ee.data.createAsset.assert_not_called()
        self.ee.data.exportImage.assert_not_called()

    def test_inventory_pagination_and_quota_errors_remain_visible(self):
        self.ee.data.getAsset.side_effect = None
        self.ee.data.getAsset.return_value = {"type": "IMAGE_COLLECTION"}
        self.ee.data.listAssets.side_effect = [{"assets": [self.assets[0]], "nextPageToken": "next"}, {"assets": [self.assets[1]]}]
        self.ee.data.getAssetRootQuota.side_effect = RuntimeError("quota permission unavailable")
        result = c.fetch_inventory(self.ee, COLLECTION)
        self.assertEqual(len(result["assets"]), 2)
        self.assertIsNone(result["quota"])
        self.assertIn("permission", result["quota_error"])
        self.ee.data.getAssetRootQuota.assert_called_once_with("projects/example/assets/")
        self.assertEqual(self.ee.data.listAssets.call_args.args[0]["pageToken"], "next")
        self.ee.data.createAsset.assert_not_called()

    def test_refresh_uses_full_operation_name_and_preserves_uncertainty(self):
        with local_temp() as tmp:
            path = Path(tmp) / "tasks.json"
            c.write_json(path, {"tasks": {"a": {"task_id": "id", "operation_name": "projects/other/operations/id"}, "b": {"task_id": "request"}}})
            self.ee.data.getOperation.return_value = {"done": True, "metadata": {"state": "SUCCEEDED"}}
            self.ee.data.getTaskStatus.return_value = [{"state": "UNKNOWN"}]
            result = c.refresh_tasks(self.ee, Path(tmp))
            self.assertEqual(result["tasks"]["a"]["state"], "SUCCEEDED")
            self.assertEqual(result["tasks"]["b"]["state"], "SUBMISSION_UNCERTAIN")
            self.ee.data.getOperation.assert_called_once_with("projects/other/operations/id")
            self.ee.data.exportImage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
