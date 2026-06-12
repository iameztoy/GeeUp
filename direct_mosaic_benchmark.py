"""Benchmark staged GeoTIFF mosaics against direct SWOT NetCDF mosaics.

This prototype is intentionally isolated from SWOTFlow's production manifests,
database, and mosaic folder. It runs both methods in separate subprocesses,
records elapsed time and peak process memory, and compares final rasters.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

import yaml

from ee_mosaic_tool import (
    GROUPING_MODE_UTM_ZONE,
    MosaicConfig,
    MosaicGroup,
    MosaicGroupKey,
    MosaicSource,
    build_mosaic_plan,
    merge_group,
)
from gdal_runtime import (
    DEFAULT_GDAL_PYTHON,
    build_gdal_runtime_env,
    current_process_gdal_check,
)
from swot_extract_tool import build_two_band_vrt, open_subdataset, validate_georef
from swot_metadata import parse_swot_l2_hr_raster_metadata


DEFAULT_SAMPLE_GROUPS = 12
DEFAULT_GDAL_CACHE_MB = 256
SUPPORTED_GROUPING_MODE = GROUPING_MODE_UTM_ZONE


@dataclass(frozen=True)
class BenchmarkGroup:
    """Serializable description of one selected mosaic group."""

    key: Dict[str, str]
    output_name: str
    sources: list[str]


def load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML mapping, returning an empty mapping for an empty file."""
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return data


def configured_path(
    data: Mapping[str, Any],
    section: str,
    key: str,
    base_dir: Path,
) -> Path | None:
    """Return one optional config path resolved relative to the config file."""
    section_data = data.get(section, {})
    if not isinstance(section_data, Mapping):
        return None
    value = str(section_data.get(key, "") or "").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def select_representative_groups(
    groups: Sequence[MosaicGroup],
    limit: int,
    seed: int,
) -> list[MosaicGroup]:
    """Select a deterministic sample while covering observed group sizes."""
    if limit <= 0 or limit >= len(groups):
        return list(groups)

    by_size: Dict[int, list[MosaicGroup]] = {}
    for group in groups:
        by_size.setdefault(len(group.sources), []).append(group)

    randomizer = random.Random(seed)
    for bucket in by_size.values():
        randomizer.shuffle(bucket)

    selected: list[MosaicGroup] = []
    selected_ids: set[str] = set()
    for source_count in sorted(by_size):
        if len(selected) >= limit:
            break
        group = by_size[source_count][0]
        selected.append(group)
        selected_ids.add(group.output_file.name)

    remaining = [
        group
        for group in groups
        if group.output_file.name not in selected_ids
    ]
    randomizer.shuffle(remaining)
    selected.extend(remaining[: max(0, limit - len(selected))])
    return sorted(selected, key=lambda group: group.output_file.name.lower())


def serialize_group(group: MosaicGroup) -> BenchmarkGroup:
    """Convert a planned group to a compact JSON-safe payload."""
    return BenchmarkGroup(
        key=asdict(group.key),
        output_name=group.output_file.name,
        sources=[str(source.path) for source in group.sources],
    )


def deserialize_group(
    payload: Mapping[str, Any],
    output_folder: Path,
    source_paths: Sequence[Path] | None = None,
) -> MosaicGroup:
    """Rebuild a mosaic group, optionally replacing raw source paths."""
    raw_sources = [Path(value) for value in payload["sources"]]
    working_sources = list(source_paths) if source_paths is not None else raw_sources
    if len(raw_sources) != len(working_sources):
        raise ValueError("Working source count does not match the benchmark plan.")

    sources: list[MosaicSource] = []
    for raw_path, working_path in zip(raw_sources, working_sources):
        metadata = parse_swot_l2_hr_raster_metadata(raw_path)
        if metadata is None:
            raise ValueError(f"Could not parse planned SWOT source: {raw_path.name}")
        sources.append(MosaicSource(path=working_path, metadata=metadata))

    return MosaicGroup(
        key=MosaicGroupKey(**dict(payload["key"])),
        sources=sources,
        output_file=output_folder / str(payload["output_name"]),
    )


