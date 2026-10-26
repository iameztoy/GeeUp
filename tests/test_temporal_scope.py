"""Acquisition-start batches preserve ownership, existing versions and manifests."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from swotflow.catalog.history import CatalogHistory, history_path, search_with_history
from swotflow.core.temporal import acquisition_scope, acquisition_window, validate_acquisition_window, within_acquisition_window
from swotflow.earthengine.uploader import EarthEngineUIUploader, parse_config as parse_upload
from swotflow.processing import download, duplicates, extract, mosaic
from swotflow.project.updates import campaign_fields, preview_rows, update_campaign_id
from swotflow.verification.pipeline import catalogue_selection
from tests.test_download_tool import FakeGranule, FakePagedEarthaccess, granule_name


class TemporalScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start, self.end = "2024-01-01", "2024-06-30"
        self.scope = {"acquisition_start_date": self.start, "acquisition_end_date": self.end}
        self.previous = granule_name(start="20231231T235959", end="20240101T000020")
        self.current = granule_name(start="20240101T000020", end="20240101T000041")
        self.later = granule_name(start="20240701T000000", end="20240701T000020")

    def config(self, **kwargs) -> download.DownloadConfig:
        return download.DownloadConfig(output_folder=self.root / "raw", start_date=self.start,
            end_date=self.end, utm_tiles=["UTM30R"], manifest_csv=self.root / "00_logs/download_manifest.csv",
            report_csv=self.root / "00_logs/download_report.csv", **kwargs)

    def test_midnight_ownership_and_partial_year_bounds(self) -> None:
        self.assertTrue(within_acquisition_window(self.previous, "2023-01-01", "2023-12-31"))
        self.assertFalse(within_acquisition_window(self.previous, self.start, self.end))
        self.assertTrue(within_acquisition_window(self.current, self.start, self.end))
        self.assertFalse(within_acquisition_window(self.later, self.start, self.end))
        self.assertFalse(within_acquisition_window("unknown.tif", self.start, self.end))
        self.assertTrue(within_acquisition_window("unknown.tif"))
        for bounds in ((self.start, ""), ("2024-2-01", self.end), (self.end, self.start)):
            with self.assertRaises(ValueError):
                validate_acquisition_window(*bounds)
        with self.assertRaisesRegex(ValueError, "disagree"):
            acquisition_window({"processing": self.scope, "download": {**self.scope, "acquisition_end_date": "2024-12-31"}})

    def test_catalogue_retains_boundary_candidates_and_selection_scope(self) -> None:
        raw = [FakeGranule(self.previous, concept_id="G1"), FakeGranule(self.current, concept_id="G2"),
               FakeGranule(self.current.replace("_01.nc", "_02.nc"), concept_id="G3")]
        legacy_config = self.config(catalog_history_enabled=True)
        legacy = search_with_history(legacy_config, FakePagedEarthaccess(raw))
        config = replace(legacy_config, **self.scope)
        scoped = search_with_history(config, FakePagedEarthaccess(raw))
        self.assertEqual(len(legacy.selected_granules), 2)
        self.assertEqual([g.file_name for g in scoped.selected_granules], [raw[-1].name])
        boundary = next(g for g in scoped.excluded_granules if g.file_name == self.previous)
        self.assertEqual(boundary.duplicate_filter_status, "excluded_acquisition_window")
        old_version = next(g for g in scoped.excluded_granules if g.file_name == self.current)
        self.assertEqual(old_version.duplicate_filter_status, "excluded_older_version")
        snapshot = CatalogHistory(history_path(config.manifest_csv)).snapshot(scoped.history_scan_id)
        self.assertEqual(len(snapshot["candidates"]), 3)
        self.assertEqual(snapshot["scope"]["acquisition_start_date"], self.start)
        self.assertEqual(sum(s["selected"] for s in snapshot["selections"]), 1)
        query = {"start_date": self.start, "end_date": self.end, "utm_tiles": ["UTM30R"]}
        self.assertEqual(catalogue_selection(config.manifest_csv.parent, query)[0], legacy.history_scan_id)
        self.assertEqual(catalogue_selection(config.manifest_csv.parent, {**query, **self.scope})[0], scoped.history_scan_id)
        with self.assertRaisesRegex(ValueError, "exact mosaic"):
            catalogue_selection(config.manifest_csv.parent, {**query, **self.scope}, legacy.history_scan_id)
        row = next(r for r in preview_rows(scoped) if r["file_name"] == self.previous)
        self.assertEqual(row["status"], "EXCLUDED_ACQUISITION_WINDOW")

    def test_download_does_not_download_or_overwrite_history_for_adjacent_year(self) -> None:
        config = self.config()
        earlier = download.normalize_granule(FakeGranule(self.previous, concept_id="G1", size_mb=None))
        old_preview = download.DownloadPreview(download.build_download_query(config), [earlier])
        download.write_download_manifest(config, old_preview, {earlier.identity: ("DOWNLOADED", "old-path")})
        original = download.read_download_manifest(config.manifest_csv)[earlier.identity].copy()
        fake = FakePagedEarthaccess([FakeGranule(self.previous, concept_id="G1", size_mb=None),
                                    FakeGranule(self.current, concept_id="G2", size_mb=None)])
        complete_path = download.local_complete_path

        def current_batch_complete_path(settings, granule):
            self.assertNotEqual(granule.file_name, self.previous,
                                "A boundary record must not trigger verification outside the active batch")
            return complete_path(settings, granule)

        with patch.object(download, "local_complete_path", side_effect=current_batch_complete_path):
            result = download.run_download(replace(config, **self.scope), earthaccess_module=fake)
        self.assertTrue(result.all_complete)
        self.assertEqual(fake.downloaded, [self.current])
        self.assertEqual(download.read_download_manifest(config.manifest_csv)[earlier.identity], original)
        self.assertIn("EXCLUDED_ACQUISITION_WINDOW", config.report_csv.read_text(encoding="utf-8-sig"))

    def test_local_stage_plans_scope_mixed_cached_years_but_defaults_do_not(self) -> None:
        raw, tifs = self.root / "raw", self.root / "tifs"
        raw.mkdir()
        tifs.mkdir()
        for name in (self.previous, self.current, self.later):
            (raw / name).write_bytes(b"fixture")
            (tifs / Path(name).with_suffix(".tif").name).write_bytes(b"fixture")
        extraction = extract.ExtractConfig(raw, self.root / "extracted", **self.scope)
        self.assertEqual([s.path.name for s in extract.build_extraction_plan(extraction).selected], [self.current])
        self.assertEqual(len(extract.build_extraction_plan(replace(extraction, acquisition_start_date="", acquisition_end_date="")).selected), 3)
        duplicate_config = duplicates.DuplicateConfig(raw, **self.scope)
        self.assertEqual([s.path.name for s in duplicates.collect_files(duplicate_config)[0]], [self.current])
        mosaic_config = mosaic.MosaicConfig(tifs, self.root / "mosaics", **self.scope)
        self.assertEqual([p.name for p in mosaic.collect_input_files(mosaic_config)], [Path(self.current).with_suffix(".tif").name])
        uploader = EarthEngineUIUploader.__new__(EarthEngineUIUploader)
        uploader.config = SimpleNamespace(input_folder=tifs, upload=SimpleNamespace(extensions=[".tif"], recursive=False), **self.scope)
        self.assertEqual([p.name for p in uploader.collect_input_files()], [Path(self.current).with_suffix(".tif").name])

    def test_all_stage_parsers_use_the_same_processing_scope(self) -> None:
        mapping = {"processing": self.scope, "input_folder": str(self.root),
            "download": {"start_date": self.start, "end_date": self.end, "utm_tiles": ["UTM30R"]},
            "destination_parent": "projects/example/assets/test", "mosaic": {"input_folder": str(self.root)}}
        configs = [download.parse_config(mapping, self.root), extract.parse_config(mapping, self.root),
                   duplicates.parse_config(mapping, self.root), parse_upload(mapping, self.root)]
        path = self.root / "config.yaml"
        path.write_text(yaml.safe_dump(mapping), encoding="utf-8")
        configs.append(mosaic.load_config_file(path))
        for config in configs:
            self.assertEqual((config.acquisition_start_date, config.acquisition_end_date), (self.start, self.end))

    def test_campaign_identity_distinguishes_ownership_without_changing_legacy(self) -> None:
        config = self.config()
        expected = hashlib.sha256(f"{config.collection_short_name}|best|{self.start}|{self.end}".encode()).hexdigest()[:16]
        self.assertEqual(update_campaign_id(config), expected)
        owned = replace(config, **self.scope)
        self.assertNotEqual(update_campaign_id(owned), expected)
        self.assertEqual(acquisition_scope(campaign_fields(owned)), self.scope)
        mapped = {"processing": self.scope, "download": campaign_fields(config)}
        self.assertEqual(update_campaign_id(mapped), update_campaign_id(owned))


if __name__ == "__main__":
    unittest.main()
