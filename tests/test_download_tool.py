import tempfile
import threading
import unittest
import csv
from pathlib import Path

from swot_download_tool import (
    DownloadConfig,
    EXCLUDED_OLDER_VERSION_STATUS,
    PRODUCT_FILTER_ALL,
    build_download_preview,
    build_granule_patterns,
    dedupe_granules,
    existing_download_path,
    file_appears_complete,
    generate_utm_tiles,
    load_config_file,
    manifest_downloaded_tiles,
    move_incomplete_download,
    normalize_utm_tiles,
    run_download,
    write_download_report,
)


def granule_name(
    *,
    utm: str = "UTM30R",
    scene: str = "001F",
    start: str = "20260117T052508",
    end: str = "20260117T052529",
    crid: str = "PID0",
    counter: str = "01",
) -> str:
    return (
        f"SWOT_L2_HR_Raster_100m_{utm}_N_x_x_x_044_387_{scene}_"
        f"{start}_{end}_{crid}_{counter}.nc"
    )


class FakeGranule(dict):
    def __init__(self, name: str, *, concept_id: str, size_mb: float | None = 10.0) -> None:
        super().__init__(
            {
                "meta": {"concept-id": concept_id},
                "umm": {
                    "GranuleUR": name.removesuffix(".nc"),
                    "TemporalExtent": {
                        "RangeDateTime": {
                            "BeginningDateTime": "2026-01-17T05:25:08Z",
                            "EndingDateTime": "2026-01-17T05:25:29Z",
                        }
                    },
                },
            }
        )
        self.name = name
        self.size_mb = size_mb

    def data_links(self, access: str = "external") -> list[str]:
        return [f"https://archive.podaac.earthdata.nasa.gov/example/{self.name}"]

    def size(self) -> float | None:
        return self.size_mb


class FakeEarthaccess:
    def __init__(self, results: list[FakeGranule]) -> None:
        self.results = results
        self.downloaded: list[str] = []
        self.download_batches: list[list[str]] = []
        self.login_calls = 0

    def search_data(self, **kwargs):
        patterns = kwargs["granule_name"]
        if isinstance(patterns, str):
            patterns = [patterns]
        matches = []
        for granule in self.results:
            for pattern in patterns:
                prefix = pattern.removesuffix("*")
                if granule.name.startswith(prefix):
                    matches.append(granule)
                    break
        count = kwargs.get("count", -1)
        return matches if count == -1 else matches[:count]

    def login(self, **kwargs):
        self.login_calls += 1
        return object()

    def download(self, granules, local_path: str, threads: int = 4, show_progress: bool = False):
        paths = []
        root = Path(local_path)
        root.mkdir(parents=True, exist_ok=True)
        self.download_batches.append([granule.name for granule in granules])
        for granule in granules:
            path = root / granule.name
            path.write_text("downloaded", encoding="utf-8")
            self.downloaded.append(granule.name)
            paths.append(str(path))
        return paths


