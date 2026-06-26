"""PIXC CMR preview and safe download helpers."""

from __future__ import annotations

import csv
import json
import math
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import unquote, urlparse

from swot_metadata import swot_product_rank

from .config import DEFAULT_COLLECTION_LABEL, DEFAULT_COLLECTION_SHORT_NAME


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PIXC_PROCESSING_DIR = PROJECT_ROOT / "PIXC_Processing"
DEFAULT_RAW_DOWNLOADS_DIR = DEFAULT_PIXC_PROCESSING_DIR / "01_raw_downloads"
DEFAULT_LOGS_DIR = DEFAULT_PIXC_PROCESSING_DIR / "00_logs"
DEFAULT_REPORT_CSV = DEFAULT_LOGS_DIR / "pixc_download_preview.csv"
DEFAULT_MANIFEST_CSV = DEFAULT_LOGS_DIR / "pixc_download_manifest.csv"
DEFAULT_EVENTS_CSV = DEFAULT_LOGS_DIR / "pixc_download_events.csv"

PROVIDER = "POCLOUD"
DEFAULT_MAX_GRANULES = 100
DEFAULT_THREADS = 4
DEFAULT_BATCH_SIZE = 5
CMR_PAGE_SIZE = 2000
EXCLUDED_OLDER_VERSION_STATUS = "EXCLUDED_OLDER_VERSION"
EXCLUDED_BY_USER_SELECTION_STATUS = "EXCLUDED_BY_USER_SELECTION"
AUTH_FAILED_STATUS = "AUTH_FAILED"

ProgressCallback = Callable[[int, int, str], None]
BBox = tuple[float, float, float, float]

_AUTHENTICATED = False
_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}$")
_UTM_RE = re.compile(r"^(?:UTM)?(?P<zone>\d{1,2})(?P<band>[C-HJ-NP-Xc-hj-np-x])$")
_PIXC_TILE_RE = re.compile(r"^(?P<number>\d{1,4})(?P<side>[LRFlrf])$")
_INTEGER_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class PixcTileId:
    """One SWOT PIXC along-track tile token with swath side."""

    tile_number: int
    swath_side: str

    @classmethod
    def parse(cls, value: str) -> "PixcTileId":
        """Parse a PIXC tile token such as 102R, 001L, or 1F."""
        text = str(value or "").strip().upper()
        match = _PIXC_TILE_RE.match(text)
        if not match:
            raise ValueError("Tile IDs must look like 102R, 001L, or 1F.")
        tile_number = int(match.group("number"))
        if tile_number <= 0:
            raise ValueError("PIXC tile number must be greater than zero.")
        return cls(tile_number=tile_number, swath_side=match.group("side").upper())

    @property
    def token(self) -> str:
        """Return the display token used in PIXC filenames."""
        return f"{self.tile_number:03d}{self.swath_side}"

    @property
    def cmr_token(self) -> str:
        """Return the compact token used in CMR pass/tile queries."""
        return f"{self.tile_number}{self.swath_side}"


@dataclass(frozen=True)
class PixcTrackFilter:
    """Optional CMR cycle/pass/tile filter for PIXC preview searches."""

    cycle_id: Optional[int] = None
    pass_id: Optional[int] = None
    tile_ids: tuple[PixcTileId, ...] = ()

    @classmethod
    def from_text(
        cls,
        *,
        cycle: str = "",
        pass_id: str = "",
        tiles: str = "",
    ) -> Optional["PixcTrackFilter"]:
        """Build a track filter from GUI text fields."""
        cycle_value = parse_optional_positive_int(cycle, "Cycle")
        pass_value = parse_optional_positive_int(pass_id, "Pass")
        tile_values = parse_tile_list(tiles)
        if cycle_value is None and pass_value is None and not tile_values:
            return None
        return validate_track_filter(
            cls(cycle_id=cycle_value, pass_id=pass_value, tile_ids=tuple(tile_values))
        )

    @property
    def has_any_filter(self) -> bool:
        """Return True when the filter narrows a CMR query."""
        return self.cycle_id is not None or self.pass_id is not None or bool(self.tile_ids)

    def display_text(self) -> str:
        """Return a compact human-readable track filter description."""
        parts: list[str] = []
        if self.cycle_id is not None:
            parts.append(f"cycle={self.cycle_id}")
        if self.pass_id is not None:
            parts.append(f"pass={self.pass_id}")
        if self.tile_ids:
            parts.append("tiles=" + ",".join(tile.token for tile in self.tile_ids))
        return "; ".join(parts)


@dataclass
class PixcDownloadConfig:
    """Runtime settings for PIXC CMR search/download."""

    collection_short_name: str = DEFAULT_COLLECTION_SHORT_NAME
    collection_version_label: str = DEFAULT_COLLECTION_LABEL
    output_folder: Path = DEFAULT_RAW_DOWNLOADS_DIR
    start_date: str = ""
    end_date: str = ""
    bbox: Optional[BBox] = None
    utm_tile: str = ""
    track_filter: Optional[PixcTrackFilter] = None
    reference_tiles: tuple[str, ...] = ()
    max_granules: int = DEFAULT_MAX_GRANULES
    report_csv: Path = DEFAULT_REPORT_CSV
    manifest_csv: Path = DEFAULT_MANIFEST_CSV
    events_csv: Path = DEFAULT_EVENTS_CSV
    skip_existing: bool = True
    skip_manifest_existing: bool = True
    threads: int = DEFAULT_THREADS
    batch_size: int = DEFAULT_BATCH_SIZE


@dataclass
class PixcDownloadQuery:
    """CMR query parameters derived from a PIXC config."""

    collection_short_name: str
    temporal: tuple[str, str]
    bbox: Optional[BBox] = None
    track_filter: Optional[PixcTrackFilter] = None
    reference_tiles: tuple[str, ...] = ()
    max_granules: int = DEFAULT_MAX_GRANULES


@dataclass
class PixcFilenameMetadata:
    """Product-version metadata parsed from a SWOT-like filename."""

    duplicate_key: tuple[str, ...]
    crid: str
    product_counter: str
    range_beginning: str
    range_ending: str
    cycle_id: Optional[int] = None
    pass_id: Optional[int] = None
    tile_id: str = ""
    swath_side: str = ""


@dataclass
class PixcDownloadGranule:
    """One matched PIXC granule normalized for preview/reporting."""

    identity: str
    file_name: str
    start_time: str = ""
    end_time: str = ""
    size_mb: Optional[float] = None
    cycle_id: Optional[int] = None
    pass_id: Optional[int] = None
    tile_id: str = ""
    swath_side: str = ""
    footprint: Optional[dict[str, Any]] = None
    links: list[str] = field(default_factory=list)
    selected_for_download: bool = True
    duplicate_filter_status: str = "selected"
    preferred_file_name: str = ""
    duplicate_reason: str = ""
    local_status: str = "MATCHED"
    local_path: str = ""
    source: Any = field(default=None, repr=False, compare=False)


@dataclass
class PixcDownloadPreview:
    """Search result before optional downloading."""

    query: PixcDownloadQuery
    granules: list[PixcDownloadGranule] = field(default_factory=list)
    total_hits: Optional[int] = None
    report_csv: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def selected_granules(self) -> list[PixcDownloadGranule]:
        """Return granules selected for download after product-version filtering."""
        return [granule for granule in self.granules if granule.selected_for_download]

    @property
    def excluded_granules(self) -> list[PixcDownloadGranule]:
        """Return remote matches excluded by product-version filtering."""
        return [granule for granule in self.granules if not granule.selected_for_download]

    @property
    def selected_known_size_mb(self) -> float:
        """Return known size for selected granules."""
        return sum(granule.size_mb or 0.0 for granule in self.selected_granules)

    @property
    def selected_missing_size_count(self) -> int:
        """Return selected granules without size metadata."""
        return sum(1 for granule in self.selected_granules if granule.size_mb is None)


