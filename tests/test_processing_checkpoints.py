"""Completed local work survives interruption before final stage CSV export."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swotflow.processing import extract as e, mosaic as m
from swotflow.verification import pipeline as p
from tests import test_pipeline_verification as fixtures
from tests.test_extract_tool import nc_name
from tests.test_mosaic_tool import swot_name


class VerifiedReceiptRecoveryTests(unittest.TestCase):
    setUp = fixtures.ProofTests.setUp

    def test_extract_current_receipt_recovers_without_manifest_or_rewrite(self):
        config = e.ExtractConfig(self.root, self.root, verified_workflow=True,
                                manifest_csv=self.store.logs / "extract_manifest.csv")
        source = e.ExtractSource(self.raw, e.parse_filename(self.raw))
        with mock.patch.object(e, "validate_output_geotiff", return_value={"xsize": 1, "ysize": 1, "band_count": 2}), \
             mock.patch.object(e, "remove_geotiff_and_sidecars") as remove, \
             mock.patch.object(e, "export_geotiff_from_vrt") as export:
            row = e.process_one_file(source, config, object(), {})
        self.assertEqual(row["status"], "skipped_existing")
        remove.assert_not_called()
        export.assert_not_called()
        self.assertEqual(self.extract.read_bytes(), b"extracted")

    def test_extract_changed_output_cannot_recover_from_receipt(self):
        config = e.ExtractConfig(self.root, self.root, verified_workflow=True,
                                manifest_csv=self.store.logs / "extract_manifest.csv")
        source = e.ExtractSource(self.raw, e.parse_filename(self.raw))
        self.extract.write_bytes(b"damaged")
        with mock.patch.object(e, "build_two_band_vrt", side_effect=RuntimeError("rebuild required")):
            with self.assertRaisesRegex(RuntimeError, "rebuild required"):
                e.process_one_file(source, config, object(), {})
        self.assertFalse(self.extract.exists())

    def test_mosaic_current_receipt_recovers_missing_or_old_manifest(self):
        config = m.MosaicConfig(self.root, self.root / "out", verified_workflow=True,
                                require_all_inputs=True, manifest_csv=self.store.logs / "mosaic_manifest.csv")
        group = m.build_mosaic_plan(config).groups[0]
        group.output_file.parent.mkdir(parents=True, exist_ok=True)
        group.output_file.write_bytes(b"completed mosaic")
        self.store.record_artifact("mosaic", group.output_file, [s.path for s in group.sources], config, "interrupted", {})
        for history in ({}, {group.output_file.name: {"status": "MOSAIC_CREATED", "source_signature": "old"}}):
            with self.subTest(history=bool(history)):
                row, _ = m.preflight_mosaic_group(group, config, history, False)
                self.assertEqual(row["status"], "SKIPPED_EXISTS")
        config.grouping_mode = m.GROUPING_MODE_UTM_ZONE_HEMISPHERE
        row, _ = m.preflight_mosaic_group(group, config, {}, False)
        self.assertEqual(row["status"], "STALE_EXISTS")


class InterruptedStageCheckpointTests(unittest.TestCase):
    def test_extract_commits_completed_file_before_next_progress_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for scene in ("000A", "001A"):
                (root / nc_name(scene=scene)).write_bytes(b"source")
            config = e.ExtractConfig(root, root / "out", manifest_csv=root / "logs/extract_manifest.csv",
                                     errors_csv=root / "logs/extract_errors.csv")
            calls = []
            def process(source, cfg, driver, history):
                calls.append(source.path.name)
                output = e.build_output_path(source.path, cfg.output_folder, cfg.target_crs_mode)
                output.write_bytes(b"finished")
                return {"record_id": e.extract_record_id(source, cfg), "status": "written",
                        "input_nc": str(source.path), "output_tif": str(output), **source.metadata}
            def interrupt(current, total, message):
                if current == 1:
                    raise KeyboardInterrupt("power-loss boundary")
            with mock.patch.object(e, "require_gdal", return_value=object()), \
                 mock.patch.object(e, "process_one_file", side_effect=process):
                with self.assertRaises(KeyboardInterrupt):
                    e.run_extract(config, check_gdal=False, progress_callback=interrupt)
            saved = e.read_extract_manifest(config.manifest_csv)
            self.assertEqual(len(saved), 1)
            self.assertEqual(len(calls), 1)
            self.assertTrue(Path(next(iter(saved.values()))["output_tif"]).is_file())
            self.assertFalse(config.manifest_csv.exists())  # SQLite owns the committed checkpoint.
            with mock.patch.object(e, "require_gdal", return_value=object()), \
                 mock.patch.object(e, "validate_output_geotiff", return_value={"xsize": 1, "ysize": 1, "band_count": 2}), \
                 mock.patch.object(e, "_process_one_file", wraps=e._process_one_file) as original, \
                 mock.patch.object(e, "build_two_band_vrt", side_effect=RuntimeError("second output only")) as build:
                code, _, rows, errors = e.run_extract(config, check_gdal=False, progress_callback=lambda *args: None)
            self.assertEqual(code, 2)
            self.assertEqual([r["status"] for r in rows], ["skipped_existing"])
            self.assertEqual(len(errors), 1)
            self.assertEqual(build.call_count, 1)

    def test_mosaic_commits_completed_group_before_next_progress_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for day in ("07", "08"):
                (root / swot_name(start=f"202507{day}T010000", end=f"202507{day}T010100")).write_bytes(b"source")
            config = m.MosaicConfig(root, root / "out", manifest_csv=root / "logs/mosaic_manifest.csv",
                                   report_csv=root / "logs/mosaic_report.csv")
            def execute(group, cfg, signature):
                group.output_file.parent.mkdir(exist_ok=True)
                group.output_file.write_bytes(b"finished")
                return m.add_mosaic_tracking_fields(m.report_row_for_group(group, "COPIED_SINGLETON", "fixture"), group, signature)
            def interrupt(current, total, message):
                if current == 1:
                    raise KeyboardInterrupt("power-loss boundary")
            with mock.patch.object(m, "execute_mosaic_group", side_effect=execute):
                with self.assertRaises(KeyboardInterrupt):
                    m.run_mosaic(config, progress_callback=interrupt)
            saved = m.read_mosaic_manifest(config.manifest_csv)
            self.assertEqual(len(saved), 1)
            self.assertFalse(config.manifest_csv.exists())
            with mock.patch.object(m, "execute_mosaic_group", side_effect=execute) as build:
                code, rows = m.run_mosaic(config)
            self.assertEqual(code, 0)
            self.assertEqual(build.call_count, 1)
            self.assertEqual({r["status"] for r in rows}, {"SKIPPED_EXISTS", "COPIED_SINGLETON"})
            self.assertEqual(len(m.read_mosaic_manifest(config.manifest_csv)), 2)


class RasterReplacementSidecarTests(unittest.TestCase):
    def test_rewrite_extract_removes_all_companions_only(self):
        from swotflow.core.sidecars import geotiff_sidecars
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raster = root / "input.tif"
            unrelated = root / "input_other.tif.aux"
            unrelated.write_bytes(b"keep")
            for path in [raster, *geotiff_sidecars(raster)]:
                path.write_bytes(b"old")
            e.remove_geotiff_and_sidecars(raster)
            self.assertFalse(any(p.exists() for p in [raster, *geotiff_sidecars(raster)]))
            self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_mosaic_replacement_discards_old_masks_and_disabled_world_file(self):
        from swotflow.core.sidecars import geotiff_sidecars
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            final = root / "mosaic.tif"
            for path in [final, *geotiff_sidecars(final)]:
                path.write_bytes(b"stale")
            pending = m.temporary_mosaic_path(final)
            pending.write_bytes(b"new pixels")
            config = m.MosaicConfig(root, root, write_world_file=False)
            m.promote_temporary_output(pending, final, config)
            self.assertEqual(final.read_bytes(), b"new pixels")
            self.assertFalse(any(p.exists() for p in geotiff_sidecars(final)))

    def test_failed_replace_preserves_previous_raster_and_companions(self):
        from swotflow.core.sidecars import geotiff_sidecars
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            final = root / "mosaic.tif"
            for path in [final, *geotiff_sidecars(final)]:
                path.write_bytes(b"previous")
            pending = m.temporary_mosaic_path(final)
            pending.write_bytes(b"replacement")
            with mock.patch.object(Path, "replace", side_effect=PermissionError("locked TIFF")):
                with self.assertRaises(PermissionError):
                    m.promote_temporary_output(pending, final, m.MosaicConfig(root, root))
            for path in [final, *geotiff_sidecars(final)]:
                self.assertEqual(path.read_bytes(), b"previous")
            self.assertEqual(pending.read_bytes(), b"replacement")

    def test_failed_sidecar_removal_never_certifies_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / swot_name()).write_bytes(b"source")
            config = m.MosaicConfig(root, root / "out", verified_workflow=True,
                                   manifest_csv=root / "logs/mosaic_manifest.csv")
            group = m.build_mosaic_plan(config).groups[0]
            group.output_file.parent.mkdir()
            sidecar = Path(str(group.output_file) + ".aux.xml")
            sidecar.write_bytes(b"old metadata")
            unlink = Path.unlink
            def fail_old_companion(path, *args, **kwargs):
                if path == sidecar:
                    raise PermissionError("locked old companion")
                return unlink(path, *args, **kwargs)
            def create(group, config):
                pending = m.temporary_mosaic_path(group.output_file)
                pending.write_bytes(b"new pixels")
                m.promote_temporary_output(pending, group.output_file, config)
            with mock.patch.object(Path, "unlink", fail_old_companion), \
                 mock.patch.object(m, "select_compatible_sources", return_value=(group.sources, [])), \
                 mock.patch.object(m, "copy_singleton", side_effect=create), \
                 mock.patch.object(p.VerificationStore, "record_artifact") as receipt:
                row = m.execute_mosaic_group(group, config, m.group_source_signature(group))
            self.assertEqual(row["status"], "ERROR")
            receipt.assert_not_called()

    def test_partial_mosaic_cleanup_removes_every_exact_companion(self):
        from swotflow.core.sidecars import geotiff_sidecars
        with tempfile.TemporaryDirectory() as temp:
            final = Path(temp) / "mosaic.tif"
            final.write_bytes(b"previous good pixels")
            pending = m.temporary_mosaic_path(final)
            for path in [pending, *geotiff_sidecars(pending)]:
                path.write_bytes(b"partial")
            m.cleanup_temporary_output(final)
            self.assertEqual(final.read_bytes(), b"previous good pixels")
            self.assertFalse(any(p.exists() for p in [pending, *geotiff_sidecars(pending)]))


if __name__ == "__main__":
    unittest.main()
