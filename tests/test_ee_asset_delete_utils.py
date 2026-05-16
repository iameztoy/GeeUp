import unittest

from Utils.delete_ee_collection_children import (
    AssetRecord,
    infer_project_from_asset,
    list_child_assets,
    normalize_asset_record,
    parse_args,
    render_progress_bar,
    should_delete_record,
    sort_for_safe_deletion,
)


class EeAssetDeleteUtilsTests(unittest.TestCase):
    def test_infers_project_from_modern_asset_id(self):
        asset = "projects/example-project/assets/example_collection"
        self.assertEqual(infer_project_from_asset(asset), "example-project")

    def test_legacy_asset_id_has_no_inferred_project(self):
        self.assertIsNone(infer_project_from_asset("users/example/my_collection"))

    def test_normalizes_list_assets_output(self):
        record = normalize_asset_record(
            {
                "name": "projects/example/assets/folder/image_01",
                "type": "image",
            }
        )
        self.assertEqual(record.asset_id, "projects/example/assets/folder/image_01")
        self.assertEqual(record.asset_type, "IMAGE")

    def test_default_policy_deletes_only_images(self):
        self.assertTrue(should_delete_record(AssetRecord("projects/a/assets/x", "IMAGE"), all_types=False))
        self.assertFalse(should_delete_record(AssetRecord("projects/a/assets/x", "TABLE"), all_types=False))
        self.assertTrue(should_delete_record(AssetRecord("projects/a/assets/x", "TABLE"), all_types=True))

    def test_sort_for_deletion_puts_deeper_children_first(self):
        parent = AssetRecord("projects/a/assets/folder", "IMAGE_COLLECTION")
        child = AssetRecord("projects/a/assets/folder/child", "IMAGE")
        grandchild = AssetRecord("projects/a/assets/folder/child/grandchild", "IMAGE")
        sorted_records = sort_for_safe_deletion([parent, grandchild, child])
        self.assertEqual([record.asset_id for record in sorted_records], [grandchild.asset_id, child.asset_id, parent.asset_id])

    def test_list_child_assets_uses_single_params_dict_and_pages(self):
        calls = []

        class FakeData:
            @staticmethod
            def listAssets(params):
                calls.append(dict(params))
                if "pageToken" not in params:
                    return {
                        "assets": [{"id": "projects/a/assets/folder/image_01", "type": "IMAGE"}],
                        "nextPageToken": "next",
                    }
                return {"assets": [{"id": "projects/a/assets/folder/image_02", "type": "IMAGE"}]}

        class FakeEe:
            data = FakeData()

        records = list_child_assets(FakeEe(), "projects/a/assets/folder", page_size=500)

        self.assertEqual([record.asset_id for record in records], ["projects/a/assets/folder/image_01", "projects/a/assets/folder/image_02"])
        self.assertEqual(calls[0], {"parent": "projects/a/assets/folder", "pageSize": "500"})
        self.assertEqual(calls[1], {"parent": "projects/a/assets/folder", "pageSize": "500", "pageToken": "next"})

    def test_progress_every_argument_defaults_and_parses(self):
        self.assertEqual(parse_args([]).progress_every, 1)
        self.assertEqual(parse_args(["--progress-every", "25"]).progress_every, 25)

    def test_count_only_argument_parses(self):
        self.assertTrue(parse_args(["--count-only"]).count_only)

    def test_verbose_argument_parses(self):
        self.assertTrue(parse_args(["--verbose"]).verbose)

    def test_render_progress_bar(self):
        rendered = render_progress_bar(5, 10, errors=1)
        self.assertIn("5/10", rendered)
        self.assertIn("50.0%", rendered)
        self.assertIn("errors=1", rendered)


if __name__ == "__main__":
    unittest.main()
