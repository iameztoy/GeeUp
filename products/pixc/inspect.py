"""PIXC NetCDF inspection helpers.

The inspector intentionally keeps NetCDF dependencies optional. Tests can use
NetCDF-like objects, while real file inspection imports netCDF4 only when a file
is opened.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


COUNT_NAME_TOKENS = ("class", "classification", "qual", "quality", "flag")
RANGE_NAME_TOKENS = (
    "lat",
    "lon",
    "height",
    "elevation",
    "wse",
    "sig0",
    "sigma0",
    "backscatter",
    "water_frac",
    "water_fraction",
    "area",
)


@dataclass
class PixcInspectionSummary:
    """Flattened inspection summary for one PIXC NetCDF file."""

    file_path: str = ""
    file_size_bytes: int = 0
    groups: list[dict[str, Any]] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    variables: list[dict[str, Any]] = field(default_factory=list)
    variable_stats: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


@dataclass
class InspectionReportPaths:
    """Files written for one inspection summary."""

    summary_json: Path
    variables_csv: Path
    value_counts_csv: Path


def import_netcdf4_dataset() -> Any:
    """Return netCDF4.Dataset, raising a helpful runtime error if unavailable."""
    try:
        from netCDF4 import Dataset  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "netCDF4 is not installed. Install PIXC inspection dependencies before "
            "inspecting real NetCDF files."
        ) from exc
    return Dataset


def clean_value(value: Any) -> Any:
    """Convert common NetCDF/NumPy values into JSON/CSV-friendly values."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return str(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (list, tuple)):
        return [clean_value(item) for item in value]
    try:
        # NumPy arrays and netCDF attributes often expose tolist().
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            return clean_value(tolist())
    except ValueError:
        pass
    return value


def object_attributes(obj: Any) -> dict[str, Any]:
    """Read NetCDF-style attributes from a group or variable."""
    names_func = getattr(obj, "ncattrs", None)
    if not callable(names_func):
        return {}
    attrs: dict[str, Any] = {}
    for name in names_func():
        try:
            attrs[str(name)] = clean_value(obj.getncattr(name))
        except Exception:
            attrs[str(name)] = "<unreadable>"
    return attrs


def dimension_size(dimension: Any) -> int:
    """Return the size of a NetCDF dimension-like object."""
    try:
        return int(len(dimension))
    except TypeError:
        size = getattr(dimension, "size", 0)
        return int(size or 0)


def dimension_unlimited(dimension: Any) -> bool:
    """Return whether a NetCDF dimension-like object is unlimited."""
    unlimited = getattr(dimension, "isunlimited", None)
    if callable(unlimited):
        try:
            return bool(unlimited())
        except Exception:
            return False
    return bool(getattr(dimension, "unlimited", False))


def variable_size(variable: Any) -> int:
    """Return the number of values in a NetCDF variable-like object."""
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
    return total


def child_path(parent_path: str, name: str) -> str:
    """Return a normalized NetCDF child path."""
    return f"/{name}" if parent_path == "/" else f"{parent_path}/{name}"


def stat_kind_for_variable(name: str) -> str:
    """Return the default statistic type for a PIXC variable name."""
    lower = name.lower()
    if any(token in lower for token in COUNT_NAME_TOKENS):
        return "counts"
    if any(token in lower for token in RANGE_NAME_TOKENS):
        return "range"
    return ""


def variable_summary(group_path: str, name: str, variable: Any) -> dict[str, Any]:
    """Return a flat variable metadata summary."""
    attrs = object_attributes(variable)
    return {
        "group_path": group_path,
        "name": name,
        "path": child_path(group_path, name),
        "dimensions": list(getattr(variable, "dimensions", ()) or ()),
        "shape": list(getattr(variable, "shape", ()) or ()),
        "size": variable_size(variable),
        "dtype": str(getattr(variable, "dtype", "")),
        "units": str(attrs.get("units", "") or ""),
        "long_name": str(attrs.get("long_name", "") or ""),
        "standard_name": str(attrs.get("standard_name", "") or ""),
        "fill_value": attrs.get("_FillValue", attrs.get("missing_value", "")),
        "stat_kind": stat_kind_for_variable(name),
        "attributes": attrs,
    }


