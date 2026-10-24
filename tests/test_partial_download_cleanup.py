import json
import os
import sqlite3
import struct
import tempfile
import time
import unittest
from datetime import datetime
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from partial_download_cleanup import partial_product_name, plan_partial_downloads
from project_database import upsert_project_rows
from project_insights import (
    COMPLETED_EXTRACT_STATUSES, CleanupCandidate, delete_cleanup_candidates, plan_cleanup_candidates,
)
from project_operation_lock import download_folder_lock
from swot_download_tool import DownloadConfig, run_download


def attribute(name: str, value: str | int) -> bytes:
    encoded = name.encode("ascii") + b"\0"
    if isinstance(value, int):
        dtype = b"\x10\x08\x00\x00" + struct.pack("<IHH", 2, 0, 16)
        space = b"\x02\x01\x01\x01" + struct.pack("<QQ", 1, 1)
        data = struct.pack("<h", value)
    else:
        data = value.encode("ascii")
        dtype = b"\x13\x00\x00\x00" + struct.pack("<I", len(data))
        space = b"\x02\x00\x00\x00"
    return struct.pack("<BBHHHB", 3, 0, len(encoded), len(dtype), len(space), 0) + encoded + dtype + space + data


def header(**overrides: str | int) -> bytes:
    values = dict(short_name="L2_HR_Raster", descriptor_string="100m_UTM31T_N_x_x_x",
                  cycle_number=56, pass_number=391, scene_number=119, crid="PID0",
                  product_version="01", time_granule_start="2026-09-24T17:56:52.888368Z",
                  time_granule_end="2026-09-24T17:57:13.989590Z")
    values.update(overrides)
    return b"\x89HDF\r\n\x1a\n\x02\x08\x08\x00" + b"".join(attribute(k, v) for k, v in values.items())


NAME = "SWOT_L2_HR_Raster_100m_UTM31T_N_x_x_x_056_391_119F_20260924T175652_20260924T175713_PID0_01.nc"


class PartialCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "01_raw_downloads"
        self.raw.mkdir()
        self.logs = self.root / "00_logs"
        self.logs.mkdir()
        self.download_manifest = self.logs / "download_manifest.csv"
        self.extract_manifest = self.logs / "extract_manifest.csv"
        self.partial = self.raw / "partial_123abcde"
        self.partial.write_bytes(header())
        self.old_time = time.time() - 7200
        os.utime(self.partial, (self.old_time, self.old_time))
        stamp = datetime.fromtimestamp(self.old_time + 1200).isoformat()
        self.download = dict(granule_id="G1", file_name=NAME, utm_tile="UTM31T",
                             downloaded="yes", last_downloaded=stamp, last_status="DOWNLOADED")
        self.extract = dict(record_id=NAME + "|original", input_nc=str(self.raw / NAME),
                            output_tif=str(self.root / "02_extracted_geotiffs" / (NAME[:-3] + ".tif")),
                            status="written", updated_at=stamp, utm_zone="31", mgrs_band="T")
        self.write_proof()
        self.config = {"processing": {"root": str(self.root), "raw_downloads": str(self.raw),
                                      "mosaics": str(self.root / "03_mosaics")},
                       "download": {"output_folder": str(self.raw), "manifest_csv": str(self.download_manifest)},
                       "extract": {"manifest_csv": str(self.extract_manifest)}}

    def write_proof(self) -> None:
        upsert_project_rows(self.download_manifest, [self.download], replace_dataset=True)
        upsert_project_rows(self.extract_manifest, [self.extract], replace_dataset=True)

    def preview(self) -> list[CleanupCandidate]:
        return [c for c in plan_cleanup_candidates(self.config) if c.stage == "partial"]

    def test_full_identity_including_version_and_no_fuzzy_matching(self) -> None:
        self.assertEqual(partial_product_name(header()), NAME)
        self.assertEqual(partial_product_name(header(product_version="102")), NAME.replace("PID0_01", "PID0_102"))
        for data in (b"bad", header()[:100], header() + attribute("crid", "PID0"),
                     header(short_name="L2_HR_PIXC"), header(descriptor_string="100m_UTM61T_N_x_x_x")):
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                partial_product_name(data)

    def test_cleanup_after_raw_and_extract_removed_preserves_history_and_audits(self) -> None:
        candidates = self.preview()
        self.assertEqual(len(candidates), 1)
        self.assertIn(NAME, candidates[0].reason)
        deleted, size, errors = delete_cleanup_candidates(candidates)
        self.assertEqual((deleted, size, errors), (1, len(header()), []))
        self.assertFalse(self.partial.exists())
        events = [json.loads(line) for line in (self.logs / "partial_download_cleanup.jsonl").read_text().splitlines()]
        self.assertEqual([e['status'] for e in events], ['delete_requested', 'deleted'])
        self.assertEqual(events[-1]['product_name'], NAME)
        with closing(sqlite3.connect(self.root / 'swotflow.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE dataset='download_manifest'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE dataset='extract_manifest'").fetchone()[0], 1)

    def test_original_products_still_present_are_not_needed_for_partial_cleanup(self) -> None:
        original = self.raw / NAME
        original.write_bytes(b"original")
        self.assertEqual(delete_cleanup_candidates(self.preview())[0], 1)
        self.assertEqual(original.read_bytes(), b"original")

    def test_missing_failed_or_older_completion_proof_is_retained(self) -> None:
        cases = [('downloaded', 'no'), ('last_downloaded', ''),
                 ('last_downloaded', datetime.fromtimestamp(self.old_time - 1).isoformat())]
        for key, value in cases:
            original = self.download[key]
            self.download[key] = value
            self.write_proof()
            self.assertEqual(self.preview(), [])
            self.download[key] = original
        for key, value in [('status', 'ERROR'), ('updated_at', ''),
                           ('updated_at', datetime.fromtimestamp(self.old_time - 1).isoformat())]:
            original = self.extract[key]
            self.extract[key] = value
            self.write_proof()
            self.assertEqual(self.preview(), [])
            self.extract[key] = original
        self.assertTrue(self.partial.exists())

    def test_wrong_product_version_and_unknown_headers_retained(self) -> None:
        for data in (header(product_version="02"), header(crid="PGD0"), header(scene_number=118),
                     header(pass_number=390), b"corrupt"):
            self.partial.write_bytes(data)
            os.utime(self.partial, (self.old_time, self.old_time))
            self.assertEqual(self.preview(), [])

    def test_recent_partial_is_not_eligible(self) -> None:
        os.utime(self.partial, None)
        self.assertEqual(self.preview(), [])

    def test_changed_file_after_preview_is_not_deleted(self) -> None:
        candidates = self.preview()
        self.partial.write_bytes(header(product_version="02"))
        os.utime(self.partial, (self.old_time, self.old_time))
        deleted, _, errors = delete_cleanup_candidates(candidates)
        self.assertEqual(deleted, 0)
        self.assertTrue(errors)
        self.assertTrue(self.partial.exists())

    def test_proof_is_reloaded_before_delete(self) -> None:
        candidates = self.preview()
        self.extract['status'] = 'ERROR'
        self.write_proof()
        self.assertEqual(delete_cleanup_candidates(candidates)[0], 0)
        self.assertTrue(self.partial.exists())

    def test_active_download_lock_blocks_preview_and_stale_preview_deletion(self) -> None:
        candidates = self.preview()
        with download_folder_lock(self.raw):
            self.assertEqual(self.preview(), [])
            deleted, _, errors = delete_cleanup_candidates(candidates)
            self.assertEqual(deleted, 0)
            self.assertTrue(errors)
            with self.assertRaisesRegex(RuntimeError, 'download'):
                run_download(DownloadConfig(output_folder=self.raw), earthaccess_module=mock.Mock())
        self.assertTrue(self.partial.exists())
        self.assertEqual(len(self.preview()), 1)

    def test_download_holds_lock_and_releases_it_on_failure(self) -> None:
        def interrupted(*args):
            with self.assertRaises(RuntimeError):
                with download_folder_lock(self.raw):
                    pass
            raise ValueError('simulated interruption')
        with mock.patch('swot_download_tool._run_download', side_effect=interrupted):
            with self.assertRaises(ValueError):
                run_download(DownloadConfig(output_folder=self.raw))
        with download_folder_lock(self.raw):
            pass

    def test_db_unavailable_never_falls_back_to_csv_for_deletion(self) -> None:
        candidates = self.preview()
        with mock.patch('partial_download_cleanup._completion_times', side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(self.preview(), [])
            deleted, _, errors = delete_cleanup_candidates(candidates)
        self.assertEqual(deleted, 0)
        self.assertTrue(errors)
        self.assertTrue(self.partial.exists())

    def test_missing_preview_proof_cannot_use_generic_deletion(self) -> None:
        forged = CleanupCandidate('temporary', self.partial, '', '')
        self.assertEqual(delete_cleanup_candidates([forged])[0], 0)
        self.assertTrue(self.partial.exists())

    def test_hardlinks_are_retained(self) -> None:
        try:
            os.link(self.partial, self.raw / 'partial_second00')
        except OSError as exc:
            self.skipTest(f'Hard links unavailable: {exc}')
        self.assertEqual(self.preview(), [])

    def test_nested_partials_are_retained(self) -> None:
        nested = self.raw / 'other'
        nested.mkdir()
        self.partial.rename(nested / self.partial.name)
        self.assertEqual(self.preview(), [])

    def test_automation_does_not_plan_partial_deletion(self) -> None:
        candidates = plan_cleanup_candidates(self.config, include_partial_downloads=False)
        self.assertFalse(any(c.stage == 'partial' for c in candidates))

    def test_gui_protects_partials_during_automation(self) -> None:
        from swotflow_gui import LauncherApp
        app = SimpleNamespace(cleanup_delete_running=False, automation_running=True,
                              automation_preflight_running=False, download_stop_event=None)
        with mock.patch('swotflow_gui.messagebox.showinfo') as info:
            with mock.patch('swotflow_gui.messagebox.askyesno') as confirm:
                LauncherApp.delete_cleanup_files(app, self.preview(), 'selected files')
        info.assert_called_once()
        confirm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
