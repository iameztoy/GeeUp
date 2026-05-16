"""Search and download SWOT L2 HR Raster 100 m granules with earthaccess."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

import yaml

from swot_metadata import parse_swot_l2_hr_raster_metadata


PROGRESS_PREFIX = "GEEUP_PROGRESS"
ProgressCallback = Callable[[int, int, str], None]

DEFAULT_PROCESSING_ROOT = "./SWOT_Processing"
DEFAULT_PROCESSING_PATHS = {
    "root": DEFAULT_PROCESSING_ROOT,
    "raw_downloads": f"{DEFAULT_PROCESSING_ROOT}/01_raw_downloads",
    "extracted_geotiffs": f"{DEFAULT_PROCESSING_ROOT}/02_extracted_geotiffs",
    "mosaics": f"{DEFAULT_PROCESSING_ROOT}/03_mosaics",
    "logs": f"{DEFAULT_PROCESSING_ROOT}/00_logs",
}

DEFAULT_COLLECTION_SHORT_NAME = "SWOT_L2_HR_Raster_100m_D"
DEFAULT_COLLECTION_LABEL = "Version D active"
COLLECTION_LABELS = {
    "Version D active": "SWOT_L2_HR_Raster_100m_D",
    "Version C superseded": "SWOT_L2_HR_Raster_100m_2.0",
}
COLLECTION_LABEL_BY_SHORT_NAME = {
    value: label for label, value in COLLECTION_LABELS.items()
}
MGRS_LATITUDE_BANDS = "CDEFGHJKLMNPQRSTUVWX"
UTM_TILE_RE = re.compile(r"^UTM(?P<zone>0[1-9]|[1-5]\d|60)(?P<band>[C-HJ-NP-X])$")
_AUTHENTICATED = False


@dataclass
class DownloadConfig:
    """Runtime settings for SWOT Earthdata search/download."""

    collection_short_name: str = DEFAULT_COLLECTION_SHORT_NAME
    collection_version_label: str = DEFAULT_COLLECTION_LABEL
    output_folder: Path = Path(DEFAULT_PROCESSING_PATHS["raw_downloads"])
    start_date: str = ""
    end_date: str = ""
    utm_tiles: List[str] = field(default_factory=list)
    max_granules: Optional[int] = None
    report_csv: Path = Path(f"{DEFAULT_PROCESSING_PATHS['logs']}/download_preview.csv")
    skip_existing: bool = True
    threads: int = 4
    base_dir: Path = Path.cwd()


@dataclass
class DownloadQuery:
    """CMR query parameters derived from config."""

    collection_short_name: str
    temporal: Tuple[str, str]
    granule_patterns: List[str]
    max_granules: Optional[int] = None


@dataclass
class DownloadGranule:
    """One matched Earthdata granule normalized for preview/reporting."""

    identity: str
    file_name: str
    utm_tile: str = ""
    start_time: str = ""
    end_time: str = ""
    size_mb: Optional[float] = None
    links: List[str] = field(default_factory=list)
    source: Any = field(default=None, repr=False, compare=False)


@dataclass
class DownloadPreview:
    """Search result before optional downloading."""

    query: DownloadQuery
    granules: List[DownloadGranule] = field(default_factory=list)

    @property
    def total_known_size_mb(self) -> float:
        """Return the sum of granule sizes that CMR/earthaccess exposed."""
        return sum(granule.size_mb or 0.0 for granule in self.granules)

    @property
    def missing_size_count(self) -> int:
        """Return how many granules have no reliable size metadata."""
        return sum(1 for granule in self.granules if granule.size_mb is None)


@dataclass
class DownloadResult:
    """Result from one download run."""

    preview: DownloadPreview
    downloaded_files: List[Path] = field(default_factory=list)
    skipped_existing: List[Path] = field(default_factory=list)
    failures: List[Tuple[DownloadGranule, str]] = field(default_factory=list)
    report_csv: Optional[Path] = None


DEFAULT_CONFIG: Dict[str, Any] = {
    "processing": DEFAULT_PROCESSING_PATHS,
    "download": {
        "collection_short_name": DEFAULT_COLLECTION_SHORT_NAME,
        "collection_version_label": DEFAULT_COLLECTION_LABEL,
        "output_folder": "",
        "start_date": "",
        "end_date": "",
        "utm_tiles": [],
        "max_granules": None,
        "report_csv": "",
        "skip_existing": True,
        "threads": 4,
    },
}


def generate_utm_tiles() -> List[str]:
    """Return all normal UTM/MGRS grid-zone tokens used in SWOT filenames."""
    return [
        f"UTM{zone:02d}{band}"
        for zone in range(1, 61)
        for band in MGRS_LATITUDE_BANDS
    ]


VALID_UTM_TILES = set(generate_utm_tiles())


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge two dictionaries."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_path(value: str | Path | None, base_dir: Path) -> Path:
    """Resolve a config path against the config file directory."""
    if value in (None, ""):
        raise ValueError("A required download path value was empty.")
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def normalize_optional_int(value: Any) -> Optional[int]:
    """Normalize an optional integer from YAML/UI input."""
    if value in (None, ""):
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def normalize_utm_tiles(value: Any) -> List[str]:
    """Normalize and validate selected UTM tile tokens."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_tiles = re.split(r"[\s,;]+", value.strip())
    elif isinstance(value, (list, tuple, set)):
        raw_tiles = [str(item) for item in value]
    else:
        raise ValueError("download.utm_tiles must be a list or comma-separated string.")

    tiles: List[str] = []
    invalid: List[str] = []
    seen: set[str] = set()
    for raw_tile in raw_tiles:
        tile = raw_tile.strip().upper()
        if not tile:
            continue
        if not UTM_TILE_RE.match(tile):
            invalid.append(raw_tile.strip())
            continue
        if tile not in seen:
            seen.add(tile)
            tiles.append(tile)
    if invalid:
        raise ValueError(
            "Invalid UTM tile token(s): "
            f"{', '.join(invalid)}. Use values like UTM30R."
        )
    return tiles