class DownloadToolTests(unittest.TestCase):
    def test_utm_generation_and_wildcard_patterns(self) -> None:
        tiles = generate_utm_tiles()

        self.assertIn("UTM01C", tiles)
        self.assertIn("UTM30R", tiles)
        self.assertIn("UTM60X", tiles)
        self.assertNotIn("UTM30I", tiles)
        self.assertEqual(normalize_utm_tiles("utm30r, UTM29R UTM30R"), ["UTM30R", "UTM29R"])
        self.assertEqual(
            build_granule_patterns(["UTM30R", "UTM29R"]),
            [
                "SWOT_L2_HR_Raster_100m_UTM30R*",
                "SWOT_L2_HR_Raster_100m_UTM29R*",
            ],
        )

    def test_load_config_uses_processing_raw_downloads_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "processing:",
                        "  raw_downloads: raw",
                        "  logs: logs",
                        "download:",
                        "  start_date: '2026-01-01'",
                        "  end_date: '2026-01-31'",
                        "  utm_tiles: ['UTM30R']",
                        "  batch_size: 50",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config_file(config_path)

            self.assertEqual(config.output_folder, root / "raw")
            self.assertEqual(config.report_csv, root / "logs" / "download_preview.csv")
            self.assertEqual(config.manifest_csv, root / "logs" / "download_manifest.csv")
            self.assertEqual(config.collection_short_name, "SWOT_L2_HR_Raster_100m_D")
            self.assertEqual(config.batch_size, 50)

    def test_preview_deduplicates_and_aggregates_known_sizes(self) -> None:
        duplicate = FakeGranule(granule_name(utm="UTM30R"), concept_id="G1", size_mb=12.5)
        earthaccess = FakeEarthaccess(
            [
                duplicate,
                duplicate,
                FakeGranule(granule_name(utm="UTM29R"), concept_id="G2", size_mb=None),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            config = DownloadConfig(
                output_folder=Path(temp),
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R", "UTM29R"],
                report_csv=Path(temp) / "report.csv",
            )

            preview = build_download_preview(config, earthaccess_module=earthaccess)

            self.assertEqual(len(preview.granules), 2)
            self.assertEqual(preview.total_known_size_mb, 12.5)
            self.assertEqual(preview.missing_size_count, 1)
            self.assertEqual(sorted(g.utm_tile for g in preview.granules), ["UTM29R", "UTM30R"])

    def test_preview_filters_to_best_product_version_but_reports_excluded_rows(self) -> None:
        old_name = granule_name(utm="UTM30R", crid="PID0", counter="01")
        best_name = granule_name(utm="UTM30R", crid="PGD0", counter="02")
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(old_name, concept_id="OLD", size_mb=0.5),
                FakeGranule(best_name, concept_id="BEST", size_mb=0.6),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = DownloadConfig(
                output_folder=root,
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R"],
                report_csv=root / "report.csv",
                manifest_csv=root / "manifest.csv",
            )

            preview = build_download_preview(config, earthaccess_module=earthaccess)
            write_download_report(config, preview)
            result = run_download(config, earthaccess_module=earthaccess)

            with (root / "report.csv").open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            excluded = [row for row in rows if row["status"] == EXCLUDED_OLDER_VERSION_STATUS]

            self.assertEqual(len(preview.granules), 2)
            self.assertEqual([granule.file_name for granule in preview.selected_granules], [best_name])
            self.assertEqual([granule.file_name for granule in preview.excluded_granules], [old_name])
            self.assertEqual(preview.selected_known_size_mb, 0.6)
            self.assertEqual(preview.excluded_known_size_mb, 0.5)
            self.assertEqual(earthaccess.downloaded, [best_name])
            self.assertTrue(result.all_complete)
            self.assertEqual(len(result.missing_granules), 0)
            self.assertEqual(len(excluded), 1)
            self.assertEqual(excluded[0]["selected_for_download"], "no")
            self.assertEqual(excluded[0]["preferred_file_name"], best_name)
            self.assertIn(EXCLUDED_OLDER_VERSION_STATUS, (root / "manifest.csv").read_text(encoding="utf-8"))

    def test_all_product_version_mode_downloads_every_match(self) -> None:
        old_name = granule_name(utm="UTM30R", crid="PID0", counter="01")
        best_name = granule_name(utm="UTM30R", crid="PGD0", counter="02")
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(old_name, concept_id="OLD", size_mb=None),
                FakeGranule(best_name, concept_id="BEST", size_mb=None),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = DownloadConfig(
                output_folder=root,
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R"],
                report_csv=root / "report.csv",
                product_version_filter=PRODUCT_FILTER_ALL,
            )

            preview = build_download_preview(config, earthaccess_module=earthaccess)
            result = run_download(config, earthaccess_module=earthaccess)

            self.assertEqual(len(preview.selected_granules), 2)
            self.assertEqual(preview.excluded_granules, [])
            self.assertEqual(earthaccess.downloaded, [best_name, old_name])
            self.assertTrue(result.all_complete)

    def test_existing_file_skip_logic_and_run_download(self) -> None:
        existing_name = granule_name(utm="UTM30R")
        new_name = granule_name(utm="UTM31R")
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(existing_name, concept_id="G1", size_mb=None),
                FakeGranule(new_name, concept_id="G2"),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / existing_name).write_text("already here", encoding="utf-8")
            config = DownloadConfig(
                output_folder=root,
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R", "UTM31R"],
                report_csv=root / "report.csv",
                skip_existing=True,
            )

            preview = build_download_preview(config, earthaccess_module=earthaccess)
            existing = existing_download_path(
                config,
                next(granule for granule in preview.granules if granule.file_name == existing_name),
            )
            result = run_download(config, earthaccess_module=earthaccess)

            self.assertEqual(existing, root / existing_name)
            self.assertEqual([path.name for path in result.skipped_existing], [existing_name])
            self.assertEqual([path.name for path in result.downloaded_files], [new_name])
            self.assertEqual(earthaccess.downloaded, [new_name])
            self.assertEqual(earthaccess.download_batches, [[new_name]])
            self.assertTrue((root / "report.csv").exists())

    def test_run_download_batches_multiple_granules(self) -> None:
        names = [
            granule_name(utm="UTM30R", scene="001F"),
            granule_name(utm="UTM31R", scene="002F"),
            granule_name(utm="UTM32R", scene="003F"),
        ]
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(name, concept_id=f"G{index}", size_mb=None)
                for index, name in enumerate(names)
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = DownloadConfig(
                output_folder=root,
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R", "UTM31R", "UTM32R"],
                report_csv=root / "report.csv",
                batch_size=2,
            )

            result = run_download(config, earthaccess_module=earthaccess)

            self.assertTrue(result.all_complete)
            self.assertEqual(
                earthaccess.download_batches,
                [names[:2], names[2:]],
            )
            self.assertEqual(len(result.downloaded_files), 3)

    def test_manifest_skip_avoids_redownload_after_raw_file_deleted(self) -> None:
        name = granule_name(utm="UTM30R")
        earthaccess = FakeEarthaccess([FakeGranule(name, concept_id="G1", size_mb=None)])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = DownloadConfig(
                output_folder=root,
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R"],
                report_csv=root / "report.csv",
                manifest_csv=root / "manifest.csv",
            )
            first = run_download(config, earthaccess_module=earthaccess)
            (root / name).unlink()
            preview = build_download_preview(config, earthaccess_module=earthaccess)
            write_download_report(config, preview)
            preview_report_text = (root / "report.csv").read_text(encoding="utf-8")
            self.assertIn("SKIPPED_MANIFEST", preview_report_text)
            self.assertIn(",yes,no,yes,", preview_report_text)

            second = run_download(config, earthaccess_module=earthaccess)
            report_text = (root / "report.csv").read_text(encoding="utf-8")

            self.assertTrue(first.all_complete)
            self.assertTrue(second.all_complete)
            self.assertEqual(len(second.skipped_manifest), 1)
            self.assertEqual(earthaccess.downloaded, [name])
            self.assertIn("SKIPPED_MANIFEST", report_text)
            self.assertIn(",yes,no,yes,", report_text)
            self.assertEqual(manifest_downloaded_tiles(root / "manifest.csv"), ["UTM30R"])

    def test_incomplete_existing_file_is_retried(self) -> None:
        incomplete_name = granule_name(utm="UTM30R")
        earthaccess = FakeEarthaccess(
            [FakeGranule(incomplete_name, concept_id="G1", size_mb=10.0)]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local_file = root / incomplete_name
            local_file.write_bytes(b"partial")
            config = DownloadConfig(
                output_folder=root,
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R"],
                report_csv=root / "report.csv",
                skip_existing=True,
            )
            preview = build_download_preview(config, earthaccess_module=earthaccess)
            granule = preview.granules[0]

            self.assertFalse(file_appears_complete(local_file, granule.size_mb))
            self.assertIsNone(existing_download_path(config, granule))
            moved = move_incomplete_download(config, granule)

            self.assertIsNotNone(moved)
            self.assertTrue(moved.exists())
            self.assertFalse(local_file.exists())

            result = run_download(config, earthaccess_module=earthaccess)

            self.assertEqual([path.name for path in result.downloaded_files], [incomplete_name])
            self.assertTrue(local_file.exists())

    def test_run_download_reports_missing_and_downloaded_flags(self) -> None:
        failed_name = granule_name(utm="UTM30R")

        class FailingEarthaccess(FakeEarthaccess):
            def download(self, granules, local_path: str, threads: int = 4, show_progress: bool = False):
                raise RuntimeError("network down")

        earthaccess = FailingEarthaccess([FakeGranule(failed_name, concept_id="G1")])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = DownloadConfig(
                output_folder=root,
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R"],
                report_csv=root / "report.csv",
            )

            result = run_download(config, earthaccess_module=earthaccess)
            report_text = (root / "report.csv").read_text(encoding="utf-8")

            self.assertFalse(result.all_complete)
            self.assertEqual(len(result.missing_granules), 1)
            self.assertIn("downloaded", report_text)
            self.assertIn("FAILED", report_text)
            self.assertIn(",no,", report_text)

    def test_run_download_can_be_stopped_before_attempting_files(self) -> None:
        first_name = granule_name(utm="UTM30R")
        second_name = granule_name(utm="UTM31R")
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(first_name, concept_id="G1"),
                FakeGranule(second_name, concept_id="G2"),
            ]
        )
        stop_event = threading.Event()
        progress_events: list[tuple[int, int, str]] = []

        def progress(current: int, total: int, message: str) -> None:
            progress_events.append((current, total, message))
            if message == "Starting download":
                stop_event.set()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = DownloadConfig(
                output_folder=root,
                start_date="2026-01-01",
                end_date="2026-01-31",
                utm_tiles=["UTM30R", "UTM31R"],
                report_csv=root / "report.csv",
                batch_size=1,
            )

            result = run_download(
                config,
                earthaccess_module=earthaccess,
                progress_callback=progress,
                stop_event=stop_event,
            )

            self.assertTrue(result.stopped)
            self.assertEqual(result.downloaded_files, [])
            self.assertEqual(len(result.missing_granules), 2)
            self.assertIn("Download stop requested", [event[2] for event in progress_events])
            report_text = (root / "report.csv").read_text(encoding="utf-8")
            self.assertIn("CANCELLED", report_text)

    def test_deduplication_prefers_first_seen_granule(self) -> None:
        first = FakeGranule(granule_name(scene="001F"), concept_id="G1")
        second = FakeGranule(granule_name(scene="001F"), concept_id="G1")

        self.assertEqual(dedupe_granules([first, second]), [first])


if __name__ == "__main__":
    unittest.main()
