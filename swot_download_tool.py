"""Search and download SWOT L2 HR Raster 100 m granules with earthaccess."""

from __future__ import annotations

import argparse
import csv
import re
import sys
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

import yaml

from swot_metadata import parse_swot_l2_hr_raster_metadata, swot_product_rank
from workflow_manifest import upsert_workflow_manifest, workflow_manifest_path


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
DEFAULT_REPORT_CSV = Path(f"{DEFAULT_PROCESSING_PATHS['logs']}/download_preview.csv")
DEFAULT_MANIFEST_CSV = Path(f"{DEFAULT_PROCESSING_PATHS['logs']}/download_manifest.csv")
COLLECTION_LABELS = {
    "Version D active": "SWOT_L2_HR_Raster_100m_D",
    "Version C superseded": "SWOT_L2_HR_Raster_100m_2.0",
}
COLLECTION_LABEL_BY_SHORT_NAME = {
    value: label for label, value in COLLECTION_LABELS.items()
}
PRODUCT_FILTER_BEST = "best"
PRODUCT_FILTER_ALL = "all"
DEFAULT_PRODUCT_VERSION_FILTER = PRODUCT_FILTER_BEST
DEFAULT_PRODUCT_VERSION_FILTER_LABEL = "Best product version only"
PRODUCT_VERSION_FILTER_LABELS = {
    "Best product version only": PRODUCT_FILTER_BEST,
    "All matching files": PRODUCT_FILTER_ALL,
}
PRODUCT_VERSION_FILTER_LABEL_BY_VALUE = {
    value: label for label, value in PRODUCT_VERSION_FILTER_LABELS.items()
}
EXCLUDED_OLDER_VERSION_STATUS = "EXCLUDED_OLDER_VERSION"
MGRS_LATITUDE_BANDS = "CDEFGHJKLMNPQRSTUVWX"
UTM_TILE_RE = re.compile(r"^UTM(?P<zone>0[1-9]|[1-5]\d|60)(?P<band>[C-HJ-NP-X])$")
_AUTHENTICATED = False

warnings.filterwarnings(
    "ignore",
    message=r"As of version 1\.0, `DataGranule\.size` will be accessed as an attribute.*",
    category=FutureWarning,
    module=r"earthaccess\.results",
)


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
    report_csv: Path = DEFAULT_REPORT_CSV
    manifest_csv: Path = DEFAULT_MANIFEST_CSV
    skip_existing: bool = True
    skip_manifest_existing: bool = True
    threads: int = 6
    batch_size: int = 25
    product_version_filter: str = DEFAULT_PRODUCT_VERSION_FILTER
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
    selected_for_download: bool = True
    duplicate_filter_status: str = "selected"
    preferred_file_name: str = ""
    duplicate_reason: str = ""
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

    @property
    def selected_granules(self) -> List[DownloadGranule]:
        """Return granules selected for download after product-version filtering."""
        return [granule for granule in self.granules if granule.selected_for_download]

    @property
    def excluded_granules(self) -> List[DownloadGranule]:
        """Return remote matches excluded by product-version filtering."""
        return [granule for granule in self.granules if not granule.selected_for_download]

    @property
    def selected_known_size_mb(self) -> float:
        """Return known size for selected granules."""
        return sum(granule.size_mb or 0.0 for granule in self.selected_granules)

    @property
    def excluded_known_size_mb(self) -> float:
        """Return known size for excluded granules."""
        return sum(granule.size_mb or 0.0 for granule in self.excluded_granules)

    @property
    def selected_missing_size_count(self) -> int:
        """Return selected granules without size metadata."""
        return sum(1 for granule in self.selected_granules if granule.size_mb is None)

    @property
    def excluded_missing_size_count(self) -> int:
        """Return excluded granules without size metadata."""
        return sum(1 for granule in self.excluded_granules if granule.size_mb is None)