def validate_date_text(value: str, field_name: str) -> date:
    """Parse a YYYY-MM-DD UI/config date."""
    stripped = str(value or "").strip()
    if not stripped:
        raise ValueError(f"download.{field_name} is required.")
    try:
        return datetime.strptime(stripped, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"download.{field_name} must use YYYY-MM-DD format.") from exc


def load_config_file(config_path: Path) -> DownloadConfig:
    """Load download settings from YAML config."""
    with config_path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    merged = deep_merge(DEFAULT_CONFIG, user_config)
    return parse_config(merged, config_path.parent.resolve())


def parse_config(data: Dict[str, Any], base_dir: Path) -> DownloadConfig:
    """Convert raw config data into a validated DownloadConfig."""
    processing_data = data.get("processing", {})
    download_data = data.get("download", {})
    output_folder = (
        download_data.get("output_folder")
        or processing_data.get("raw_downloads")
        or DEFAULT_PROCESSING_PATHS["raw_downloads"]
    )
    logs_folder = processing_data.get("logs") or DEFAULT_PROCESSING_PATHS["logs"]
    collection_short_name = str(
        download_data.get("collection_short_name", DEFAULT_COLLECTION_SHORT_NAME)
    ).strip()
    config = DownloadConfig(
        collection_short_name=collection_short_name,
        collection_version_label=str(
            download_data.get(
                "collection_version_label",
                COLLECTION_LABEL_BY_SHORT_NAME.get(collection_short_name, DEFAULT_COLLECTION_LABEL),
            )
        ).strip(),
        output_folder=resolve_path(output_folder, base_dir),
        start_date=str(download_data.get("start_date", "")).strip(),
        end_date=str(download_data.get("end_date", "")).strip(),
        utm_tiles=normalize_utm_tiles(download_data.get("utm_tiles", [])),
        max_granules=normalize_optional_int(download_data.get("max_granules")),
        report_csv=resolve_path(
            download_data.get("report_csv") or f"{logs_folder}/download_preview.csv",
            base_dir,
        ),
        skip_existing=bool(download_data.get("skip_existing", True)),
        threads=int(download_data.get("threads", 4) or 4),
        base_dir=base_dir,
    )
    validate_config(config)
    return config


