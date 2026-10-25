"""Verified downloads must detect missing catalogue inputs and corrupt transfers."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import swotflow.verification.download as integrity
import swotflow.catalog.history as history
import swotflow.processing.download as d
from tests.test_download_tool import FakeGranule, FakePagedEarthaccess, FakeEarthaccess, granule_name


class VerifiedDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = d.DownloadConfig(output_folder=self.root / "raw", start_date="2026-01-01", end_date="2026-01-31",
            utm_tiles=["UTM30R"], manifest_csv=self.root / "00_logs/download_manifest.csv",
            report_csv=self.root / "00_logs/download_preview.csv", verified_downloads=True)

    def granule(self, counter: str = "01", concept: str = "G1", data: bytes = b"downloaded", **kwargs) -> FakeGranule:
        g = FakeGranule(granule_name(counter=counter, **kwargs), concept_id=concept, size_mb=None)
        g["umm"]["DataGranule"] = {"ArchiveAndDistributionInformation": [{"Name": g.name,
            "SizeInBytes": len(data), "Checksum": {"Algorithm": "MD5", "Value": hashlib.md5(data).hexdigest()}}]}
        return g

    def test_both_catalogues_are_queried_but_alias_downloads_once(self) -> None:
        g = self.granule()
        fake = FakePagedEarthaccess([g])
        result = d.run_download(self.config, fake)
        snap = history.CatalogHistory(history.history_path(self.config.manifest_csv)).snapshot(result.preview.history_scan_id)
        self.assertEqual(snap["scope"]["collections"], ["SWOT_L2_HR_Raster_100m_D", "SWOT_L2_HR_Raster_D"])
        self.assertEqual(len(snap["candidates"]), 2)
        self.assertEqual(fake.downloaded, [g.name])
        self.assertTrue(result.all_complete)

    def test_parent_only_source_is_downloaded_and_lower_version_retained(self) -> None:
        old, new = self.granule(), self.granule("02", "G2")
        fake = FakePagedEarthaccess([])
        queries = [FakePagedEarthaccess([old]).granule_query(), FakePagedEarthaccess([new]).granule_query()]
        with mock.patch.object(fake, "granule_query", side_effect=queries):
            result = d.run_download(self.config, fake)
        self.assertEqual(fake.downloaded, [new.name])
        self.assertEqual(len(result.preview.excluded_granules), 1)

    def test_incomplete_and_limited_searches_cannot_start_download(self) -> None:
        fake = FakeEarthaccess([self.granule()])
        with self.assertRaisesRegex(ValueError, "accounting"):
            d.run_download(self.config, fake)
        self.assertEqual(fake.downloaded, [])
        with self.assertRaisesRegex(ValueError, "complete catalogue"):
            d.run_download(replace(self.config, max_granules=1), fake)

    def test_same_size_corruption_fails_and_retry_quarantines_file(self) -> None:
        g = self.granule(data=b"abcdefghij")
        fake = FakePagedEarthaccess([g])  # writes ten bytes, but wrong content
        result = d.run_download(self.config, fake)
        self.assertFalse(result.all_complete)
        self.assertEqual(result.downloaded_files, [])
        result = d.run_download(self.config, fake)
        self.assertFalse(result.all_complete)
        self.assertTrue((self.config.output_folder / (g.name + ".incomplete")).exists())

    def test_manifest_success_does_not_skip_missing_raw(self) -> None:
        g = self.granule()
        fake = FakePagedEarthaccess([g])
        d.run_download(self.config, fake)
        (self.config.output_folder / g.name).unlink()
        result = d.run_download(self.config, fake)
        self.assertEqual(fake.downloaded, [g.name, g.name])
        self.assertEqual(result.skipped_manifest, [])

    def test_receipts_never_skip_rechecking_changed_file(self) -> None:
        g = d.normalize_granule(self.granule())
        path = self.root / g.file_name
        path.write_bytes(b"downloaded")
        logs = self.root / "logs"
        self.assertTrue(integrity.verified_file(path, g, logs))
        self.assertTrue(integrity.verified_file(path, g, logs))
        path.write_bytes(b"wrongbytes")
        self.assertFalse(integrity.verified_file(path, g, logs))

    def test_reused_config_rechecks_raw_file_on_each_run(self) -> None:
        import os
        g = self.granule()
        fake = FakePagedEarthaccess([g])
        d.run_download(self.config, fake)
        path = self.config.output_folder / g.name
        stat = path.stat()
        path.write_bytes(b"wrongbytes")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = d.run_download(self.config, fake)
        self.assertTrue(result.all_complete)
        self.assertEqual(fake.downloaded, [g.name, g.name])
        self.assertEqual(path.read_bytes(), b"downloaded")

    def test_missing_checksum_stops_before_login(self) -> None:
        g = self.granule()
        del g["umm"]["DataGranule"]
        fake = FakePagedEarthaccess([g])
        with self.assertRaisesRegex(ValueError, "verification metadata"):
            d.run_download(self.config, fake)
        self.assertEqual(fake.login_calls, 0)

    def test_conflicting_alias_checksums_stop_before_transfer(self) -> None:
        fake = FakePagedEarthaccess([])
        queries = [FakePagedEarthaccess([self.granule()]).granule_query(),
                   FakePagedEarthaccess([self.granule(concept="G2", data=b"different!")]).granule_query()]
        with mock.patch.object(fake, "granule_query", side_effect=queries):
            with self.assertRaisesRegex(ValueError, "aliases disagree"):
                d.run_download(self.config, fake)
        self.assertEqual(fake.downloaded, [])

    def test_reset_project_blocks_download_before_search(self) -> None:
        self.config.manifest_csv.parent.mkdir(parents=True)
        (self.config.manifest_csv.parent / "collection_reset.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(d, "build_download_preview") as search:
            with self.assertRaisesRegex(ValueError, "marked for reset"):
                d.run_download(self.config)
        search.assert_not_called()

    def test_nested_run_logs_cannot_bypass_project_reset(self) -> None:
        (self.root / "project.yaml").write_text("{}", encoding="utf-8")
        self.config.manifest_csv.parent.mkdir(parents=True)
        (self.config.manifest_csv.parent / "collection_reset.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "marked for reset"):
            integrity.assert_project_not_reset(self.root / "00_logs" / "runs" / "old-run")
