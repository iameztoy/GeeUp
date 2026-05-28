import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from swotflow_automation import (
    AutomationConfig,
    AutomationRunState,
    AutomationStageResult,
    AutomationTilePlan,
    build_tile_config_dict,
    preflight_automation,
    run_automation,
)


def base_config(root: Path) -> dict:
    logs = root / "00_logs"
    return {
        "processing": {
            "root": str(root),
            "raw_downloads": str(root / "01_raw_downloads"),
            "extracted_geotiffs": str(root / "02_extracted_geotiffs"),
            "mosaics": str(root / "03_mosaics"),
            "logs": str(logs),
        },
        "download": {
            "collection_short_name": "SWOT_L2_HR_Raster_100m_D",
            "collection_version_label": "Version D active",
            "product_version_filter": "best",
            "output_folder": str(root / "01_raw_downloads"),
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "utm_tiles": [],
            "report_csv": str(logs / "download_preview.csv"),
            "manifest_csv": str(logs / "download_manifest.csv"),
        },
        "duplicates": {
            "input_folder": str(root / "01_raw_downloads"),
            "log_folder": str(logs),
            "moved_folder_name": "moved",
        },
        "extract": {
            "input_folder": str(root / "01_raw_downloads"),
            "output_folder": str(root / "02_extracted_geotiffs"),
            "manifest_csv": str(logs / "extract_manifest.csv"),
            "errors_csv": str(logs / "extract_errors.csv"),
        },
        "mosaic": {
            "input_folder": str(root / "02_extracted_geotiffs"),
            "output_folder": str(root / "03_mosaics"),
            "report_csv": str(logs / "mosaic_report.csv"),
            "manifest_csv": str(logs / "mosaic_manifest.csv"),
        },
        "gdal": {"python": sys.executable},
        "upload": {"scope": "all", "utm_tiles": []},
        "execution": {"resume": True, "require_confirmation": True},
        "artifacts": {
            "logs_dir": str(logs),
            "report_csv": str(logs / "upload_report.csv"),
            "ee_asset_inventory_csv": str(logs / "ee_asset_inventory.csv"),
        },
        "input_folder": str(root / "03_mosaics"),
        "destination_parent": "projects/example/assets/collection",
    }


def fake_granule(tile: str) -> SimpleNamespace:
    return SimpleNamespace(
        identity=f"g-{tile}",
        file_name=(
            f"SWOT_L2_HR_Raster_100m_{tile}_N_x_x_x_034_266_001A_"
            "20260102T000000_20260102T010000_PGD0_01.nc"
        ),
        size_mb=None,
    )


class AutomationTests(unittest.TestCase):
    def test_tile_config_scopes_stage_filters_without_changing_project_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = base_config(root)
            tile_config = build_tile_config_dict(
                config,
                "UTM34M",
                root / "00_logs" / "automation_runs" / "run",
                start_date="2026-01-01",
                end_date="2026-01-31",
                include_upload=True,
            )

            self.assertEqual(tile_config["download"]["utm_tiles"], ["UTM34M"])
            self.assertEqual(tile_config["duplicates"]["utm_tiles"], ["UTM34M"])
            self.assertEqual(tile_config["extract"]["utm_tiles"], ["UTM34M"])
            self.assertEqual(tile_config["mosaic"]["utm_tiles"], ["UTM34M"])
            self.assertEqual(tile_config["upload"]["scope"], "selected_utm")
            self.assertEqual(tile_config["upload"]["utm_tiles"], ["UTM34M"])
            self.assertEqual(
                tile_config["download"]["manifest_csv"],
                config["download"]["manifest_csv"],
            )
            self.assertEqual(
                tile_config["mosaic"]["manifest_csv"],
                config["mosaic"]["manifest_csv"],
            )
            self.assertIn("automation_runs", tile_config["download"]["report_csv"])

    def test_preflight_uses_manifest_and_project_counts_to_classify_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = base_config(root)
            logs = root / "00_logs"
            logs.mkdir(parents=True)
            (logs / "download_manifest.csv").write_text(
                "\n".join(
                    [
                        "granule_id,file_name,utm_tile,downloaded",
                        f"g-UTM34M,{fake_granule('UTM34M').file_name},UTM34M,yes",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            def preview_builder(download_config):
                granule = fake_granule(download_config.utm_tiles[0])
                return SimpleNamespace(
                    granules=[granule],
                    selected_granules=[granule],
                    excluded_granules=[],
                )

            automation_config = AutomationConfig(
                project_root=root,
                base_config=config,
                utm_tiles=["UTM34M", "UTM35M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
            )
            with mock.patch(
                "swotflow_automation.tile_counts_from_insights",
                return_value={"UTM34M": (10, 10, 4, 4, 0)},
            ):
                state = preflight_automation(automation_config, preview_builder=preview_builder)

            classifications = {plan.tile: plan.classification for plan in state.tile_plans}
            pending = {plan.tile: plan.pending_downloads for plan in state.tile_plans}
            self.assertTrue(state.preflight_ok)
            self.assertEqual(classifications["UTM34M"], "already complete")
            self.assertEqual(classifications["UTM35M"], "new")
            self.assertEqual(pending["UTM34M"], 0)
            self.assertEqual(pending["UTM35M"], 1)

    def test_run_automation_skips_complete_tiles_and_continues_new_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M", "UTM35M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
            )
            run_dir = root / "00_logs" / "automation_runs" / "run"
            state = AutomationRunState(run_id="run", run_dir=run_dir, config=config, preflight_ok=True)
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="already complete", message="done"),
                AutomationTilePlan(tile="UTM35M", classification="new", message="new", pending_downloads=1),
            ]

            def command_result(state_arg, tile, stage, tile_config_path, gdal_python):
                return AutomationStageResult(
                    run_id=state_arg.run_id,
                    tile=tile,
                    stage=stage,
                    status="success",
                    message="ok",
                )

            def cleanup_result(state_arg, tile_config, tile, stage):
                return AutomationStageResult(
                    run_id=state_arg.run_id,
                    tile=tile,
                    stage=stage,
                    status="success",
                    message="cleanup ok",
                )

            with mock.patch("swotflow_automation.execute_command_result", side_effect=command_result):
                with mock.patch("swotflow_automation.execute_cleanup_result", side_effect=cleanup_result):
                    result = run_automation(config, preflight_state=state)

            skipped = [row for row in result.stage_results if row.tile == "UTM34M"]
            executed = [row.stage for row in result.stage_results if row.tile == "UTM35M"]
            self.assertTrue(skipped)
            self.assertTrue(all(row.status == "skipped" for row in skipped))
            self.assertEqual(
                executed,
                ["download", "duplicates", "extract", "raw_cleanup", "mosaic", "extracted_cleanup"],
            )


if __name__ == "__main__":
    unittest.main()