def summarize_variable_data(
    group_path: str,
    name: str,
    variable: Any,
    *,
    max_values_for_stats: int = 5_000_000,
    max_value_counts: int = 50,
) -> dict[str, Any]:
    """Summarize values for a key PIXC variable when it is reasonably sized."""
    info = variable_summary(group_path, name, variable)
    stat_kind = str(info.get("stat_kind", ""))
    stats: dict[str, Any] = {
        "group_path": group_path,
        "name": name,
        "path": info["path"],
        "stat_kind": stat_kind,
        "status": "not_selected",
        "size": info["size"],
        "missing_count": 0,
        "valid_count": 0,
        "min": "",
        "max": "",
        "distinct_count": "",
        "value_counts": [],
    }
    if not stat_kind:
        return stats
    if int(info["size"]) > max_values_for_stats:
        stats["status"] = "skipped_too_large"
        return stats

    try:
        import numpy as np
    except ImportError:
        stats["status"] = "skipped_numpy_missing"
        return stats

    try:
        raw = variable[:]
    except Exception as exc:
        stats["status"] = "read_failed"
        stats["error"] = str(exc)
        return stats

    try:
        data = np.ma.array(raw)
        flattened = data.reshape(-1)
        mask = np.ma.getmaskarray(flattened)
        missing_count = int(mask.sum()) if getattr(mask, "size", 0) else int(bool(mask))
        valid_values = flattened.compressed()

        fill_value = info.get("fill_value", "")
        if fill_value not in ("", None):
            try:
                fill_mask = valid_values == fill_value
                missing_count += int(fill_mask.sum())
                valid_values = valid_values[~fill_mask]
            except Exception:
                pass

        if getattr(valid_values, "size", 0) and np.issubdtype(valid_values.dtype, np.floating):
            finite_mask = np.isfinite(valid_values)
            missing_count += int((~finite_mask).sum())
            valid_values = valid_values[finite_mask]

        stats["missing_count"] = missing_count
        stats["valid_count"] = int(getattr(valid_values, "size", 0))
        if stats["valid_count"] == 0:
            stats["status"] = "no_valid_values"
            return stats

        if np.issubdtype(valid_values.dtype, np.number):
            stats["min"] = clean_value(valid_values.min())
            stats["max"] = clean_value(valid_values.max())

        if stat_kind == "counts":
            unique, counts = np.unique(valid_values, return_counts=True)
            stats["distinct_count"] = int(len(unique))
            pairs = sorted(
                zip(unique, counts),
                key=lambda item: int(item[1]),
                reverse=True,
            )[:max_value_counts]
            stats["value_counts"] = [
                {"value": clean_value(value), "count": int(count)}
                for value, count in pairs
            ]
        stats["status"] = "summarized"
        return stats
    except Exception as exc:
        stats["status"] = "summarize_failed"
        stats["error"] = str(exc)
        return stats


def iter_mapping_items(value: Any) -> Iterable[tuple[str, Any]]:
    """Yield sorted items from a NetCDF mapping-like object."""
    if not value:
        return []
    if isinstance(value, Mapping):
        return sorted(value.items(), key=lambda item: str(item[0]).lower())
    items = getattr(value, "items", None)
    if callable(items):
        return sorted(items(), key=lambda item: str(item[0]).lower())
    return []


def inspect_group(
    group: Any,
    *,
    path: str = "/",
    summary: PixcInspectionSummary | None = None,
    include_stats: bool = True,
    max_values_for_stats: int = 5_000_000,
    max_value_counts: int = 50,
) -> PixcInspectionSummary:
    """Inspect one NetCDF group-like object recursively."""
    result = summary or PixcInspectionSummary()
    result.groups.append({"path": path, "attributes": object_attributes(group)})

    for name, dimension in iter_mapping_items(getattr(group, "dimensions", {})):
        result.dimensions.append(
            {
                "group_path": path,
                "name": str(name),
                "path": child_path(path, str(name)),
                "size": dimension_size(dimension),
                "unlimited": dimension_unlimited(dimension),
            }
        )

    for name, variable in iter_mapping_items(getattr(group, "variables", {})):
        name_text = str(name)
        result.variables.append(variable_summary(path, name_text, variable))
        if include_stats:
            stats = summarize_variable_data(
                path,
                name_text,
                variable,
                max_values_for_stats=max_values_for_stats,
                max_value_counts=max_value_counts,
            )
            if stats.get("status") != "not_selected":
                result.variable_stats.append(stats)

    for name, child in iter_mapping_items(getattr(group, "groups", {})):
        inspect_group(
            child,
            path=child_path(path, str(name)),
            summary=result,
            include_stats=include_stats,
            max_values_for_stats=max_values_for_stats,
            max_value_counts=max_value_counts,
        )
    return result


