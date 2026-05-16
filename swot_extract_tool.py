"""Extract SWOT NetCDF raster variables to two-band GeoTIFFs with GDAL."""

from __future__ import annotations

import argparse
import csv
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from gdal_runtime import REQUIRED_GDAL_DRIVERS, current_process_gdal_check


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

TARGET_CRS_ORIGINAL = "original"
TARGET_CRS_AFRICA_LAEA = "africa_laea"
TARGET_CRS_WGS84 = "wgs84"
VALID_TARGET_CRS_MODES = {
    TARGET_CRS_ORIGINAL,
    TARGET_CRS_AFRICA_LAEA,
    TARGET_CRS_WGS84,
}
AFRICA_LAEA_PROJ4 = (
    "+proj=laea +lat_0=5 +lon_0=20 +x_0=0 +y_0=0 "
    "+datum=WGS84 +units=m +no_defs"
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "processing": DEFAULT_PROCESSING_PATHS,
    "extract": {
        "input_folder": DEFAULT_PROCESSING_PATHS["raw_downloads"],
        "output_folder": DEFAULT_PROCESSING_PATHS["extracted_geotiffs"],
        "target_crs_mode": TARGET_CRS_ORIGINAL,
        "year_selection": "all",
        "limit_files": None,
        "skip_existing": True,
        "resampling_alg": "near",
        "manifest_csv": f"{DEFAULT_PROCESSING_PATHS['logs']}/extract_manifest.csv",
        "errors_csv": f"{DEFAULT_PROCESSING_PATHS['logs']}/extract_errors.csv",
    },
}

FILENAME_RE = re.compile(
    r"^SWOT_L2_HR_Raster_100m_UTM(?P<utm_zone>\d{2})(?P<mgrs_band>[A-Z])_"
    r"(?P<overlap>[A-Z])_(?P<spare1>[^_]+)_(?P<spare2>[^_]+)_(?P<spare3>[^_]+)_"
    r"(?P<cycle>\d{3})_(?P<pass>\d{3})_(?P<scene>\d{3}[A-Z])_"
    r"(?P<start>\d{8}T\d{6})_(?P<end>\d{8}T\d{6})_"
    r"(?P<crid>[^_]+)_(?P<counter>\d+)(?:_swot)?\.nc$"
)

METADATA_COLUMNS = [
    "utm_zone",
    "mgrs_band",
    "overlap",
    "spare1",
    "spare2",
    "spare3",
    "cycle",
    "pass",
    "scene",
    "start",
    "end",
    "crid",
    "counter",
    "year",
    "date",
]
MANIFEST_COLUMNS = [
    "status",
    "input_nc",
    "output_tif",
    "xsize",
    "ysize",
    "band_count",
    *METADATA_COLUMNS,
]
ERROR_COLUMNS = ["input_nc", "error", *METADATA_COLUMNS]


@dataclass
class ExtractConfig:
    """Runtime settings for the GDAL extraction tool."""

    input_folder: Path
    output_folder: Path
    target_crs_mode: str = TARGET_CRS_ORIGINAL
    year_selection: Any = "all"
    limit_files: Optional[int] = None
    skip_existing: bool = True
    resampling_alg: str = "near"
    manifest_csv: Path = Path(DEFAULT_CONFIG["extract"]["manifest_csv"])
    errors_csv: Path = Path(DEFAULT_CONFIG["extract"]["errors_csv"])
    base_dir: Path = Path.cwd()


@dataclass
class ExtractSource:
    """One selected NetCDF input and parsed SWOT filename metadata."""

    path: Path
    metadata: Dict[str, Any]


@dataclass
class ExtractPlan:
    """Scan result before optional GeoTIFF writing."""

    selected: List[ExtractSource] = field(default_factory=list)
    unmatched: List[Path] = field(default_factory=list)
    available_years: List[int] = field(default_factory=list)
    total_nc_files: int = 0


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
    """Resolve a path against the config file directory."""
    if value in (None, ""):
        raise ValueError("A required extraction path value was empty.")
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def load_config_file(config_path: Path) -> ExtractConfig:
    """Load extraction settings from YAML config."""
    with config_path.open("r", encoding="utf-8") as handle:
        user_config = yaml.safe_load(handle) or {}
    merged = deep_merge(DEFAULT_CONFIG, user_config)
    return parse_config(merged, config_path.parent.resolve())