@dataclass
class DownloadResult:
    """Result from one download run."""

    preview: DownloadPreview
    downloaded_files: List[Path] = field(default_factory=list)
    skipped_existing: List[Path] = field(default_factory=list)
    skipped_manifest: List[DownloadGranule] = field(default_factory=list)
    failures: List[Tuple[DownloadGranule, str]] = field(default_factory=list)
    missing_granules: List[DownloadGranule] = field(default_factory=list)
    stopped: bool = False
    report_csv: Optional[Path] = None

    @property
    def complete_count(self) -> int:
        """Return how many matched granules are locally or historically accounted for."""
        return len(self.preview.granules) - len(self.missing_granules)

    @property
    def all_complete(self) -> bool:
        """Return True when every matched granule is locally or historically accounted for."""
        return not self.missing_granules


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
        "manifest_csv": "",
        "skip_existing": True,
        "skip_manifest_existing": True,
        "threads": 6,
        "batch_size": 25,
        "product_version_filter": DEFAULT_PRODUCT_VERSION_FILTER,
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


def normalize_product_version_filter(value: Any) -> str:
    """Normalize the product-version filter mode."""
    text = str(value or DEFAULT_PRODUCT_VERSION_FILTER).strip()
    if text in PRODUCT_VERSION_FILTER_LABELS:
        return PRODUCT_VERSION_FILTER_LABELS[text]
    lowered = text.lower().replace("-", "_").replace(" ", "_")
    if lowered in {"best", "best_only", "best_product_version_only", "highest", "preferred"}:
        return PRODUCT_FILTER_BEST
    if lowered in {"all", "none", "all_matching_files", "download_all"}:
        return PRODUCT_FILTER_ALL
    raise ValueError("download.product_version_filter must be 'best' or 'all'.")


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
        manifest_csv=resolve_path(
            download_data.get("manifest_csv") or f"{logs_folder}/download_manifest.csv",
            base_dir,
        ),
        skip_existing=bool(download_data.get("skip_existing", True)),
        skip_manifest_existing=bool(download_data.get("skip_manifest_existing", True)),
        threads=int(download_data.get("threads", 6) or 6),
        batch_size=int(download_data.get("batch_size", 25) or 25),
        product_version_filter=normalize_product_version_filter(
            download_data.get("product_version_filter", DEFAULT_PRODUCT_VERSION_FILTER)
        ),
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
    if config.manifest_csv.exists() and config.manifest_csv.is_dir():
        raise ValueError(f"Download manifest path is a directory: {config.manifest_csv}")
    if config.max_granules is not None and config.max_granules < 1:
        raise ValueError("download.max_granules must be blank or at least 1.")
    if config.threads < 1:
        raise ValueError("download.threads must be at least 1.")
    if config.batch_size < 1:
        raise ValueError("download.batch_size must be at least 1.")
    normalize_product_version_filter(config.product_version_filter)


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
    size_value = mapping_value(granule, "size")
    if size_value not in (None, "") and not callable(size_value):
        try:
            return float(size_value)
        except (TypeError, ValueError):
            pass

    size_attr = getattr(granule, "size", None)
    if size_attr not in (None, "") and not callable(size_attr):
        try:
            return float(size_attr)
        except (TypeError, ValueError):
            pass

    if callable(size_attr):
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"As of version 1\.0, `DataGranule\.size` will be accessed as an attribute.*",
                    category=FutureWarning,
                )
                value = size_attr()
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


def product_duplicate_key(granule: DownloadGranule) -> Optional[Tuple[str, ...]]:
    """Return a stable key for files that differ only by product version."""
    metadata = parse_swot_l2_hr_raster_metadata(granule.file_name)
    if metadata is None:
        return None
    fields = metadata.fields
    return (
        fields.get("descriptor", ""),
        fields.get("cycle_id", ""),
        fields.get("pass_id", ""),
        fields.get("scene_id", ""),
        fields.get("range_beginning", ""),
        fields.get("range_ending", ""),
        fields.get("filename_suffix", ""),
    )