def build_benchmark_groups(
    raw_folder: Path,
    planning_output: Path,
    recursive: bool,
    utm_tiles: Sequence[str],
    sample_groups: int,
    seed: int,
) -> tuple[list[BenchmarkGroup], int]:
    """Plan raw NetCDF groups using the production mosaic naming rules."""
    config = MosaicConfig(
        input_folder=raw_folder,
        output_folder=planning_output,
        grouping_mode=SUPPORTED_GROUPING_MODE,
        recursive=recursive,
        write_world_file=False,
        extensions=[".nc"],
        utm_tiles=list(utm_tiles),
    )
    plan = build_mosaic_plan(config)
    selected = select_representative_groups(plan.groups, sample_groups, seed)
    return [serialize_group(group) for group in selected], len(plan.report_rows)


def paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either resolved path contains the other."""
    left = left.resolve()
    right = right.resolve()
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def validate_output_location(raw_folder: Path, output_root: Path) -> None:
    """Keep benchmark artifacts outside the raw input tree."""
    if paths_overlap(raw_folder, output_root):
        raise ValueError(
            "Benchmark output must not contain, equal, or be contained by the raw input folder."
        )


def folder_bytes(folder: Path) -> int:
    """Return the size of all files below one folder."""
    if not folder.exists():
        return 0
    total = 0
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def peak_rss_bytes() -> int:
    """Return peak resident memory for the current process when available."""
    try:
        import psutil

        info = psutil.Process(os.getpid()).memory_info()
        return int(getattr(info, "peak_wset", info.rss))
    except (ImportError, OSError):
        pass

    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, OSError):
        return 0


def require_gdal(cache_mb: int) -> Any:
    """Initialize GDAL and constrain its process-wide block cache."""
    current_process_gdal_check()
    from osgeo import gdal

    gdal.UseExceptions()
    gdal.SetCacheMax(max(1, int(cache_mb)) * 1024 * 1024)
    return gdal


def create_direct_vrt(nc_path: Path, vrt_path: Path, gdal: Any) -> None:
    """Create a tiny two-band VRT that references one raw SWOT NetCDF."""
    wse_dataset = open_subdataset(nc_path, "wse", gdal)
    quality_dataset = open_subdataset(nc_path, "wse_qual", gdal)
    validate_georef(wse_dataset, f"{nc_path.name} / wse")
    validate_georef(quality_dataset, f"{nc_path.name} / wse_qual")
    if (
        wse_dataset.RasterXSize != quality_dataset.RasterXSize
        or wse_dataset.RasterYSize != quality_dataset.RasterYSize
    ):
        raise RuntimeError(f"{nc_path.name}: wse and wse_qual dimensions differ.")

    vrt_path.parent.mkdir(parents=True, exist_ok=True)
    vrt_dataset = gdal.BuildVRT(
        str(vrt_path),
        [wse_dataset, quality_dataset],
        options=gdal.BuildVRTOptions(separate=True),
    )
    if vrt_dataset is None:
        raise RuntimeError(f"Could not build direct source VRT for {nc_path.name}")
    vrt_dataset.GetRasterBand(1).SetDescription("wse")
    vrt_dataset.GetRasterBand(2).SetDescription("wse_qual")
    vrt_dataset.FlushCache()
    vrt_dataset = None
    wse_dataset = None
    quality_dataset = None


def extract_staged_source(nc_path: Path, output_path: Path, gdal: Any) -> None:
    """Run the current extraction operation without writing stage manifests."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vrt_dataset, vrt_path = build_two_band_vrt(nc_path, gdal)
    try:
        output_dataset = gdal.Translate(str(output_path), vrt_dataset, format="GTiff")
        if output_dataset is None:
            raise RuntimeError(f"Could not extract staged source: {nc_path.name}")
        output_dataset.FlushCache()
        output_dataset = None
    finally:
        vrt_dataset = None
        try:
            gdal.Unlink(vrt_path)
        except Exception:
            pass


