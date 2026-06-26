"""Earth Engine reference imagery helpers for PIXC point visualization."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .download import parse_pixc_filename_metadata


SOURCE_SENTINEL2 = "sentinel2"
SOURCE_SENTINEL1 = "sentinel1"
SOURCE_LANDSAT = "landsat"
SOURCE_LANDSAT8 = "landsat8"
SOURCE_LANDSAT9 = "landsat9"
METHOD_CLOSEST = "closest"
METHOD_COMPOSITE = "composite"
DEFAULT_REFERENCE_SOURCES = (SOURCE_SENTINEL2, SOURCE_SENTINEL1, SOURCE_LANDSAT)
VIEWER_REFERENCE_SOURCES = (SOURCE_SENTINEL2, SOURCE_SENTINEL1, SOURCE_LANDSAT8, SOURCE_LANDSAT9)
DEFAULT_REFERENCE_METHODS = (METHOD_CLOSEST, METHOD_COMPOSITE)
DEFAULT_REFERENCE_WINDOW_DAYS = 14

SOURCE_LABELS = {
    SOURCE_SENTINEL2: "Sentinel-2",
    SOURCE_SENTINEL1: "Sentinel-1",
    SOURCE_LANDSAT: "Landsat 8/9",
    SOURCE_LANDSAT8: "Landsat 8",
    SOURCE_LANDSAT9: "Landsat 9",
}
METHOD_LABELS = {
    METHOD_CLOSEST: "closest",
    METHOD_COMPOSITE: "composite",
}

REFERENCE_IMAGERY_LOG_COLUMNS = [
    "timestamp",
    "requested_date",
    "window_days",
    "source",
    "method",
    "layer_name",
    "status",
    "scene_id",
    "scene_date",
    "message",
]


@dataclass(frozen=True)
class EarthEngineReferenceConfig:
    """Settings for dated Earth Engine reference imagery."""

    enabled: bool = False
    reference_date: str = ""
    window_days: int = DEFAULT_REFERENCE_WINDOW_DAYS
    ee_project: str = ""
    sources: tuple[str, ...] = DEFAULT_REFERENCE_SOURCES
    methods: tuple[str, ...] = DEFAULT_REFERENCE_METHODS


@dataclass
class EarthEngineTileLayer:
    """One temporary Earth Engine tile layer for the browser point viewer."""

    name: str
    source: str
    method: str
    tile_url: str
    attribution: str
    opacity: float = 0.78
    default_visible: bool = False
    requested_date: str = ""
    scene_id: str = ""
    scene_date: str = ""
    status: str = "OK"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable layer payload."""
        return asdict(self)


@dataclass
class EarthEngineImageryResult:
    """Reference imagery layers and non-fatal warnings."""

    layers: list[EarthEngineTileLayer] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EarthEngineBandPreset:
    """One viewer-selectable Earth Engine visualization preset."""

    key: str
    source: str
    name: str
    kind: str
    bands: tuple[str, ...] = ()
    min: float | tuple[float, ...] | None = None
    max: float | tuple[float, ...] | None = None
    gamma: float | None = None
    palette: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EarthEngineImageSearchConfig:
    """Viewer-side image search settings."""

    source: str
    start_date: str
    end_date: str
    bbox: tuple[float, float, float, float]
    ee_project: str = ""
    max_images: int = 50


