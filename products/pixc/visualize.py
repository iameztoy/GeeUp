"""PIXC point visualization helpers."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .inspect import clean_value, import_netcdf4_dataset, object_attributes


DEFAULT_MAX_POINTS = 50_000
LATITUDE_NAMES = {"latitude", "lat"}
LONGITUDE_NAMES = {"longitude", "lon", "long"}
DEFAULT_ATTRIBUTE_HINTS = (
    "classification",
    "class",
    "quality",
    "quality_flag",
    "water_frac",
    "water_fraction",
    "height",
    "sig0",
    "sigma0",
)
CATEGORICAL_MAX_DISTINCT = 24
CATEGORY_COLORS = (
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#17becf",
    "#bcbd22",
    "#393b79",
    "#637939",
    "#8c6d31",
)
CONTINUOUS_STOPS = (
    (68, 1, 84),
    (58, 82, 139),
    (32, 144, 140),
    (94, 201, 98),
    (253, 231, 37),
)
PIXC_CLASSIFICATION_LABELS = {
    "1": "land",
    "2": "land_near_water",
    "3": "water_near_land",
    "4": "open_water",
    "5": "dark_water",
    "6": "low_coh_water_near_land",
    "7": "open_low_coh_water",
}


@dataclass(frozen=True)
class PixcPointVariable:
    """One NetCDF variable available for PIXC point visualization."""

    path: str
    group_path: str
    name: str
    dimensions: tuple[str, ...] = ()
    shape: tuple[int, ...] = ()
    size: int = 0
    dtype: str = ""
    units: str = ""
    long_name: str = ""
    standard_name: str = ""


@dataclass
class PixcPointVariableCatalog:
    """Variables and detected coordinate paths for one PIXC file."""

    file_path: str = ""
    variables: list[PixcPointVariable] = field(default_factory=list)
    latitude_path: str = ""
    longitude_path: str = ""
    default_attribute_path: str = ""

    @property
    def point_attribute_paths(self) -> list[str]:
        """Return variable paths that can be used as point attributes."""
        coordinate_paths = {self.latitude_path, self.longitude_path}
        return [variable.path for variable in self.variables if variable.path not in coordinate_paths]


@dataclass(frozen=True)
class PixcPointMapConfig:
    """Settings for building a PIXC point map payload."""

    file_path: Path
    attribute_path: str = ""
    latitude_path: str = ""
    longitude_path: str = ""
    max_points: int = DEFAULT_MAX_POINTS
    allowed_value_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class PixcMultiPointMapConfig:
    """Settings for building a multi-file PIXC point map payload."""

    file_paths: tuple[Path, ...]
    attribute_path: str = ""
    latitude_path: str = ""
    longitude_path: str = ""
    max_points_per_file: int = DEFAULT_MAX_POINTS
    allowed_value_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class PixcAttributeValue:
    """One distinct attribute value available for PIXC point filtering."""

    key: str
    label: str
    meaning: str = ""
    count: int = 0


@dataclass
class PixcSampledPoints:
    """Sampled point arrays before colors are assigned."""

    file_path: str
    file_name: str
    latitude_path: str
    longitude_path: str
    attribute_path: str
    total_points: int
    valid_points: int
    max_points: int
    sampled: bool
    bbox: Optional[tuple[float, float, float, float]]
    reference_bbox: Optional[tuple[float, float, float, float]]
    latitude: Any
    longitude: Any
    values: Any
    original_indices: Any


@dataclass
class PixcPointMapLayer:
    """One file layer in a browser-ready PIXC point map."""

    layer_id: str
    file_path: str
    file_name: str
    total_points: int
    valid_points: int
    rendered_points: int
    sampled: bool
    bbox: Optional[tuple[float, float, float, float]]
    reference_bbox: Optional[tuple[float, float, float, float]]
    features: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PixcPointMapData:
    """Browser-ready sampled PIXC point map data."""

    file_path: str
    attribute_path: str
    latitude_path: str
    longitude_path: str
    total_points: int
    valid_points: int
    rendered_points: int
    max_points: int
    sampled: bool
    bbox: Optional[tuple[float, float, float, float]]
    reference_bbox: Optional[tuple[float, float, float, float]]
    color_mode: str
    legend: list[dict[str, Any]] = field(default_factory=list)
    features: list[dict[str, Any]] = field(default_factory=list)
    layers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


@dataclass
class PixcMultiPointMapData:
    """Browser-ready sampled PIXC point map data for multiple files."""

    file_path: str
    file_paths: list[str]
    attribute_path: str
    latitude_path: str
    longitude_path: str
    total_points: int
    valid_points: int
    rendered_points: int
    max_points: int
    sampled: bool
    bbox: Optional[tuple[float, float, float, float]]
    reference_bbox: Optional[tuple[float, float, float, float]]
    color_mode: str
    legend: list[dict[str, Any]] = field(default_factory=list)
    features: list[dict[str, Any]] = field(default_factory=list)
    layers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def child_path(parent_path: str, name: str) -> str:
    """Return a normalized NetCDF child path."""
    return f"/{name}" if parent_path == "/" else f"{parent_path}/{name}"


def iter_mapping_items(value: Any) -> Iterable[tuple[str, Any]]:
    """Yield sorted NetCDF mapping items."""
    if not value:
        return []
    if isinstance(value, Mapping):
        return sorted(value.items(), key=lambda item: str(item[0]).lower())
    items = getattr(value, "items", None)
    if callable(items):
        return sorted(items(), key=lambda item: str(item[0]).lower())
    return []


def variable_size(variable: Any) -> int:
    """Return the number of values in one NetCDF variable."""
    size = getattr(variable, "size", None)
    if size is not None:
        try:
            return int(size)
        except (TypeError, ValueError):
            pass
    total = 1
    shape = tuple(getattr(variable, "shape", ()) or ())
    for length in shape:
        total *= int(length)
    return int(total)


def variable_info(group_path: str, name: str, variable: Any) -> PixcPointVariable:
    """Return point-variable metadata for one NetCDF variable."""
    attrs = object_attributes(variable)
    return PixcPointVariable(
        path=child_path(group_path, name),
        group_path=group_path,
        name=name,
        dimensions=tuple(str(item) for item in getattr(variable, "dimensions", ()) or ()),
        shape=tuple(int(item) for item in getattr(variable, "shape", ()) or ()),
        size=variable_size(variable),
        dtype=str(getattr(variable, "dtype", "") or ""),
        units=str(attrs.get("units", "") or ""),
        long_name=str(attrs.get("long_name", "") or ""),
        standard_name=str(attrs.get("standard_name", "") or ""),
    )


def collect_dataset_variables(group: Any, *, path: str = "/") -> list[PixcPointVariable]:
    """Collect variables recursively from a NetCDF dataset/group."""
    variables: list[PixcPointVariable] = []
    for name, variable in iter_mapping_items(getattr(group, "variables", {})):
        variables.append(variable_info(path, str(name), variable))
    for name, child in iter_mapping_items(getattr(group, "groups", {})):
        variables.extend(collect_dataset_variables(child, path=child_path(path, str(name))))
    return variables


def variable_by_path(group: Any, path: str) -> Any:
    """Return a NetCDF variable by absolute path."""
    normalized = "/" + str(path or "").strip().strip("/")
    if normalized == "/":
        raise ValueError("Variable path is required.")
    parts = [part for part in normalized.split("/") if part]
    current = group
    for part in parts[:-1]:
        groups = getattr(current, "groups", {}) or {}
        if part not in groups:
            raise KeyError(f"NetCDF group not found: {part}")
        current = groups[part]
    variables = getattr(current, "variables", {}) or {}
    name = parts[-1]
    if name not in variables:
        raise KeyError(f"NetCDF variable not found: {normalized}")
    return variables[name]


def choose_coordinate_path(variables: Sequence[PixcPointVariable], names: set[str]) -> str:
    """Choose the best coordinate variable path from known name aliases."""
    exact = [variable for variable in variables if variable.name.lower() in names]
    if not exact:
        return ""
    exact.sort(key=lambda item: ("/pixel_cloud" not in item.path.lower(), len(item.shape) != 1, item.path))
    return exact[0].path


def choose_default_attribute(
    variables: Sequence[PixcPointVariable],
    latitude_path: str,
    longitude_path: str,
) -> str:
    """Choose a useful default attribute to color PIXC points."""
    coordinate_paths = {latitude_path, longitude_path}
    candidates = [variable for variable in variables if variable.path not in coordinate_paths and variable.size > 0]
    for hint in DEFAULT_ATTRIBUTE_HINTS:
        for variable in candidates:
            name = variable.name.lower()
            path = variable.path.lower()
            if name == hint or path.endswith("/" + hint):
                return variable.path
    return candidates[0].path if candidates else ""


def discover_dataset_point_variables(dataset: Any, *, file_path: str | Path = "") -> PixcPointVariableCatalog:
    """Discover coordinate and attribute variables from an open NetCDF dataset."""
    variables = collect_dataset_variables(dataset)
    latitude_path = choose_coordinate_path(variables, LATITUDE_NAMES)
    longitude_path = choose_coordinate_path(variables, LONGITUDE_NAMES)
    default_attribute = choose_default_attribute(variables, latitude_path, longitude_path)
    return PixcPointVariableCatalog(
        file_path=str(file_path or ""),
        variables=variables,
        latitude_path=latitude_path,
        longitude_path=longitude_path,
        default_attribute_path=default_attribute,
    )


def discover_pixc_point_variables(path: str | Path) -> PixcPointVariableCatalog:
    """Open a PIXC NetCDF file and discover point visualization variables."""
    nc_path = Path(path)
    if not nc_path.exists() or not nc_path.is_file():
        raise FileNotFoundError(f"PIXC NetCDF file not found: {nc_path}")
    Dataset = import_netcdf4_dataset()
    dataset = Dataset(str(nc_path), mode="r")
    try:
        return discover_dataset_point_variables(dataset, file_path=nc_path)
    finally:
        close = getattr(dataset, "close", None)
        if callable(close):
            close()


def read_variable_values(variable: Any) -> Any:
    """Read one NetCDF variable as a flat masked NumPy array."""
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is required to visualize PIXC point variables.") from exc
    data = np.ma.array(variable[:]).reshape(-1)
    attrs = object_attributes(variable)
    fill_value = attrs.get("_FillValue", attrs.get("missing_value", ""))
    if fill_value not in ("", None):
        try:
            data = np.ma.masked_where(data == fill_value, data)
        except Exception:
            pass
    return data


def valid_coordinate_mask(latitude: Any, longitude: Any) -> Any:
    """Return a NumPy mask for valid lon/lat coordinates."""
    import numpy as np

    lat_mask = np.ma.getmaskarray(latitude)
    lon_mask = np.ma.getmaskarray(longitude)
    lat_values = np.ma.filled(latitude, np.nan).astype(float)
    lon_values = np.ma.filled(longitude, np.nan).astype(float)
    finite = np.isfinite(lat_values) & np.isfinite(lon_values)
    in_range = (lat_values >= -90.0) & (lat_values <= 90.0) & (lon_values >= -180.0) & (lon_values <= 180.0)
    return (~lat_mask) & (~lon_mask) & finite & in_range


def coordinate_bbox(latitude: Any, longitude: Any, mask: Any) -> Optional[tuple[float, float, float, float]]:
    """Return a WGS84 bbox for all valid coordinates in a PIXC file."""
    import numpy as np

    indices = np.flatnonzero(mask)
    if indices.size == 0:
        return None
    lat_values = np.asarray(latitude[indices], dtype=float)
    lon_values = np.asarray(longitude[indices], dtype=float)
    return (
        float(lon_values.min()),
        float(lat_values.min()),
        float(lon_values.max()),
        float(lat_values.max()),
    )


def value_valid_mask(values: Any) -> Any:
    """Return a NumPy mask for values that can be rendered."""
    import numpy as np

    mask = ~np.ma.getmaskarray(values)
    try:
        if np.issubdtype(values.dtype, np.floating):
            filled = np.ma.filled(values, np.nan)
            mask = mask & np.isfinite(filled)
    except TypeError:
        pass
    return mask


def select_sample_indices(valid_count: int, max_points: int) -> Any:
    """Return deterministic sample indices in the valid-value coordinate space."""
    import numpy as np

    limit = max(1, int(max_points or DEFAULT_MAX_POINTS))
    if valid_count <= limit:
        return np.arange(valid_count)
    return np.linspace(0, valid_count - 1, num=limit, dtype=int)


def value_key(value: Any) -> str:
    """Return a stable display key for one attribute value."""
    cleaned = clean_value(value)
    if isinstance(cleaned, float) and cleaned.is_integer():
        return str(int(cleaned))
    return str(cleaned)


def hex_color(rgb: tuple[int, int, int]) -> str:
    """Format RGB as a hex color."""
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def interpolate_color(value: float, min_value: float, max_value: float) -> str:
    """Return a continuous color for a numeric value."""
    if not math.isfinite(value) or not math.isfinite(min_value) or not math.isfinite(max_value) or min_value == max_value:
        return hex_color(CONTINUOUS_STOPS[len(CONTINUOUS_STOPS) // 2])
    position = max(0.0, min(1.0, (value - min_value) / (max_value - min_value)))
    scaled = position * (len(CONTINUOUS_STOPS) - 1)
    left = int(math.floor(scaled))
    right = min(len(CONTINUOUS_STOPS) - 1, left + 1)
    fraction = scaled - left
    rgb = tuple(
        int(round(CONTINUOUS_STOPS[left][channel] * (1.0 - fraction) + CONTINUOUS_STOPS[right][channel] * fraction))
        for channel in range(3)
    )
    return hex_color(rgb)


def normalize_flag_meaning(value: object) -> str:
    """Return a compact display label from a NetCDF flag meaning token."""
    return str(value or "").strip().replace("_", " ")


def flag_value_labels(variable: Any, attribute_path: str = "") -> dict[str, str]:
    """Return value labels from NetCDF flag metadata or PIXC classification defaults."""
    attrs = object_attributes(variable)
    labels: dict[str, str] = {}
    flag_values = attrs.get("flag_values", "")
    flag_meanings = attrs.get("flag_meanings", "")
    if flag_values not in ("", None) and flag_meanings not in ("", None):
        try:
            values = clean_value(flag_values)
            if not isinstance(values, (list, tuple)):
                values = [values]
            meanings = str(flag_meanings).split()
            for value, meaning in zip(values, meanings):
                labels[value_key(value)] = normalize_flag_meaning(meaning)
        except Exception:
            labels = {}
    variable_name = str(attribute_path or "").strip("/").split("/")[-1].lower()
    if not labels and variable_name == "classification":
        labels = {key: normalize_flag_meaning(value) for key, value in PIXC_CLASSIFICATION_LABELS.items()}
    return labels


def display_value_label(key: str, value_labels: Mapping[str, str]) -> str:
    """Return a legend/popup label for one categorical value."""
    meaning = str(value_labels.get(key, "") or "").strip()
    return f"{key} - {meaning}" if meaning else key


def sort_attribute_values(values: Iterable[PixcAttributeValue]) -> list[PixcAttributeValue]:
    """Return attribute values in a stable display order."""

    def key(item: PixcAttributeValue) -> tuple[int, float, str]:
        try:
            return (0, float(item.key), item.key)
        except ValueError:
            return (1, 0.0, item.key.lower())

    return sorted(values, key=key)


def summarize_attribute_values_from_dataset(
    dataset: Any,
    attribute_path: str,
    *,
    max_distinct: int = 500,
) -> tuple[list[PixcAttributeValue], dict[str, str]]:
    """Return distinct valid values for one NetCDF attribute variable."""
    import numpy as np

    if not attribute_path:
        raise ValueError("Choose an attribute before loading attribute values.")
    attribute_variable = variable_by_path(dataset, attribute_path)
    values = read_variable_values(attribute_variable)
    mask = value_valid_mask(values)
    valid_values = np.ma.array(values[mask]).compressed()
    value_labels = flag_value_labels(attribute_variable, attribute_path)
    if valid_values.size == 0:
        return [], value_labels
    unique, counts = np.unique(valid_values, return_counts=True)
    if len(unique) > max_distinct:
        raise ValueError(
            f"Attribute {attribute_path} has more than {max_distinct} distinct values. "
            "Use the full map or choose a categorical attribute for value filtering."
        )
    rows = [
        PixcAttributeValue(
            key=value_key(value),
            label=display_value_label(value_key(value), value_labels),
            meaning=str(value_labels.get(value_key(value), "") or ""),
            count=int(count),
        )
        for value, count in zip(unique, counts)
    ]
    return sort_attribute_values(rows), value_labels


def summarize_pixc_attribute_values(
    file_paths: Sequence[str | Path],
    attribute_path: str,
    *,
    max_distinct: int = 500,
) -> list[PixcAttributeValue]:
    """Return distinct valid values for an attribute across one or more PIXC files."""
    if not file_paths:
        raise ValueError("Choose one or more PIXC NetCDF files before loading attribute values.")
    Dataset = import_netcdf4_dataset()
    counts_by_key: dict[str, int] = {}
    labels: dict[str, str] = {}
    for file_path in file_paths:
        nc_path = Path(file_path)
        if not nc_path.exists() or not nc_path.is_file():
            raise FileNotFoundError(f"PIXC NetCDF file not found: {nc_path}")
        dataset = Dataset(str(nc_path), mode="r")
        try:
            rows, row_labels = summarize_attribute_values_from_dataset(
                dataset,
                attribute_path,
                max_distinct=max_distinct,
            )
            labels.update({key: value for key, value in row_labels.items() if key not in labels})
            for row in rows:
                counts_by_key[row.key] = counts_by_key.get(row.key, 0) + int(row.count)
            if len(counts_by_key) > max_distinct:
                raise ValueError(
                    f"Attribute {attribute_path} has more than {max_distinct} distinct values across selected files. "
                    "Use the full map or choose a categorical attribute for value filtering."
                )
        finally:
            close = getattr(dataset, "close", None)
            if callable(close):
                close()
    rows = [
        PixcAttributeValue(
            key=key,
            label=display_value_label(key, labels),
            meaning=str(labels.get(key, "") or ""),
            count=count,
        )
        for key, count in counts_by_key.items()
    ]
    return sort_attribute_values(rows)


def build_color_mapping(
    values: Any,
    value_labels: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str], list[dict[str, Any]], tuple[float, float] | None]:
    """Build color metadata for categorical or continuous point values."""
    import numpy as np

    value_labels = value_labels or {}
    if values.size == 0:
        return "categorical", {}, [], None
    numeric = np.issubdtype(values.dtype, np.number)
    unique, counts = np.unique(values, return_counts=True)
    categorical = (not numeric) or len(unique) <= CATEGORICAL_MAX_DISTINCT or np.issubdtype(values.dtype, np.integer)
    if categorical:
        order = sorted(zip(unique, counts), key=lambda item: (-int(item[1]), value_key(item[0])))
        mapping: dict[str, str] = {}
        legend: list[dict[str, Any]] = []
        for index, (value, count) in enumerate(order):
            key = value_key(value)
            color = CATEGORY_COLORS[index % len(CATEGORY_COLORS)]
            mapping[key] = color
            legend.append(
                {
                    "value": key,
                    "meaning": str(value_labels.get(key, "") or ""),
                    "label": display_value_label(key, value_labels),
                    "color": color,
                    "count": int(count),
                }
            )
        return "categorical", mapping, legend, None
    numeric_values = values.astype(float)
    min_value = float(numeric_values.min())
    max_value = float(numeric_values.max())
    legend = [
        {"label": f"{min_value:.3f}", "color": interpolate_color(min_value, min_value, max_value)},
        {"label": f"{max_value:.3f}", "color": interpolate_color(max_value, min_value, max_value)},
    ]
    return "continuous", {}, legend, (min_value, max_value)


def build_point_features(
    latitude: Any,
    longitude: Any,
    values: Any,
    *,
    original_indices: Any,
    color_mode: str,
    category_colors: Mapping[str, str],
    continuous_range: tuple[float, float] | None,
    value_labels: Mapping[str, str] | None = None,
    file_name: str = "",
) -> list[dict[str, Any]]:
    """Build browser-ready GeoJSON point features."""
    value_labels = value_labels or {}
    features: list[dict[str, Any]] = []
    for point_index, (lat, lon, value, original_index) in enumerate(
        zip(latitude, longitude, values, original_indices)
    ):
        key = value_key(value)
        if color_mode == "continuous" and continuous_range is not None:
            color = interpolate_color(float(value), continuous_range[0], continuous_range[1])
        else:
            color = category_colors.get(key, CATEGORY_COLORS[point_index % len(CATEGORY_COLORS)])
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(lon), float(lat)],
                },
                "properties": {
                    "index": int(original_index),
                    "value": clean_value(value),
                    "value_label": key,
                    "value_meaning": str(value_labels.get(key, "") or ""),
                    "display_value": display_value_label(key, value_labels),
                    "file_name": file_name,
                    "color": color,
                },
            }
        )
    return features


def sample_pixc_points_from_dataset(dataset: Any, config: PixcPointMapConfig) -> tuple[PixcSampledPoints, dict[str, str]]:
    """Sample valid PIXC points from an open NetCDF dataset before assigning colors."""
    import numpy as np

    catalog = discover_dataset_point_variables(dataset, file_path=config.file_path)
    latitude_path = config.latitude_path or catalog.latitude_path
    longitude_path = config.longitude_path or catalog.longitude_path
    attribute_path = config.attribute_path or catalog.default_attribute_path
    if not latitude_path or not longitude_path:
        raise ValueError("Could not detect latitude/longitude variables for this PIXC file.")
    if not attribute_path:
        raise ValueError("Choose a point attribute variable to visualize.")

    latitude = read_variable_values(variable_by_path(dataset, latitude_path))
    longitude = read_variable_values(variable_by_path(dataset, longitude_path))
    attribute_variable = variable_by_path(dataset, attribute_path)
    values = read_variable_values(attribute_variable)
    value_labels = flag_value_labels(attribute_variable, attribute_path)
    total_points = min(latitude.size, longitude.size, values.size)
    if latitude.size != total_points:
        latitude = latitude[:total_points]
    if longitude.size != total_points:
        longitude = longitude[:total_points]
    if values.size != total_points:
        values = values[:total_points]

    coordinate_mask = valid_coordinate_mask(latitude, longitude)
    reference_bbox = coordinate_bbox(latitude, longitude, coordinate_mask)
    mask = coordinate_mask & value_valid_mask(values)
    valid_indices = np.flatnonzero(mask)
    allowed_value_keys = {str(item) for item in getattr(config, "allowed_value_keys", ()) if str(item)}
    if allowed_value_keys:
        valid_values = np.ma.array(values[valid_indices]).compressed()
        valid_indices = valid_indices[: valid_values.size]
        keep = np.array([value_key(value) in allowed_value_keys for value in valid_values], dtype=bool)
        valid_indices = valid_indices[keep]
    valid_points = int(valid_indices.size)
    if valid_points == 0:
        if allowed_value_keys:
            raise ValueError("No valid PIXC points found for the selected attribute value filter.")
        raise ValueError("No valid PIXC points found for the selected coordinates and attribute.")

    sample_positions = select_sample_indices(valid_points, config.max_points)
    selected_indices = valid_indices[sample_positions]
    sampled_latitude = np.asarray(latitude[selected_indices], dtype=float)
    sampled_longitude = np.asarray(longitude[selected_indices], dtype=float)
    sampled_values = np.ma.array(values[selected_indices]).compressed()
    selected_indices = selected_indices[: sampled_values.size]
    sampled_latitude = sampled_latitude[: sampled_values.size]
    sampled_longitude = sampled_longitude[: sampled_values.size]

    bbox = (
        float(sampled_longitude.min()),
        float(sampled_latitude.min()),
        float(sampled_longitude.max()),
        float(sampled_latitude.max()),
    )
    sampled = PixcSampledPoints(
        file_path=str(config.file_path),
        file_name=Path(config.file_path).name,
        latitude_path=latitude_path,
        longitude_path=longitude_path,
        attribute_path=attribute_path,
        total_points=int(total_points),
        valid_points=valid_points,
        max_points=max(1, int(config.max_points or DEFAULT_MAX_POINTS)),
        sampled=valid_points > max(1, int(config.max_points or DEFAULT_MAX_POINTS)),
        bbox=bbox,
        reference_bbox=reference_bbox,
        latitude=sampled_latitude,
        longitude=sampled_longitude,
        values=sampled_values,
        original_indices=selected_indices,
    )
    return sampled, value_labels


def map_data_from_samples(
    samples: Sequence[PixcSampledPoints],
    *,
    value_labels: Mapping[str, str] | None = None,
) -> PixcMultiPointMapData:
    """Build shared-color multi-file point map data from sampled points."""
    import numpy as np

    if not samples:
        raise ValueError("No PIXC point samples are available.")
    combined_values = np.concatenate([np.asarray(sample.values) for sample in samples])
    color_mode, category_colors, legend, continuous_range = build_color_mapping(combined_values, value_labels)
    layers: list[PixcPointMapLayer] = []
    all_features: list[dict[str, Any]] = []
    west_values: list[float] = []
    south_values: list[float] = []
    east_values: list[float] = []
    north_values: list[float] = []
    reference_west_values: list[float] = []
    reference_south_values: list[float] = []
    reference_east_values: list[float] = []
    reference_north_values: list[float] = []
    for layer_index, sample in enumerate(samples, start=1):
        features = build_point_features(
            sample.latitude,
            sample.longitude,
            sample.values,
            original_indices=sample.original_indices,
            color_mode=color_mode,
            category_colors=category_colors,
            continuous_range=continuous_range,
            value_labels=value_labels,
            file_name=sample.file_name,
        )
        layer_id = f"file_{layer_index}"
        for feature in features:
            feature["properties"]["layer_id"] = layer_id
        all_features.extend(features)
        if sample.bbox is not None:
            west, south, east, north = sample.bbox
            west_values.append(west)
            south_values.append(south)
            east_values.append(east)
            north_values.append(north)
        if sample.reference_bbox is not None:
            ref_west, ref_south, ref_east, ref_north = sample.reference_bbox
            reference_west_values.append(ref_west)
            reference_south_values.append(ref_south)
            reference_east_values.append(ref_east)
            reference_north_values.append(ref_north)
        layers.append(
            PixcPointMapLayer(
                layer_id=layer_id,
                file_path=sample.file_path,
                file_name=sample.file_name,
                total_points=sample.total_points,
                valid_points=sample.valid_points,
                rendered_points=len(features),
                sampled=sample.sampled,
                bbox=sample.bbox,
                reference_bbox=sample.reference_bbox,
                features=features,
            )
        )
    bbox = None
    if west_values:
        bbox = (min(west_values), min(south_values), max(east_values), max(north_values))
    reference_bbox = None
    if reference_west_values:
        reference_bbox = (
            min(reference_west_values),
            min(reference_south_values),
            max(reference_east_values),
            max(reference_north_values),
        )
    return PixcMultiPointMapData(
        file_path=samples[0].file_path,
        file_paths=[sample.file_path for sample in samples],
        attribute_path=samples[0].attribute_path,
        latitude_path=samples[0].latitude_path,
        longitude_path=samples[0].longitude_path,
        total_points=sum(sample.total_points for sample in samples),
        valid_points=sum(sample.valid_points for sample in samples),
        rendered_points=len(all_features),
        max_points=max(sample.max_points for sample in samples),
        sampled=any(sample.sampled for sample in samples),
        bbox=bbox,
        reference_bbox=reference_bbox,
        color_mode=color_mode,
        legend=legend,
        features=all_features,
        layers=[asdict(layer) for layer in layers],
    )


def build_pixc_point_map_from_dataset(dataset: Any, config: PixcPointMapConfig) -> PixcPointMapData:
    """Build sampled point map data from an open NetCDF dataset."""
    sample, value_labels = sample_pixc_points_from_dataset(dataset, config)
    multi = map_data_from_samples([sample], value_labels=value_labels)
    return PixcPointMapData(
        file_path=sample.file_path,
        attribute_path=sample.attribute_path,
        latitude_path=sample.latitude_path,
        longitude_path=sample.longitude_path,
        total_points=sample.total_points,
        valid_points=sample.valid_points,
        rendered_points=multi.rendered_points,
        max_points=sample.max_points,
        sampled=sample.sampled,
        bbox=sample.bbox,
        reference_bbox=sample.reference_bbox,
        color_mode=multi.color_mode,
        legend=multi.legend,
        features=multi.features,
        layers=multi.layers,
    )


def build_pixc_point_map(config: PixcPointMapConfig) -> PixcPointMapData:
    """Open a PIXC NetCDF file and build sampled browser map data."""
    nc_path = Path(config.file_path)
    if not nc_path.exists() or not nc_path.is_file():
        raise FileNotFoundError(f"PIXC NetCDF file not found: {nc_path}")
    Dataset = import_netcdf4_dataset()
    dataset = Dataset(str(nc_path), mode="r")
    try:
        return build_pixc_point_map_from_dataset(dataset, config)
    finally:
        close = getattr(dataset, "close", None)
        if callable(close):
            close()


def build_pixc_multi_point_map(config: PixcMultiPointMapConfig) -> PixcMultiPointMapData:
    """Open one or more PIXC NetCDF files and build a shared-color point map."""
    if not config.file_paths:
        raise ValueError("Choose at least one PIXC NetCDF file to visualize.")
    Dataset = import_netcdf4_dataset()
    samples: list[PixcSampledPoints] = []
    value_labels: dict[str, str] = {}
    for file_path in config.file_paths:
        nc_path = Path(file_path)
        if not nc_path.exists() or not nc_path.is_file():
            raise FileNotFoundError(f"PIXC NetCDF file not found: {nc_path}")
        dataset = Dataset(str(nc_path), mode="r")
        try:
            sample, labels = sample_pixc_points_from_dataset(
                dataset,
                PixcPointMapConfig(
                    file_path=nc_path,
                    attribute_path=config.attribute_path,
                    latitude_path=config.latitude_path,
                    longitude_path=config.longitude_path,
                    max_points=config.max_points_per_file,
                    allowed_value_keys=config.allowed_value_keys,
                ),
            )
            samples.append(sample)
            if labels and not value_labels:
                value_labels = labels
        finally:
            close = getattr(dataset, "close", None)
            if callable(close):
                close()
    return map_data_from_samples(samples, value_labels=value_labels)
