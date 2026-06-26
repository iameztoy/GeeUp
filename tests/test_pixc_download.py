import csv
import tempfile
import unittest
from pathlib import Path

from products.pixc import download as pixc_download_module
from products.pixc.download import (
    AUTH_FAILED_STATUS,
    EXCLUDED_OLDER_VERSION_STATUS,
    EXCLUDED_BY_USER_SELECTION_STATUS,
    PixcTileId,
    PixcDownloadConfig,
    PixcTrackFilter,
    apply_preview_granule_selection,
    cmr_track_query_params,
    download_pixc_granules,
    granule_preview_feature,
    list_downloaded_pixc_files,
    parse_pixc_filename_metadata,
    preview_pixc_granules,
    search_kwargs,
    utm_tile_to_bbox,
    validate_config,
)


def pixc_name(
    *,
    cycle: str = "044",
    pass_id: str = "387",
    tile_id: str = "001F",
    crid: str = "PID0",
    counter: str = "01",
    start: str = "20260117T052508",
    end: str = "20260117T052529",
) -> str:
    return f"SWOT_L2_HR_PIXC_{cycle}_{pass_id}_{tile_id}_{start}_{end}_{crid}_{counter}.nc"


class FakeGranule(dict):
    def __init__(self, name: str, *, concept_id: str, size_mb: float | None = 0.5) -> None:
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
                    "SpatialExtent": {
                        "HorizontalSpatialDomain": {
                            "Geometry": {
                                "BoundingRectangles": [
                                    {
                                        "WestBoundingCoordinate": -1.0,
                                        "SouthBoundingCoordinate": 2.0,
                                        "EastBoundingCoordinate": 3.0,
                                        "NorthBoundingCoordinate": 4.0,
                                    }
                                ]
                            }
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


class FakeCMRResponse:
    def __init__(self, items: list[FakeGranule], next_offset: int | None = None) -> None:
        self._items = items
        self.headers = {}
        if next_offset is not None:
            self.headers["cmr-search-after"] = str(next_offset)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, list[FakeGranule]]:
        return {"items": self._items}


class FakeCMRSession:
    def __init__(self, query: "FakeGranuleQuery") -> None:
        self.query = query

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, int] | None = None,
    ) -> FakeCMRResponse:
        headers = headers or {}
        params = params or {}
        offset = int(headers.get("cmr-search-after", "0"))
        page_size = int(params.get("page_size", 2000))
        page = self.query.results[offset : offset + page_size]
        next_offset = offset + len(page)
        return FakeCMRResponse(page, next_offset if next_offset < len(self.query.results) else None)


class FakeGranuleQuery:
    def __init__(self, results: list[FakeGranule]) -> None:
        self.results = results
        self.headers: dict[str, str] = {}
        self.session = FakeCMRSession(self)
        self.bbox: tuple[float, float, float, float] | None = None
        self.extra_params: dict[str, object] = {}

    def short_name(self, value: str):
        return self

    def provider(self, value: str):
        return self

    def cloud_hosted(self, value: bool = True):
        return self

    def downloadable(self, value: bool = True):
        return self

    def temporal(self, start: str, end: str):
        return self

    def bounding_box(self, *value):
        self.bbox = tuple(value[0] if len(value) == 1 and isinstance(value[0], tuple) else value)
        return self

    def parameters(self, **kwargs):
        self.extra_params.update(kwargs)
        return self

    def granule_name(self, value):
        raise AssertionError("PIXC download must not search by raster granule_name patterns")

    def hits(self) -> int:
        return len(self.results)

    def _build_url(self) -> str:
        return "https://cmr.example.test/search"