def product_preference_key(granule: DownloadGranule) -> Tuple[int, int, int, int, int, str]:
    """Return the product-version preference key for one granule."""
    metadata = parse_swot_l2_hr_raster_metadata(granule.file_name)
    if metadata is None:
        return (0, -1, -1, -1, -1, granule.file_name.lower())
    fields = metadata.fields
    return (*swot_product_rank(fields.get("crid", ""), fields.get("product_counter", "")), granule.file_name.lower())


def reset_product_filter_fields(granules: Sequence[DownloadGranule]) -> None:
    """Reset product filter fields before applying a mode."""
    for granule in granules:
        granule.selected_for_download = True
        granule.duplicate_filter_status = "selected"
        granule.preferred_file_name = ""
        granule.duplicate_reason = ""


def apply_product_version_filter(
    granules: Sequence[DownloadGranule],
    mode: str,
) -> List[DownloadGranule]:
    """Mark older product versions as excluded while preserving all preview rows."""
    filter_mode = normalize_product_version_filter(mode)
    reset_product_filter_fields(granules)
    if filter_mode == PRODUCT_FILTER_ALL:
        for granule in granules:
            granule.duplicate_filter_status = "all_matching_files"
        return list(granules)

    grouped: Dict[Tuple[str, ...], List[DownloadGranule]] = {}
    for granule in granules:
        key = product_duplicate_key(granule)
        if key is None:
            granule.duplicate_filter_status = "selected_unparsed"
            continue
        grouped.setdefault(key, []).append(granule)

    for group in grouped.values():
        if len(group) == 1:
            group[0].duplicate_filter_status = "selected_single_version"
            continue
        preferred = max(group, key=product_preference_key)
        for granule in group:
            granule.preferred_file_name = preferred.file_name
            if granule is preferred:
                granule.selected_for_download = True
                granule.duplicate_filter_status = "selected_best_version"
                granule.duplicate_reason = (
                    f"Kept best CRID/product counter among {len(group)} remote version(s)."
                )
            else:
                granule.selected_for_download = False
                granule.duplicate_filter_status = "excluded_older_version"
                granule.duplicate_reason = (
                    f"Superseded by {preferred.file_name} for the same SWOT observation."
                )
    return list(granules)


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
    apply_product_version_filter(granules, config.product_version_filter)
    return DownloadPreview(query=query, granules=granules)


def existing_download_path(config: DownloadConfig, granule: DownloadGranule) -> Optional[Path]:
    """Return an existing local file path for a granule when skip-existing can detect one."""
    if not granule.file_name:
        return None
    candidate = config.output_folder / granule.file_name
    if not candidate.exists():
        return None
    return candidate if file_appears_complete(candidate, granule.size_mb) else None


def file_appears_complete(path: Path, expected_size_mb: Optional[float]) -> bool:
    """Return False for zero-byte or clearly undersized local downloads."""
    try:
        actual_size = path.stat().st_size
    except OSError:
        return False
    if actual_size <= 0:
        return False
    if expected_size_mb in (None, 0):
        return True
    expected_size = float(expected_size_mb) * 1024 * 1024
    tolerance = max(1024 * 1024, expected_size * 0.05)
    return actual_size + tolerance >= expected_size


def next_incomplete_path(path: Path) -> Path:
    """Return an unused sidecar path for an incomplete local download."""
    candidate = path.with_name(f"{path.name}.incomplete")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.incomplete{counter}")
        counter += 1
    return candidate


def move_incomplete_download(config: DownloadConfig, granule: DownloadGranule) -> Optional[Path]:
    """Move a clearly incomplete local file aside before retrying a download."""
    if not granule.file_name:
        return None
    candidate = config.output_folder / granule.file_name
    if not candidate.exists() or file_appears_complete(candidate, granule.size_mb):
        return None
    target = next_incomplete_path(candidate)
    candidate.replace(target)
    return target


