import tempfile
import unittest
from pathlib import Path

from swot_download_tool import (
    DownloadConfig,
    build_download_preview,
    build_granule_patterns,
    dedupe_granules,
    existing_download_path,
    generate_utm_tiles,
    load_config_file,
    normalize_utm_tiles,
    run_download,
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
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config_file(config_path)

            self.assertEqual(config.output_folder, root / "raw")
            self.assertEqual(config.report_csv, root / "logs" / "download_preview.csv")
            self.assertEqual(config.collection_short_name, "SWOT_L2_HR_Raster_100m_D")

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

    def test_existing_file_skip_logic_and_run_download(self) -> None:
        existing_name = granule_name(utm="UTM30R")
        new_name = granule_name(utm="UTM31R")
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(existing_name, concept_id="G1"),
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
            self.assertTrue((root / "report.csv").exists())

    def test_deduplication_prefers_first_seen_granule(self) -> None:
        first = FakeGranule(granule_name(scene="001F"), concept_id="G1")
        second = FakeGranule(granule_name(scene="001F"), concept_id="G1")

        self.assertEqual(dedupe_granules([first, second]), [first])


if __name__ == "__main__":
    unittest.main()