@dataclass
class PixcDownloadResult:
    """Result from one PIXC download run."""

    preview: PixcDownloadPreview
    downloaded_files: list[Path] = field(default_factory=list)
    skipped_existing: list[Path] = field(default_factory=list)
    skipped_manifest: list[PixcDownloadGranule] = field(default_factory=list)
    failures: list[tuple[PixcDownloadGranule, str]] = field(default_factory=list)
    missing_granules: list[PixcDownloadGranule] = field(default_factory=list)
    stopped: bool = False
    report_csv: Optional[Path] = None
    manifest_csv: Optional[Path] = None
    statuses: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def all_complete(self) -> bool:
        """Return True when every selected granule is accounted for."""
        return not self.missing_granules


@dataclass(frozen=True)
class PixcDownloadedFile:
    """One project download inventory row from the manifest and raw folder."""

    file_name: str
    local_path: Path
    raw_exists: bool
    downloaded: bool
    last_status: str = ""
    size_mb: str = ""
    granule_id: str = ""
    last_seen: str = ""
    last_downloaded: str = ""
    source: str = ""


def import_earthaccess() -> Any:
    """Import earthaccess lazily with a helpful error."""
    try:
        import earthaccess  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "earthaccess is not installed. Install the existing SWOTFlow download "
            "dependencies before searching or downloading PIXC files."
        ) from exc
    return earthaccess


def authenticate(
    strategy: str = "all",
    persist: bool = False,
    earthaccess_module: Any = None,
) -> Any:
    """Authenticate with Earthdata through earthaccess."""
    global _AUTHENTICATED
    earthaccess = earthaccess_module or import_earthaccess()
    login = getattr(earthaccess, "login", None)
    if not callable(login):
        _AUTHENTICATED = True
        return None
    try:
        auth = login(strategy=strategy, persist=persist)
    except (TypeError, ValueError):
        auth = login(persist=persist)
    _AUTHENTICATED = True
    return auth


def ensure_authenticated(earthaccess_module: Any = None) -> None:
    """Authenticate once per Python process before downloading."""
    if not _AUTHENTICATED:
        authenticate(earthaccess_module=earthaccess_module)


def earthdata_auth_error_message(exc: Exception) -> str:
    """Return a user-facing Earthdata auth failure message."""
    raw_message = str(exc).strip() or exc.__class__.__name__
    lower_message = raw_message.lower()
    if "netrc" in lower_message or "earthdata" in lower_message or "login" in lower_message:
        return (
            "Earthdata authentication is not configured for this Python environment. "
            "SWOTFlow asked earthaccess to authenticate using its standard login strategy. "
            "If Earthdata asks for credentials, enter them in the terminal. You can also create "
            "%USERPROFILE%\\_netrc or run the existing SWOTFlow Earthdata login setup, "
            f"then retry. Original error: {raw_message}"
        )
    return raw_message


