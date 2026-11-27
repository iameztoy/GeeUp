"""Lossless GeoTIFF encoding choices shared by Mosaic and Automation."""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree


COMPRESSION_LABELS = {
    "Uncompressed (current behavior)": "none",
    "ZSTD (lossless, recommended)": "zstd",
    "DEFLATE (lossless)": "deflate",
}
COMPRESSION_LABEL_BY_VALUE = {value: label for label, value in COMPRESSION_LABELS.items()}


def normalize_compression(value: Any) -> str:
    """Validate a codec; missing configuration preserves historical output."""
    choice = str(value or "none").strip().lower()
    if choice not in COMPRESSION_LABEL_BY_VALUE:
        raise ValueError("mosaic.compression must be none, zstd or deflate (lossless only).")
    return choice


def lossless_creation_options(compression: str, gdal: Any, data_type: int | None) -> list[str]:
    """Choose lossless predictors without changing dtype, values, masks or grids.

    Check the installed codec before creating an output: GDAL can otherwise
    warn and fall back to an uncompressed file. Sparse TIFF blocks and lossy
    precision options are deliberately not offered.
    """
    choice = normalize_compression(compression)
    if choice == "none":
        return []
    driver = gdal.GetDriverByName("GTiff")
    if driver is None:
        raise ValueError("The configured GDAL environment has no GeoTIFF driver.")
    options = ElementTree.fromstring(driver.GetMetadataItem("DMD_CREATIONOPTIONLIST") or "<CreationOptionList/>")
    supported = {value.text for value in options.findall("./Option[@name='COMPRESS']/Value")}
    if choice.upper() not in supported:
        raise ValueError(f"The configured GDAL environment does not support {choice.upper()} compression. "
                         "Choose another lossless codec or Uncompressed; no output was replaced.")
    result = ["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256", f"COMPRESS={choice.upper()}",
              "ZSTD_LEVEL=3" if choice == "zstd" else "ZLEVEL=1", "NUM_THREADS=1"]
    if data_type in (gdal.GDT_Float32, gdal.GDT_Float64):
        result.append("PREDICTOR=3")
    elif data_type in (gdal.GDT_Byte, gdal.GDT_UInt16, gdal.GDT_Int16, gdal.GDT_UInt32, gdal.GDT_Int32):
        result.append("PREDICTOR=2")
    return result