def validate_config(config: DownloadConfig) -> None:
    """Raise ValueError when download configuration is invalid."""
    start = validate_date_text(config.start_date, "start_date")
    end = validate_date_text(config.end_date, "end_date")
    if start > end:
        raise ValueError("download.start_date cannot be after download.end_date.")
    if not config.collection_short_name:
        raise ValueError("download.collection_short_name is required.")
    if not config.utm_tiles:
        raise ValueError("Select at least one UTM tile before searching.")
    if config.output_folder.exists() and not config.output_folder.is_dir():
        raise ValueError(f"Download output path is not a directory: {config.output_folder}")
    if config.report_csv.exists() and config.report_csv.is_dir():
        raise ValueError(f"Download report path is a directory: {config.report_csv}")
    if config.max_granules is not None and config.max_granules < 1:
        raise ValueError("download.max_granules must be blank or at least 1.")
    if config.threads < 1:
        raise ValueError("download.threads must be at least 1.")


def build_granule_patterns(utm_tiles: Sequence[str]) -> List[str]:
    """Build CMR readable-granule-name wildcard patterns for UTM tiles."""
    return [f"SWOT_L2_HR_Raster_100m_{tile}*" for tile in normalize_utm_tiles(utm_tiles)]


def build_download_query(config: DownloadConfig) -> DownloadQuery:
    """Return CMR query parameters for one config."""
    return DownloadQuery(
        collection_short_name=config.collection_short_name,
        temporal=(config.start_date, config.end_date),
        granule_patterns=build_granule_patterns(config.utm_tiles),
        max_granules=config.max_granules,
    )


def import_earthaccess() -> Any:
    """Import earthaccess lazily so tests can run without the dependency installed."""
    try:
        import earthaccess  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "earthaccess is not installed. Install dependencies with "
            "python -m pip install -r requirements.txt."
        ) from exc
    return earthaccess


def authenticate(
    strategy: str = "all",
    persist: bool = False,
    earthaccess_module: Any = None,
) -> Any:
    """Authenticate with Earthdata Login without storing credentials by default."""
    global _AUTHENTICATED
    earthaccess = earthaccess_module or import_earthaccess()
    try:
        auth = earthaccess.login(strategy=strategy, persist=persist)
    except (TypeError, ValueError):
        auth = earthaccess.login(persist=persist)
    _AUTHENTICATED = True
    return auth


def ensure_authenticated(earthaccess_module: Any = None) -> None:
    """Authenticate once per Python process before downloading."""
    if not _AUTHENTICATED:
        authenticate(earthaccess_module=earthaccess_module)


def search_kwargs(query: DownloadQuery, granule_name: str | Sequence[str], count: int) -> Dict[str, Any]:
    """Build earthaccess.search_data keyword arguments."""
    return {
        "short_name": query.collection_short_name,
        "provider": "POCLOUD",
        "cloud_hosted": True,
        "downloadable": True,
        "temporal": query.temporal,
        "granule_name": granule_name,
        "count": count,
    }


def run_search(
    earthaccess: Any,
    query: DownloadQuery,
    granule_name: str | Sequence[str],
    count: int,
) -> List[Any]:
    """Call earthaccess.search_data and return a list."""
    return list(earthaccess.search_data(**search_kwargs(query, granule_name, count)))


def granule_identity(granule: Any) -> str:
    """Return a stable identity for deduplicating CMR granules."""
    meta = mapping_value(granule, "meta") or {}
    umm = mapping_value(granule, "umm") or {}
    for container, keys in (
        (meta, ("concept-id", "concept_id", "native-id", "native_id")),
        (umm, ("GranuleUR", "ProducerGranuleId")),
    ):
        for key in keys:
            value = mapping_value(container, key)
            if value:
                return str(value)
    return granule_file_name(granule) or repr(granule)


def dedupe_granules(granules: Iterable[Any]) -> List[Any]:
    """Deduplicate overlapping tile-query results while keeping stable order."""
    unique: List[Any] = []
    seen: set[str] = set()
    for granule in granules:
        identity = granule_identity(granule)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(granule)
    return unique