def validate_date_text(value: str, field_name: str) -> str:
    """Validate a date/datetime text value accepted by CMR."""
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    try:
        parse_datetime_for_compare(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date or datetime.") from exc
    return text


def parse_datetime_for_compare(value: str) -> datetime:
    """Parse ISO text into a timezone-naive UTC datetime for comparisons."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_optional_positive_int(value: str, field_name: str) -> Optional[int]:
    """Parse an optional positive integer from GUI text."""
    text = str(value or "").strip()
    if not text:
        return None
    if not _INTEGER_RE.match(text):
        raise ValueError(f"{field_name} must be a positive integer.")
    parsed = int(text)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return parsed


def parse_tile_list(value: str) -> list[PixcTileId]:
    """Parse a comma-separated PIXC tile list."""
    text = str(value or "").strip()
    if not text:
        return []
    tiles: list[PixcTileId] = []
    seen: set[str] = set()
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        tile = PixcTileId.parse(part)
        if tile.token in seen:
            continue
        seen.add(tile.token)
        tiles.append(tile)
    return tiles


def validate_track_filter(track_filter: Optional[PixcTrackFilter]) -> Optional[PixcTrackFilter]:
    """Validate CMR cycle/pass/tile search constraints."""
    if track_filter is None:
        return None
    if track_filter.pass_id is not None and track_filter.cycle_id is None:
        raise ValueError("Pass filters require exactly one cycle.")
    if track_filter.tile_ids and track_filter.cycle_id is None:
        raise ValueError("Tile filters require exactly one cycle.")
    if track_filter.tile_ids and track_filter.pass_id is None:
        raise ValueError("Tile filters require one pass because CMR nests tiles under passes.")
    return track_filter


def validate_bbox(value: Optional[Sequence[float]]) -> Optional[BBox]:
    """Validate a CMR bounding box ordered west, south, east, north."""
    if value is None:
        return None
    if len(value) != 4:
        raise ValueError("Bounding box must contain west, south, east, north.")
    west, south, east, north = [float(item) for item in value]
    if not -180.0 <= west <= 180.0 or not -180.0 <= east <= 180.0:
        raise ValueError("Bounding box longitudes must be between -180 and 180.")
    if not -90.0 <= south <= 90.0 or not -90.0 <= north <= 90.0:
        raise ValueError("Bounding box latitudes must be between -90 and 90.")
    if west >= east:
        raise ValueError("Bounding box west longitude must be smaller than east longitude.")
    if south >= north:
        raise ValueError("Bounding box south latitude must be smaller than north latitude.")
    return west, south, east, north


def parse_bbox_text(west: str, south: str, east: str, north: str) -> BBox:
    """Parse four bbox text fields into a validated CMR bbox."""
    try:
        bbox = validate_bbox((float(west), float(south), float(east), float(north)))
        if bbox is None:
            raise ValueError("Bounding box is required.")
        return bbox
    except ValueError as exc:
        raise ValueError(f"Invalid bounding box: {exc}") from exc


def normalize_reference_tile_names_for_download(value: object) -> tuple[str, ...]:
    """Normalize selected SWOT reference tile names for download config."""
    if value in (None, ""):
        return ()
    from .reference_tiles import load_pixc_reference_tiles, normalize_reference_tile_names

    names = normalize_reference_tile_names(value)
    if not names:
        return ()
    load_pixc_reference_tiles().require_tiles(names)
    return tuple(names)


def validate_config(config: PixcDownloadConfig) -> PixcDownloadQuery:
    """Validate config and return a normalized query."""
    start = validate_date_text(config.start_date, "Start date")
    end = validate_date_text(config.end_date, "End date")
    if parse_datetime_for_compare(start) > parse_datetime_for_compare(end):
        raise ValueError("Start date must be earlier than or equal to end date.")
    if int(config.max_granules) <= 0:
        raise ValueError("Max granules must be greater than zero.")
    return PixcDownloadQuery(
        collection_short_name=str(config.collection_short_name or DEFAULT_COLLECTION_SHORT_NAME),
        temporal=(start, end),
        bbox=validate_bbox(config.bbox),
        track_filter=validate_track_filter(config.track_filter),
        reference_tiles=normalize_reference_tile_names_for_download(config.reference_tiles),
        max_granules=int(config.max_granules),
    )


def normalize_utm_tile_name(value: str) -> str:
    """Return a normalized single UTM tile token such as UTM34M."""
    match = _UTM_RE.match(str(value or "").strip())
    if not match:
        raise ValueError("Use one UTM tile such as UTM34M.")
    zone = int(match.group("zone"))
    if not 1 <= zone <= 60:
        raise ValueError("UTM zone must be between 1 and 60.")
    return f"UTM{zone:02d}{match.group('band').upper()}"


def web_mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Convert one EPSG:3857 point to WGS84 lon/lat."""
    radius = 6378137.0
    lon = math.degrees(float(x) / radius)
    lat = math.degrees(2.0 * math.atan(math.exp(float(y) / radius)) - math.pi / 2.0)
    return max(-180.0, min(180.0, lon)), max(-90.0, min(90.0, lat))


def utm_tile_to_bbox(tile_name: str) -> BBox:
    """Return an approximate WGS84 bbox for one UTM display tile."""
    from utm_map_selector import load_display_geometry

    normalized = normalize_utm_tile_name(tile_name)
    geometry = load_display_geometry()
    tile = geometry.tiles.get(normalized)
    if tile is None:
        raise ValueError(f"Unknown UTM tile: {normalized}")
    minx, miny, maxx, maxy = tile.bounds
    west, south = web_mercator_to_lonlat(minx, miny)
    east, north = web_mercator_to_lonlat(maxx, maxy)
    return validate_bbox((round(west, 6), round(south, 6), round(east, 6), round(north, 6))) or (
        west,
        south,
        east,
        north,
    )


def bbox_to_text(bbox: Optional[BBox]) -> str:
    """Format a bbox for reports and status messages."""
    if bbox is None:
        return ""
    return ",".join(f"{value:.6f}" for value in bbox)


def track_filter_to_text(track_filter: Optional[PixcTrackFilter]) -> str:
    """Format a track filter for reports and status messages."""
    if track_filter is None:
        return ""
    return track_filter.display_text()


def reference_tiles_to_text(reference_tiles: Sequence[str]) -> str:
    """Format selected reference tiles for reports and status messages."""
    return ",".join(reference_tiles)


def cmr_track_query_params(track_filter: Optional[PixcTrackFilter]) -> dict[str, str]:
    """Build raw CMR query parameters for cycle/pass/tile filtering."""
    track_filter = validate_track_filter(track_filter)
    if track_filter is None:
        return {}
    params: dict[str, str] = {}
    if track_filter.cycle_id is not None:
        params["cycle[]"] = str(track_filter.cycle_id)
    if track_filter.pass_id is not None:
        params["passes[0][pass]"] = str(track_filter.pass_id)
        if track_filter.tile_ids:
            params["passes[0][tiles]"] = ",".join(tile.cmr_token for tile in track_filter.tile_ids)
    return params


def reference_tile_cmr_query_params(reference_tiles: Sequence[str], cycle_id: Optional[int]) -> dict[str, str]:
    """Build CMR cycle/pass/tile params for selected SWOT reference tiles."""
    if not reference_tiles or cycle_id is None:
        return {}
    from .reference_tiles import load_pixc_reference_tiles

    index = load_pixc_reference_tiles()
    grouped: dict[int, list[str]] = {}
    for tile in index.require_tiles(reference_tiles):
        grouped.setdefault(tile.pass_num, []).append(f"{tile.tile_num}{tile.tile_side}")
    params: dict[str, str] = {"cycle[]": str(cycle_id)}
    for index_value, (pass_num, tile_tokens) in enumerate(sorted(grouped.items())):
        params[f"passes[{index_value}][pass]"] = str(pass_num)
        params[f"passes[{index_value}][tiles]"] = ",".join(sorted(set(tile_tokens), key=tile_tokens.index))
    return params


def query_cmr_params(query: PixcDownloadQuery) -> dict[str, str]:
    """Return the track-related CMR params for a query."""
    cycle_id = query.track_filter.cycle_id if query.track_filter is not None else None
    reference_params = reference_tile_cmr_query_params(query.reference_tiles, cycle_id)
    if reference_params:
        return reference_params
    return cmr_track_query_params(query.track_filter)


def apply_query_params_to_granule_query(granule_query: Any, params: Mapping[str, str]) -> Any:
    """Attach raw CMR parameters to an earthaccess query when its API allows it."""
    if not params:
        return granule_query

    parameters = getattr(granule_query, "parameters", None)
    if callable(parameters):
        try:
            return parameters(**params)
        except TypeError:
            try:
                return parameters(params)
            except TypeError:
                pass

    for attribute in ("params", "_params", "query_params"):
        current = getattr(granule_query, attribute, None)
        if isinstance(current, dict):
            current.update(params)
            return granule_query

    return granule_query


def apply_track_filter_to_granule_query(granule_query: Any, query: PixcDownloadQuery) -> Any:
    """Attach track/reference parameters to an earthaccess query when possible."""
    return apply_query_params_to_granule_query(granule_query, query_cmr_params(query))


def search_kwargs(query: PixcDownloadQuery, count: int) -> dict[str, Any]:
    """Build earthaccess.search_data keyword arguments for PIXC."""
    kwargs: dict[str, Any] = {
        "short_name": query.collection_short_name,
        "provider": PROVIDER,
        "cloud_hosted": True,
        "downloadable": True,
        "temporal": query.temporal,
        "count": int(count),
    }
    if query.bbox is not None:
        kwargs["bounding_box"] = query.bbox
    kwargs.update(query_cmr_params(query))
    return kwargs


def build_earthaccess_granule_query(earthaccess: Any, query: PixcDownloadQuery) -> Any:
    """Build an earthaccess granule query without raster-style filename filters."""
    granule_query = earthaccess.granule_query()
    granule_query = granule_query.short_name(query.collection_short_name)
    granule_query = granule_query.provider(PROVIDER)
    granule_query = granule_query.cloud_hosted(True)
    downloadable = getattr(granule_query, "downloadable", None)
    if callable(downloadable):
        granule_query = downloadable(True)
    granule_query = granule_query.temporal(query.temporal[0], query.temporal[1])
    if query.bbox is not None:
        bounding_box = getattr(granule_query, "bounding_box", None)
        if not callable(bounding_box):
            raise RuntimeError("earthaccess.granule_query does not support bounding_box.")
        try:
            granule_query = bounding_box(*query.bbox)
        except TypeError:
            granule_query = bounding_box(query.bbox)
    return apply_track_filter_to_granule_query(granule_query, query)


def wrap_cmr_page_items(earthaccess: Any, items: Sequence[Any]) -> list[Any]:
    """Convert raw CMR page items to earthaccess DataGranule objects when available."""
    data_granule = getattr(earthaccess, "DataGranule", None)
    if data_granule is None:
        results_module = getattr(earthaccess, "results", None)
        data_granule = getattr(results_module, "DataGranule", None)
    if data_granule is None:
        return list(items)
    return [data_granule(item, cloud_hosted=True) for item in items]


def run_search_paged(
    earthaccess: Any,
    query: PixcDownloadQuery,
    progress_callback: Optional[ProgressCallback] = None,
    stop_event: Any = None,
) -> tuple[list[Any], Optional[int]]:
    """Call CMR page by page so broad PIXC searches can report hit counts."""
    if not hasattr(earthaccess, "granule_query"):
        raise RuntimeError("earthaccess.granule_query is not available.")

    granule_query = build_earthaccess_granule_query(earthaccess, query)
    total_hits = int(granule_query.hits())
    limit = min(total_hits, query.max_granules)
    if progress_callback is not None:
        progress_callback(0, limit, f"CMR found {total_hits} matching PIXC granule(s)")
    if limit <= 0:
        return [], total_hits

    url = granule_query._build_url()
    headers = dict(getattr(granule_query, "headers", {}) or {})
    session = granule_query.session
    query_params = query_cmr_params(query)
    results: list[Any] = []

    while len(results) < limit:
        if stop_event is not None and stop_event.is_set():
            break
        page_size = min(CMR_PAGE_SIZE, limit - len(results))
        request_params: dict[str, Any] = {"page_size": page_size}
        request_params.update(query_params)
        response = session.get(url, headers=headers, params=request_params)
        try:
            response.raise_for_status()
        except Exception as exc:
            response_text = str(exc)
            if getattr(exc, "response", None) is not None:
                response_text = str(getattr(exc.response, "text", response_text))
            raise RuntimeError(response_text) from exc

        document = response.json()
        latest = list(document.get("items", []) or [])
        if not latest:
            break
        results.extend(wrap_cmr_page_items(earthaccess, latest))
        if progress_callback is not None:
            progress_callback(
                min(len(results), limit),
                limit,
                f"Retrieved CMR metadata for {min(len(results), limit)}/{limit} preview granule(s)",
            )

        search_after = response.headers.get("cmr-search-after")
        if search_after:
            headers["cmr-search-after"] = search_after
        elif len(latest) >= page_size and len(results) < limit:
            break
        if len(latest) < page_size:
            break

    return results[:limit], total_hits


def run_search(earthaccess: Any, query: PixcDownloadQuery) -> tuple[list[Any], Optional[int]]:
    """Call earthaccess.search_data and return a list."""
    return list(earthaccess.search_data(**search_kwargs(query, query.max_granules))), None


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


def granule_links(granule: Any) -> list[str]:
    """Return external data links for a granule."""
    data_links = getattr(granule, "data_links", None)
    if callable(data_links):
        try:
            links = data_links(access="external")
        except TypeError:
            links = data_links()
        return [str(link) for link in links if link]

    umm = mapping_value(granule, "umm") or {}
    links: list[str] = []
    for related in mapping_value(umm, "RelatedUrls", []) or []:
        related_type = str(mapping_value(related, "Type", "")).upper()
        url = mapping_value(related, "URL")
        if url and "GET" in related_type:
            links.append(str(url))
    return links


def link_file_name(link: str) -> str:
    """Return the basename from one URL."""
    parsed = urlparse(str(link))
    return Path(unquote(parsed.path)).name


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
        name = str(granule_ur)
        return name if Path(name).suffix else f"{name}.nc"
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


def granule_temporal(granule: Any) -> tuple[str, str]:
    """Return beginning/end timestamps from CMR metadata."""
    umm = mapping_value(granule, "umm") or {}
    temporal = mapping_value(umm, "TemporalExtent") or {}
    range_datetime = mapping_value(temporal, "RangeDateTime") or {}
    start = mapping_value(range_datetime, "BeginningDateTime") or ""
    end = mapping_value(range_datetime, "EndingDateTime") or ""
    return str(start), str(end)


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


def dedupe_granules(granules: Iterable[Any]) -> list[Any]:
    """Deduplicate overlapping search results while keeping stable order."""
    unique: list[Any] = []
    seen: set[str] = set()
    for granule in granules:
        identity = granule_identity(granule)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(granule)
    return unique


def parse_pixc_track_tokens(tokens: Sequence[str]) -> tuple[Optional[int], Optional[int], str, str]:
    """Parse cycle/pass/tile fields from an official-style PIXC filename token list."""
    if len(tokens) < 7:
        return None, None, "", ""
    if [token.upper() for token in tokens[:4]] != ["SWOT", "L2", "HR", "PIXC"]:
        return None, None, "", ""
    if not _INTEGER_RE.match(tokens[4]) or not _INTEGER_RE.match(tokens[5]):
        return None, None, "", ""
    try:
        tile = PixcTileId.parse(tokens[6])
    except ValueError:
        return int(tokens[4]), int(tokens[5]), "", ""
    return int(tokens[4]), int(tokens[5]), tile.token, tile.swath_side


def parse_pixc_filename_metadata(file_name: str | Path) -> Optional[PixcFilenameMetadata]:
    """Parse generic SWOT product-version fields from a PIXC-style filename."""
    stem = Path(str(file_name)).stem
    tokens = stem.split("_")
    timestamp_indices = [index for index, token in enumerate(tokens) if _TIMESTAMP_RE.match(token)]
    if len(timestamp_indices) < 2:
        return None
    start_index = timestamp_indices[-2]
    end_index = timestamp_indices[-1]
    crid_index = end_index + 1
    counter_index = end_index + 2
    if counter_index >= len(tokens):
        return None
    crid = tokens[crid_index]
    product_counter = tokens[counter_index]
    if not crid or not product_counter:
        return None
    duplicate_key = tuple(tokens[:crid_index] + tokens[counter_index + 1 :])
    cycle_id, pass_id, tile_id, swath_side = parse_pixc_track_tokens(tokens)
    return PixcFilenameMetadata(
        duplicate_key=duplicate_key,
        crid=crid,
        product_counter=product_counter,
        range_beginning=tokens[start_index],
        range_ending=tokens[end_index],
        cycle_id=cycle_id,
        pass_id=pass_id,
        tile_id=tile_id,
        swath_side=swath_side,
    )


def mapping_sequence(container: Any, key: str) -> list[Any]:
    """Return a mapping value as a list."""
    value = mapping_value(container, key, [])
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def mapping_float(container: Any, *keys: str) -> Optional[float]:
    """Return the first parseable float for one of several mapping keys."""
    for key in keys:
        value = mapping_value(container, key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def bbox_polygon(west: float, south: float, east: float, north: float) -> dict[str, Any]:
    """Convert a bbox to GeoJSON polygon geometry."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


def points_polygon(points: Sequence[Any]) -> Optional[dict[str, Any]]:
    """Convert CMR point records to a GeoJSON polygon geometry."""
    coordinates: list[list[float]] = []
    for point in points:
        lon = mapping_float(point, "Longitude", "longitude", "lon")
        lat = mapping_float(point, "Latitude", "latitude", "lat")
        if lon is None or lat is None:
            continue
        coordinates.append([lon, lat])
    if len(coordinates) < 3:
        return None
    if coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0])
    return {"type": "Polygon", "coordinates": [coordinates]}


def granule_footprint(granule: Any) -> Optional[dict[str, Any]]:
    """Return GeoJSON-like CMR spatial geometry when available."""
    umm = mapping_value(granule, "umm") or {}
    spatial_extent = mapping_value(umm, "SpatialExtent") or {}
    horizontal = mapping_value(spatial_extent, "HorizontalSpatialDomain") or {}
    geometry = mapping_value(horizontal, "Geometry") or {}

    for polygon in mapping_sequence(geometry, "GPolygons"):
        boundary = mapping_value(polygon, "Boundary") or {}
        points = mapping_sequence(boundary, "Points") or mapping_sequence(polygon, "Points")
        footprint = points_polygon(points)
        if footprint is not None:
            return footprint

    rectangles = mapping_sequence(geometry, "BoundingRectangles") or mapping_sequence(
        horizontal,
        "BoundingRectangles",
    )
    for rectangle in rectangles:
        west = mapping_float(rectangle, "WestBoundingCoordinate", "west")
        south = mapping_float(rectangle, "SouthBoundingCoordinate", "south")
        east = mapping_float(rectangle, "EastBoundingCoordinate", "east")
        north = mapping_float(rectangle, "NorthBoundingCoordinate", "north")
        if None in (west, south, east, north):
            continue
        try:
            bbox = validate_bbox((west, south, east, north))
        except ValueError:
            continue
        if bbox is None:
            continue
        return bbox_polygon(*bbox)

    return None


def footprint_to_text(footprint: Optional[Mapping[str, Any]]) -> str:
    """Serialize footprint geometry compactly for CSV reports."""
    if not footprint:
        return ""
    return json.dumps(footprint, separators=(",", ":"), sort_keys=True)


def granule_preview_feature(granule: PixcDownloadGranule) -> Optional[dict[str, Any]]:
    """Return a GeoJSON feature for a preview granule footprint."""
    if not granule.footprint:
        return None
    granule_id = manifest_key(granule)
    return {
        "type": "Feature",
        "id": granule_id,
        "properties": {
            "granule_id": granule_id,
            "file_name": granule.file_name,
            "cycle_id": "" if granule.cycle_id is None else str(granule.cycle_id),
            "pass_id": "" if granule.pass_id is None else str(granule.pass_id),
            "tile_id": granule.tile_id,
            "status": granule.local_status,
        },
        "geometry": granule.footprint,
    }


def normalize_granule(granule: Any) -> PixcDownloadGranule:
    """Convert one earthaccess DataGranule into a PIXC preview row."""
    file_name = granule_file_name(granule)
    start_time, end_time = granule_temporal(granule)
    metadata = parse_pixc_filename_metadata(file_name)
    return PixcDownloadGranule(
        identity=granule_identity(granule),
        file_name=file_name,
        start_time=start_time,
        end_time=end_time,
        size_mb=granule_size_mb(granule),
        cycle_id=None if metadata is None else metadata.cycle_id,
        pass_id=None if metadata is None else metadata.pass_id,
        tile_id="" if metadata is None else metadata.tile_id,
        swath_side="" if metadata is None else metadata.swath_side,
        footprint=granule_footprint(granule),
        links=granule_links(granule),
        source=granule,
    )


def product_preference_key(granule: PixcDownloadGranule) -> tuple[int, int, int, int, int, str]:
    """Return the product-version preference key for one granule."""
    metadata = parse_pixc_filename_metadata(granule.file_name)
    if metadata is None:
        return (0, -1, -1, -1, -1, granule.file_name.lower())
    return (*swot_product_rank(metadata.crid, metadata.product_counter), granule.file_name.lower())


def apply_product_version_filter(granules: Sequence[PixcDownloadGranule]) -> None:
    """Mark older product versions as excluded while preserving all preview rows."""
    grouped: dict[tuple[str, ...], list[PixcDownloadGranule]] = {}
    for granule in granules:
        granule.selected_for_download = True
        granule.duplicate_filter_status = "selected"
        granule.preferred_file_name = ""
        granule.duplicate_reason = ""
        metadata = parse_pixc_filename_metadata(granule.file_name)
        if metadata is None:
            granule.duplicate_filter_status = "selected_unparsed"
            continue
        grouped.setdefault(metadata.duplicate_key, []).append(granule)

    for group in grouped.values():
        if len(group) == 1:
            group[0].duplicate_filter_status = "selected_single_version"
            continue
        preferred = max(group, key=product_preference_key)
        for granule in group:
            granule.preferred_file_name = preferred.file_name
            if granule is preferred:
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


def granule_matches_track_filter(
    granule: PixcDownloadGranule,
    track_filter: Optional[PixcTrackFilter],
) -> bool:
    """Return True when parsed granule metadata matches the optional track filter."""
    if track_filter is None:
        return True
    if (
        track_filter.cycle_id is not None
        and granule.cycle_id is not None
        and granule.cycle_id != track_filter.cycle_id
    ):
        return False
    if (
        track_filter.pass_id is not None
        and granule.pass_id is not None
        and granule.pass_id != track_filter.pass_id
    ):
        return False
    if track_filter.tile_ids:
        accepted = {tile.token for tile in track_filter.tile_ids}
        if granule.tile_id and granule.tile_id not in accepted:
            return False
    return True


def granule_reference_tile_name(granule: PixcDownloadGranule) -> str:
    """Return the reference tile name implied by parsed PIXC filename metadata."""
    if granule.pass_id is None or not granule.tile_id:
        return ""
    return f"{granule.pass_id:03d}_{granule.tile_id}"


def granule_matches_reference_tiles(
    granule: PixcDownloadGranule,
    reference_tiles: Sequence[str],
) -> bool:
    """Return True when parsed granule metadata matches selected reference tiles."""
    if not reference_tiles:
        return True
    return granule_reference_tile_name(granule) in set(reference_tiles)


def non_download_status(granule: PixcDownloadGranule) -> str:
    """Return a status for preview rows excluded before download."""
    if granule.duplicate_filter_status == "excluded_older_version":
        return EXCLUDED_OLDER_VERSION_STATUS
    return EXCLUDED_BY_USER_SELECTION_STATUS


def apply_preview_granule_selection(
    preview: PixcDownloadPreview,
    selected_ids: Iterable[str],
) -> int:
    """Limit a preview to selected granule IDs and return the selected count."""
    selected = {str(item) for item in selected_ids if str(item)}
    if not selected:
        return 0
    selected_count = 0
    for granule in preview.granules:
        key = manifest_key(granule)
        if key in selected:
            if granule.duplicate_filter_status != "excluded_older_version":
                granule.selected_for_download = True
                selected_count += 1
            granule.local_status = "MATCHED" if granule.selected_for_download else non_download_status(granule)
        else:
            if granule.selected_for_download:
                granule.selected_for_download = False
                granule.local_status = EXCLUDED_BY_USER_SELECTION_STATUS
    return selected_count


def search_matching_granules(
    config: PixcDownloadConfig,
    earthaccess_module: Any = None,
    progress_callback: Optional[ProgressCallback] = None,
    stop_event: Any = None,
) -> tuple[list[Any], PixcDownloadQuery, Optional[int]]:
    """Search CMR for matching PIXC granules."""
    earthaccess = earthaccess_module or import_earthaccess()
    query = validate_config(config)
    if progress_callback is not None:
        progress_callback(0, query.max_granules, "Searching CMR for matching PIXC granules")
    try:
        granules, total_hits = run_search_paged(
            earthaccess,
            query,
            progress_callback=progress_callback,
            stop_event=stop_event,
        )
    except Exception:
        if progress_callback is not None:
            progress_callback(0, query.max_granules, "CMR paged search unavailable; using earthaccess search_data")
        granules, total_hits = run_search(earthaccess, query)

    unique = dedupe_granules(granules)[: query.max_granules]
    if progress_callback is not None:
        progress_callback(len(unique), query.max_granules, f"Matched {len(unique)} preview granule(s)")
    return unique, query, total_hits


def preview_pixc_granules(
    config: PixcDownloadConfig,
    earthaccess_module: Any = None,
    progress_callback: Optional[ProgressCallback] = None,
    stop_event: Any = None,
) -> PixcDownloadPreview:
    """Search CMR and normalize matching PIXC granules for display/reporting."""
    raw_granules, query, total_hits = search_matching_granules(
        config,
        earthaccess_module=earthaccess_module,
        progress_callback=progress_callback,
        stop_event=stop_event,
    )
    granules: list[PixcDownloadGranule] = []
    total_raw = len(raw_granules)
    for index, raw_granule in enumerate(raw_granules, start=1):
        granules.append(normalize_granule(raw_granule))
        if progress_callback is not None and (index == total_raw or index % 500 == 0):
            progress_callback(index, total_raw, f"Normalized CMR metadata for {index}/{total_raw} granule(s)")
    if query.track_filter is not None:
        granules = [
            granule
            for granule in granules
            if granule_matches_track_filter(granule, query.track_filter)
        ]
    if query.reference_tiles:
        selected_reference_tiles = set(query.reference_tiles)
        granules = [
            granule
            for granule in granules
            if granule_matches_reference_tiles(granule, selected_reference_tiles)
        ]
    granules = sorted(granules, key=lambda item: item.file_name.lower())
    apply_product_version_filter(granules)

    warnings_list: list[str] = []
    if total_hits is not None and total_hits > len(granules):
        warnings_list.append(
            f"CMR found {total_hits} match(es); preview contains {len(granules)} after limits and local filters."
        )
    if query.bbox is None and query.track_filter is None and not query.reference_tiles:
        warnings_list.append("No spatial filter is active. Keep max_granules low for date-only previews.")
    elif query.bbox is None and (query.track_filter is not None or query.reference_tiles):
        warnings_list.append("No bbox is active; preview is limited by the cycle/pass/tile filter and max_granules.")
    if query.reference_tiles and (query.track_filter is None or query.track_filter.cycle_id is None):
        warnings_list.append(
            "Reference tiles are filtered locally after date/spatial search because no explicit cycle is set."
        )

    preview = PixcDownloadPreview(
        query=query,
        granules=granules,
        total_hits=total_hits,
        warnings=warnings_list,
    )
    statuses = preview_statuses_from_existing(config, preview)
    apply_statuses(preview, statuses)
    preview.report_csv = write_download_report(config, preview, statuses)
    return preview


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


def existing_download_path(config: PixcDownloadConfig, granule: PixcDownloadGranule) -> Optional[Path]:
    """Return a complete local file path for a granule when present."""
    if not granule.file_name:
        return None
    candidate = config.output_folder / granule.file_name
    if not candidate.exists():
        return None
    return candidate if file_appears_complete(candidate, granule.size_mb) else None


def next_incomplete_path(path: Path) -> Path:
    """Return an unused sidecar path for an incomplete local download."""
    candidate = path.with_name(f"{path.name}.incomplete")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.incomplete{counter}")
        counter += 1
    return candidate


def move_incomplete_download(config: PixcDownloadConfig, granule: PixcDownloadGranule) -> Optional[Path]:
    """Move a clearly incomplete local file aside before retrying a download."""
    if not granule.file_name:
        return None
    candidate = config.output_folder / granule.file_name
    if not candidate.exists() or file_appears_complete(candidate, granule.size_mb):
        return None
    target = next_incomplete_path(candidate)
    candidate.replace(target)
    return target


def chunks(items: Sequence[PixcDownloadGranule], size: int) -> Iterable[Sequence[PixcDownloadGranule]]:
    """Yield fixed-size chunks from a sequence."""
    chunk_size = max(1, int(size or 1))
    for start in range(0, len(items), chunk_size):
        yield items[start : start + chunk_size]


def manifest_key(granule: PixcDownloadGranule) -> str:
    """Return the stable manifest key for one granule."""
    return granule.identity or granule.file_name


def read_csv_rows(path: Path, key_column: str) -> dict[str, dict[str, str]]:
    """Read a CSV into rows keyed by one column."""
    if not path.exists():
        return {}
    rows: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            key = row.get(key_column) or row.get("file_name") or ""
            if key:
                rows[key] = {column: str(value or "") for column, value in row.items()}
    return rows


def read_download_manifest(path: Path) -> dict[str, dict[str, str]]:
    """Load the standalone PIXC download manifest."""
    return read_csv_rows(path, "granule_id")


def manifest_row_downloaded(row: Mapping[str, str] | None) -> bool:
    """Return True when a manifest row records a successful previous download."""
    if not row:
        return False
    return str(row.get("downloaded", "")).strip().lower() in {"yes", "true", "1"}


def manifest_has_downloaded(
    manifest: Mapping[str, Mapping[str, str]],
    granule: PixcDownloadGranule,
) -> bool:
    """Return True when a granule is already known as downloaded in the manifest."""
    return manifest_row_downloaded(manifest.get(manifest_key(granule)))


def preview_statuses_from_existing(
    config: PixcDownloadConfig,
    preview: PixcDownloadPreview,
) -> dict[str, tuple[str, str]]:
    """Return preview statuses from local raw files and the standalone manifest."""
    statuses: dict[str, tuple[str, str]] = {}
    manifest = read_download_manifest(config.manifest_csv)
    for granule in preview.granules:
        key = manifest_key(granule)
        if not granule.selected_for_download:
            statuses[key] = (non_download_status(granule), "")
            continue
        existing = existing_download_path(config, granule)
        if existing is not None:
            status = "SKIPPED_EXISTING" if config.skip_existing else "LOCAL_COMPLETE"
            statuses[key] = (status, str(existing))
            continue
        if config.skip_manifest_existing and manifest_has_downloaded(manifest, granule):
            manifest_row = manifest.get(key, {})
            statuses[key] = ("SKIPPED_MANIFEST", str(manifest_row.get("local_path", "")))
        else:
            statuses[key] = ("MATCHED", "")
    return statuses


def apply_statuses(
    preview: PixcDownloadPreview,
    statuses: Mapping[str, tuple[str, str]],
) -> None:
    """Store local status text on preview granules for GUI rendering."""
    for granule in preview.granules:
        status, local_path = statuses.get(manifest_key(granule), statuses.get(granule.identity, ("MATCHED", "")))
        granule.local_status = status
        granule.local_path = local_path


EVENT_COLUMNS = [
    "timestamp",
    "level",
    "event",
    "message",
    "granule_id",
    "file_name",
    "local_path",
]


def append_download_event(
    config: PixcDownloadConfig,
    level: str,
    event: str,
    message: str,
    granule: PixcDownloadGranule | None = None,
    local_path: str | Path = "",
) -> Path:
    """Append one chronological PIXC download event to the project log."""
    path = config.events_csv
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVENT_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": timestamp,
                "level": str(level or "INFO").upper(),
                "event": str(event or ""),
                "message": str(message or ""),
                "granule_id": manifest_key(granule) if granule is not None else "",
                "file_name": granule.file_name if granule is not None else "",
                "local_path": str(local_path or ""),
            }
        )
    return path


REPORT_COLUMNS = [
    "status",
    "file_name",
    "start_time",
    "end_time",
    "size_mb",
    "cycle_id",
    "pass_id",
    "tile_id",
    "swath_side",
    "downloaded",
    "raw_exists",
    "known_from_manifest",
    "selected_for_download",
    "duplicate_filter_status",
    "preferred_file_name",
    "duplicate_reason",
    "local_path",
    "granule_id",
    "collection_short_name",
    "bbox",
    "track_filter",
    "reference_tiles",
    "cmr_total_hits",
    "footprint",
    "links",
]


def write_rows(path: Path, rows: Iterable[Mapping[str, Any]], fieldnames: Sequence[str]) -> Path:
    """Write simple standalone CSV rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    return path


def write_download_report(
    config: PixcDownloadConfig,
    preview: PixcDownloadPreview,
    statuses: Optional[Mapping[str, tuple[str, str]]] = None,
) -> Path:
    """Write the standalone PIXC preview/download report CSV."""
    statuses = statuses if statuses is not None else preview_statuses_from_existing(config, preview)
    apply_statuses(preview, statuses)
    rows = []
    for granule in preview.granules:
        key = manifest_key(granule)
        status, local_path = statuses.get(key, ("MATCHED", ""))
        raw_path = existing_download_path(config, granule)
        raw_exists = raw_path is not None
        known_from_manifest = status == "SKIPPED_MANIFEST"
        downloaded = (
            raw_exists
            or known_from_manifest
            or status in {"DOWNLOADED", "SKIPPED_EXISTING", "LOCAL_COMPLETE"}
        )
        rows.append(
            {
                "status": status,
                "file_name": granule.file_name,
                "start_time": granule.start_time,
                "end_time": granule.end_time,
                "size_mb": "" if granule.size_mb is None else f"{granule.size_mb:.3f}",
                "cycle_id": "" if granule.cycle_id is None else str(granule.cycle_id),
                "pass_id": "" if granule.pass_id is None else str(granule.pass_id),
                "tile_id": granule.tile_id,
                "swath_side": granule.swath_side,
                "downloaded": "yes" if downloaded else "no",
                "raw_exists": "yes" if raw_exists else "no",
                "known_from_manifest": "yes" if known_from_manifest else "no",
                "selected_for_download": "yes" if granule.selected_for_download else "no",
                "duplicate_filter_status": granule.duplicate_filter_status,
                "preferred_file_name": granule.preferred_file_name,
                "duplicate_reason": granule.duplicate_reason,
                "local_path": str(raw_path or local_path),
                "granule_id": key,
                "collection_short_name": preview.query.collection_short_name,
                "bbox": bbox_to_text(preview.query.bbox),
                "track_filter": track_filter_to_text(preview.query.track_filter),
                "reference_tiles": reference_tiles_to_text(preview.query.reference_tiles),
                "cmr_total_hits": "" if preview.total_hits is None else str(preview.total_hits),
                "footprint": footprint_to_text(granule.footprint),
                "links": " ".join(granule.links),
            }
        )
    return write_rows(config.report_csv, rows, REPORT_COLUMNS)


MANIFEST_COLUMNS = [
    "granule_id",
    "file_name",
    "start_time",
    "end_time",
    "size_mb",
    "cycle_id",
    "pass_id",
    "tile_id",
    "swath_side",
    "collection_short_name",
    "bbox",
    "track_filter",
    "reference_tiles",
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


def write_download_manifest(
    config: PixcDownloadConfig,
    preview: PixcDownloadPreview,
    statuses: Mapping[str, tuple[str, str]],
) -> Path:
    """Merge one PIXC run into the cumulative standalone download manifest."""
    rows = read_download_manifest(config.manifest_csv)
    timestamp = datetime.now().replace(microsecond=0).isoformat()
    for granule in preview.granules:
        key = manifest_key(granule)
        existing = rows.get(key, {})
        status, local_path = statuses.get(key, ("MATCHED", ""))
        raw_path = existing_download_path(config, granule)
        raw_exists = raw_path is not None
        status_downloaded = status in {"DOWNLOADED", "SKIPPED_EXISTING", "LOCAL_COMPLETE", "SKIPPED_MANIFEST"}
        downloaded = status_downloaded or manifest_row_downloaded(existing) or raw_exists
        last_downloaded = existing.get("last_downloaded", "")
        if status in {"DOWNLOADED", "SKIPPED_EXISTING", "LOCAL_COMPLETE"}:
            last_downloaded = timestamp
        rows[key] = {
            "granule_id": key,
            "file_name": granule.file_name,
            "start_time": granule.start_time,
            "end_time": granule.end_time,
            "size_mb": "" if granule.size_mb is None else f"{granule.size_mb:.3f}",
            "cycle_id": "" if granule.cycle_id is None else str(granule.cycle_id),
            "pass_id": "" if granule.pass_id is None else str(granule.pass_id),
            "tile_id": granule.tile_id,
            "swath_side": granule.swath_side,
            "collection_short_name": preview.query.collection_short_name,
            "bbox": bbox_to_text(preview.query.bbox),
            "track_filter": track_filter_to_text(preview.query.track_filter),
            "reference_tiles": reference_tiles_to_text(preview.query.reference_tiles),
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
    return write_rows(config.manifest_csv, rows.values(), MANIFEST_COLUMNS)


def manifest_row_local_path(raw_folder: Path, row: Mapping[str, str]) -> Optional[Path]:
    """Return the local path represented by one manifest row."""
    local_path = str(row.get("local_path", "") or "").strip()
    file_name = str(row.get("file_name", "") or "").strip()
    if local_path:
        path = Path(local_path)
        return path if path.is_absolute() else raw_folder / path
    if file_name:
        return raw_folder / file_name
    return None


def manifest_row_inventory_status(row: Mapping[str, str], raw_exists: bool) -> str:
    """Return a display status for one manifest inventory row."""
    status = str(row.get("last_status", "") or "").strip()
    if manifest_row_downloaded(row) and not raw_exists:
        return "MISSING_LOCAL_FILE"
    if raw_exists and not status:
        return "LOCAL_FILE"
    return status or "MANIFEST"


def list_downloaded_pixc_files(raw_folder: str | Path, manifest_csv: str | Path) -> list[PixcDownloadedFile]:
    """Return downloaded/local PIXC files known to a project."""
    raw_path = Path(raw_folder)
    manifest_path = Path(manifest_csv)
    manifest = read_download_manifest(manifest_path)
    inventory: dict[str, PixcDownloadedFile] = {}

    for key, row in manifest.items():
        local_path = manifest_row_local_path(raw_path, row)
        if local_path is None:
            continue
        raw_exists = local_path.exists() and local_path.is_file()
        downloaded = manifest_row_downloaded(row)
        status = manifest_row_inventory_status(row, raw_exists)
        include = downloaded or raw_exists or status in {
            "DOWNLOADED",
            "SKIPPED_EXISTING",
            "LOCAL_COMPLETE",
            "SKIPPED_MANIFEST",
        }
        if not include:
            continue
        file_name = str(row.get("file_name", "") or local_path.name)
        inventory[file_name.lower()] = PixcDownloadedFile(
            file_name=file_name,
            local_path=local_path,
            raw_exists=raw_exists,
            downloaded=downloaded,
            last_status=status,
            size_mb=str(row.get("size_mb", "") or ""),
            granule_id=str(row.get("granule_id", "") or key),
            last_seen=str(row.get("last_seen", "") or ""),
            last_downloaded=str(row.get("last_downloaded", "") or ""),
            source="manifest",
        )

    if raw_path.exists():
        for pattern in ("*.nc", "*.nc4"):
            for file_path in sorted(raw_path.glob(pattern), key=lambda item: item.name.lower()):
                key = file_path.name.lower()
                existing = inventory.get(key)
                if existing is not None:
                    inventory[key] = PixcDownloadedFile(
                        file_name=existing.file_name,
                        local_path=file_path,
                        raw_exists=True,
                        downloaded=existing.downloaded,
                        last_status=existing.last_status if existing.last_status != "MISSING_LOCAL_FILE" else "LOCAL_FILE",
                        size_mb=existing.size_mb,
                        granule_id=existing.granule_id,
                        last_seen=existing.last_seen,
                        last_downloaded=existing.last_downloaded,
                        source=existing.source,
                    )
                    continue
                inventory[key] = PixcDownloadedFile(
                    file_name=file_path.name,
                    local_path=file_path,
                    raw_exists=True,
                    downloaded=False,
                    last_status="LOCAL_FILE_UNTRACKED",
                    size_mb="",
                    granule_id="",
                    last_seen="",
                    last_downloaded="",
                    source="raw_folder",
                )

    return sorted(
        inventory.values(),
        key=lambda item: (
            not item.raw_exists,
            item.file_name.lower(),
        ),
    )


def update_statuses_from_local_files(
    config: PixcDownloadConfig,
    preview: PixcDownloadPreview,
    statuses: dict[str, tuple[str, str]],
) -> list[PixcDownloadGranule]:
    """Mark selected preview granules according to current local completeness."""
    missing: list[PixcDownloadGranule] = []
    for granule in preview.granules:
        key = manifest_key(granule)
        complete_path = existing_download_path(config, granule)
        current_status, current_path = statuses.get(key, ("", ""))
        if not granule.selected_for_download or current_status in {
            EXCLUDED_OLDER_VERSION_STATUS,
            EXCLUDED_BY_USER_SELECTION_STATUS,
        }:
            statuses[key] = (non_download_status(granule), current_path)
            continue
        if complete_path is not None:
            if current_status in {"DOWNLOADED", "SKIPPED_EXISTING"}:
                statuses[key] = (current_status, str(complete_path))
            else:
                statuses[key] = ("LOCAL_COMPLETE", str(complete_path))
            continue
        if current_status == "SKIPPED_MANIFEST":
            statuses[key] = (current_status, current_path)
            continue
        missing.append(granule)
        if current_status in {"FAILED", "CANCELLED"}:
            statuses[key] = (current_status, current_path)
        elif current_status:
            statuses[key] = ("MISSING", current_path)
        else:
            statuses[key] = ("MISSING", "")
    return missing


def download_pixc_granules(
    config: PixcDownloadConfig,
    preview: Optional[PixcDownloadPreview] = None,
    earthaccess_module: Any = None,
    progress_callback: Optional[ProgressCallback] = None,
    stop_event: Any = None,
) -> PixcDownloadResult:
    """Download selected/missing PIXC granules from a preview."""
    earthaccess = earthaccess_module or import_earthaccess()
    preview = preview or preview_pixc_granules(
        config,
        earthaccess_module=earthaccess,
        progress_callback=progress_callback,
        stop_event=stop_event,
    )
    config.output_folder.mkdir(parents=True, exist_ok=True)
    if progress_callback is not None:
        progress_callback(0, 0, "Checking local PIXC files and standalone manifest")

    statuses: dict[str, tuple[str, str]] = {}
    to_download: list[PixcDownloadGranule] = []
    skipped_existing: list[Path] = []
    skipped_manifest: list[PixcDownloadGranule] = []
    manifest = read_download_manifest(config.manifest_csv)
    for granule in preview.granules:
        key = manifest_key(granule)
        if not granule.selected_for_download:
            statuses[key] = (non_download_status(granule), "")
            continue
        existing = existing_download_path(config, granule)
        if config.skip_existing and existing is not None:
            skipped_existing.append(existing)
            statuses[key] = ("SKIPPED_EXISTING", str(existing))
        elif config.skip_manifest_existing and manifest_has_downloaded(manifest, granule):
            skipped_manifest.append(granule)
            manifest_row = manifest.get(key, {})
            statuses[key] = ("SKIPPED_MANIFEST", str(manifest_row.get("local_path", "")))
        else:
            to_download.append(granule)

    append_download_event(
        config,
        "INFO",
        "download_prepared",
        (
            f"{len(to_download)} file(s) queued; {len(skipped_existing)} local file(s) "
            f"and {len(skipped_manifest)} manifest-known file(s) skipped."
        ),
    )

    if to_download:
        if progress_callback is not None:
            progress_callback(0, len(to_download), f"Authenticating Earthdata before downloading {len(to_download)} file(s)")
        try:
            ensure_authenticated(earthaccess)
        except Exception as exc:
            message = earthdata_auth_error_message(exc)
            for granule in to_download:
                statuses[manifest_key(granule)] = (AUTH_FAILED_STATUS, message)
            apply_statuses(preview, statuses)
            write_download_report(config, preview, statuses)
            write_download_manifest(config, preview, statuses)
            append_download_event(config, "ERROR", "auth_failed", message)
            if progress_callback is not None:
                progress_callback(0, len(to_download), "Earthdata authentication failed")
            raise RuntimeError(message) from exc
        append_download_event(
            config,
            "INFO",
            "auth_ok",
            f"Earthdata authentication completed for {len(to_download)} queued file(s).",
        )

    downloaded_files: list[Path] = []
    failures: list[tuple[PixcDownloadGranule, str]] = []
    total = len(to_download)
    if progress_callback is not None:
        progress_callback(
            0,
            total,
            (
                f"Starting transfer of {total} file(s); "
                f"{len(skipped_existing)} local and {len(skipped_manifest)} manifest-known file(s) skipped"
            )
            if total
            else "No new PIXC files to download from the current preview",
        )
    if total == 0:
        append_download_event(config, "INFO", "download_noop", "No new PIXC files to download from the current preview.")

    stopped = False
    completed_attempts = 0
    for batch in chunks(to_download, config.batch_size):
        if stop_event is not None and stop_event.is_set():
            stopped = True
            for pending in to_download[completed_attempts:]:
                statuses[manifest_key(pending)] = ("CANCELLED", "")
            if progress_callback is not None:
                progress_callback(completed_attempts, total, "Download stop requested")
            append_download_event(config, "WARNING", "download_stopped", "Download stop requested by the user.")
            break
        for granule in batch:
            move_incomplete_download(config, granule)
        if progress_callback is not None:
            progress_callback(completed_attempts, total, f"Downloading batch of {len(batch)} PIXC file(s)")
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
                complete_path = existing_download_path(config, granule)
                statuses[manifest_key(granule)] = (
                    ("DOWNLOADED", str(complete_path))
                    if complete_path is not None
                    else ("MISSING", "")
                )
            message = f"Finished batch of {len(batch)} PIXC file(s)"
            append_download_event(config, "INFO", "batch_finished", message)
        except Exception as exc:
            for granule in batch:
                complete_path = existing_download_path(config, granule)
                if complete_path is not None:
                    statuses[manifest_key(granule)] = ("DOWNLOADED", str(complete_path))
                else:
                    failures.append((granule, str(exc)))
                    statuses[manifest_key(granule)] = ("FAILED", str(exc))
            message = f"Failed batch of {len(batch)} PIXC file(s)"
            append_download_event(config, "ERROR", "batch_failed", f"{message}: {exc}")
        completed_attempts += len(batch)
        if progress_callback is not None:
            progress_callback(completed_attempts, total, message)

    missing_granules = update_statuses_from_local_files(config, preview, statuses)
    apply_statuses(preview, statuses)
    report_csv = write_download_report(config, preview, statuses)
    manifest_csv = write_download_manifest(config, preview, statuses)
    append_download_event(
        config,
        "INFO" if not failures and not stopped else "WARNING",
        "download_finished",
        (
            f"{len(downloaded_files)} downloaded, {len(skipped_existing)} local skipped, "
            f"{len(skipped_manifest)} manifest skipped, {len(failures)} failed, "
            f"{len(missing_granules)} not complete."
        ),
    )
    return PixcDownloadResult(
        preview=preview,
        downloaded_files=downloaded_files,
        skipped_existing=skipped_existing,
        skipped_manifest=skipped_manifest,
        failures=failures,
        missing_granules=missing_granules,
        stopped=stopped,
        report_csv=report_csv,
        manifest_csv=manifest_csv,
        statuses=statuses,
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