def parse_config(data: Dict[str, Any], base_dir: Path) -> ExtractConfig:
    """Convert raw config data into a validated ExtractConfig."""
    extract_data = data.get("extract", {})
    processing_data = data.get("processing", {})
    input_folder = (
        extract_data.get("input_folder")
        or processing_data.get("raw_downloads")
        or DEFAULT_PROCESSING_PATHS["raw_downloads"]
    )
    output_folder = (
        extract_data.get("output_folder")
        or processing_data.get("extracted_geotiffs")
        or DEFAULT_PROCESSING_PATHS["extracted_geotiffs"]
    )
    logs_folder = processing_data.get("logs") or DEFAULT_PROCESSING_PATHS["logs"]
    config = ExtractConfig(
        input_folder=resolve_path(input_folder, base_dir),
        output_folder=resolve_path(output_folder, base_dir),
        target_crs_mode=str(
            extract_data.get("target_crs_mode", TARGET_CRS_ORIGINAL)
        ).strip(),
        year_selection=extract_data.get("year_selection", "all"),
        limit_files=normalize_limit_files(extract_data.get("limit_files")),
        skip_existing=bool(extract_data.get("skip_existing", True)),
        resampling_alg=str(extract_data.get("resampling_alg", "near")).strip() or "near",
        manifest_csv=resolve_path(
            extract_data.get("manifest_csv", f"{logs_folder}/extract_manifest.csv"),
            base_dir,
        ),
        errors_csv=resolve_path(
            extract_data.get("errors_csv", f"{logs_folder}/extract_errors.csv"),
            base_dir,
        ),
        base_dir=base_dir,
    )
    validate_config(config)
    return config


def validate_config(config: ExtractConfig) -> None:
    """Raise ValueError when extraction configuration is invalid."""
    if config.target_crs_mode not in VALID_TARGET_CRS_MODES:
        raise ValueError(
            "extract.target_crs_mode must be one of: "
            f"{', '.join(sorted(VALID_TARGET_CRS_MODES))}."
        )
    if config.limit_files is not None and config.limit_files < 0:
        raise ValueError("extract.limit_files cannot be negative.")
    if config.input_folder.exists() and not config.input_folder.is_dir():
        raise ValueError(f"Extraction input path is not a directory: {config.input_folder}")
    if config.output_folder.exists() and not config.output_folder.is_dir():
        raise ValueError(f"Extraction output path is not a directory: {config.output_folder}")


def normalize_limit_files(value: Any) -> Optional[int]:
    """Normalize an optional test limit from config/UI input."""
    if value in (None, ""):
        return None
    return int(value)


def parse_filename(path: Path) -> Optional[Dict[str, Any]]:
    """Parse a SWOT L2 HR Raster 100m NetCDF filename using the notebook regex."""
    match = FILENAME_RE.match(path.name)
    if not match:
        return None
    values: Dict[str, Any] = match.groupdict()
    values["year"] = int(values["start"][:4])
    values["date"] = values["start"][:8]
    return values


def normalize_year_selection(year_selection: Any) -> Optional[set[int]]:
    """Return selected years, or None for all years."""
    if year_selection is None:
        return None
    if isinstance(year_selection, str):
        stripped = year_selection.strip()
        if not stripped or stripped.lower() == "all":
            return None
        return {int(part.strip()) for part in stripped.split(",") if part.strip()}
    if isinstance(year_selection, int):
        return {year_selection}
    if isinstance(year_selection, (list, tuple, set)):
        return {int(year) for year in year_selection}
    raise ValueError("year_selection must be 'all', an int, or a list/comma-separated set of years.")


def build_output_path(nc_path: Path, output_folder: Path, target_crs_mode: str) -> Path:
    """Return the notebook-compatible GeoTIFF output path."""
    suffix = ""
    if target_crs_mode == TARGET_CRS_AFRICA_LAEA:
        suffix = "_africa_laea"
    elif target_crs_mode == TARGET_CRS_WGS84:
        suffix = "_wgs84"
    return output_folder / f"{nc_path.stem}{suffix}.tif"