class FakeEarthaccess:
    def __init__(self, results: list[FakeGranule]) -> None:
        self.results = results
        self.queries: list[FakeGranuleQuery] = []
        self.search_calls: list[dict[str, object]] = []
        self.downloaded: list[str] = []
        self.download_batches: list[list[str]] = []
        self.login_calls = 0
        self.login_kwargs: list[dict[str, object]] = []

    def granule_query(self) -> FakeGranuleQuery:
        query = FakeGranuleQuery(self.results)
        self.queries.append(query)
        return query

    def search_data(self, **kwargs):
        self.search_calls.append(kwargs)
        count = int(kwargs.get("count", len(self.results)))
        return self.results[:count]

    def login(self, **kwargs):
        self.login_calls += 1
        self.login_kwargs.append(dict(kwargs))
        return object()

    def download(self, granules, local_path: str, threads: int = 4, show_progress: bool = False):
        root = Path(local_path)
        root.mkdir(parents=True, exist_ok=True)
        batch = [granule.name for granule in granules]
        self.download_batches.append(batch)
        paths = []
        for granule in granules:
            path = root / granule.name
            path.write_text("downloaded", encoding="utf-8")
            self.downloaded.append(granule.name)
            paths.append(str(path))
        return paths


class AuthFailingEarthaccess(FakeEarthaccess):
    def login(self, **kwargs):
        self.login_calls += 1
        self.login_kwargs.append(dict(kwargs))
        raise RuntimeError(r"No .netrc found at C:\Users\ibana\_netrc")


