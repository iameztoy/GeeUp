"""Shared SWOT L2 HR Raster filename parsing helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple


DEFAULT_METADATA_EXTRA_PROPERTIES: Dict[str, str] = {
    "swot_descriptor": "descriptor",
    "swot_grid_resolution": "grid_resolution",
    "swot_coordinate_system": "coordinate_system",
    "swot_granule_overlap": "granule_overlap",
    "swot_cycle_id": "cycle_id",
    "swot_pass_id": "pass_id",
    "swot_scene_id": "scene_id",
    "swot_crid": "crid",
    "swot_product_counter": "product_counter",
}


SWOT_L2_HR_RASTER_PATTERN = re.compile(
    r"^SWOT_L2_HR_Raster_"
    r"(?P<descriptor>.+)_"
    r"(?P<cycle_id>\d{3})_"
    r"(?P<pass_id>\d{3})_"
    r"(?P<scene_id>[A-Za-z0-9]{4})_"
    r"(?P<range_beginning>\d{8}T\d{6})_"
    r"(?P<range_ending>\d{8}T\d{6})_"
    r"(?P<crid>[A-Za-z0-9]+)_"
    r"(?P<product_counter>\d+)"
    r"(?P<filename_suffix>(?:_.+)?)$"
)

CRID_FIDELITY_RANK = {
    "O": 0,
    "I": 1,
    "G": 2,
}
SUPPORTED_CRID_PRODUCERS = {"P", "D"}


@dataclass
class ParsedMetadata:
    """Metadata parsed from a SWOT L2 HR Raster filename."""

    start_time: str = ""
    end_time: str = ""
    properties: Dict[str, str] = field(default_factory=dict)
    status: str = "METADATA_DISABLED"
    error_message: str = ""
    fields: Dict[str, str] = field(default_factory=dict)


def parse_swot_l2_hr_raster_metadata(
    file_name: str | Path,
    extra_properties: Dict[str, str] | None = None,
) -> Optional[ParsedMetadata]:
    """Parse SWOT L2 HR Raster metadata from a filename."""
    stem = Path(file_name).stem
    match = SWOT_L2_HR_RASTER_PATTERN.match(stem)
    if not match:
        return None

    values = match.groupdict()
    descriptor_parts = values["descriptor"].split("_")
    values["grid_resolution"] = descriptor_parts[0] if len(descriptor_parts) > 0 else ""
    values["coordinate_system"] = descriptor_parts[1] if len(descriptor_parts) > 1 else ""
    values["granule_overlap"] = descriptor_parts[2] if len(descriptor_parts) > 2 else ""
    values["start_time"] = format_swot_timestamp(values["range_beginning"])
    values["end_time"] = format_swot_timestamp(values["range_ending"])

    property_map = (
        DEFAULT_METADATA_EXTRA_PROPERTIES if extra_properties is None else extra_properties
    )
    properties = {
        property_name: str(values[source_field])
        for property_name, source_field in property_map.items()
        if values.get(source_field) not in (None, "")
    }
    return ParsedMetadata(
        start_time=values["start_time"],
        end_time=values["end_time"],
        properties=properties,
        status="METADATA_PARSED",
        fields=values,
    )


def format_swot_timestamp(value: str) -> str:
    """Convert a SWOT timestamp to the Earth Engine upload dialog format."""
    return datetime.strptime(value, "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M:%S")


def _alnum_rank(value: str) -> int:
    """Return a sortable rank for one alphanumeric CRID character."""
    if not value or not value.isalnum():
        return -1
    if value.isdigit():
        return int(value)
    return 10 + ord(value.upper()) - ord("A")


def crid_rank(crid: str) -> Tuple[int, int, int, int]:
    """Rank SWOT CRIDs from least to most preferred.

    The rank follows SWOT release-note guidance: newer major release first,
    then higher minor release, then higher product fidelity.
    """
    value = str(crid or "").strip().upper()
    if len(value) != 4:
        return (0, -1, -1, -1)
    producer, fidelity, major, minor = value
    fidelity_rank = CRID_FIDELITY_RANK.get(fidelity, -1)
    supported = int(producer in SUPPORTED_CRID_PRODUCERS and fidelity_rank >= 0)
    return (
        supported,
        _alnum_rank(major),
        _alnum_rank(minor),
        fidelity_rank,
    )


def product_counter_rank(product_counter: str | int | None) -> int:
    """Return a sortable product-counter rank."""
    try:
        return int(str(product_counter or "").strip())
    except ValueError:
        return -1


def swot_product_rank(crid: str, product_counter: str | int | None) -> Tuple[int, int, int, int, int]:
    """Rank a SWOT product version using CRID first and counter last."""
    return (*crid_rank(crid), product_counter_rank(product_counter))


def processing_level_label(crid: str, product_counter: str | int | None) -> str:
    """Return a future-proof CRID/product-counter processing-level label."""
    crid_text = str(crid or "").strip().upper()
    counter_text = str(product_counter or "").strip()
    if crid_text and counter_text:
        return f"{crid_text}_{counter_text}"
    if crid_text:
        return crid_text
    return "UNKNOWN"


def processing_level_rank(file_name: str | Path) -> Tuple[int, int, int, int, int]:
    """Return the ranked processing level parsed from a SWOT HR Raster filename."""
    metadata = parse_swot_l2_hr_raster_metadata(file_name)
    if metadata is None:
        return swot_product_rank("", "")
    fields = metadata.fields
    return swot_product_rank(fields.get("crid", ""), fields.get("product_counter", ""))


def processing_level_from_filename(file_name: str | Path) -> str:
    """Return the CRID/product-counter label parsed from a SWOT HR Raster filename."""
    metadata = parse_swot_l2_hr_raster_metadata(file_name)
    if metadata is None:
        return ""
    fields = metadata.fields
    return processing_level_label(fields.get("crid", ""), fields.get("product_counter", ""))


def product_identity_parts(file_name: str | Path) -> Optional[Tuple[str, ...]]:
    """Return HR Raster identity fields while ignoring only CRID/product counter."""
    metadata = parse_swot_l2_hr_raster_metadata(file_name)
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


def product_identity_key(file_name: str | Path) -> str:
    """Return a stable string key for same-observation HR Raster products."""
    parts = product_identity_parts(file_name)
    return "" if parts is None else "|".join(parts)