def build_extraction_plan(config: ExtractConfig) -> ExtractPlan:
    """Scan NetCDF inputs, parse SWOT names, and apply the year/limit filters."""
    selected_years = normalize_year_selection(config.year_selection)
    all_nc_files = sorted(config.input_folder.glob("*.nc")) if config.input_folder.exists() else []
    selected: List[ExtractSource] = []
    unmatched: List[Path] = []
    parsed_years: set[int] = set()

    for path in all_nc_files:
        metadata = parse_filename(path)
        if metadata is None:
            unmatched.append(path)
            continue
        parsed_years.add(int(metadata["year"]))
        if selected_years is not None and metadata["year"] not in selected_years:
            continue
        selected.append(ExtractSource(path=path, metadata=metadata))

    if config.limit_files is not None:
        selected = selected[: config.limit_files]

    return ExtractPlan(
        selected=selected,
        unmatched=unmatched,
        available_years=sorted(parsed_years),
        total_nc_files=len(all_nc_files),
    )


def get_target_srs(target_crs_mode: str) -> Optional[str]:
    """Return the target CRS WKT, or None for original CRS."""
    if target_crs_mode == TARGET_CRS_ORIGINAL:
        return None

    from osgeo import osr

    srs = osr.SpatialReference()
    if target_crs_mode == TARGET_CRS_WGS84:
        srs.ImportFromEPSG(4326)
        return srs.ExportToWkt()
    if target_crs_mode == TARGET_CRS_AFRICA_LAEA:
        srs.ImportFromProj4(AFRICA_LAEA_PROJ4)
        return srs.ExportToWkt()
    raise ValueError(f"Unsupported target CRS mode: {target_crs_mode}")


def require_gdal() -> Any:
    """Import GDAL lazily so planning tests can run outside the GDAL runtime."""
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise RuntimeError(
            "GDAL is required for extraction. Run this tool with the configured "
            "conda GDAL Python, for example D:\\SWOT\\conda_envs\\swot_gdal\\python.exe."
        ) from exc
    gdal.UseExceptions()
    return gdal


def require_netcdf_driver(gdal: Any) -> Any:
    """Return the GDAL netCDF driver or raise with the notebook's installation hint."""
    driver = gdal.GetDriverByName("netCDF")
    if driver is None:
        raise RuntimeError(
            "GDAL netCDF support is not available in this environment.\n"
            "Install the missing plugin with:\n"
            "  mamba install -p D:\\SWOT\\conda_envs\\swot_gdal -c conda-forge libgdal-netcdf"
        )
    return driver


def open_netcdf_root(nc_path: Path, gdal: Any) -> Any:
    """Open the root NetCDF dataset with GDAL."""
    require_netcdf_driver(gdal)
    try:
        dataset = gdal.Open(str(nc_path))
    except RuntimeError as exc:
        raise RuntimeError(f"Could not open NetCDF root dataset: {nc_path.name}\n{exc}") from exc
    if dataset is None:
        raise RuntimeError(f"Could not open NetCDF root dataset: {nc_path.name}")
    return dataset


def open_subdataset(nc_path: Path, variable_name: str, gdal: Any) -> Any:
    """Open a named NetCDF variable subdataset, matching the notebook fallback logic."""
    require_netcdf_driver(gdal)
    explicit_name = f'NETCDF:"{nc_path}":{variable_name}'
    try:
        dataset = gdal.Open(explicit_name)
    except RuntimeError as exc:
        dataset = None
        explicit_error = str(exc)
    else:
        explicit_error = None

    if dataset is not None:
        return dataset

    root = open_netcdf_root(nc_path, gdal)
    subdatasets = root.GetSubDatasets()
    for subdataset_name, subdataset_description in subdatasets:
        if subdataset_name.endswith(f":{variable_name}") or variable_name in subdataset_description:
            try:
                dataset = gdal.Open(subdataset_name)
            except RuntimeError:
                dataset = None
            if dataset is not None:
                return dataset

    available = [name for name, _description in subdatasets[:15]]
    extra = f"\nExplicit NETCDF open error: {explicit_error}" if explicit_error else ""
    raise RuntimeError(
        f"Variable '{variable_name}' could not be opened from {nc_path.name}.{extra}\n"
        f"Available subdatasets (first 15): {available}"
    )