def worker_mosaic_config(output_folder: Path) -> MosaicConfig:
    """Return the narrow config used by isolated benchmark workers."""
    return MosaicConfig(
        input_folder=output_folder.parent,
        output_folder=output_folder,
        grouping_mode=SUPPORTED_GROUPING_MODE,
        overwrite=True,
        write_world_file=False,
        extensions=[".tif"],
        workers=1,
    )


def run_staged_worker(
    groups: Sequence[Mapping[str, Any]],
    method_root: Path,
    gdal: Any,
    keep_intermediates: bool,
) -> Dict[str, Any]:
    """Extract all selected inputs, then mosaic the extracted GeoTIFFs."""
    extracted_root = method_root / "intermediate_geotiffs"
    output_root = method_root / "outputs"
    source_map: Dict[str, Path] = {}

    for payload in groups:
        for raw_value in payload["sources"]:
            raw_path = Path(raw_value)
            key = str(raw_path.resolve()).casefold()
            if key in source_map:
                continue
            extracted_path = extracted_root / f"source_{len(source_map):06d}.tif"
            extract_staged_source(raw_path, extracted_path, gdal)
            source_map[key] = extracted_path

    peak_intermediate_bytes = folder_bytes(extracted_root)
    mosaic_config = worker_mosaic_config(output_root)
    completed = 0
    for payload in groups:
        staged_paths = [
            source_map[str(Path(value).resolve()).casefold()]
            for value in payload["sources"]
        ]
        group = deserialize_group(payload, output_root, staged_paths)
        merge_group(group, mosaic_config)
        completed += 1

    if not keep_intermediates:
        shutil.rmtree(extracted_root, ignore_errors=True)
    return {
        "groups_completed": completed,
        "inputs_processed": len(source_map),
        "peak_intermediate_bytes": peak_intermediate_bytes,
        "final_output_bytes": folder_bytes(output_root),
    }


def run_direct_worker(
    groups: Sequence[Mapping[str, Any]],
    method_root: Path,
    gdal: Any,
    keep_intermediates: bool,
) -> Dict[str, Any]:
    """Create group-local VRTs from NetCDF and write only final mosaics."""
    vrt_root = method_root / "source_vrts"
    output_root = method_root / "outputs"
    mosaic_config = worker_mosaic_config(output_root)
    peak_intermediate_bytes = 0
    inputs_processed = 0
    completed = 0

    for group_index, payload in enumerate(groups):
        group_vrt_root = vrt_root / f"group_{group_index:04d}"
        vrt_paths: list[Path] = []
        for source_index, raw_value in enumerate(payload["sources"]):
            raw_path = Path(raw_value)
            vrt_path = group_vrt_root / f"source_{source_index:02d}.vrt"
            create_direct_vrt(raw_path, vrt_path, gdal)
            vrt_paths.append(vrt_path)
            inputs_processed += 1
        peak_intermediate_bytes = max(
            peak_intermediate_bytes,
            folder_bytes(vrt_root),
        )

        group = deserialize_group(payload, output_root, vrt_paths)
        merge_group(group, mosaic_config)
        completed += 1
        if not keep_intermediates:
            shutil.rmtree(group_vrt_root, ignore_errors=True)

    if not keep_intermediates:
        shutil.rmtree(vrt_root, ignore_errors=True)
    return {
        "groups_completed": completed,
        "inputs_processed": inputs_processed,
        "peak_intermediate_bytes": peak_intermediate_bytes,
        "final_output_bytes": folder_bytes(output_root),
    }


def raster_structure(dataset: Any, gdal: Any) -> Dict[str, Any]:
    """Return structural fields that must match between benchmark outputs."""
    return {
        "width": dataset.RasterXSize,
        "height": dataset.RasterYSize,
        "bands": dataset.RasterCount,
        "projection": dataset.GetProjectionRef(),
        "geotransform": list(dataset.GetGeoTransform()),
        "data_types": [
            gdal.GetDataTypeName(dataset.GetRasterBand(index).DataType)
            for index in range(1, dataset.RasterCount + 1)
        ],
        "nodata": [
            dataset.GetRasterBand(index).GetNoDataValue()
            for index in range(1, dataset.RasterCount + 1)
        ],
        "descriptions": [
            dataset.GetRasterBand(index).GetDescription()
            for index in range(1, dataset.RasterCount + 1)
        ],
    }