def chunks(items: Sequence[DownloadGranule], size: int) -> Iterable[Sequence[DownloadGranule]]:
    """Yield fixed-size chunks from a sequence."""
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + max(1, size)]


def effective_manifest_csv(config: DownloadConfig) -> Path:
    """Return the manifest path, defaulting beside a custom report CSV."""
    if config.manifest_csv == DEFAULT_MANIFEST_CSV and config.report_csv != DEFAULT_REPORT_CSV:
        return config.report_csv.with_name("download_manifest.csv")
    return config.manifest_csv


def local_complete_path(config: DownloadConfig, granule: DownloadGranule) -> Optional[Path]:
    """Return the local path when a complete local file exists for a granule."""
    return existing_download_path(config, granule)


MANIFEST_COLUMNS = [
    "granule_id",
    "file_name",
    "utm_tile",
    "start_time",
    "end_time",
    "size_mb",
    "collection_short_name",
    "downloaded",
    "raw_exists",
    "local_path",
    "last_status",
    "selected_for_download",
    "duplicate_filter_status",
    "preferred_file_name",
    "duplicate_reason",
    "first_seen",
    "last_seen",
    "last_downloaded",
]


def manifest_key(granule: DownloadGranule) -> str:
    """Return the stable manifest key for one granule."""
    return granule.identity or granule.file_name


def read_download_manifest(path: Path) -> Dict[str, Dict[str, str]]:
    """Load a cumulative download manifest keyed by granule id."""
    if not path.exists() or not path.is_file():
        return {}
    rows: Dict[str, Dict[str, str]] = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = row.get("granule_id") or row.get("file_name")
                if key:
                    rows[key] = {column: str(value or "") for column, value in row.items()}
    except OSError:
        return {}
    return rows


def manifest_row_downloaded(row: Mapping[str, str] | None) -> bool:
    """Return True when a manifest row records a successful previous download."""
    if not row:
        return False
    return str(row.get("downloaded", "")).strip().lower() in {"yes", "true", "1"}


def manifest_has_downloaded(
    manifest: Mapping[str, Mapping[str, str]],
    granule: DownloadGranule,
) -> bool:
    """Return True when a granule is already known as downloaded in the manifest."""
    return manifest_row_downloaded(manifest.get(manifest_key(granule)))


def manifest_downloaded_tiles(path: Path) -> List[str]:
    """Return UTM tiles that have at least one downloaded granule in a manifest."""
    tiles = {
        row.get("utm_tile", "")
        for row in read_download_manifest(path).values()
        if manifest_row_downloaded(row)
    }
    return sorted(tile for tile in tiles if tile)


