"""Annual scheduling preserves real catalogue/manifests and full-range Statistics.

Only NASA transport and raster pixel creation are replaced with tiny fixtures;
selection, plans, tracking, cleanup, accounting and parent refresh stay real.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import yaml

from swotflow.catalog.history import CatalogHistory, history_path
from swotflow.core.metadata import parse_swot_l2_hr_raster_metadata
from swotflow.processing import automation as auto, download, duplicates, extract, mosaic
from swotflow.project.insights import collect_project_insights
from swotflow.project.updates import campaign_rows, expected_rows, update_campaign_id
from tests.test_automation import base_config
from tests.test_download_tool import FakeGranule, FakePagedEarthaccess, granule_name


class YearlyPipelineTests(unittest.TestCase):
    def setup_project(self, root: Path, *, annual: bool):
        base = base_config(root)
        base["download"].update(start_date="2023-07-01", end_date="2024-06-30",
            utm_tiles=["UTM30R", "UTM31S"], catalog_history_enabled=True, skip_manifest_existing=True)
        base["extract"]["year_selection"] = "all"
        settings = auto.AutomationConfig(root, base, ["UTM30R", "UTM31S"],
            "2023-07-01", "2024-06-30", process_by_year=annual, min_free_space_gb=0,
            prevent_system_sleep=False, run_id="pipeline", run_dir=root / "00_logs/automation_runs/pipeline")
        return auto.normalize_automation_config(settings)

    def catalogue(self):
        result = []
        for tile in ("UTM30R", "UTM31S"):
            for start, end in (("20231231T235950", "20240101T000020"),
                               ("20240102T010000", "20240102T010020")):
                for counter in ("01", "02"):
                    name = granule_name(utm=tile, start=start, end=end, counter=counter)
                    granule = FakeGranule(name, concept_id=f"G{len(result)}", size_mb=None)
                    parsed = parse_swot_l2_hr_raster_metadata(name)
                    granule["umm"]["TemporalExtent"]["RangeDateTime"].update(
                        BeginningDateTime=parsed.start_time.replace(" ", "T") + "Z",
                        EndingDateTime=parsed.end_time.replace(" ", "T") + "Z")
                    result.append(granule)
        return result

    def harness(self, catalogue, downloads, retained):
        stack = ExitStack()

        def nasa(settings):
            # CMR overlap deliberately returns the Dec31 file again in January.
            available = [g for g in catalogue
                         if (parsed := parse_swot_l2_hr_raster_metadata(g.name)).start_time[:10] <= settings.end_date
                         and parsed.end_time[:10] >= settings.start_date]
            return FakePagedEarthaccess(available)

        def fetch(settings, **kwargs):
            fake = nasa(settings)
            result = download.run_download(settings, earthaccess_module=fake, **kwargs)
            downloads.append((settings.utm_tiles[0], settings.start_date, settings.end_date, list(fake.downloaded)))
            ledger = CatalogHistory(history_path(settings.manifest_csv))
            retained["snapshots"][result.preview.history_scan_id] = ledger.snapshot(result.preview.history_scan_id)
            if settings.end_date == "2023-12-31":
                rows = download.read_download_manifest(settings.manifest_csv)
                retained["first_year"].update({key: deepcopy(value) for key, value in rows.items()
                    if value.get("utm_tile") == settings.utm_tiles[0] and "20231231T235950" in value.get("file_name", "")})
            return result

        def science(state, tile, stage, config_path, *_args, **_kwargs):
            config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
            if stage == "duplicates":
                duplicates.move_duplicates(duplicates.parse_config(config, state.config.project_root))
            elif stage == "extract":
                settings = extract.parse_config(config, state.config.project_root)
                settings.output_folder.mkdir(parents=True, exist_ok=True)
                rows = []
                for source in extract.build_extraction_plan(settings).selected:
                    output = extract.build_output_path(source.path, settings.output_folder, settings.target_crs_mode)
                    output.write_bytes(b"fixture raster pixels")
                    rows.append({**source.metadata, "input_nc": str(source.path), "output_tif": str(output), "status": "written"})
                extract.write_extract_manifest(settings, rows)
                extract.write_extract_workflow_manifest(settings, rows, [])
            elif stage == "mosaic":
                settings = mosaic.parse_config(config, state.config.project_root)
                settings.output_folder.mkdir(parents=True, exist_ok=True)
                rows = []
                for group in mosaic.build_mosaic_plan(settings).groups:
                    group.output_file.write_bytes(b"fixture mosaic pixels")
                    rows.append(mosaic.report_row_for_group(group, "MOSAIC_CREATED", "Pixel fixture"))
                mosaic.write_mosaic_manifest(settings, rows)
                mosaic.write_mosaic_workflow_manifest(settings, rows)
            else:
                raise AssertionError(f"Unexpected external stage: {stage}")
            return auto.AutomationStageResult(state.run_id, tile, stage, "success", config_path=str(config_path))

        stack.enter_context(mock.patch.object(auto, "build_download_preview",
            side_effect=lambda settings: download.build_download_preview(settings, earthaccess_module=nasa(settings))))
        stack.enter_context(mock.patch.object(auto, "run_download", side_effect=fetch))
        stack.enter_context(mock.patch.object(auto, "execute_command_result", side_effect=science))
        stack.enter_context(mock.patch.object(auto, "windows_automation_reboot_warnings", return_value=[]))
        return stack

    def run_fixture(self, root, *, annual):
        config = self.setup_project(root, annual=annual)
        catalogue, downloads = self.catalogue(), []
        retained = {"snapshots": {}, "first_year": {}}
        with self.harness(catalogue, downloads, retained):
            # This is the real preflight, real scheduler, real download wrapper,
            # real cleanup, real reconciliation and real campaign refresh.
            preflight = auto.preflight_automation(config, preview_builder=auto.build_download_preview)
            self.assertTrue(preflight.preflight_ok, preflight.errors)
            state = auto.run_automation(config, preflight_state=preflight)
        self.assertFalse(state.errors, state.errors)
        self.assertFalse(state.stopped)
        return config, state, catalogue, downloads, retained

    def test_two_tiles_two_years_preserve_history_and_refresh_full_range_statistics(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, state, catalogue, downloads, retained = self.run_fixture(root, annual=True)
            self.assertEqual([(tile, start[:4]) for tile, start, _end, _names in downloads],
                             [(tile, year) for tile in config.utm_tiles for year in ("2023", "2024")])
            self.assertEqual([batch["status"] for batch in state.year_batches], ["complete"] * 4)
            self.assertEqual(len([r for r in state.stage_results if r.stage == "reconciliation" and r.status == "warning"]), 4)
            self.assertEqual(len([r for r in state.stage_results if r.stage == "campaign_refresh" and r.status == "success"]), 4)
            downloaded = {name for _tile, _start, _end, names in downloads for name in names}
            preferred = {g.name for g in catalogue if g.name.endswith("_02.nc")}
            self.assertEqual(downloaded, preferred)
            self.assertEqual(sum(len(names) for _tile, _start, _end, names in downloads), 4)
            rows = download.read_download_manifest(Path(config.base_config["download"]["manifest_csv"]))
            self.assertEqual({r["file_name"] for r in rows.values()}, {g.name for g in catalogue})
            self.assertEqual({r["file_name"] for r in rows.values() if r["downloaded"] == "yes"}, preferred)
            for key, before in retained["first_year"].items():
                self.assertEqual(rows[key], before, "January overlap must not rewrite December download history")
            ledger = CatalogHistory(history_path(Path(config.base_config["download"]["manifest_csv"])))
            for scan_id, snapshot in retained["snapshots"].items():
                self.assertEqual(ledger.snapshot(scan_id), snapshot)
            candidates = {row["filename"] for scan in ledger.scans() for row in ledger.snapshot(scan["id"])["candidates"]}
            self.assertEqual(candidates, {g.name for g in catalogue})
            self.assertTrue(any("excluded_acquisition_window" in selection["reason"]
                for scan in ledger.scans() for selection in ledger.snapshot(scan["id"])["selections"]))
            self.assertEqual(list((root / "01_raw_downloads").glob("*.nc")), [])
            self.assertEqual(list((root / "02_extracted_geotiffs").glob("*.tif")), [])
            self.assertEqual(sum(r.deleted_files for r in state.stage_results if r.stage == "raw_cleanup"), 4)
            self.assertEqual(sum(r.deleted_files for r in state.stage_results if r.stage == "extracted_cleanup"), 4)
            parent_id = update_campaign_id(config.base_config)
            parent_expected = [r for r in expected_rows(config.base_config) if r["campaign_id"] == parent_id]
            self.assertEqual({r["file_name"] for r in parent_expected}, preferred)
            self.assertEqual({r["origin_kind"] for r in parent_expected}, {"catalogue_snapshot"})
            parent = next(row for row in campaign_rows(config.base_config) if row["campaign_id"] == parent_id)
            self.assertEqual((parent["start_date"], parent["end_date"], parent["expected_granules"]),
                             ("2023-07-01", "2024-06-30", "4"))
            statistics = collect_project_insights(config.base_config)
            coverage = statistics.update_coverage_campaign_rows[parent_id]
            self.assertEqual({row[0]: row[1:5] for row in coverage}, {tile: (2, 2, 2, 2) for tile in config.utm_tiles})
            self.assertEqual(statistics.yearly_automation["tiles_complete"], 2)
            self.assertEqual(statistics.yearly_automation["batches_complete"], 4)
            self.assertEqual(statistics.yearly_automation["years_complete"], 2)

    def test_default_off_keeps_one_full_range_pipeline_per_tile(self):
        with TemporaryDirectory() as temporary:
            config, state, _catalogue, downloads, _retained = self.run_fixture(Path(temporary), annual=False)
            self.assertEqual([(tile, start, end) for tile, start, end, _names in downloads],
                [(tile, "2023-07-01", "2024-06-30") for tile in config.utm_tiles])
            self.assertEqual(state.year_batches, [])
            self.assertNotIn("acquisition_start_date", config.base_config["processing"])
            self.assertFalse(any(r.stage in {"reconciliation", "campaign_refresh"} for r in state.stage_results))
            statistics = collect_project_insights(config.base_config)
            self.assertEqual(statistics.yearly_automation, {})
            self.assertEqual(len([r for r in download.read_download_manifest(
                Path(config.base_config["download"]["manifest_csv"])).values() if r["downloaded"] == "yes"]), 4)


if __name__ == "__main__":
    unittest.main()