def compare_rasters(
    staged_path: Path,
    direct_path: Path,
    gdal: Any,
    tolerance: float,
) -> Dict[str, Any]:
    """Compare two outputs block by block without loading whole rasters."""
    import numpy as np

    staged = gdal.Open(str(staged_path))
    direct = gdal.Open(str(direct_path))
    if staged is None or direct is None:
        return {"equivalent": False, "reason": "Could not open one or both outputs."}

    staged_structure = raster_structure(staged, gdal)
    direct_structure = raster_structure(direct, gdal)
    if staged_structure != direct_structure:
        return {
            "equivalent": False,
            "reason": "Raster structure differs.",
            "staged_structure": staged_structure,
            "direct_structure": direct_structure,
        }

    mismatch_pixels = [0 for _ in range(staged.RasterCount)]
    max_abs_diff = [0.0 for _ in range(staged.RasterCount)]
    block_width = min(512, staged.RasterXSize)
    block_height = min(512, staged.RasterYSize)

    for band_index in range(1, staged.RasterCount + 1):
        left_band = staged.GetRasterBand(band_index)
        right_band = direct.GetRasterBand(band_index)
        nodata = left_band.GetNoDataValue()
        for y_offset in range(0, staged.RasterYSize, block_height):
            rows = min(block_height, staged.RasterYSize - y_offset)
            for x_offset in range(0, staged.RasterXSize, block_width):
                columns = min(block_width, staged.RasterXSize - x_offset)
                left = left_band.ReadAsArray(x_offset, y_offset, columns, rows)
                right = right_band.ReadAsArray(x_offset, y_offset, columns, rows)
                if left is None or right is None:
                    raise RuntimeError("Could not read an output block during comparison.")

                if nodata is None:
                    left_valid = np.ones(left.shape, dtype=bool)
                    right_valid = np.ones(right.shape, dtype=bool)
                elif isinstance(nodata, float) and np.isnan(nodata):
                    left_valid = ~np.isnan(left)
                    right_valid = ~np.isnan(right)
                else:
                    left_valid = left != nodata
                    right_valid = right != nodata

                valid_mismatch = left_valid != right_valid
                common_valid = left_valid & right_valid
                different = valid_mismatch.copy()
                if np.any(common_valid):
                    delta = np.abs(
                        left[common_valid].astype(np.float64)
                        - right[common_valid].astype(np.float64)
                    )
                    max_abs_diff[band_index - 1] = max(
                        max_abs_diff[band_index - 1],
                        float(np.max(delta)),
                    )
                    different[common_valid] |= delta > tolerance
                mismatch_pixels[band_index - 1] += int(np.count_nonzero(different))

    staged = None
    direct = None
    return {
        "equivalent": not any(mismatch_pixels),
        "mismatch_pixels_by_band": mismatch_pixels,
        "max_abs_diff_by_band": max_abs_diff,
    }


def run_compare_worker(
    groups: Sequence[Mapping[str, Any]],
    run_root: Path,
    gdal: Any,
    tolerance: float,
) -> Dict[str, Any]:
    """Compare every staged and direct final mosaic."""
    rows: list[Dict[str, Any]] = []
    for payload in groups:
        output_name = str(payload["output_name"])
        comparison = compare_rasters(
            run_root / "staged" / "outputs" / output_name,
            run_root / "direct" / "outputs" / output_name,
            gdal,
            tolerance,
        )
        rows.append({"output_name": output_name, **comparison})
    return {
        "groups_compared": len(rows),
        "equivalent_groups": sum(1 for row in rows if row["equivalent"]),
        "mismatched_groups": sum(1 for row in rows if not row["equivalent"]),
        "comparisons": rows,
    }