def search_matching_granules(
    config: DownloadConfig,
    earthaccess_module: Any = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> List[Any]:
    """Search CMR for matching granules and deduplicate overlap."""
    earthaccess = earthaccess_module or import_earthaccess()
    query = build_download_query(config)
    count = config.max_granules if config.max_granules is not None else -1
    if progress_callback is not None:
        progress_callback(0, 0, "Searching CMR")

    try:
        granules = run_search(earthaccess, query, query.granule_patterns, count)
    except Exception:
        granules = []
        for index, pattern in enumerate(query.granule_patterns, start=1):
            if config.max_granules is not None and len(granules) >= config.max_granules:
                break
            if progress_callback is not None:
                progress_callback(index - 1, len(query.granule_patterns), f"Searching {pattern}")
            remaining = (
                config.max_granules - len(granules)
                if config.max_granules is not None
                else -1
            )
            granules.extend(run_search(earthaccess, query, pattern, remaining))

    unique = dedupe_granules(granules)
    if config.max_granules is not None:
        unique = unique[: config.max_granules]
    if progress_callback is not None:
        progress_callback(len(unique), len(unique), f"Matched {len(unique)} granules")
    return unique


def mapping_value(container: Any, key: str, default: Any = None) -> Any:
    """Read a value from dict-like objects or attributes."""
    if isinstance(container, Mapping):
        return container.get(key, default)
    getter = getattr(container, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            pass
    return getattr(container, key, default)


def granule_links(granule: Any) -> List[str]:
    """Return external data links for a granule."""
    data_links = getattr(granule, "data_links", None)
    if callable(data_links):
        try:
            links = data_links(access="external")
        except TypeError:
            links = data_links()
        return [str(link) for link in links if link]

    umm = mapping_value(granule, "umm") or {}
    links: List[str] = []
    for related in mapping_value(umm, "RelatedUrls", []) or []:
        related_type = str(mapping_value(related, "Type", "")).upper()
        url = mapping_value(related, "URL")
        if url and "GET" in related_type:
            links.append(str(url))
    return links


def link_file_name(link: str) -> str:
    """Return the basename from one URL."""
    parsed = urlparse(str(link))
    name = Path(unquote(parsed.path)).name
    return name


def granule_file_name(granule: Any) -> str:
    """Return the best expected local data filename for a granule."""
    links = granule_links(granule)
    for link in links:
        name = link_file_name(link)
        if name.lower().endswith((".nc", ".nc4", ".h5", ".hdf5")):
            return name
    for link in links:
        name = link_file_name(link)
        if name:
            return name

    umm = mapping_value(granule, "umm") or {}
    granule_ur = mapping_value(umm, "GranuleUR") or mapping_value(umm, "ProducerGranuleId")
    if granule_ur:
        return str(granule_ur)
    return str(mapping_value(granule, "title", "") or repr(granule))


def granule_size_mb(granule: Any) -> Optional[float]:
    """Return size in MB when exposed by earthaccess or CMR metadata."""
    size_method = getattr(granule, "size", None)
    if callable(size_method):
        try:
            value = size_method()
            if value is not None:
                return float(value)
        except Exception:
            pass

    umm = mapping_value(granule, "umm") or {}
    data_granule = mapping_value(umm, "DataGranule") or {}
    archive_info = mapping_value(data_granule, "ArchiveAndDistributionInformation") or []
    if isinstance(archive_info, Mapping):
        archive_info = [archive_info]
    total = 0.0
    found = False
    for item in archive_info:
        raw_size = mapping_value(item, "Size")
        if raw_size in (None, ""):
            continue
        unit = str(mapping_value(item, "SizeUnit", "MB")).strip().lower()
        multiplier = {
            "kb": 1 / 1024,
            "kbyte": 1 / 1024,
            "kbytes": 1 / 1024,
            "mb": 1,
            "mbyte": 1,
            "mbytes": 1,
            "gb": 1024,
            "gbyte": 1024,
            "gbytes": 1024,
        }.get(unit, 1)
        total += float(raw_size) * multiplier
        found = True
    return total if found else None


def granule_temporal(granule: Any, file_name: str) -> Tuple[str, str]:
    """Return beginning/end timestamps from CMR metadata or filename parsing."""
    umm = mapping_value(granule, "umm") or {}
    temporal = mapping_value(umm, "TemporalExtent") or {}
    range_datetime = mapping_value(temporal, "RangeDateTime") or {}
    start = mapping_value(range_datetime, "BeginningDateTime") or ""
    end = mapping_value(range_datetime, "EndingDateTime") or ""
    if start or end:
        return str(start), str(end)

    try:
        metadata = parse_swot_l2_hr_raster_metadata(file_name)
    except ValueError:
        metadata = None
    if metadata is None:
        return "", ""
    return metadata.start_time, metadata.end_time


def granule_utm_tile(file_name: str) -> str:
    """Return the UTM token parsed from a SWOT filename when available."""
    try:
        metadata = parse_swot_l2_hr_raster_metadata(file_name)
    except ValueError:
        metadata = None
    if metadata is None:
        match = re.search(r"_UTM(\d{2}[C-HJ-NP-X])_", file_name)
        return f"UTM{match.group(1)}" if match else ""
    return metadata.fields.get("coordinate_system", "")


def normalize_granule(granule: Any) -> DownloadGranule:
    """Convert one earthaccess DataGranule into a preview row."""
    file_name = granule_file_name(granule)
    start_time, end_time = granule_temporal(granule, file_name)
    return DownloadGranule(
        identity=granule_identity(granule),
        file_name=file_name,
        utm_tile=granule_utm_tile(file_name),
        start_time=start_time,
        end_time=end_time,
        size_mb=granule_size_mb(granule),
        links=granule_links(granule),
        source=granule,
    )


def build_download_preview(
    config: DownloadConfig,
    earthaccess_module: Any = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> DownloadPreview:
    """Search CMR and normalize matching granules for display/reporting."""
    query = build_download_query(config)
    raw_granules = search_matching_granules(
        config,
        earthaccess_module=earthaccess_module,
        progress_callback=progress_callback,
    )
    granules = sorted(
        (normalize_granule(granule) for granule in raw_granules),
        key=lambda granule: granule.file_name.lower(),
    )
    return DownloadPreview(query=query, granules=granules)


def existing_download_path(config: DownloadConfig, granule: DownloadGranule) -> Optional[Path]:
    """Return an existing local file path for a granule when skip-existing can detect one."""
    if not granule.file_name:
        return None
    candidate = config.output_folder / granule.file_name
    return candidate if candidate.exists() else None


def write_download_report(
    config: DownloadConfig,
    preview: DownloadPreview,
    statuses: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Path:
    """Write a CSV preview/download report."""
    config.report_csv.parent.mkdir(parents=True, exist_ok=True)
    statuses = statuses or {}
    columns = [
        "status",
        "file_name",
        "utm_tile",
        "start_time",
        "end_time",
        "size_mb",
        "local_path",
        "granule_id",
        "links",
    ]
    with config.report_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for granule in preview.granules:
            status, local_path = statuses.get(granule.identity, ("MATCHED", ""))
            writer.writerow(
                {
                    "status": status,
                    "file_name": granule.file_name,
                    "utm_tile": granule.utm_tile,
                    "start_time": granule.start_time,
                    "end_time": granule.end_time,
                    "size_mb": "" if granule.size_mb is None else f"{granule.size_mb:.3f}",
                    "local_path": local_path,
                    "granule_id": granule.identity,
                    "links": " ".join(granule.links),
                }
            )
    return config.report_csv


def run_download(
    config: DownloadConfig,
    earthaccess_module: Any = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> DownloadResult:
    """Search for matching granules and download missing files."""
    earthaccess = earthaccess_module or import_earthaccess()
    preview = build_download_preview(
        config,
        earthaccess_module=earthaccess,
        progress_callback=progress_callback,
    )
    config.output_folder.mkdir(parents=True, exist_ok=True)

    statuses: Dict[str, Tuple[str, str]] = {}
    to_download: List[DownloadGranule] = []
    skipped: List[Path] = []
    for granule in preview.granules:
        existing = existing_download_path(config, granule)
        if config.skip_existing and existing is not None:
            skipped.append(existing)
            statuses[granule.identity] = ("SKIPPED_EXISTING", str(existing))
        else:
            to_download.append(granule)

    if to_download:
        ensure_authenticated(earthaccess)

    downloaded_files: List[Path] = []
    failures: List[Tuple[DownloadGranule, str]] = []
    total = len(to_download)
    if progress_callback is not None:
        progress_callback(0, total, "Starting download" if total else "No new files to download")

    for index, granule in enumerate(to_download, start=1):
        if progress_callback is not None:
            progress_callback(index - 1, total, f"Downloading {granule.file_name}")
        try:
            paths = earthaccess.download(
                [granule.source],
                local_path=str(config.output_folder),
                threads=config.threads,
                show_progress=False,
            )
            normalized_paths = [Path(path) for path in paths or []]
            downloaded_files.extend(normalized_paths)
            local_path = str(normalized_paths[0]) if normalized_paths else ""
            statuses[granule.identity] = ("DOWNLOADED", local_path)
            message = f"Downloaded {granule.file_name}"
        except Exception as exc:
            failures.append((granule, str(exc)))
            statuses[granule.identity] = ("FAILED", str(exc))
            message = f"FAILED: {granule.file_name}"
        if progress_callback is not None:
            progress_callback(index, total, message)

    report_csv = write_download_report(config, preview, statuses)
    return DownloadResult(
        preview=preview,
        downloaded_files=downloaded_files,
        skipped_existing=skipped,
        failures=failures,
        report_csv=report_csv,
    )


def format_size(size_mb: float, missing_count: int = 0) -> str:
    """Return a compact human-readable size summary."""
    if size_mb >= 1024:
        text = f"{size_mb / 1024:.2f} GB"
    else:
        text = f"{size_mb:.1f} MB"
    if missing_count:
        text += f" plus {missing_count} unknown-size granule(s)"
    return text


def print_preview_summary(preview: DownloadPreview, report_csv: Path) -> None:
    """Print a concise preview summary for CLI users."""
    print(f"Collection: {preview.query.collection_short_name}")
    print(f"Temporal: {preview.query.temporal[0]} to {preview.query.temporal[1]}")
    print(f"Patterns: {', '.join(preview.query.granule_patterns)}")
    print(f"Granules: {len(preview.granules)}")
    print(f"Known size: {format_size(preview.total_known_size_mb, preview.missing_size_count)}")
    print(f"Report CSV: {report_csv}")


def print_download_summary(result: DownloadResult) -> None:
    """Print a concise download summary for CLI users."""
    print(f"Granules matched: {len(result.preview.granules)}")
    print(f"Downloaded files: {len(result.downloaded_files)}")
    print(f"Skipped existing: {len(result.skipped_existing)}")
    print(f"Failed files: {len(result.failures)}")
    if result.report_csv:
        print(f"Report CSV: {result.report_csv}")
    for granule, error in result.failures[:10]:
        print(f"FAILED {granule.file_name}: {error}")


def print_progress(current: int, total: int, message: str) -> None:
    """Emit one machine-readable progress line for the GUI launcher."""
    safe_message = str(message).replace("\t", " ").replace("\n", " ")
    print(f"{PROGRESS_PREFIX}\tdownload\t{current}\t{total}\t{safe_message}", flush=True)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Search and download SWOT L2 HR Raster 100 m granules."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and write the preview report without downloading.",
    )
    parser.add_argument(
        "--login-check",
        action="store_true",
        help="Authenticate with Earthdata Login and exit.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    args = build_arg_parser().parse_args(argv)
    try:
        if args.login_check:
            authenticate()
            print("Earthdata authentication succeeded.")
            return 0

        config = load_config_file(Path(args.config))
        if args.dry_run:
            preview = build_download_preview(config, progress_callback=print_progress)
            report_csv = write_download_report(config, preview)
            print_preview_summary(preview, report_csv)
            return 0

        result = run_download(config, progress_callback=print_progress)
        print_download_summary(result)
        return 1 if result.failures else 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