def inspect_dataset(
    dataset: Any,
    *,
    file_path: str | Path = "",
    file_size_bytes: int = 0,
    include_stats: bool = True,
    max_values_for_stats: int = 5_000_000,
    max_value_counts: int = 50,
) -> PixcInspectionSummary:
    """Inspect an already-open NetCDF dataset-like object."""
    summary = PixcInspectionSummary(
        file_path=str(file_path) if file_path else "",
        file_size_bytes=int(file_size_bytes or 0),
    )
    return inspect_group(
        dataset,
        path="/",
        summary=summary,
        include_stats=include_stats,
        max_values_for_stats=max_values_for_stats,
        max_value_counts=max_value_counts,
    )


def inspect_netcdf(
    path: str | Path,
    *,
    include_stats: bool = True,
    max_values_for_stats: int = 5_000_000,
    max_value_counts: int = 50,
) -> PixcInspectionSummary:
    """Open and inspect one NetCDF file."""
    nc_path = Path(path)
    if not nc_path.exists() or not nc_path.is_file():
        raise FileNotFoundError(f"PIXC NetCDF file not found: {nc_path}")
    Dataset = import_netcdf4_dataset()
    dataset = Dataset(str(nc_path), mode="r")
    try:
        return inspect_dataset(
            dataset,
            file_path=nc_path,
            file_size_bytes=nc_path.stat().st_size,
            include_stats=include_stats,
            max_values_for_stats=max_values_for_stats,
            max_value_counts=max_value_counts,
        )
    finally:
        close = getattr(dataset, "close", None)
        if callable(close):
            close()


def write_inspection_reports(
    summary: PixcInspectionSummary,
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> InspectionReportPaths:
    """Write JSON and CSV inspection reports."""
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    report_stem = stem or Path(summary.file_path or "pixc_inspection").stem
    summary_json = folder / f"{report_stem}_inspection_summary.json"
    variables_csv = folder / f"{report_stem}_variables.csv"
    value_counts_csv = folder / f"{report_stem}_value_counts.csv"

    summary_json.write_text(
        json.dumps(summary.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    variable_fields = [
        "group_path",
        "name",
        "path",
        "dimensions",
        "shape",
        "size",
        "dtype",
        "units",
        "long_name",
        "standard_name",
        "fill_value",
        "stat_kind",
    ]
    with variables_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=variable_fields)
        writer.writeheader()
        for row in summary.variables:
            writer.writerow({field: clean_value(row.get(field, "")) for field in variable_fields})

    count_fields = ["variable_path", "value", "count"]
    with value_counts_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=count_fields)
        writer.writeheader()
        for stats in summary.variable_stats:
            for row in stats.get("value_counts", []) or []:
                writer.writerow(
                    {
                        "variable_path": stats.get("path", ""),
                        "value": row.get("value", ""),
                        "count": row.get("count", ""),
                    }
                )

    return InspectionReportPaths(
        summary_json=summary_json,
        variables_csv=variables_csv,
        value_counts_csv=value_counts_csv,
    )


def summarize_for_status(summary: PixcInspectionSummary) -> str:
    """Return a compact human-readable inspection summary."""
    stat_count = sum(1 for row in summary.variable_stats if row.get("status") == "summarized")
    return (
        f"Inspected {len(summary.groups)} group(s), {len(summary.dimensions)} dimension(s), "
        f"{len(summary.variables)} variable(s), with {stat_count} summarized key variable(s)."
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the PIXC inspector CLI parser."""
    parser = argparse.ArgumentParser(description="Inspect a SWOT PIXC NetCDF file.")
    parser.add_argument("netcdf", type=Path, help="Path to one PIXC NetCDF file.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("PIXC_Processing") / "00_logs" / "inspection",
        help="Folder for inspection JSON/CSV reports.",
    )
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help="Only list structure; do not read variable arrays for statistics.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the PIXC inspector from the command line."""
    args = build_arg_parser().parse_args(argv)
    summary = inspect_netcdf(args.netcdf, include_stats=not args.no_stats)
    reports = write_inspection_reports(summary, args.output_dir)
    print(summarize_for_status(summary))
    print(f"Summary JSON: {reports.summary_json}")
    print(f"Variables CSV: {reports.variables_csv}")
    print(f"Value counts CSV: {reports.value_counts_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

