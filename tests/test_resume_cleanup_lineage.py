"""Power-loss resume, safe raster companion cleanup and mosaic version metadata."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from swotflow.processing import automation as auto, extract, mosaic
from swotflow.core.metadata import parse_swot_l2_hr_raster_metadata, processing_version_summary, product_identity_key
from swotflow.core.sidecars import geotiff_sidecars
from swotflow.project.insights import CleanupCandidate, delete_cleanup_candidates
from swotflow.verification import pipeline as proof
from swotflow.verification.resume import verified_resume_stages
import tests.test_pipeline_verification as fixtures
from tests.test_automation import base_config


class ResumeCleanupTests(TestCase):
    setUp = fixtures.ProofTests.setUp

    def config_and_selection(self):
        """Bind tiny real receipts to normal Automation settings; no NASA transport."""
        config = base_config(self.root)
        out = self.root / "mosaics"
        out.mkdir()
        self.mosaic = self.mosaic.replace(out / self.mosaic.name)
        for key in ("raw_downloads", "extracted_geotiffs", "mosaics"):
            config["processing"][key] = str(self.root)
        for section in ("extract", "mosaic"):
            config[section].update(input_folder=str(self.root), output_folder=str(self.root))
        config["processing"]["mosaics"] = str(out)
        config["mosaic"]["output_folder"] = str(out)
        config["download"].update(verified_downloads=True, utm_tiles=["UTM30R"])
        config["mosaic"]["selection_scan_id"] = "frozen-scan"
        selected = ("frozen-scan", self.mosaic_proof["sources"])
        self.enterContext(mock.patch.object(proof, "catalogue_selection", return_value=selected))
        ex = extract.parse_config(config, self.root)
        mo = mosaic.parse_config(config, self.root)
        self.store.record_artifact("extract", self.extract, [self.raw], ex, "extract", {})
        self.store.record_artifact("mosaic", self.mosaic, [self.extract], mo, "mosaic", {})
        return config

    def test_resume_after_raw_cleanup_reuses_verified_extractions(self):
        config = self.config_and_selection()
        self.raw.unlink()
        completed = {"download", "duplicates", "extract", "raw_cleanup"}
        stages, scan = verified_resume_stages(config, deepcopy(config), completed, self.root)
        self.assertEqual(stages, completed)
        self.assertEqual(scan, "frozen-scan")
        self.extract.write_bytes(b"changed")
        self.assertEqual(verified_resume_stages(config, config, completed, self.root), (set(), ""))

    def test_resume_after_extracted_cleanup_requires_completed_current_mosaic(self):
        config = self.config_and_selection()
        self.raw.unlink()
        self.extract.unlink()
        completed = {"download", "duplicates", "extract", "raw_cleanup", "mosaic", "extracted_cleanup"}
        self.assertEqual(verified_resume_stages(config, config, completed, self.root)[0], completed)
        self.assertEqual(verified_resume_stages(config, config, completed - {"mosaic"}, self.root)[0], set())
        self.mosaic.write_bytes(b"bad mosaic")
        self.assertEqual(verified_resume_stages(config, config, completed, self.root)[0], set())

    def test_changed_settings_source_version_scope_or_missing_selection_cannot_resume(self):
        config = self.config_and_selection()
        completed = {"download", "duplicates", "extract"}
        changed = deepcopy(config)
        changed["extract"]["resampling_alg"] = "bilinear"
        self.assertEqual(verified_resume_stages(changed, config, completed, self.root)[0], set())
        changed = deepcopy(config)
        changed["download"]["end_date"] = "2026-02-28"
        self.assertEqual(verified_resume_stages(changed, config, completed, self.root)[0], set())
        saved = deepcopy(config)
        saved["mosaic"].pop("selection_scan_id")
        self.assertEqual(verified_resume_stages(config, saved, completed, self.root)[0], set())
        with mock.patch.object(proof, "catalogue_selection", return_value=("new", {self.name: {"hash": "changed"}})):
            self.assertEqual(verified_resume_stages(config, config, completed, self.root)[0], set())

    def test_automation_resume_runs_mosaic_without_downloading_again_in_both_modes(self):
        base = self.config_and_selection()
        for annual_child in (False, True):
            with self.subTest(annual_child=annual_child):
                base.setdefault("automation", {})["yearly_batch"] = annual_child
                cfg = auto.AutomationConfig(self.root, base, ["UTM30R"], "2026-01-01", "2026-01-31",
                    min_free_space_gb=0, cleanup_enabled=False, prevent_system_sleep=False)
                run_dir = self.store.logs / ("annual" if annual_child else "normal")
                state = auto.AutomationRunState("resume", run_dir, cfg, preflight_ok=True)
                state.tile_plans = [auto.AutomationTilePlan("UTM30R", "partial/resumable", "interrupted")]
                state.stage_results = [auto.AutomationStageResult("resume", "UTM30R", s, "success")
                                       for s in ("download", "duplicates", "extract", "raw_cleanup")]
                path = auto.tile_run_dir(state, "UTM30R") / "automation_config.yaml"
                tile_config = auto.build_tile_config_dict(base, "UTM30R", run_dir,
                    start_date=cfg.start_date, end_date=cfg.end_date, include_upload=False)
                tile_config["mosaic"]["selection_scan_id"] = "frozen-scan"
                auto.write_tile_config(tile_config, path)
                auto.write_run_state(state)
                state = auto.load_automation_run_state(state.json_path)
                self.raw.unlink(missing_ok=True)
                def command(state, tile, stage, *_args, **_kwargs):
                    self.assertEqual(stage, "mosaic")
                    return auto.AutomationStageResult(state.run_id, tile, stage, "success")
                with mock.patch.object(auto, "execute_download_result") as download, \
                     mock.patch.object(auto, "execute_command_result", side_effect=command) as processing:
                    auto.run_automation(cfg, preflight_state=state)
                download.assert_not_called()
                processing.assert_called_once()

    def candidates_with_sidecars(self):
        for companion in geotiff_sidecars(self.extract):
            companion.write_text("sidecar fixture", encoding="utf-8")
        self.store.record_artifact("extract", self.extract, [self.raw], self.config, "ex", {})
        self.store.record_artifact("mosaic", self.mosaic, [self.extract], self.config, "mo", {})
        candidates = [CleanupCandidate("extracted", p, "completed", "manifest")
                      for p in [self.extract, *geotiff_sidecars(self.extract)]]
        config = {"download": {"manifest_csv": str(self.store.logs / "download_manifest.csv")}}
        return config, candidates

    def test_verified_cleanup_removes_companions_and_preserves_other_files_and_history(self):
        config, candidates = self.candidates_with_sidecars()
        unrelated = self.root / "unrelated.tif.aux.xml"
        unrelated.write_text("keep", encoding="utf-8")
        allowed = proof.cleanup_guards(config, candidates)
        self.assertEqual(len(allowed), len(candidates))
        count, _, errors = delete_cleanup_candidates(allowed)
        self.assertEqual((count, errors), (len(candidates), []))
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.mosaic.exists())
        self.assertTrue(self.store.artifact(self.extract))

    def test_orphan_companions_with_known_downstream_are_cleaned(self):
        config, candidates = self.candidates_with_sidecars()
        self.extract.unlink()
        allowed = proof.cleanup_guards(config, candidates[1:])
        self.assertEqual(delete_cleanup_candidates(allowed)[0], len(candidates) - 1)

    def test_changed_parent_or_downstream_prevents_companion_deletion(self):
        config, candidates = self.candidates_with_sidecars()
        allowed = proof.cleanup_guards(config, candidates)
        self.mosaic.write_bytes(b"changed")
        self.assertEqual(delete_cleanup_candidates(allowed)[0], 0)
        self.assertTrue(all(c.path.exists() for c in candidates))

    def test_sidecar_alone_cannot_modify_a_live_parent(self):
        config, candidates = self.candidates_with_sidecars()
        allowed = proof.cleanup_guards(config, candidates[1:])
        self.assertEqual(delete_cleanup_candidates(allowed)[0], 0)
        self.assertTrue(self.extract.exists())

    def test_yearly_cleanup_scopes_sidecars_by_their_recorded_parent(self):
        config, candidates = self.candidates_with_sidecars()
        allowed = proof.cleanup_guards(config, candidates)
        config["download"].update(acquisition_start_date="2026-01-01", acquisition_end_date="2026-12-31",
                                  utm_tiles=["UTM30R"])
        config["automation"] = {"yearly_batch": True}
        with mock.patch.object(auto, "plan_cleanup_candidates", return_value=allowed):
            count, _, errors = auto.cleanup_stage(config, "UTM30R", "extracted")
        self.assertEqual((count, errors), (len(candidates), []))

    def test_atomic_checkpoint_preserves_previous_run_on_replace_failure(self):
        cfg = auto.AutomationConfig(self.root, base_config(self.root), ["UTM30R"], "2026-01-01", "2026-01-31")
        state = auto.AutomationRunState("run", self.root / "run", cfg)
        auto.write_run_state(state)
        before = state.json_path.read_bytes()
        state.stopped = True
        with mock.patch.object(auto.os, "replace", side_effect=OSError("simulated power loss")):
            with self.assertRaises(OSError):
                auto.write_run_state(state)
        self.assertEqual(state.json_path.read_bytes(), before)
        self.assertFalse(list(state.run_dir.glob("*.tmp")))


class VersionRangeTests(TestCase):
    def test_new_names_repeat_homogeneous_version_and_preserve_legacy_outputs(self):
        from tests.test_mosaic_tool import swot_name
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / swot_name(counter="02")
            source.touch()
            config = mosaic.MosaicConfig(root, root / "out", manifest_csv=root / "mosaic_manifest.csv")
            group = mosaic.build_mosaic_plan(config).groups[0]
            self.assertTrue(group.output_file.name.endswith("_PVPID0-02_to_PID0-02.tif"))
            legacy = group.output_file.with_name(group.output_file.name.split("_PV")[0] + ".tif")
            legacy.parent.mkdir()
            legacy.touch()
            self.assertEqual(mosaic.build_mosaic_plan(config).groups[0].output_file, legacy)
            group.output_file = legacy
            row = mosaic.report_row_for_group(group, "COPIED_SINGLETON", "fixture")
            mosaic.write_mosaic_manifest(config, [row])
            legacy.unlink()  # Uploaded/cleaned legacy files keep the recorded name too.
            self.assertEqual(mosaic.build_mosaic_plan(config).groups[0].output_file, legacy)
            stored = mosaic.read_mosaic_manifest(config.manifest_csv)[legacy.name]
            self.assertEqual(stored["processing_min"], "PID0_02")
            self.assertEqual(json.loads(stored["processing_levels"]), ["PID0_02"])
            stale = dict(row, status="STALE_EXISTS", processing_min="PGD0_99",
                         processing_levels='["PGD0_99"]', input_files='["proposed.tif"]')
            mosaic.write_mosaic_manifest(config, [stale])
            stored = mosaic.read_mosaic_manifest(config.manifest_csv)[legacy.name]
            self.assertEqual(stored["input_files"], row["input_files"])
            self.assertEqual(stored["processing_min"], "PID0_02")

    def test_upload_metadata_uses_all_manifest_versions_for_legacy_and_new_names(self):
        from tests.test_mosaic_tool import swot_name
        from tests.test_uploader_regressions import UploaderDialogRegressionTests
        from swotflow.earthengine.uploader import UploadItem
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = UploaderDialogRegressionTests()
            settings = fixture.uploader_config(root)
            sources = [root / swot_name(crid=crid, scene=f"00{i}A", counter=counter)
                       for i, (crid, counter) in enumerate((("PIC0", "01"), ("PID0", "02"), ("PGD0", "01")))]
            for source in sources:
                source.touch()
            cfg = mosaic.MosaicConfig(root, root / "out", manifest_csv=settings.mosaic_manifest_csv)
            group = mosaic.build_mosaic_plan(cfg).groups[0]
            for legacy in (False, True):
                if legacy:
                    group.output_file = group.output_file.with_name(group.output_file.name.split("_PV")[0] + ".tif")
                mosaic.write_mosaic_manifest(cfg, [mosaic.report_row_for_group(group, "MOSAIC_CREATED", "fixture")])
                uploader = fixture.uploader(settings)
                item = UploadItem(local_file=group.output_file, asset_name=group.output_file.stem,
                                  asset_id="projects/example/assets/test")
                parsed = uploader.parse_metadata_for_item(item)
                self.assertEqual(parsed.properties["swot_processing_min"], "PIC0_01")
                self.assertEqual(parsed.properties["swot_processing_max"], "PGD0_01")
                self.assertEqual(json.loads(parsed.properties["swot_processing_levels"]), ["PIC0_01", "PID0_02", "PGD0_01"])

    def test_actual_rank_and_counters_not_alphabetic_order(self):
        fields = [{"crid": "PID0", "product_counter": "02"},
                  {"crid": "PGD0", "product_counter": "01"},
                  {"crid": "PID0", "product_counter": "01"}]
        summary = processing_version_summary(fields)
        self.assertEqual(summary["processing_min"], "PID0_01")
        self.assertEqual(summary["processing_max"], "PGD0_01")
        self.assertEqual(json.loads(summary["processing_levels"]), ["PID0_01", "PID0_02", "PGD0_01"])

    def test_range_does_not_change_dates_identity_or_legacy_parsing(self):
        legacy = "SWOT_L2_HR_Raster_100m_UTM30R_N_x_x_x_001_126_MOSA_20240721T010000_20240721T010100_MIXD_01.tif"
        new = legacy[:-4] + "_PVPIC0-01_to_PGD0-02.tif"
        parsed = parse_swot_l2_hr_raster_metadata(new)
        old = parse_swot_l2_hr_raster_metadata(legacy)
        self.assertEqual((parsed.start_time, parsed.end_time), (old.start_time, old.end_time))
        self.assertEqual(parsed.properties["swot_processing_min"], "PIC0_01")
        self.assertEqual(parsed.properties["swot_processing_max"], "PGD0_02")
        self.assertEqual(product_identity_key(new), product_identity_key(legacy))
