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
    PROJECT_ROOT,
    build_tile_config_dict,
    cleanup_stage,
    execute_download_result,
    execute_command_result,
    load_latest_automation_run_state,
    preflight_automation,
    run_automation,
    write_run_state,
    write_tile_config,
)
from swot_download_tool import DEFAULT_CMR_REQUEST_TIMEOUT_SECONDS
from project_updates import record_update_expected_rows
from power_management import active_hours_leave_overnight_unprotected, hour_inside_active_hours


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
            config["chrome"] = {"user_data_dir": "./chrome-profile"}
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
                tile_config["chrome"]["user_data_dir"],
                str((PROJECT_ROOT / "chrome-profile").resolve()),
            )
            self.assertEqual(
                tile_config["download"]["manifest_csv"],
                config["download"]["manifest_csv"],
            )
            self.assertEqual(
                tile_config["mosaic"]["manifest_csv"],
                config["mosaic"]["manifest_csv"],
            )
            self.assertEqual(
                tile_config["download"]["search_request_timeout_seconds"],
                DEFAULT_CMR_REQUEST_TIMEOUT_SECONDS,
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
                return_value={"UTM34M": (10, 10, 4, 0, 4, 0)},
            ):
                state = preflight_automation(automation_config, preview_builder=preview_builder)

            classifications = {plan.tile: plan.classification for plan in state.tile_plans}
            pending = {plan.tile: plan.pending_downloads for plan in state.tile_plans}
            estimated_mosaics = {plan.tile: plan.estimated_mosaic_groups for plan in state.tile_plans}
            self.assertTrue(state.preflight_ok)
            self.assertEqual(classifications["UTM34M"], "already complete")
            self.assertEqual(classifications["UTM35M"], "new")
            self.assertEqual(pending["UTM34M"], 0)
            self.assertEqual(pending["UTM35M"], 1)
            self.assertEqual(estimated_mosaics["UTM34M"], 1)
            self.assertEqual(estimated_mosaics["UTM35M"], 1)

    def test_preflight_includes_windows_reboot_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            automation_config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
            )
            preview_builder = mock.Mock(
                return_value=SimpleNamespace(
                    granules=[],
                    selected_granules=[],
                    excluded_granules=[],
                )
            )

            with mock.patch(
                "swotflow_automation.windows_automation_reboot_warnings",
                return_value=["Windows Update reports a pending reboot."],
            ):
                state = preflight_automation(automation_config, preview_builder=preview_builder)

            self.assertTrue(state.preflight_ok)
            self.assertIn("Windows Update reports a pending reboot.", state.warnings)

    def test_preflight_reuses_cached_update_expected_rows_without_cmr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = base_config(root)
            tile = "UTM34M"
            granule = fake_granule(tile)
            record_update_expected_rows(
                config,
                [
                    {
                        "file_name": granule.file_name,
                        "granule_id": granule.identity,
                        "utm_tile": tile,
                        "start_time": "2026-01-02T00:00:00",
                        "end_time": "2026-01-02T01:00:00",
                        "size_mb": "",
                        "selected_for_download": "yes",
                        "status": "MATCHED",
                    }
                ],
                source="test",
                campaign_tiles=[tile],
            )
            automation_config = AutomationConfig(
                project_root=root,
                base_config=config,
                utm_tiles=[tile],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
            )
            preview_builder = mock.Mock(side_effect=AssertionError("CMR should not be queried"))

            with mock.patch(
                "swotflow_automation.windows_automation_reboot_warnings",
                return_value=[],
            ):
                state = preflight_automation(automation_config, preview_builder=preview_builder)

            preview_builder.assert_not_called()
            self.assertTrue(state.preflight_ok)
            self.assertEqual(state.tile_plans[0].matched_granules, 1)
            self.assertEqual(state.tile_plans[0].pending_downloads, 1)
            self.assertIn("cached expected granules", state.tile_plans[0].message)

    def test_retryable_cmr_preflight_failure_warns_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            automation_config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
            )

            def preview_builder(_download_config):
                raise TimeoutError(
                    "CMR request failed or timed out after 60 seconds: "
                    "Read timed out."
                )

            with mock.patch(
                "swotflow_automation.windows_automation_reboot_warnings",
                return_value=[],
            ):
                state = preflight_automation(automation_config, preview_builder=preview_builder)

            self.assertTrue(state.preflight_ok)
            self.assertEqual(state.errors, [])
            self.assertIn("CMR search timed out", state.warnings[0])
            self.assertEqual(state.tile_plans[0].classification, "cmr retry later")

    def test_active_hours_helpers_detect_overnight_risk(self) -> None:
        self.assertTrue(hour_inside_active_hours(10, 7, 20))
        self.assertFalse(hour_inside_active_hours(22, 7, 20))
        self.assertTrue(hour_inside_active_hours(2, 20, 7))
        self.assertTrue(active_hours_leave_overnight_unprotected(7, 20))
        self.assertFalse(active_hours_leave_overnight_unprotected(20, 7))

    def test_upload_enabled_preflight_syncs_ee_before_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = base_config(root)

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
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                include_upload=True,
                min_free_space_gb=0,
            )
            progress_rows = []
            with mock.patch(
                "swotflow_automation.sync_ee_assets_for_preflight",
                return_value=(0, "Earth Engine asset sync completed.", root / "sync.log"),
            ) as sync:
                with mock.patch(
                    "swotflow_automation.tile_counts_from_insights",
                    return_value={},
                ):
                    state = preflight_automation(
                        automation_config,
                        preview_builder=preview_builder,
                        progress_callback=lambda *row: progress_rows.append(row),
                    )

            sync.assert_called_once()
            self.assertTrue(state.preflight_ok)
            self.assertTrue(any(row[1] == "ee_sync" and row[2] == "success" for row in progress_rows))

    def test_upload_enabled_preflight_blocks_when_ee_sync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            automation_config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                include_upload=True,
                min_free_space_gb=0,
            )
            preview_builder = mock.Mock()
            with mock.patch(
                "swotflow_automation.sync_ee_assets_for_preflight",
                return_value=(1, "credentials unavailable", root / "sync.log"),
            ):
                state = preflight_automation(
                    automation_config,
                    preview_builder=preview_builder,
                )

            self.assertFalse(state.preflight_ok)
            self.assertIn("Earth Engine asset sync failed", state.errors[0])
            preview_builder.assert_not_called()

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

            def command_result(state_arg, tile, stage, tile_config_path, gdal_python, **_kwargs):
                return AutomationStageResult(
                    run_id=state_arg.run_id,
                    tile=tile,
                    stage=stage,
                    status="success",
                    message="ok",
                )

            def download_result(state_arg, tile, tile_config_path, **_kwargs):
                return AutomationStageResult(
                    run_id=state_arg.run_id,
                    tile=tile,
                    stage="download",
                    status="success",
                    message="download ok",
                )

            def cleanup_result(state_arg, tile_config, tile, stage):
                return AutomationStageResult(
                    run_id=state_arg.run_id,
                    tile=tile,
                    stage=stage,
                    status="success",
                    message="cleanup ok",
                )

            with mock.patch("swotflow_automation.execute_download_result", side_effect=download_result):
                with mock.patch("swotflow_automation.execute_command_result", side_effect=command_result):
                    with mock.patch("swotflow_automation.execute_cleanup_result", side_effect=cleanup_result):
                        result = run_automation(config, preflight_state=state)

            complete_rows = [row for row in result.stage_results if row.tile == "UTM34M"]
            skipped = [row for row in complete_rows if row.status == "skipped"]
            cleaned = [row.stage for row in complete_rows if row.status == "success"]
            executed = [row.stage for row in result.stage_results if row.tile == "UTM35M"]
            self.assertTrue(skipped)
            self.assertEqual(cleaned, [])
            self.assertEqual(
                executed,
                ["download", "duplicates", "extract", "raw_cleanup", "mosaic", "extracted_cleanup"],
            )

    def test_run_automation_does_not_rerun_cleanup_for_complete_uploaded_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                include_upload=True,
                min_free_space_gb=0,
            )
            run_dir = root / "00_logs" / "automation_runs" / "run"
            state = AutomationRunState(run_id="run", run_dir=run_dir, config=config, preflight_ok=True)
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="already complete", message="done"),
            ]

            def cleanup_result(state_arg, tile_config, tile, stage):
                return AutomationStageResult(
                    run_id=state_arg.run_id,
                    tile=tile,
                    stage=stage,
                    status="success",
                    message="cleanup ok",
                    deleted_files=1,
                )

            with mock.patch("swotflow_automation.execute_download_result") as download:
                with mock.patch("swotflow_automation.execute_command_result") as command:
                    with mock.patch("swotflow_automation.execute_cleanup_result", side_effect=cleanup_result) as cleanup:
                        result = run_automation(config, preflight_state=state)

            download.assert_not_called()
            command.assert_not_called()
            cleanup.assert_not_called()
            cleaned = [
                row.stage
                for row in result.stage_results
                if row.tile == "UTM34M" and row.status == "success"
            ]
            skipped = [
                row.stage
                for row in result.stage_results
                if row.tile == "UTM34M" and row.status == "skipped"
            ]
            self.assertEqual(cleaned, [])
            self.assertIn("mosaic_cleanup", skipped)

    def test_retryable_cmr_download_failure_continues_even_when_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M", "UTM35M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
                continue_on_tile_failure=False,
            )
            run_dir = root / "00_logs" / "automation_runs" / "run"
            state = AutomationRunState(run_id="run", run_dir=run_dir, config=config, preflight_ok=True)
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="needs update", message="new", pending_downloads=1),
                AutomationTilePlan(tile="UTM35M", classification="needs update", message="new", pending_downloads=1),
            ]

            def download_result(state_arg, tile, tile_config_path, **_kwargs):
                status = "failed" if tile == "UTM34M" else "success"
                message = (
                    "Download failed: CMR search failed for selected UTM tile(s). "
                    "CMR request failed or timed out after 60 seconds."
                    if tile == "UTM34M"
                    else "download ok"
                )
                return AutomationStageResult(
                    run_id=state_arg.run_id,
                    tile=tile,
                    stage="download",
                    status=status,
                    message=message,
                )

            def command_result(state_arg, tile, stage, tile_config_path, gdal_python, **_kwargs):
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

            with mock.patch("swotflow_automation.execute_download_result", side_effect=download_result):
                with mock.patch("swotflow_automation.execute_command_result", side_effect=command_result):
                    with mock.patch("swotflow_automation.execute_cleanup_result", side_effect=cleanup_result):
                        result = run_automation(config, preflight_state=state)

            self.assertIn(
                ("UTM34M", "download", "failed"),
                [(row.tile, row.stage, row.status) for row in result.stage_results],
            )
            self.assertIn(
                ("UTM35M", "download", "success"),
                [(row.tile, row.stage, row.status) for row in result.stage_results],
            )

    def test_run_automation_uses_keep_awake_guard_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
                prevent_system_sleep=True,
            )
            run_dir = root / "00_logs" / "automation_runs" / "run"
            state = AutomationRunState(run_id="run", run_dir=run_dir, config=config, preflight_ok=True)
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="already complete", message="done"),
            ]

            with mock.patch("swotflow_automation.keep_system_awake") as keep_awake:
                keep_awake.return_value.__enter__.return_value = None
                keep_awake.return_value.__exit__.return_value = None
                run_automation(config, preflight_state=state)

            keep_awake.assert_called_once_with(enabled=True, keep_display_awake=False)

    def test_retryable_cmr_download_failure_is_retried_after_main_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M", "UTM35M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
                deferred_download_retry_passes=1,
            )
            run_dir = root / "00_logs" / "automation_runs" / "run"
            state = AutomationRunState(run_id="run", run_dir=run_dir, config=config, preflight_ok=True)
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="needs update", message="new", pending_downloads=1),
                AutomationTilePlan(tile="UTM35M", classification="needs update", message="new", pending_downloads=1),
            ]
            attempts: dict[str, int] = {}

            def download_result(state_arg, tile, tile_config_path, **_kwargs):
                attempts[tile] = attempts.get(tile, 0) + 1
                failed_first_try = tile == "UTM34M" and attempts[tile] == 1
                return AutomationStageResult(
                    run_id=state_arg.run_id,
                    tile=tile,
                    stage="download",
                    status="failed" if failed_first_try else "success",
                    message=(
                        "Download failed: CMR search failed for selected UTM tile(s). "
                        "CMR request failed or timed out after 60 seconds."
                        if failed_first_try
                        else "download ok"
                    ),
                )

            def command_result(state_arg, tile, stage, tile_config_path, gdal_python, **_kwargs):
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

            with mock.patch("swotflow_automation.execute_download_result", side_effect=download_result):
                with mock.patch("swotflow_automation.execute_command_result", side_effect=command_result):
                    with mock.patch("swotflow_automation.execute_cleanup_result", side_effect=cleanup_result):
                        result = run_automation(config, preflight_state=state)

            tile_stage_statuses = [
                (row.tile, row.stage, row.status)
                for row in result.stage_results
                if row.stage == "download"
            ]
            self.assertEqual(attempts["UTM34M"], 2)
            self.assertEqual(attempts["UTM35M"], 1)
            self.assertEqual(
                tile_stage_statuses,
                [
                    ("UTM34M", "download", "failed"),
                    ("UTM35M", "download", "success"),
                    ("UTM34M", "download", "success"),
                ],
            )

    def test_mosaic_cleanup_sweep_removes_verified_candidates_from_previous_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_tile_mosaic = root / "03_mosaics" / "SWOT_L2_HR_Raster_100m_UTM29Q_MOSA.tif"
            current_tile_raw = root / "01_raw_downloads" / "SWOT_L2_HR_Raster_100m_UTM30P.nc"
            old_tile_mosaic.parent.mkdir(parents=True)
            current_tile_raw.parent.mkdir(parents=True)
            old_tile_mosaic.write_bytes(b"mosaic")
            current_tile_raw.write_bytes(b"raw")

            candidates = [
                SimpleNamespace(stage="mosaic", path=old_tile_mosaic, size_bytes=6),
                SimpleNamespace(stage="raw", path=current_tile_raw, size_bytes=3),
            ]

            def fake_delete(selected):
                selected = list(selected)
                return len(selected), sum(item.size_bytes for item in selected), []

            with mock.patch("swotflow_automation.plan_cleanup_candidates", return_value=candidates):
                with mock.patch("swotflow_automation.delete_cleanup_candidates", side_effect=fake_delete):
                    deleted, bytes_deleted, errors = cleanup_stage({}, "UTM30P", "mosaic")

            self.assertEqual(deleted, 1)
            self.assertEqual(bytes_deleted, 6)
            self.assertEqual(errors, [])

    def test_execute_download_result_runs_in_process_and_forwards_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = base_config(root)
            run_dir = root / "00_logs" / "automation_runs" / "run"
            tile_config = build_tile_config_dict(
                config,
                "UTM34M",
                run_dir,
                start_date="2026-01-01",
                end_date="2026-01-31",
                include_upload=False,
            )
            tile_config_path = write_tile_config(tile_config, run_dir / "UTM34M" / "automation_config.yaml")
            state = AutomationRunState(run_id="run", run_dir=run_dir, config=AutomationConfig(
                project_root=root,
                base_config=config,
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
            ))
            granule = fake_granule("UTM34M")
            fake_result = SimpleNamespace(
                preview=SimpleNamespace(
                    granules=[granule],
                    selected_granules=[granule],
                    excluded_granules=[],
                ),
                downloaded_files=[root / "01_raw_downloads" / granule.file_name],
                skipped_existing=[],
                skipped_manifest=[],
                failures=[],
                missing_granules=[],
                stopped=False,
                report_csv=run_dir / "UTM34M" / "download_preview.csv",
                complete_count=1,
            )
            progress_rows = []

            def fake_run_download(download_config, *, progress_callback=None, stop_event=None):
                self.assertEqual(download_config.utm_tiles, ["UTM34M"])
                self.assertIsNone(stop_event)
                self.assertIsNotNone(progress_callback)
                progress_callback(1, 1, "downloaded test granule")
                return fake_result

            with mock.patch("swotflow_automation.run_download", side_effect=fake_run_download):
                result = execute_download_result(
                    state,
                    "UTM34M",
                    tile_config_path,
                    progress_callback=lambda tile, stage, status, message: progress_rows.append(
                        (tile, stage, status, message)
                    ),
                )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.return_code, 0)
            self.assertIn("Download complete", result.message)
            self.assertEqual(progress_rows[-1], ("UTM34M", "download", "running", "downloaded test granule"))
            self.assertIn("GEEUP_PROGRESS\tdownload\t1\t1", Path(result.log_path).read_text(encoding="utf-8"))

    def test_latest_automation_run_state_round_trips_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                include_upload=True,
                run_id="run",
                run_dir=root / "00_logs" / "automation_runs" / "run",
            )
            state = AutomationRunState(run_id="run", run_dir=config.run_dir, config=config, preflight_ok=True)
            state.tile_plans = [
                AutomationTilePlan(
                    tile="UTM34M",
                    classification="new",
                    message="pending",
                    pending_downloads=1,
                    estimated_mosaic_groups=2,
                    recorded_mosaic_groups=1,
                    pending_mosaic_groups=1,
                )
            ]
            state.stage_results = [
                AutomationStageResult(
                    run_id="run",
                    tile="UTM34M",
                    stage="download",
                    status="success",
                    message="ok",
                )
            ]
            write_run_state(state)

            loaded = load_latest_automation_run_state(config.base_config, root)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.run_id, "run")
            self.assertEqual(loaded.config.utm_tiles, ["UTM34M"])
            self.assertTrue(loaded.config.include_upload)
            self.assertEqual(loaded.tile_plans[0].pending_downloads, 1)
            self.assertEqual(loaded.tile_plans[0].estimated_mosaic_groups, 2)
            self.assertEqual(loaded.tile_plans[0].recorded_mosaic_groups, 1)
            self.assertEqual(loaded.tile_plans[0].pending_mosaic_groups, 1)
            self.assertEqual(loaded.stage_results[0].stage, "download")

    def test_run_automation_resume_skips_completed_stage_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
            )
            run_dir = root / "00_logs" / "automation_runs" / "run"
            state = AutomationRunState(run_id="run", run_dir=run_dir, config=config, preflight_ok=True)
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="new", message="new", pending_downloads=1),
            ]
            state.stage_results = [
                AutomationStageResult(
                    run_id="run",
                    tile="UTM34M",
                    stage="download",
                    status="success",
                    message="already done",
                )
            ]

            def command_result(state_arg, tile, stage, tile_config_path, gdal_python, **_kwargs):
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

            with mock.patch("swotflow_automation.execute_download_result") as download:
                with mock.patch("swotflow_automation.execute_command_result", side_effect=command_result):
                    with mock.patch("swotflow_automation.execute_cleanup_result", side_effect=cleanup_result):
                        result = run_automation(config, preflight_state=state)

            download.assert_not_called()
            download_rows = [
                row
                for row in result.stage_results
                if row.tile == "UTM34M" and row.stage == "download"
            ]
            self.assertEqual(len(download_rows), 1)
            self.assertEqual(download_rows[0].status, "success")

    def test_upload_exit_code_two_is_warning_so_automation_can_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                include_upload=True,
            )
            state = AutomationRunState(
                run_id="run",
                run_dir=root / "00_logs" / "automation_runs" / "run",
                config=config,
            )
            config_path = write_tile_config(
                build_tile_config_dict(
                    config.base_config,
                    "UTM34M",
                    state.run_dir,
                    start_date=config.start_date,
                    end_date=config.end_date,
                    include_upload=True,
                ),
                state.run_dir / "UTM34M" / "automation_config.yaml",
            )
            with mock.patch(
                "swotflow_automation.run_subprocess_stage",
                return_value=(2, "Run finished with upload errors."),
            ):
                result = execute_command_result(
                    state,
                    "UTM34M",
                    "upload",
                    config_path,
                    Path(sys.executable),
                )

            self.assertEqual(result.status, "warning")
            self.assertIn("preserve unverified mosaics", result.message)

    def test_run_automation_resume_clears_stopped_flag_without_duplicate_skip_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AutomationConfig(
                project_root=root,
                base_config=base_config(root),
                utm_tiles=["UTM34M"],
                start_date="2026-01-01",
                end_date="2026-01-31",
                min_free_space_gb=0,
            )
            run_dir = root / "00_logs" / "automation_runs" / "run"
            state = AutomationRunState(
                run_id="run",
                run_dir=run_dir,
                config=config,
                preflight_ok=True,
                stopped=True,
            )
            state.tile_plans = [
                AutomationTilePlan(tile="UTM34M", classification="new", message="new", pending_downloads=1),
            ]
            state.stage_results = [
                AutomationStageResult(
                    run_id="run",
                    tile="UTM34M",
                    stage="download",
                    status="success",
                    message="already done",
                )
            ]

            def command_result(state_arg, tile, stage, tile_config_path, gdal_python, **_kwargs):
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

            with mock.patch("swotflow_automation.execute_download_result") as download:
                with mock.patch("swotflow_automation.execute_command_result", side_effect=command_result):
                    with mock.patch("swotflow_automation.execute_cleanup_result", side_effect=cleanup_result):
                        result = run_automation(config, preflight_state=state)

            download.assert_not_called()
            self.assertFalse(result.stopped)
            download_rows = [
                row
                for row in result.stage_results
                if row.tile == "UTM34M" and row.stage == "download"
            ]
            self.assertEqual(len(download_rows), 1)


if __name__ == "__main__":
    unittest.main()