@dataclass
class EarthEngineImageRecord:
    """One Earth Engine image found for the viewer."""

    image_id: str
    source: str
    source_label: str
    date: str = ""
    cloud: str = ""
    platform: str = ""
    orbit: str = ""
    polarizations: str = ""
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EarthEngineImageSearchResult:
    """Viewer-side search results and warnings."""

    images: list[EarthEngineImageRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EarthEngineTileLayerRequest:
    """Request to create one viewer-side Earth Engine tile layer."""

    source: str
    image_id: str
    band_preset: str
    ee_project: str = ""
    layer_name: str = ""


S2_BAND_PRESETS = (
    EarthEngineBandPreset("s2_true_color", SOURCE_SENTINEL2, "True Color (4,3,2)", "rgb", ("B4", "B3", "B2"), 0, 3000, 1.2),
    EarthEngineBandPreset("s2_highlight_natural", SOURCE_SENTINEL2, "Highlight Optimized Natural Color (4,3,2)", "highlight_rgb", ("B4", "B3", "B2"), 0, 2200, 1.1),
    EarthEngineBandPreset("s2_false_color", SOURCE_SENTINEL2, "False Color (8,4,3)", "rgb", ("B8", "B4", "B3"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_swir", SOURCE_SENTINEL2, "SWIR (12,8,4)", "rgb", ("B12", "B8", "B4"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_agriculture", SOURCE_SENTINEL2, "Agriculture (11,8,2)", "rgb", ("B11", "B8", "B2"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_geology", SOURCE_SENTINEL2, "Geology (12,11,2)", "rgb", ("B12", "B11", "B2"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_bathymetric", SOURCE_SENTINEL2, "Bathymetric (4,3,1)", "rgb", ("B4", "B3", "B1"), 0, 3000, 1.2),
    EarthEngineBandPreset("s2_rgb_864", SOURCE_SENTINEL2, "RGB (8,6,4)", "rgb", ("B8", "B6", "B4"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_rgb_854", SOURCE_SENTINEL2, "RGB (8,5,4)", "rgb", ("B8", "B5", "B4"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_rgb_8114", SOURCE_SENTINEL2, "RGB (8,11,4)", "rgb", ("B8", "B11", "B4"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_rgb_81112", SOURCE_SENTINEL2, "RGB (8,11,12)", "rgb", ("B8", "B11", "B12"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_rgb_1183", SOURCE_SENTINEL2, "RGB (11,8,3)", "rgb", ("B11", "B8", "B3"), 0, 3500, 1.2),
    EarthEngineBandPreset("s2_ndvi", SOURCE_SENTINEL2, "NDVI", "ndvi", ("B8", "B4"), -0.4, 0.9, None, ("#7f3b08", "#f7f7f7", "#1a9850")),
)

LS89_BAND_PRESETS = (
    EarthEngineBandPreset("ls_true_color", SOURCE_LANDSAT8, "Natural Color (4,3,2)", "rgb", ("SR_B4", "SR_B3", "SR_B2"), 7000, 18000, 1.2),
    EarthEngineBandPreset("ls_highlight_natural", SOURCE_LANDSAT8, "Highlight Optimized Natural Color (4,3,2)", "highlight_rgb", ("SR_B4", "SR_B3", "SR_B2"), 7000, 15000, 1.1),
    EarthEngineBandPreset("ls_color_infrared", SOURCE_LANDSAT8, "Color Infrared (5,4,3)", "rgb", ("SR_B5", "SR_B4", "SR_B3"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_urban", SOURCE_LANDSAT8, "False Color Urban (7,6,4)", "rgb", ("SR_B7", "SR_B6", "SR_B4"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_agriculture", SOURCE_LANDSAT8, "Agriculture (6,5,2)", "rgb", ("SR_B6", "SR_B5", "SR_B2"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_geology", SOURCE_LANDSAT8, "Geology (7,6,2)", "rgb", ("SR_B7", "SR_B6", "SR_B2"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_atmospheric", SOURCE_LANDSAT8, "Atmospheric Penetration (7,6,5)", "rgb", ("SR_B7", "SR_B6", "SR_B5"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_healthy_vegetation", SOURCE_LANDSAT8, "Healthy Vegetation (5,6,2)", "rgb", ("SR_B5", "SR_B6", "SR_B2"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_land_water", SOURCE_LANDSAT8, "Land/Water (5,6,4)", "rgb", ("SR_B5", "SR_B6", "SR_B4"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_swir", SOURCE_LANDSAT8, "Shortwave Infrared (7,5,4)", "rgb", ("SR_B7", "SR_B5", "SR_B4"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_vegetation", SOURCE_LANDSAT8, "Vegetation Analysis (6,5,4)", "rgb", ("SR_B6", "SR_B5", "SR_B4"), 7000, 19000, 1.2),
    EarthEngineBandPreset("ls_bathymetric", SOURCE_LANDSAT8, "Bathymetric (4,3,1)", "rgb", ("SR_B4", "SR_B3", "SR_B1"), 7000, 18000, 1.2),
    EarthEngineBandPreset("ls_ndvi", SOURCE_LANDSAT8, "NDVI", "ndvi", ("SR_B5", "SR_B4"), -0.4, 0.9, None, ("#7f3b08", "#f7f7f7", "#1a9850")),
)

S1_BAND_PRESETS = (
    EarthEngineBandPreset("s1_auto_single", SOURCE_SENTINEL1, "Auto single (prefer VH)", "s1_auto_single", (), -25, 0),
    EarthEngineBandPreset("s1_auto_rgb_ratio", SOURCE_SENTINEL1, "Auto RGB ratio (dual-pol)", "s1_rgb_ratio", (), (-25, -30, -8), (0, 0, 8)),
    EarthEngineBandPreset("s1_vv", SOURCE_SENTINEL1, "Band: VV", "rgb", ("VV",), -25, 0),
    EarthEngineBandPreset("s1_vh", SOURCE_SENTINEL1, "Band: VH", "rgb", ("VH",), -30, 0),
    EarthEngineBandPreset("s1_hh", SOURCE_SENTINEL1, "Band: HH", "rgb", ("HH",), -25, 0),
    EarthEngineBandPreset("s1_hv", SOURCE_SENTINEL1, "Band: HV", "rgb", ("HV",), -30, 0),
    EarthEngineBandPreset("s1_cross_minus_co", SOURCE_SENTINEL1, "Index: cross - co (dB)", "s1_cross_minus_co", (), -10, 10, None, ("#2c7bb6", "#ffffbf", "#d7191c")),
    EarthEngineBandPreset("s1_ndpi", SOURCE_SENTINEL1, "NDPI (dual-pol)", "s1_ndpi", (), -1, 1, None, ("#313695", "#ffffbf", "#a50026")),
    EarthEngineBandPreset("s1_rvi", SOURCE_SENTINEL1, "RVI4S1-like (dual-pol)", "s1_rvi", (), 0, 1, None, ("#ffffcc", "#78c679", "#006837")),
)


def band_presets_for_source(source: str) -> tuple[EarthEngineBandPreset, ...]:
    """Return viewer band presets for one source."""
    if source == SOURCE_SENTINEL2:
        return S2_BAND_PRESETS
    if source == SOURCE_SENTINEL1:
        return S1_BAND_PRESETS
    if source in {SOURCE_LANDSAT, SOURCE_LANDSAT8, SOURCE_LANDSAT9}:
        return tuple(
            EarthEngineBandPreset(
                preset.key,
                source,
                preset.name,
                preset.kind,
                preset.bands,
                preset.min,
                preset.max,
                preset.gamma,
                preset.palette,
            )
            for preset in LS89_BAND_PRESETS
        )
    raise ValueError(f"Unsupported reference imagery source: {source}")


def band_preset_by_key(source: str, key: str) -> EarthEngineBandPreset:
    """Return one viewer band preset by key."""
    for preset in band_presets_for_source(source):
        if preset.key == key:
            return preset
    raise ValueError(f"Unsupported band combination for {SOURCE_LABELS.get(source, source)}: {key}")


def reference_imagery_options() -> dict[str, Any]:
    """Return browser-ready source and band-combination options."""
    return {
        "sources": [
            {"key": source, "label": SOURCE_LABELS[source]}
            for source in VIEWER_REFERENCE_SOURCES
        ],
        "band_presets": {
            source: [preset.to_dict() for preset in band_presets_for_source(source)]
            for source in VIEWER_REFERENCE_SOURCES
        },
    }


def normalize_reference_sources(values: Iterable[str]) -> tuple[str, ...]:
    """Return valid source tokens preserving input order."""
    allowed = set(DEFAULT_REFERENCE_SOURCES)
    selected = []
    for value in values:
        token = str(value or "").strip().lower()
        if token in allowed and token not in selected:
            selected.append(token)
    return tuple(selected) or DEFAULT_REFERENCE_SOURCES


def normalize_reference_methods(values: Iterable[str]) -> tuple[str, ...]:
    """Return valid imagery method tokens preserving input order."""
    allowed = set(DEFAULT_REFERENCE_METHODS)
    selected = []
    for value in values:
        token = str(value or "").strip().lower()
        if token in allowed and token not in selected:
            selected.append(token)
    return tuple(selected) or DEFAULT_REFERENCE_METHODS


def parse_reference_date(value: str) -> date:
    """Parse a YYYY-MM-DD or ISO datetime reference date."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("Reference date is required.")
    return date.fromisoformat(text[:10])


def pixc_timestamp_to_date(value: str) -> Optional[date]:
    """Return the date component of a SWOT PIXC timestamp token."""
    try:
        return datetime.strptime(str(value), "%Y%m%dT%H%M%S").date()
    except ValueError:
        return None


def infer_reference_date_from_filename(path: str | Path) -> Optional[date]:
    """Infer a PIXC acquisition date from a filename."""
    metadata = parse_pixc_filename_metadata(path)
    if metadata is None:
        return None
    return pixc_timestamp_to_date(metadata.range_beginning)


def infer_reference_date_from_paths(paths: Sequence[str | Path]) -> tuple[str, list[str]]:
    """Infer the reference date from selected PIXC files."""
    inferred: list[date] = []
    for path in paths:
        value = infer_reference_date_from_filename(path)
        if value is not None:
            inferred.append(value)
    if not inferred:
        return "", ["Could not infer a SWOT acquisition date from the selected PIXC filename(s)."]
    first = inferred[0]
    unique = sorted(set(inferred))
    warnings: list[str] = []
    if len(unique) > 1:
        warnings.append(
            f"Selected PIXC files contain multiple acquisition dates; using {first.isoformat()}."
        )
    return first.isoformat(), warnings


def date_window(reference: date, window_days: int) -> tuple[str, str]:
    """Return inclusive/exclusive ISO date strings for an EE date window."""
    days = max(0, int(window_days))
    start = reference - timedelta(days=days)
    end = reference + timedelta(days=days + 1)
    return start.isoformat(), end.isoformat()


def import_ee() -> Any:
    """Import the Earth Engine API lazily."""
    try:
        import ee  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "The Earth Engine Python API is not installed. Install earthengine-api and authenticate first."
        ) from exc
    return ee


def initialize_earth_engine(ee: Any, project: str = "") -> None:
    """Initialize Earth Engine without forcing an interactive authentication flow."""
    try:
        if str(project or "").strip():
            ee.Initialize(project=str(project).strip())
        else:
            ee.Initialize()
    except Exception as exc:
        raise RuntimeError(
            "Could not initialize Earth Engine. Run earthengine authenticate or set a valid EE project."
        ) from exc


def safe_get_info(value: Any, default: Any = "") -> Any:
    """Return getInfo() when available, otherwise the original value or default."""
    if value is None:
        return default
    getter = getattr(value, "getInfo", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return default
    return value


def ee_bbox_geometry(ee: Any, bbox: tuple[float, float, float, float]) -> Any:
    """Build an EE rectangle geometry from a lon/lat bbox."""
    west, south, east, north = bbox
    return ee.Geometry.Rectangle([float(west), float(south), float(east), float(north)])


def collection_for_source(
    ee: Any,
    source: str,
    *,
    geometry: Any,
    start_date: str,
    end_date: str,
) -> Any:
    """Build a filtered EE ImageCollection for one reference source."""
    if source == SOURCE_SENTINEL2:
        return (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(geometry)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", 80))
        )
    if source == SOURCE_SENTINEL1:
        return (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(geometry)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.eq("instrumentMode", "IW"))
        )
    if source in {SOURCE_LANDSAT, SOURCE_LANDSAT8, SOURCE_LANDSAT9}:
        if source == SOURCE_LANDSAT8:
            collection = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
        elif source == SOURCE_LANDSAT9:
            collection = ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
        else:
            landsat8 = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
            landsat9 = ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
            collection = landsat8.merge(landsat9)
        return (
            collection
            .filterBounds(geometry)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lte("CLOUD_COVER", 80))
        )
    raise ValueError(f"Unsupported reference imagery source: {source}")


def source_vis_params(source: str) -> dict[str, Any]:
    """Return Earth Engine visualization parameters for one source."""
    if source == SOURCE_SENTINEL2:
        return {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000, "gamma": 1.2}
    if source == SOURCE_SENTINEL1:
        return {"bands": ["VV"], "min": -25, "max": 0}
    if source in {SOURCE_LANDSAT, SOURCE_LANDSAT8, SOURCE_LANDSAT9}:
        return {"bands": ["SR_B4", "SR_B3", "SR_B2"], "min": 7000, "max": 18000, "gamma": 1.2}
    raise ValueError(f"Unsupported reference imagery source: {source}")


def source_attribution(source: str) -> str:
    """Return a compact attribution string for one source."""
    if source == SOURCE_SENTINEL2:
        return "Google Earth Engine / Copernicus Sentinel-2"
    if source == SOURCE_SENTINEL1:
        return "Google Earth Engine / Copernicus Sentinel-1"
    if source in {SOURCE_LANDSAT, SOURCE_LANDSAT8, SOURCE_LANDSAT9}:
        return "Google Earth Engine / USGS Landsat"
    return "Google Earth Engine"


def add_distance_property(ee: Any, collection: Any, reference: date) -> Any:
    """Add an absolute day-distance property for closest-scene sorting."""
    ee_date = ee.Date(reference.isoformat())

    def annotate(image: Any) -> Any:
        return image.set(
            "swotflow_abs_days",
            ee.Number(image.date().difference(ee_date, "day")).abs(),
        )

    return collection.map(annotate)


def selected_image_for_method(ee: Any, collection: Any, method: str, reference: date) -> Any:
    """Return an EE Image for closest-scene or composite rendering."""
    if method == METHOD_CLOSEST:
        return add_distance_property(ee, collection, reference).sort("swotflow_abs_days").first()
    if method == METHOD_COMPOSITE:
        return collection.median()
    raise ValueError(f"Unsupported reference imagery method: {method}")


def collection_count(collection: Any) -> int:
    """Return a best-effort collection size."""
    try:
        return int(safe_get_info(collection.size(), 0) or 0)
    except Exception:
        return 0


def image_scene_metadata(image: Any, method: str, start_date: str, end_date: str) -> tuple[str, str]:
    """Return scene id/date metadata when available."""
    if method == METHOD_COMPOSITE:
        return "", f"{start_date} to {end_date}"
    scene_id = str(safe_get_info(image.get("system:index"), "") or "")
    scene_date = ""
    try:
        scene_date = str(safe_get_info(image.date().format("YYYY-MM-dd"), "") or "")
    except Exception:
        scene_date = ""
    return scene_id, scene_date


def tile_url_from_map_id(ee: Any, map_id: Any) -> str:
    """Return a Leaflet-compatible tile URL template from an EE map id."""
    if isinstance(map_id, dict):
        fetcher = map_id.get("tile_fetcher")
        url_format = getattr(fetcher, "url_format", "") if fetcher is not None else ""
        if url_format:
            return str(url_format)
        for key in ("urlFormat", "url_format"):
            if map_id.get(key):
                return str(map_id[key])
    fetcher = getattr(map_id, "tile_fetcher", None)
    url_format = getattr(fetcher, "url_format", "") if fetcher is not None else ""
    if url_format:
        return str(url_format)
    return str(ee.data.getTileUrl(map_id, "{x}", "{y}", "{z}"))


def inclusive_end_date(value: str) -> str:
    """Return the EE-exclusive end date for a user-selected inclusive date."""
    return (parse_reference_date(value) + timedelta(days=1)).isoformat()


def normalize_viewer_source(source: str) -> str:
    """Normalize one viewer source token."""
    token = str(source or "").strip().lower().replace("-", "")
    aliases = {
        "s2": SOURCE_SENTINEL2,
        "sentinel2": SOURCE_SENTINEL2,
        "sentinel1": SOURCE_SENTINEL1,
        "s1": SOURCE_SENTINEL1,
        "landsat8": SOURCE_LANDSAT8,
        "l8": SOURCE_LANDSAT8,
        "landsat9": SOURCE_LANDSAT9,
        "l9": SOURCE_LANDSAT9,
        "landsat": SOURCE_LANDSAT,
    }
    if token in aliases:
        return aliases[token]
    raise ValueError(f"Unsupported reference imagery source: {source}")


def feature_date_text(properties: dict[str, Any]) -> str:
    """Return a YYYY-MM-DD image date from Earth Engine feature properties."""
    millis = properties.get("system:time_start")
    if isinstance(millis, (int, float)):
        try:
            return datetime.fromtimestamp(float(millis) / 1000.0, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    for key in ("SENSING_TIME", "DATE_ACQUIRED", "system:index"):
        value = str(properties.get(key, "") or "")
        if len(value) >= 10 and value[:4].isdigit():
            return value[:10]
    return ""


def feature_cloud_text(source: str, properties: dict[str, Any]) -> str:
    """Return a compact cloud-percent label when available."""
    key = "CLOUDY_PIXEL_PERCENTAGE" if source == SOURCE_SENTINEL2 else "CLOUD_COVER"
    value = properties.get(key)
    if value in ("", None):
        return ""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def feature_polarizations_text(properties: dict[str, Any]) -> str:
    """Return a compact Sentinel-1 polarization label."""
    value = properties.get("transmitterReceiverPolarisation", "")
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value or "")


def image_record_from_feature(source: str, feature: dict[str, Any]) -> EarthEngineImageRecord:
    """Normalize one Earth Engine feature-info result for the viewer."""
    properties = feature.get("properties", {}) if isinstance(feature, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    image_id = str(feature.get("id", "") or properties.get("system:id", "") or properties.get("system:index", ""))
    date_text = feature_date_text(properties)
    platform = str(properties.get("SPACECRAFT_NAME", "") or properties.get("platform_number", "") or "")
    orbit = str(properties.get("orbitProperties_pass", "") or properties.get("WRS_PATH", "") or "")
    polarizations = feature_polarizations_text(properties) if source == SOURCE_SENTINEL1 else ""
    title_parts = [SOURCE_LABELS.get(source, source)]
    if date_text:
        title_parts.append(date_text)
    if image_id:
        title_parts.append(Path(image_id).name)
    return EarthEngineImageRecord(
        image_id=image_id,
        source=source,
        source_label=SOURCE_LABELS.get(source, source),
        date=date_text,
        cloud=feature_cloud_text(source, properties),
        platform=platform,
        orbit=orbit,
        polarizations=polarizations,
        title=" - ".join(title_parts),
    )


def image_collection_features(collection: Any, max_images: int) -> list[dict[str, Any]]:
    """Return feature dictionaries from a limited EE ImageCollection."""
    limit = max(1, int(max_images or 50))
    limited = collection.limit(limit)
    info = limited.getInfo()
    if isinstance(info, dict):
        features = info.get("features", [])
        if isinstance(features, list):
            return [feature for feature in features if isinstance(feature, dict)]
    return []


def search_reference_images(
    config: EarthEngineImageSearchConfig,
    *,
    ee_module: Any = None,
) -> EarthEngineImageSearchResult:
    """Search Earth Engine images intersecting the point-cloud reference bbox."""
    result = EarthEngineImageSearchResult()
    try:
        source = normalize_viewer_source(config.source)
        start_date = parse_reference_date(config.start_date).isoformat()
        end_date = inclusive_end_date(config.end_date)
        ee = ee_module or import_ee()
        initialize_earth_engine(ee, config.ee_project)
        geometry = ee_bbox_geometry(ee, config.bbox)
        collection = (
            collection_for_source(
                ee,
                source,
                geometry=geometry,
                start_date=start_date,
                end_date=end_date,
            )
            .sort("system:time_start")
        )
        features = image_collection_features(collection, config.max_images)
        result.images = [
            record
            for record in (image_record_from_feature(source, feature) for feature in features)
            if record.image_id
        ]
        if not result.images:
            result.warnings.append(
                f"No {SOURCE_LABELS.get(source, source)} imagery found for {start_date} to {config.end_date}."
            )
    except Exception as exc:
        result.warnings.append(str(exc))
    return result


def visualization_params_for_preset(preset: EarthEngineBandPreset) -> dict[str, Any]:
    """Return Earth Engine visualization parameters for one viewer preset."""
    params: dict[str, Any] = {}
    if preset.bands and preset.kind in {"rgb", "highlight_rgb"}:
        params["bands"] = list(preset.bands)
    if preset.min is not None:
        params["min"] = preset.min
    if preset.max is not None:
        params["max"] = preset.max
    if preset.gamma is not None:
        params["gamma"] = preset.gamma
    if preset.palette:
        params["palette"] = list(preset.palette)
    return params


def image_for_viewer_layer(ee: Any, source: str, image_id: str, preset: EarthEngineBandPreset) -> Any:
    """Return the EE image to render for a viewer band preset."""
    image = ee.Image(str(image_id))
    if preset.kind == "ndvi" and len(preset.bands) >= 2:
        return image.normalizedDifference(list(preset.bands[:2])).rename("NDVI")
    if source == SOURCE_SENTINEL1:
        if preset.kind == "s1_auto_single":
            return image.select("VH")
        if preset.kind == "s1_cross_minus_co":
            return image.select("VH").subtract(image.select("VV")).rename("cross_minus_co")
        if preset.kind == "s1_ndpi":
            return image.normalizedDifference(["VV", "VH"]).rename("NDPI")
        if preset.kind == "s1_rvi":
            return image.select("VH").multiply(4).divide(image.select("VV").add(image.select("VH"))).rename("RVI")
        if preset.kind == "s1_rgb_ratio":
            co = image.select("VV")
            cross = image.select("VH")
            ratio = cross.subtract(co).rename("VH_minus_VV")
            return ee.Image.cat(co, cross, ratio)
    return image


def build_reference_tile_layer(
    request: EarthEngineTileLayerRequest,
    *,
    ee_module: Any = None,
) -> EarthEngineTileLayer:
    """Build a temporary EE tile layer for one user-selected image and band preset."""
    source = normalize_viewer_source(request.source)
    preset = band_preset_by_key(source, request.band_preset)
    ee = ee_module or import_ee()
    initialize_earth_engine(ee, request.ee_project)
    image = image_for_viewer_layer(ee, source, request.image_id, preset)
    map_id = image.getMapId(visualization_params_for_preset(preset))
    tile_url = tile_url_from_map_id(ee, map_id)
    layer_name = request.layer_name.strip() if request.layer_name else ""
    if not layer_name:
        layer_name = f"{SOURCE_LABELS.get(source, source)} - {preset.name} - {Path(request.image_id).name}"
    return EarthEngineTileLayer(
        name=layer_name,
        source=source,
        method="viewer",
        tile_url=tile_url,
        attribution=source_attribution(source),
        opacity=0.76 if source == SOURCE_SENTINEL1 else 0.84,
        scene_id=request.image_id,
        status="OK",
        message=preset.name,
    )


def build_tile_layer(
    ee: Any,
    *,
    source: str,
    method: str,
    image: Any,
    reference: date,
    start_date: str,
    end_date: str,
) -> EarthEngineTileLayer:
    """Build one browser-ready Earth Engine tile layer."""
    map_id = image.getMapId(source_vis_params(source))
    tile_url = tile_url_from_map_id(ee, map_id)
    scene_id, scene_date = image_scene_metadata(image, method, start_date, end_date)
    source_label = SOURCE_LABELS.get(source, source)
    method_label = METHOD_LABELS.get(method, method)
    date_label = scene_date or reference.isoformat()
    return EarthEngineTileLayer(
        name=f"{source_label} {method_label} ({date_label})",
        source=source,
        method=method,
        tile_url=tile_url,
        attribution=source_attribution(source),
        opacity=0.72 if source == SOURCE_SENTINEL1 else 0.82,
        requested_date=reference.isoformat(),
        scene_id=scene_id,
        scene_date=scene_date,
    )


def mark_default_reference_layer(layers: Sequence[EarthEngineTileLayer]) -> None:
    """Make the first optical closest-scene layer visible by default."""
    for layer in layers:
        layer.default_visible = False
    for layer in layers:
        if layer.method == METHOD_CLOSEST and layer.source in {SOURCE_SENTINEL2, SOURCE_LANDSAT}:
            layer.default_visible = True
            return
    if layers:
        layers[0].default_visible = True


def log_rows_for_layers(
    config: EarthEngineReferenceConfig,
    layers: Sequence[EarthEngineTileLayer],
    warnings: Sequence[str],
) -> list[dict[str, Any]]:
    """Return CSV log rows for reference imagery attempts."""
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = [
        {
            "timestamp": timestamp,
            "requested_date": config.reference_date,
            "window_days": config.window_days,
            "source": layer.source,
            "method": layer.method,
            "layer_name": layer.name,
            "status": layer.status,
            "scene_id": layer.scene_id,
            "scene_date": layer.scene_date,
            "message": layer.message,
        }
        for layer in layers
    ]
    for warning in warnings:
        rows.append(
            {
                "timestamp": timestamp,
                "requested_date": config.reference_date,
                "window_days": config.window_days,
                "source": "",
                "method": "",
                "layer_name": "",
                "status": "WARNING",
                "scene_id": "",
                "scene_date": "",
                "message": warning,
            }
        )
    return rows


def append_reference_imagery_log(
    path: str | Path,
    config: EarthEngineReferenceConfig,
    layers: Sequence[EarthEngineTileLayer],
    warnings: Sequence[str],
) -> Path:
    """Append reference imagery layer/warning rows to the project log."""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not log_path.exists() or log_path.stat().st_size == 0
    with log_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REFERENCE_IMAGERY_LOG_COLUMNS)
        if write_header:
            writer.writeheader()
        for row in log_rows_for_layers(config, layers, warnings):
            writer.writerow({field: row.get(field, "") for field in REFERENCE_IMAGERY_LOG_COLUMNS})
    return log_path


def build_reference_imagery_layers(
    config: EarthEngineReferenceConfig,
    bbox: Optional[tuple[float, float, float, float]],
    *,
    ee_module: Any = None,
    log_csv: str | Path | None = None,
) -> EarthEngineImageryResult:
    """Build temporary Earth Engine tile layers for the PIXC point viewer."""
    result = EarthEngineImageryResult()
    if not config.enabled:
        return result
    if bbox is None:
        result.warnings.append("Reference imagery was skipped because the point map has no bbox.")
        if log_csv:
            append_reference_imagery_log(log_csv, config, result.layers, result.warnings)
        return result
    try:
        reference = parse_reference_date(config.reference_date)
    except Exception as exc:
        result.warnings.append(f"Reference imagery skipped: {exc}")
        if log_csv:
            append_reference_imagery_log(log_csv, config, result.layers, result.warnings)
        return result

    start_date, end_date = date_window(reference, config.window_days)
    try:
        ee = ee_module or import_ee()
        initialize_earth_engine(ee, config.ee_project)
    except Exception as exc:
        result.warnings.append(str(exc))
        if log_csv:
            append_reference_imagery_log(log_csv, config, result.layers, result.warnings)
        return result

    geometry = ee_bbox_geometry(ee, bbox)
    for source in normalize_reference_sources(config.sources):
        try:
            collection = collection_for_source(
                ee,
                source,
                geometry=geometry,
                start_date=start_date,
                end_date=end_date,
            )
            if collection_count(collection) == 0:
                result.warnings.append(
                    f"No {SOURCE_LABELS.get(source, source)} imagery found near {reference.isoformat()}."
                )
                continue
        except Exception as exc:
            result.warnings.append(f"{SOURCE_LABELS.get(source, source)} query failed: {exc}")
            continue
        for method in normalize_reference_methods(config.methods):
            try:
                image = selected_image_for_method(ee, collection, method, reference)
                result.layers.append(
                    build_tile_layer(
                        ee,
                        source=source,
                        method=method,
                        image=image,
                        reference=reference,
                        start_date=start_date,
                        end_date=end_date,
                    )
                )
            except Exception as exc:
                result.warnings.append(
                    f"{SOURCE_LABELS.get(source, source)} {METHOD_LABELS.get(method, method)} layer failed: {exc}"
                )
    mark_default_reference_layer(result.layers)
    if log_csv:
        append_reference_imagery_log(log_csv, config, result.layers, result.warnings)
    return result
