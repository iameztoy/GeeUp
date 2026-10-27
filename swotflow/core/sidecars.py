"""Exact companion paths for a raster; never use a wildcard deletion."""
from pathlib import Path


def geotiff_sidecars(path: Path) -> list[Path]:
    """List supported GDAL companions without touching unrelated files."""
    if path.suffix.lower() not in {".tif", ".tiff"}:
        return []
    return ([Path(str(path) + suffix) for suffix in (".aux.xml", ".aux", ".msk", ".ovr")]
            + [path.with_suffix(suffix) for suffix in (".tfw", ".tifw", ".wld")])