def read_plan(path: Path) -> list[Dict[str, Any]]:
    """Load selected groups from the parent process plan."""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    groups = payload.get("groups", [])
    if not isinstance(groups, list):
        raise ValueError("Benchmark plan groups must be a list.")
    return [dict(group) for group in groups]


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write formatted JSON, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def run_worker(args: argparse.Namespace) -> int:
    """Run one isolated benchmark method or comparison process."""
    started = time.perf_counter()
    result: Dict[str, Any] = {
        "method": args.worker_mode,
        "status": "FAILED",
        "elapsed_seconds": 0.0,
        "peak_rss_bytes": 0,
    }
    try:
        gdal = require_gdal(args.gdal_cache_mb)
        groups = read_plan(Path(args.plan_json))
        run_root = Path(args.run_root)
        if args.worker_mode == "staged":
            details = run_staged_worker(
                groups,
                run_root / "staged",
                gdal,
                args.keep_intermediates,
            )
        elif args.worker_mode == "direct":
            details = run_direct_worker(
                groups,
                run_root / "direct",
                gdal,
                args.keep_intermediates,
            )
        elif args.worker_mode == "compare":
            details = run_compare_worker(
                groups,
                run_root,
                gdal,
                args.tolerance,
            )
        else:
            raise ValueError(f"Unknown worker mode: {args.worker_mode}")
        result.update(details)
        result["status"] = "COMPLETED"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        result["peak_rss_bytes"] = peak_rss_bytes()
        write_json(Path(args.result_json), result)
    return 0 if result["status"] == "COMPLETED" else 1


def run_child(
    python_path: Path,
    mode: str,
    plan_path: Path,
    run_root: Path,
    result_path: Path,
    cache_mb: int,
    tolerance: float,
    keep_intermediates: bool,
) -> Dict[str, Any]:
    """Launch one isolated worker and return its JSON result."""
    command = [
        str(python_path),
        str(Path(__file__).resolve()),
        "--worker-mode",
        mode,
        "--plan-json",
        str(plan_path),
        "--run-root",
        str(run_root),
        "--result-json",
        str(result_path),
        "--gdal-cache-mb",
        str(cache_mb),
        "--tolerance",
        str(tolerance),
    ]
    if keep_intermediates:
        command.append("--keep-intermediates")
    completed = subprocess.run(
        command,
        cwd=str(Path(__file__).resolve().parent),
        env=build_gdal_runtime_env(python_path),
        text=True,
        capture_output=True,
        check=False,
    )
    if not result_path.exists():
        raise RuntimeError(
            f"{mode} worker did not write a result (exit {completed.returncode}). "
            f"{completed.stdout}{completed.stderr}"
        )
    with result_path.open("r", encoding="utf-8") as handle:
        result = json.load(handle)
    result["return_code"] = completed.returncode
    result["stdout"] = completed.stdout
    result["stderr"] = completed.stderr
    return result


def write_summary_csv(path: Path, results: Iterable[Mapping[str, Any]]) -> None:
    """Write one compact row per benchmark method."""
    fields = [
        "method",
        "status",
        "elapsed_seconds",
        "peak_rss_mb",
        "groups_completed",
        "inputs_processed",
        "peak_intermediate_gb",
        "final_output_gb",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "method": result.get("method", ""),
                    "status": result.get("status", ""),
                    "elapsed_seconds": result.get("elapsed_seconds", ""),
                    "peak_rss_mb": round(
                        int(result.get("peak_rss_bytes", 0)) / (1024 * 1024),
                        2,
                    ),
                    "groups_completed": result.get("groups_completed", ""),
                    "inputs_processed": result.get("inputs_processed", ""),
                    "peak_intermediate_gb": round(
                        int(result.get("peak_intermediate_bytes", 0)) / (1024**3),
                        4,
                    ),
                    "final_output_gb": round(
                        int(result.get("final_output_bytes", 0)) / (1024**3),
                        4,
                    ),
                    "error": result.get("error", ""),
                }
            )