class PixcDownloadTests(unittest.TestCase):
    def test_search_kwargs_use_bbox_not_granule_name(self) -> None:
        config = PixcDownloadConfig(
            start_date="2026-01-01",
            end_date="2026-01-02",
            bbox=(-1.0, 2.0, 3.0, 4.0),
            max_granules=10,
        )

        kwargs = search_kwargs(validate_config(config), config.max_granules)

        self.assertEqual(kwargs["bounding_box"], (-1.0, 2.0, 3.0, 4.0))
        self.assertNotIn("granule_name", kwargs)

    def test_track_filter_builds_cmr_cycle_pass_tile_query(self) -> None:
        track_filter = PixcTrackFilter(
            cycle_id=44,
            pass_id=387,
            tile_ids=(PixcTileId.parse("001F"), PixcTileId.parse("102R")),
        )
        config = PixcDownloadConfig(
            start_date="2026-01-01",
            end_date="2026-01-02",
            track_filter=track_filter,
            max_granules=10,
        )

        query = validate_config(config)
        kwargs = search_kwargs(query, config.max_granules)

        self.assertEqual(kwargs["cycle[]"], "44")
        self.assertEqual(kwargs["passes[0][pass]"], "387")
        self.assertEqual(kwargs["passes[0][tiles]"], "1F,102R")
        self.assertNotIn("granule_name", kwargs)
        self.assertEqual(cmr_track_query_params(track_filter)["passes[0][tiles]"], "1F,102R")

    def test_reference_tiles_with_cycle_build_grouped_cmr_query(self) -> None:
        config = PixcDownloadConfig(
            start_date="2026-01-01",
            end_date="2026-01-02",
            track_filter=PixcTrackFilter(cycle_id=44),
            reference_tiles=("001_164L", "001_164R", "002_007L"),
            max_granules=10,
        )

        kwargs = search_kwargs(validate_config(config), config.max_granules)

        self.assertEqual(kwargs["cycle[]"], "44")
        self.assertEqual(kwargs["passes[0][pass]"], "1")
        self.assertEqual(kwargs["passes[0][tiles]"], "164L,164R")
        self.assertEqual(kwargs["passes[1][pass]"], "2")
        self.assertEqual(kwargs["passes[1][tiles]"], "7L")
        self.assertNotIn("granule_name", kwargs)

    def test_reference_tiles_without_cycle_do_not_send_pass_params_and_filter_locally(self) -> None:
        matching_name = pixc_name(pass_id="001", tile_id="164L")
        other_name = pixc_name(pass_id="001", tile_id="165L", start="20260117T052600", end="20260117T052621")
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(matching_name, concept_id="MATCH", size_mb=0.5),
                FakeGranule(other_name, concept_id="OTHER", size_mb=0.5),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = PixcDownloadConfig(
                output_folder=root / "raw",
                start_date="2026-01-01",
                end_date="2026-01-02",
                bbox=(0.3, 5.1, 1.1, 5.9),
                reference_tiles=("001_164L",),
                report_csv=root / "logs" / "preview.csv",
                manifest_csv=root / "logs" / "manifest.csv",
            )

            preview = preview_pixc_granules(config, earthaccess_module=earthaccess)

            self.assertNotIn("cycle[]", earthaccess.queries[0].extra_params)
            self.assertNotIn("passes[0][pass]", earthaccess.queries[0].extra_params)
            self.assertEqual([granule.file_name for granule in preview.granules], [matching_name])

    def test_pass_and_tile_filters_require_cycle_and_pass_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "Pass filters require"):
            validate_config(
                PixcDownloadConfig(
                    start_date="2026-01-01",
                    end_date="2026-01-02",
                    track_filter=PixcTrackFilter(pass_id=387),
                )
            )
        with self.assertRaisesRegex(ValueError, "Tile filters require one pass"):
            validate_config(
                PixcDownloadConfig(
                    start_date="2026-01-01",
                    end_date="2026-01-02",
                    track_filter=PixcTrackFilter(cycle_id=44, tile_ids=(PixcTileId.parse("001F"),)),
                )
            )

    def test_parse_pixc_filename_track_metadata(self) -> None:
        metadata = parse_pixc_filename_metadata(pixc_name())

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.cycle_id, 44)
        self.assertEqual(metadata.pass_id, 387)
        self.assertEqual(metadata.tile_id, "001F")
        self.assertEqual(metadata.swath_side, "F")

    def test_utm_tile_to_bbox_returns_valid_lon_lat_bounds(self) -> None:
        west, south, east, north = utm_tile_to_bbox("UTM34M")

        self.assertLess(west, east)
        self.assertLess(south, north)
        self.assertGreaterEqual(west, -180)
        self.assertLessEqual(east, 180)
        self.assertGreaterEqual(south, -90)
        self.assertLessEqual(north, 90)

    def test_preview_normalizes_filters_versions_and_writes_report(self) -> None:
        old_name = pixc_name(crid="PID0", counter="01")
        best_name = pixc_name(crid="PGD0", counter="02")
        unparsed_name = "UNPARSED_PIXC_FILE.nc"
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(old_name, concept_id="OLD", size_mb=0.4),
                FakeGranule(best_name, concept_id="BEST", size_mb=0.6),
                FakeGranule(unparsed_name, concept_id="UNPARSED", size_mb=None),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = PixcDownloadConfig(
                output_folder=root / "raw",
                start_date="2026-01-01",
                end_date="2026-01-02",
                bbox=(-1.0, 2.0, 3.0, 4.0),
                report_csv=root / "logs" / "preview.csv",
                manifest_csv=root / "logs" / "manifest.csv",
            )

            preview = preview_pixc_granules(config, earthaccess_module=earthaccess)

            self.assertEqual(preview.total_hits, 3)
            self.assertEqual(earthaccess.queries[0].bbox, (-1.0, 2.0, 3.0, 4.0))
            self.assertEqual(len(preview.granules), 3)
            self.assertEqual([granule.file_name for granule in preview.selected_granules], [best_name, unparsed_name])
            self.assertEqual([granule.file_name for granule in preview.excluded_granules], [old_name])
            self.assertEqual(preview.selected_known_size_mb, 0.6)
            self.assertEqual(preview.selected_missing_size_count, 1)
            self.assertEqual(preview.granules[0].cycle_id, 44)
            self.assertEqual(preview.granules[0].pass_id, 387)
            self.assertEqual(preview.granules[0].tile_id, "001F")
            self.assertIsNotNone(preview.granules[0].footprint)
            self.assertIsNotNone(granule_preview_feature(preview.granules[0]))
            self.assertTrue((root / "logs" / "preview.csv").exists())
            report_text = (root / "logs" / "preview.csv").read_text(encoding="utf-8")
            self.assertIn("selected_unparsed", report_text)
            self.assertIn(EXCLUDED_OLDER_VERSION_STATUS, report_text)
            self.assertIn("cycle_id", report_text)
            self.assertIn("footprint", report_text)

    def test_preview_track_filter_is_sent_to_paged_query_and_keeps_unparsed_rows(self) -> None:
        matching_name = pixc_name()
        other_name = pixc_name(start="20260117T052600", end="20260117T052621").replace("_044_387_", "_045_387_")
        unparsed_name = "UNPARSED_PIXC_FILE.nc"
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(matching_name, concept_id="MATCH", size_mb=0.5),
                FakeGranule(other_name, concept_id="OTHER", size_mb=0.5),
                FakeGranule(unparsed_name, concept_id="UNPARSED", size_mb=0.5),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = PixcDownloadConfig(
                output_folder=root / "raw",
                start_date="2026-01-01",
                end_date="2026-01-02",
                track_filter=PixcTrackFilter(cycle_id=44, pass_id=387, tile_ids=(PixcTileId.parse("001F"),)),
                report_csv=root / "logs" / "preview.csv",
                manifest_csv=root / "logs" / "manifest.csv",
            )

            preview = preview_pixc_granules(config, earthaccess_module=earthaccess)

            self.assertEqual(earthaccess.queries[0].extra_params["cycle[]"], "44")
            self.assertEqual(earthaccess.queries[0].extra_params["passes[0][pass]"], "387")
            self.assertEqual(earthaccess.queries[0].extra_params["passes[0][tiles]"], "1F")
            self.assertEqual([granule.file_name for granule in preview.granules], [matching_name, unparsed_name])

    def test_preview_granule_selection_limits_download_set(self) -> None:
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(pixc_name(start="20260117T052508", end="20260117T052529"), concept_id="A", size_mb=0.5),
                FakeGranule(pixc_name(start="20260117T052600", end="20260117T052621"), concept_id="B", size_mb=0.5),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = PixcDownloadConfig(
                output_folder=root / "raw",
                start_date="2026-01-01",
                end_date="2026-01-02",
                bbox=(-1.0, 2.0, 3.0, 4.0),
                report_csv=root / "logs" / "preview.csv",
                manifest_csv=root / "logs" / "manifest.csv",
            )
            preview = preview_pixc_granules(config, earthaccess_module=earthaccess)

            selected_count = apply_preview_granule_selection(preview, ["A"])

            self.assertEqual(selected_count, 1)
            self.assertEqual([granule.identity for granule in preview.selected_granules], ["A"])
            excluded = [granule for granule in preview.granules if granule.identity == "B"][0]
            self.assertEqual(excluded.local_status, EXCLUDED_BY_USER_SELECTION_STATUS)

    def test_download_skips_existing_downloads_missing_and_writes_manifest(self) -> None:
        old_name = pixc_name(crid="PID0", counter="01")
        best_name = pixc_name(crid="PGD0", counter="02")
        new_name = "UNPARSED_PIXC_FILE.nc"
        earthaccess = FakeEarthaccess(
            [
                FakeGranule(old_name, concept_id="OLD", size_mb=0.4),
                FakeGranule(best_name, concept_id="BEST", size_mb=0.5),
                FakeGranule(new_name, concept_id="NEW", size_mb=None),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            raw.mkdir()
            (raw / best_name).write_text("already downloaded", encoding="utf-8")
            config = PixcDownloadConfig(
                output_folder=raw,
                start_date="2026-01-01",
                end_date="2026-01-02",
                bbox=(-1.0, 2.0, 3.0, 4.0),
                report_csv=root / "logs" / "preview.csv",
                manifest_csv=root / "logs" / "manifest.csv",
                batch_size=1,
            )
            preview = preview_pixc_granules(config, earthaccess_module=earthaccess)

            result = download_pixc_granules(config, preview=preview, earthaccess_module=earthaccess)

            self.assertTrue(result.all_complete)
            self.assertEqual([path.name for path in result.skipped_existing], [best_name])
            self.assertEqual(earthaccess.downloaded, [new_name])
            self.assertEqual(earthaccess.download_batches, [[new_name]])
            self.assertTrue((root / "logs" / "manifest.csv").exists())
            with (root / "logs" / "manifest.csv").open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            by_name = {row["file_name"]: row for row in rows}
            self.assertEqual(by_name[best_name]["downloaded"], "yes")
            self.assertEqual(by_name[new_name]["downloaded"], "yes")
            self.assertEqual(by_name[old_name]["last_status"], EXCLUDED_OLDER_VERSION_STATUS)

    def test_project_download_inventory_merges_manifest_and_raw_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            logs = root / "logs"
            raw.mkdir()
            logs.mkdir()
            tracked = raw / "tracked.nc"
            tracked.write_text("downloaded", encoding="utf-8")
            untracked = raw / "untracked.nc"
            untracked.write_text("local", encoding="utf-8")
            manifest = logs / "manifest.csv"
            manifest.write_text(
                "\n".join(
                    [
                        "granule_id,file_name,size_mb,downloaded,raw_exists,local_path,last_status,last_seen,last_downloaded",
                        f"G1,tracked.nc,0.001,yes,yes,{tracked},DOWNLOADED,2026-01-01T00:00:00,2026-01-01T00:00:00",
                        "G2,missing.nc,0.002,yes,no,,DOWNLOADED,2026-01-01T00:00:00,2026-01-01T00:00:00",
                        "G3,preview_only.nc,0.003,no,no,,MATCHED,2026-01-01T00:00:00,",
                    ]
                ),
                encoding="utf-8",
            )

            rows = list_downloaded_pixc_files(raw, manifest)

            by_name = {row.file_name: row for row in rows}
            self.assertEqual(by_name["tracked.nc"].last_status, "DOWNLOADED")
            self.assertTrue(by_name["tracked.nc"].raw_exists)
            self.assertEqual(by_name["missing.nc"].last_status, "MISSING_LOCAL_FILE")
            self.assertFalse(by_name["missing.nc"].raw_exists)
            self.assertEqual(by_name["untracked.nc"].last_status, "LOCAL_FILE_UNTRACKED")
            self.assertNotIn("preview_only.nc", by_name)

    def test_download_auth_failure_writes_event_report_and_manifest(self) -> None:
        name = pixc_name()
        earthaccess = AuthFailingEarthaccess([FakeGranule(name, concept_id="AUTH", size_mb=0.5)])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = PixcDownloadConfig(
                output_folder=root / "raw",
                start_date="2026-01-01",
                end_date="2026-01-02",
                bbox=(-1.0, 2.0, 3.0, 4.0),
                report_csv=root / "logs" / "preview.csv",
                manifest_csv=root / "logs" / "manifest.csv",
                events_csv=root / "logs" / "events.csv",
            )
            preview = preview_pixc_granules(config, earthaccess_module=earthaccess)
            pixc_download_module._AUTHENTICATED = False

            try:
                with self.assertRaisesRegex(RuntimeError, "Earthdata authentication is not configured"):
                    download_pixc_granules(config, preview=preview, earthaccess_module=earthaccess)
            finally:
                pixc_download_module._AUTHENTICATED = False

            self.assertEqual(earthaccess.login_calls, 1)
            self.assertEqual(earthaccess.login_kwargs[0], {"strategy": "all", "persist": False})
            report_text = config.report_csv.read_text(encoding="utf-8")
            manifest_text = config.manifest_csv.read_text(encoding="utf-8")
            events_text = config.events_csv.read_text(encoding="utf-8")
            self.assertIn(AUTH_FAILED_STATUS, report_text)
            self.assertIn(AUTH_FAILED_STATUS, manifest_text)
            self.assertIn("auth_failed", events_text)
            self.assertIn("No .netrc found", events_text)


if __name__ == "__main__":
    unittest.main()