def validate_georef(dataset: Any, label: str = "dataset") -> Tuple[Tuple[float, ...], str]:
    """Validate that GDAL preserved real georeferencing."""
    geotransform = dataset.GetGeoTransform(can_return_null=True)
    projection = dataset.GetProjectionRef()
    if geotransform is None:
        raise RuntimeError(f"{label}: no geotransform found.")
    if not projection:
        raise RuntimeError(f"{label}: no projection found.")

    bad_geotransform = (
        geotransform == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        or geotransform == (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
    )
    if bad_geotransform:
        raise RuntimeError(f"{label}: default geotransform detected: {geotransform}")
    return geotransform, projection


def build_two_band_vrt(nc_path: Path, gdal: Any) -> Tuple[Any, str]:
    """Build a 2-band VRT from `wse` and `wse_qual` NetCDF variables."""
    wse_dataset = open_subdataset(nc_path, "wse", gdal)
    quality_dataset = open_subdataset(nc_path, "wse_qual", gdal)

    validate_georef(wse_dataset, f"{nc_path.name} / wse")
    validate_georef(quality_dataset, f"{nc_path.name} / wse_qual")

    vrt_path = f"/vsimem/{nc_path.stem}.vrt"
    vrt_dataset = gdal.BuildVRT(vrt_path, [wse_dataset, quality_dataset], separate=True)
    if vrt_dataset is None:
        raise RuntimeError(f"Could not build VRT for {nc_path.name}")

    try:
        vrt_dataset.GetRasterBand(1).SetDescription("wse")
        vrt_dataset.GetRasterBand(2).SetDescription("wse_qual")
    except Exception:
        pass

    geotransform = vrt_dataset.GetGeoTransform(can_return_null=True)
    projection = vrt_dataset.GetProjectionRef()
    if geotransform is None or not projection:
        raise RuntimeError(f"{nc_path.name}: VRT lost georeferencing.")
    return vrt_dataset, vrt_path


def export_geotiff_from_vrt(
    vrt_dataset: Any,
    output_path: Path,
    config: ExtractConfig,
    gdal: Any,
) -> None:
    """Write a GeoTIFF with the same GDAL calls used by the notebook."""
    target_wkt = get_target_srs(config.target_crs_mode)
    if target_wkt is None:
        output_dataset = gdal.Translate(str(output_path), vrt_dataset, format="GTiff")
    else:
        output_dataset = gdal.Warp(
            str(output_path),
            vrt_dataset,
            format="GTiff",
            dstSRS=target_wkt,
            resampleAlg=config.resampling_alg,
        )

    if output_dataset is None:
        raise RuntimeError(f"Failed to write {output_path}")
    output_dataset.FlushCache()
    output_dataset = None


def validate_output_geotiff(output_path: Path, gdal: Any) -> Dict[str, Any]:
    """Reopen and validate an output GeoTIFF."""
    dataset = gdal.Open(str(output_path))
    if dataset is None:
        raise RuntimeError(f"Could not reopen output: {output_path.name}")
    validate_georef(dataset, output_path.name)
    return {
        "xsize": dataset.RasterXSize,
        "ysize": dataset.RasterYSize,
        "band_count": dataset.RasterCount,
    }


def process_one_file(source: ExtractSource, config: ExtractConfig, gdal: Any) -> Dict[str, Any]:
    """Extract one NetCDF file into one 2-band GeoTIFF."""
    output_path = build_output_path(source.path, config.output_folder, config.target_crs_mode)
    if config.skip_existing and output_path.exists():
        info = validate_output_geotiff(output_path, gdal)
        return {
            "status": "skipped_existing",
            "input_nc": str(source.path),
            "output_tif": str(output_path),
            "xsize": info["xsize"],
            "ysize": info["ysize"],
            "band_count": info["band_count"],
            **source.metadata,
        }

    vrt_dataset, vrt_path = build_two_band_vrt(source.path, gdal)
    try:
        export_geotiff_from_vrt(vrt_dataset, output_path, config, gdal)
    finally:
        vrt_dataset = None
        gdal.Unlink(vrt_path)

    info = validate_output_geotiff(output_path, gdal)
    return {
        "status": "written",
        "input_nc": str(source.path),
        "output_tif": str(output_path),
        "xsize": info["xsize"],
        "ysize": info["ysize"],
        "band_count": info["band_count"],
        **source.metadata,
    }


def unmatched_error_rows(plan: ExtractPlan) -> List[Dict[str, Any]]:
    """Return error CSV rows for NetCDF files that did not match the SWOT pattern."""
    return [
        {
            "input_nc": str(path),
            "error": "Filename does not match SWOT_L2_HR_Raster_100m NetCDF pattern.",
        }
        for path in plan.unmatched
    ]


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    """Write CSV rows using a fixed field order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def iter_with_progress(items: Sequence[ExtractSource]) -> Iterable[ExtractSource]:
    """Use tqdm when available, matching the notebook progress behavior."""
    try:
        from tqdm import tqdm
    except ImportError:
        return items
    return tqdm(items, total=len(items), desc="Extracting SWOT GeoTIFFs")


def run_extract(
    config: ExtractConfig,
    dry_run: bool = False,
    check_gdal: bool = True,
    progress_callback: Optional[ProgressCallback] = None,
) -> Tuple[int, ExtractPlan, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Plan and optionally write extraction outputs."""
    if check_gdal:
        current_process_gdal_check(REQUIRED_GDAL_DRIVERS)
        require_netcdf_driver(require_gdal())

    plan = build_extraction_plan(config)
    if dry_run:
        return 0, plan, [], []

    config.output_folder.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = unmatched_error_rows(plan)
    gdal = require_gdal()
    total = len(plan.selected)
    if progress_callback is not None:
        progress_callback(0, total, "Starting extraction")

    iterator = plan.selected if progress_callback is not None else iter_with_progress(plan.selected)
    for index, source in enumerate(iterator, start=1):
        if progress_callback is not None:
            progress_callback(index - 1, total, f"Processing {source.path.name}")
        status = "done"
        try:
            row = process_one_file(source, config, gdal)
            results.append(row)
            status = row.get("status", "done")
        except Exception as exc:  # noqa: BLE001
            status = "error"
            errors.append(
                {
                    "input_nc": str(source.path),
                    "error": f"{type(exc).__name__}: {exc}",
                    **source.metadata,
                }
            )
        if progress_callback is not None:
            progress_callback(index, total, f"{status}: {source.path.name}")

    write_csv(config.manifest_csv, MANIFEST_COLUMNS, results)
    write_csv(config.errors_csv, ERROR_COLUMNS, errors)
    return (2 if errors else 0), plan, results, errors


def summarize_plan(plan: ExtractPlan) -> Dict[str, Any]:
    """Return concise extraction scan counts."""
    return {
        "total_nc_files": plan.total_nc_files,
        "selected_files": len(plan.selected),
        "unmatched_files": len(plan.unmatched),
        "available_years": plan.available_years,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Extract wse and wse_qual from SWOT NetCDF files to two-band GeoTIFFs."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml. Defaults to ./config.yaml.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report selected files without writing GeoTIFFs or CSVs.",
    )
    parser.add_argument(
        "--check-gdal",
        action="store_true",
        help="Validate that the current Python process can import GDAL and load required drivers.",
    )
    return parser


def print_progress(current: int, total: int, message: str) -> None:
    """Emit one machine-readable progress line for the GUI launcher."""
    safe_message = message.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    print(f"{PROGRESS_PREFIX}\textract\t{current}\t{total}\t{safe_message}", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    args = build_arg_parser().parse_args(argv)
    if args.check_gdal:
        print(current_process_gdal_check(REQUIRED_GDAL_DRIVERS))
        return 0

    config = load_config_file(args.config.resolve())
    print(current_process_gdal_check(REQUIRED_GDAL_DRIVERS))
    exit_code, plan, results, errors = run_extract(
        config,
        dry_run=args.dry_run,
        progress_callback=None if args.dry_run else print_progress,
    )
    mode = "dry run" if args.dry_run else "extraction run"
    print(
        textwrap.dedent(
            f"""
            SWOT GeoTIFF {mode} complete.
            Input folder: {config.input_folder}
            Output folder: {config.output_folder}
            Target CRS mode: {config.target_crs_mode}
            Year selection: {config.year_selection}
            Limit files: {config.limit_files}
            Manifest CSV: {config.manifest_csv}
            Errors CSV: {config.errors_csv}
            Plan counts: {summarize_plan(plan)}
            Successful rows: {len(results)}
            Error rows: {len(errors)}
            """
        ).strip()
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