def create_synthetic_netcdf(
    path: Path,
    x_origin: float,
    wse_value: float,
    quality_value: float,
    gdal: Any,
) -> None:
    """Create a tiny georeferenced two-variable NetCDF for smoke testing."""
    import numpy as np
    from osgeo import osr

    path.parent.mkdir(parents=True, exist_ok=True)
    memory = gdal.GetDriverByName("MEM").Create("", 4, 3, 2, gdal.GDT_Float32)
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromEPSG(32636)
    memory.SetProjection(spatial_ref.ExportToWkt())
    memory.SetGeoTransform((x_origin, 100.0, 0.0, 1_000_000.0, 0.0, -100.0))

    wse_band = memory.GetRasterBand(1)
    wse_band.SetDescription("wse")
    wse_band.SetMetadataItem("NETCDF_VARNAME", "wse")
    wse_band.SetNoDataValue(9.969209968386869e36)
    wse_band.WriteArray(np.full((3, 4), wse_value, dtype=np.float32))

    quality_band = memory.GetRasterBand(2)
    quality_band.SetDescription("wse_qual")
    quality_band.SetMetadataItem("NETCDF_VARNAME", "wse_qual")
    quality_band.SetNoDataValue(255)
    quality_band.WriteArray(np.full((3, 4), quality_value, dtype=np.float32))

    output = gdal.GetDriverByName("netCDF").CreateCopy(str(path), memory)
    if output is None:
        raise RuntimeError(f"Could not create synthetic NetCDF: {path}")
    output = None
    memory = None


def prepare_synthetic_inputs(output_root: Path, cache_mb: int) -> Path:
    """Create two tiny groups used to validate the complete benchmark."""
    gdal = require_gdal(cache_mb)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    raw_folder = output_root.parent / (
        f"_direct_mosaic_synthetic_raw_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    names = [
        (
            "SWOT_L2_HR_Raster_100m_UTM36M_N_x_x_x_035_225_000A_"
            "20250707T010000_20250707T010100_PID0_01.nc",
            500_000.0,
            10.0,
            1.0,
        ),
        (
            "SWOT_L2_HR_Raster_100m_UTM36M_N_x_x_x_035_225_001A_"
            "20250707T011000_20250707T011100_PID0_01.nc",
            500_300.0,
            20.0,
            2.0,
        ),
        (
            "SWOT_L2_HR_Raster_100m_UTM36M_N_x_x_x_036_226_000A_"
            "20250708T010000_20250708T010100_PID0_01.nc",
            500_000.0,
            30.0,
            3.0,
        ),
    ]
    for name, x_origin, wse_value, quality_value in names:
        create_synthetic_netcdf(
            raw_folder / name,
            x_origin,
            wse_value,
            quality_value,
            gdal,
        )
    return raw_folder


def default_run_root(output_root: Path) -> Path:
    """Return a unique benchmark run folder."""
    return output_root / datetime.now().strftime("%Y%m%d_%H%M%S")