def write_download_manifest(
    config: DownloadConfig,
    preview: DownloadPreview,
    statuses: Mapping[str, Tuple[str, str]],
) -> Path:
    """Merge one run into the cumulative project/download manifest."""
    manifest_csv = effective_manifest_csv(config)
    manifest_csv.parent.mkdir(parents=True, exist_ok=True)
    rows = read_download_manifest(manifest_csv)
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    for granule in preview.granules:
        key = manifest_key(granule)
        existing = rows.get(key, {})
        status, local_path = statuses.get(key, statuses.get(granule.identity, ("MATCHED", "")))
        raw_path = local_complete_path(config, granule)
        raw_exists = raw_path is not None
        status_downloaded = status in {"DOWNLOADED", "SKIPPED_EXISTING", "LOCAL_COMPLETE", "SKIPPED_MANIFEST"}
        downloaded = status_downloaded or manifest_row_downloaded(existing) or raw_exists
        last_downloaded = existing.get("last_downloaded", "")
        if status in {"DOWNLOADED", "SKIPPED_EXISTING", "LOCAL_COMPLETE"}:
            last_downloaded = timestamp
        size_text = "" if granule.size_mb is None else f"{granule.size_mb:.3f}"
        rows[key] = {
            "granule_id": key,
            "file_name": granule.file_name,
            "utm_tile": granule.utm_tile,
            "start_time": granule.start_time,
            "end_time": granule.end_time,
            "size_mb": size_text,
            "collection_short_name": preview.query.collection_short_name,
            "downloaded": "yes" if downloaded else "no",
            "raw_exists": "yes" if raw_exists else "no",
            "local_path": str(raw_path or local_path or existing.get("local_path", "")),
            "last_status": status,
            "selected_for_download": "yes" if granule.selected_for_download else "no",
            "duplicate_filter_status": granule.duplicate_filter_status,
            "preferred_file_name": granule.preferred_file_name,
            "duplicate_reason": granule.duplicate_reason,
            "first_seen": existing.get("first_seen") or timestamp,
            "last_seen": timestamp,
            "last_downloaded": last_downloaded,
        }
    with manifest_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for key in sorted(rows, key=lambda item: rows[item].get("file_name", item).lower()):
            row = rows[key]
            writer.writerow({column: row.get(column, "") for column in MANIFEST_COLUMNS})
    return manifest_csv


def write_download_workflow_manifest(
    config: DownloadConfig,
    preview: DownloadPreview,
    statuses: Mapping[str, Tuple[str, str]],
) -> Path:
    """Update the shared project workflow manifest with download status rows."""
    rows = []
    for granule in preview.granules:
        status, local_path = statuses.get(granule.identity, ("MATCHED", ""))
        raw_path = local_complete_path(config, granule)
        rows.append(
            {
                "stage": "download",
                "record_id": manifest_key(granule),
                "record_type": "swot_granule",
                "status": status,
                "source_path": " ".join(granule.links),
                "output_path": str(raw_path or local_path),
                "utm_tile": granule.utm_tile,
                "start_time": granule.start_time,
                "end_time": granule.end_time,
                "raw_exists": "yes" if raw_path is not None else "no",
                "output_exists": "yes" if raw_path is not None else "no",
                "known_from_stage_manifest": "yes" if status == "SKIPPED_MANIFEST" else "no",
            }
        )
    return upsert_workflow_manifest(workflow_manifest_path(config.report_csv), rows)


def update_statuses_from_local_files(
    config: DownloadConfig,
    preview: DownloadPreview,
    statuses: Dict[str, Tuple[str, str]],
) -> List[DownloadGranule]:
    """Mark every preview granule according to current local or manifest completeness."""
    missing: List[DownloadGranule] = []
    for granule in preview.granules:
        complete_path = local_complete_path(config, granule)
        current_status, current_path = statuses.get(granule.identity, ("", ""))
        if not granule.selected_for_download or current_status == EXCLUDED_OLDER_VERSION_STATUS:
            statuses[granule.identity] = (EXCLUDED_OLDER_VERSION_STATUS, current_path)
            continue
        if complete_path is not None:
            if current_status in {"DOWNLOADED", "SKIPPED_EXISTING"}:
                statuses[granule.identity] = (current_status, str(complete_path))
            else:
                statuses[granule.identity] = ("LOCAL_COMPLETE", str(complete_path))
            continue
        if current_status == "SKIPPED_MANIFEST":
            statuses[granule.identity] = (current_status, current_path)
            continue
        missing.append(granule)
        if current_status in {"FAILED", "CANCELLED"}:
            statuses[granule.identity] = (current_status, current_path)
        elif current_status:
            statuses[granule.identity] = ("MISSING", current_path)
        else:
            statuses[granule.identity] = ("MISSING", "")
    return missing


