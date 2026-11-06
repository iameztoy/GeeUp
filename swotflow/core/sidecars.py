"""Exact companion paths for a raster; never use a wildcard deletion."""
from pathlib import Path


def geotiff_scope_path(path: Path) -> Path:
    """Return a raster-like filename for scope parsing, without asserting ownership."""
    lower = path.name.lower()
    for suffix in (".aux.xml", ".aux", ".msk", ".ovr"):
        if lower.endswith((".tif" + suffix, ".tiff" + suffix)):
            return Path(str(path)[:-len(suffix)])
    if path.suffix.lower() in {".tfw", ".tifw", ".wld"}:
        return path.with_suffix(".tif")
    return path


def geotiff_sidecars(path: Path) -> list[Path]:
    """List supported GDAL companions without touching unrelated files."""
    if path.suffix.lower() not in {".tif", ".tiff"}:
        return []
    return ([Path(str(path) + suffix) for suffix in (".aux.xml", ".aux", ".msk", ".ovr")]
            + [path.with_suffix(suffix) for suffix in (".tfw", ".tifw", ".wld")])