def parent_main(args: argparse.Namespace) -> int:
    """Plan and run the full staged/direct benchmark."""
    config_path = Path(args.config).resolve()
    config_data = load_yaml(config_path) if config_path.exists() else {}
    base_dir = config_path.parent

    configured_raw = configured_path(config_data, "extract", "input_folder", base_dir)
    configured_logs = configured_path(config_data, "processing", "logs", base_dir)
    configured_python = configured_path(config_data, "gdal", "python", base_dir)
    mosaic_data = config_data.get("mosaic", {})
    configured_grouping = (
        str(mosaic_data.get("grouping_mode", SUPPORTED_GROUPING_MODE))
        if isinstance(mosaic_data, Mapping)
        else SUPPORTED_GROUPING_MODE
    )
    grouping_mode = args.grouping_mode or configured_grouping
    if grouping_mode != SUPPORTED_GROUPING_MODE:
        raise ValueError(
            "The prototype currently supports only grouping_mode=utm_zone. "
            "Common-CRS direct mosaics require a separate reprojection design."
        )

    gdal_python = Path(args.gdal_python).resolve() if args.gdal_python else configured_python
    if gdal_python is None:
        gdal_python = (Path.cwd() / DEFAULT_GDAL_PYTHON).resolve()
    if not gdal_python.exists():
        raise ValueError(f"GDAL Python executable does not exist: {gdal_python}")

    output_root = (
        Path(args.output_root).resolve()
        if args.output_root
        else (configured_logs or (Path.cwd() / "benchmark_results"))
        / "direct_mosaic_benchmarks"
    )

    if args.synthetic_smoke_test:
        raw_folder = prepare_synthetic_inputs(output_root, args.gdal_cache_mb)
    else:
        raw_folder = Path(args.raw_folder).resolve() if args.raw_folder else configured_raw
        if raw_folder is None:
            raise ValueError("Provide --raw-folder or configure extract.input_folder.")
    if not raw_folder.exists() or not raw_folder.is_dir():
        raise ValueError(f"Raw NetCDF folder does not exist: {raw_folder}")

    validate_output_location(raw_folder, output_root)
    run_root = default_run_root(output_root)
    run_root.mkdir(parents=True, exist_ok=False)

    groups, invalid_filename_count = build_benchmark_groups(
        raw_folder=raw_folder,
        planning_output=run_root / "_planning_only",
        recursive=args.recursive,
        utm_tiles=args.utm_tile,
        sample_groups=args.sample_groups,
        seed=args.seed,
    )
    if not groups:
        raise ValueError("No valid SWOT NetCDF mosaic groups were found.")

    plan_path = run_root / "benchmark_plan.json"
    write_json(
        plan_path,
        {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "raw_folder": str(raw_folder),
            "grouping_mode": grouping_mode,
            "sample_groups": len(groups),
            "source_inputs": sum(len(group.sources) for group in groups),
            "invalid_filename_count": invalid_filename_count,
            "groups": [asdict(group) for group in groups],
        },
    )

    results: list[Dict[str, Any]] = []
    for mode in ("staged", "direct"):
        result = run_child(
            python_path=gdal_python,
            mode=mode,
            plan_path=plan_path,
            run_root=run_root,
            result_path=run_root / f"{mode}_result.json",
            cache_mb=args.gdal_cache_mb,
            tolerance=args.tolerance,
            keep_intermediates=args.keep_intermediates,
        )
        results.append(result)
        if result.get("status") != "COMPLETED":
            break

    comparison: Dict[str, Any] = {"status": "NOT_RUN"}
    if len(results) == 2 and all(result.get("status") == "COMPLETED" for result in results):
        comparison = run_child(
            python_path=gdal_python,
            mode="compare",
            plan_path=plan_path,
            run_root=run_root,
            result_path=run_root / "comparison_result.json",
            cache_mb=args.gdal_cache_mb,
            tolerance=args.tolerance,
            keep_intermediates=args.keep_intermediates,
        )

    summary = {
        "run_root": str(run_root),
        "raw_folder": str(raw_folder),
        "gdal_python": str(gdal_python),
        "gdal_cache_mb": args.gdal_cache_mb,
        "sample_groups": len(groups),
        "source_inputs": sum(len(group.sources) for group in groups),
        "methods": results,
        "comparison": comparison,
    }
    if len(results) == 2 and all(result.get("status") == "COMPLETED" for result in results):
        staged_result, direct_result = results
        staged_seconds = float(staged_result.get("elapsed_seconds", 0.0))
        direct_seconds = float(direct_result.get("elapsed_seconds", 0.0))
        staged_intermediate = int(staged_result.get("peak_intermediate_bytes", 0))
        direct_intermediate = int(direct_result.get("peak_intermediate_bytes", 0))
        summary["derived"] = {
            "speedup_ratio": (
                round(staged_seconds / direct_seconds, 4)
                if direct_seconds > 0
                else None
            ),
            "elapsed_reduction_percent": (
                round((staged_seconds - direct_seconds) * 100.0 / staged_seconds, 2)
                if staged_seconds > 0
                else None
            ),
            "intermediate_reduction_percent": (
                round(
                    (staged_intermediate - direct_intermediate)
                    * 100.0
                    / staged_intermediate,
                    2,
                )
                if staged_intermediate > 0
                else None
            ),
            "peak_rss_delta_mb": round(
                (
                    int(direct_result.get("peak_rss_bytes", 0))
                    - int(staged_result.get("peak_rss_bytes", 0))
                )
                / (1024 * 1024),
                2,
            ),
        }
    write_json(run_root / "benchmark_summary.json", summary)
    write_summary_csv(run_root / "benchmark_summary.csv", results)

    print(f"Benchmark folder: {run_root}")
    for result in results:
        peak_mb = int(result.get("peak_rss_bytes", 0)) / (1024 * 1024)
        intermediate_gb = int(result.get("peak_intermediate_bytes", 0)) / (1024**3)
        print(
            f"{result.get('method')}: {result.get('status')}, "
            f"{result.get('elapsed_seconds')} s, peak RSS {peak_mb:.1f} MB, "
            f"intermediate {intermediate_gb:.3f} GB"
        )
    if comparison.get("status") == "COMPLETED":
        print(
            "Equivalent outputs: "
            f"{comparison.get('equivalent_groups', 0)}/"
            f"{comparison.get('groups_compared', 0)}"
        )
    derived = summary.get("derived", {})
    if isinstance(derived, Mapping) and derived.get("speedup_ratio") is not None:
        print(f"Direct speedup: {derived['speedup_ratio']}x")

    methods_ok = len(results) == 2 and all(
        result.get("status") == "COMPLETED" for result in results
    )
    comparison_ok = (
        comparison.get("status") == "COMPLETED"
        and int(comparison.get("mismatched_groups", 1)) == 0
    )
    return 0 if methods_ok and comparison_ok else 2


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the public and internal benchmark CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark the current extract-then-mosaic workflow against direct "
            "mosaicking from SWOT NetCDF subdatasets."
        )
    )
    parser.add_argument("--config", default="config.yaml", help="SWOTFlow YAML config.")
    parser.add_argument(
        "--raw-folder",
        help="Raw SWOT NetCDF folder; defaults to extract.input_folder.",
    )
    parser.add_argument(
        "--output-root",
        help="Benchmark parent folder; defaults below processing.logs.",
    )
    parser.add_argument(
        "--gdal-python",
        help="GDAL Python executable; defaults to gdal.python.",
    )
    parser.add_argument(
        "--grouping-mode",
        help="Prototype supports utm_zone only.",
    )
    parser.add_argument(
        "--sample-groups",
        type=int,
        default=DEFAULT_SAMPLE_GROUPS,
        help=f"Representative mosaic groups to test (default: {DEFAULT_SAMPLE_GROUPS}).",
    )
    parser.add_argument("--seed", type=int, default=20260612, help="Sampling seed.")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan raw NetCDF subfolders.",
    )
    parser.add_argument(
        "--utm-tile",
        action="append",
        default=[],
        help="Limit to one UTM tile; repeat for multiple tiles.",
    )
    parser.add_argument(
        "--gdal-cache-mb",
        type=int,
        default=DEFAULT_GDAL_CACHE_MB,
        help=f"Per-worker GDAL cache cap in MB (default: {DEFAULT_GDAL_CACHE_MB}).",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.0,
        help="Allowed absolute pixel difference (default: exact match).",
    )
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Retain staged GeoTIFFs and direct VRTs after measurement.",
    )
    parser.add_argument(
        "--synthetic-smoke-test",
        action="store_true",
        help="Generate tiny NetCDF inputs and validate the full benchmark path.",
    )

    parser.add_argument(
        "--worker-mode",
        choices=("staged", "direct", "compare"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--plan-json", help=argparse.SUPPRESS)
    parser.add_argument("--run-root", help=argparse.SUPPRESS)
    parser.add_argument("--result-json", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a worker process or the parent benchmark coordinator."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.worker_mode:
        missing = [
            name
            for name in ("plan_json", "run_root", "result_json")
            if not getattr(args, name)
        ]
        if missing:
            parser.error(f"Worker mode requires: {', '.join(missing)}")
        return run_worker(args)
    try:
        return parent_main(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