def preview_statuses_from_existing(
    config: DownloadConfig,
    preview: DownloadPreview,
) -> Dict[str, Tuple[str, str]]:
    """Return preview statuses from local raw files and the project manifest."""
    statuses: Dict[str, Tuple[str, str]] = {}
    manifest = read_download_manifest(effective_manifest_csv(config))
    for granule in preview.granules:
        if not granule.selected_for_download:
            statuses[granule.identity] = (EXCLUDED_OLDER_VERSION_STATUS, "")
            continue
        existing = existing_download_path(config, granule)
        if existing is not None:
            status = "SKIPPED_EXISTING" if config.skip_existing else "LOCAL_COMPLETE"
            statuses[granule.identity] = (status, str(existing))
            continue
        if config.skip_manifest_existing and manifest_has_downloaded(manifest, granule):
            manifest_row = manifest.get(manifest_key(granule), {})
            statuses[granule.identity] = (
                "SKIPPED_MANIFEST",
                str(manifest_row.get("local_path", "")),
            )
        else:
            statuses[granule.identity] = ("MATCHED", "")
    return statuses


def write_download_report(
    config: DownloadConfig,
    preview: DownloadPreview,
    statuses: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Path:
    """Write a CSV preview/download report."""
    config.report_csv.parent.mkdir(parents=True, exist_ok=True)
    statuses = statuses if statuses is not None else preview_statuses_from_existing(config, preview)
    columns = [
        "status",
        "file_name",
        "utm_tile",
        "start_time",
        "end_time",
        "size_mb",
        "downloaded",
        "raw_exists",
        "known_from_manifest",
        "selected_for_download",
        "duplicate_filter_status",
        "preferred_file_name",
        "duplicate_reason",
        "local_path",
        "granule_id",
        "links",
    ]
    with config.report_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for granule in preview.granules:
            status, local_path = statuses.get(granule.identity, ("MATCHED", ""))
            raw_exists = local_complete_path(config, granule) is not None
            known_from_manifest = status == "SKIPPED_MANIFEST"
            downloaded = (
                "yes"
                if raw_exists
                or known_from_manifest
                or status in {"DOWNLOADED", "SKIPPED_EXISTING", "LOCAL_COMPLETE"}
                else "no"
            )
            writer.writerow(
                {
                    "status": status,
                    "file_name": granule.file_name,
                    "utm_tile": granule.utm_tile,
                    "start_time": granule.start_time,
                    "end_time": granule.end_time,
                    "size_mb": "" if granule.size_mb is None else f"{granule.size_mb:.3f}",
                    "downloaded": downloaded,
                    "raw_exists": "yes" if raw_exists else "no",
                    "known_from_manifest": "yes" if known_from_manifest else "no",
                    "selected_for_download": "yes" if granule.selected_for_download else "no",
                    "duplicate_filter_status": granule.duplicate_filter_status,
                    "preferred_file_name": granule.preferred_file_name,
                    "duplicate_reason": granule.duplicate_reason,
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
    stop_event: Any = None,
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
    skipped_manifest: List[DownloadGranule] = []
    manifest = read_download_manifest(effective_manifest_csv(config))
    for granule in preview.granules:
        if not granule.selected_for_download:
            statuses[granule.identity] = (EXCLUDED_OLDER_VERSION_STATUS, "")
            continue
        existing = existing_download_path(config, granule)
        if config.skip_existing and existing is not None:
            skipped.append(existing)
            statuses[granule.identity] = ("SKIPPED_EXISTING", str(existing))
        elif config.skip_manifest_existing and manifest_has_downloaded(manifest, granule):
            skipped_manifest.append(granule)
            manifest_row = manifest.get(manifest_key(granule), {})
            statuses[granule.identity] = (
                "SKIPPED_MANIFEST",
                str(manifest_row.get("local_path", "")),
            )
        else:
            to_download.append(granule)

    if to_download:
        ensure_authenticated(earthaccess)

    downloaded_files: List[Path] = []
    failures: List[Tuple[DownloadGranule, str]] = []
    total = len(to_download)
    if progress_callback is not None:
        progress_callback(0, total, "Starting download" if total else "No new files to download")

    stopped = False
    completed_attempts = 0
    for batch in chunks(to_download, config.batch_size):
        if stop_event is not None and stop_event.is_set():
            stopped = True
            for pending in to_download[completed_attempts:]:
                statuses[pending.identity] = ("CANCELLED", "")
            if progress_callback is not None:
                progress_callback(completed_attempts, total, "Download stop requested")
            break
        for granule in batch:
            move_incomplete_download(config, granule)
        if progress_callback is not None:
            if len(batch) == 1:
                message = f"Downloading {batch[0].file_name}"
            else:
                message = f"Downloading batch of {len(batch)} files"
            progress_callback(completed_attempts, total, message)
        try:
            paths = earthaccess.download(
                [granule.source for granule in batch],
                local_path=str(config.output_folder),
                threads=config.threads,
                show_progress=False,
            )
            normalized_paths = [Path(path) for path in paths or []]
            downloaded_files.extend(normalized_paths)
            for granule in batch:
                complete_path = local_complete_path(config, granule)
                if complete_path is not None:
                    statuses[granule.identity] = ("DOWNLOADED", str(complete_path))
                else:
                    statuses[granule.identity] = ("MISSING", "")
            message = (
                f"Downloaded {batch[0].file_name}"
                if len(batch) == 1
                else f"Finished batch of {len(batch)} files"
            )
        except Exception as exc:
            for granule in batch:
                complete_path = local_complete_path(config, granule)
                if complete_path is not None:
                    statuses[granule.identity] = ("DOWNLOADED", str(complete_path))
                else:
                    failures.append((granule, str(exc)))
                    statuses[granule.identity] = ("FAILED", str(exc))
            message = (
                f"FAILED: {batch[0].file_name}"
                if len(batch) == 1
                else f"FAILED batch of {len(batch)} files"
            )
        completed_attempts += len(batch)
        if progress_callback is not None:
            progress_callback(completed_attempts, total, message)

    missing_granules = update_statuses_from_local_files(config, preview, statuses)
    report_csv = write_download_report(config, preview, statuses)
    write_download_manifest(config, preview, statuses)
    write_download_workflow_manifest(config, preview, statuses)
    return DownloadResult(
        preview=preview,
        downloaded_files=downloaded_files,
        skipped_existing=skipped,
        skipped_manifest=skipped_manifest,
        failures=failures,
        missing_granules=missing_granules,
        stopped=stopped,
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
    print(f"Granules matched: {len(preview.granules)}")
    print(f"Selected for download: {len(preview.selected_granules)}")
    print(f"Excluded older versions: {len(preview.excluded_granules)}")
    print(
        "Selected known size: "
        f"{format_size(preview.selected_known_size_mb, preview.selected_missing_size_count)}"
    )
    if preview.excluded_granules:
        print(
            "Excluded known size: "
            f"{format_size(preview.excluded_known_size_mb, preview.excluded_missing_size_count)}"
        )
    print(f"Report CSV: {report_csv}")


def print_download_summary(result: DownloadResult) -> None:
    """Print a concise download summary for CLI users."""
    print(f"Granules matched: {len(result.preview.granules)}")
    print(f"Selected for download: {len(result.preview.selected_granules)}")
    print(f"Excluded older versions: {len(result.preview.excluded_granules)}")
    print(f"Accounted for: {result.complete_count}")
    print(f"Downloaded files: {len(result.downloaded_files)}")
    print(f"Skipped existing: {len(result.skipped_existing)}")
    print(f"Skipped manifest-known: {len(result.skipped_manifest)}")
    print(f"Failed files: {len(result.failures)}")
    print(f"Missing files: {len(result.missing_granules)}")
    if result.stopped:
        print("Download stopped before all files were attempted.")
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
        return 1 if result.failures or result.missing_granules or result.stopped else 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
