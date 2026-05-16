"""Helpers for launching scripts inside the SWOT GDAL conda runtime."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


DEFAULT_GDAL_PYTHON = Path(".conda") / "swot_gdal" / ("python.exe" if os.name == "nt" else "bin/python")
REQUIRED_GDAL_DRIVERS = ("GTiff", "VRT", "netCDF")


@dataclass
class GdalRuntimeCheck:
    """Result from probing a GDAL Python runtime."""

    ok: bool
    python: Path
    stdout: str
    stderr: str
    returncode: int


def infer_conda_prefix(python_path: str | Path) -> Path:
    """Infer a conda prefix from a Python executable path."""
    python = Path(python_path)
    if python.name.lower() == "python.exe":
        return python.parent
    return python.parent.parent


def build_gdal_runtime_env(
    python_path: str | Path,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return environment variables needed for conda-forge GDAL on Windows."""
    env = dict(os.environ if base_env is None else base_env)
    prefix = infer_conda_prefix(python_path)
    library = prefix / "Library"

    path_parts = [
        str(prefix),
        str(library / "mingw-w64" / "bin"),
        str(library / "usr" / "bin"),
        str(library / "bin"),
        str(prefix / "Scripts"),
    ]
    existing_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(path_parts + ([existing_path] if existing_path else []))
    env["CONDA_PREFIX"] = str(prefix)

    gdal_data = library / "share" / "gdal"
    proj_lib = library / "share" / "proj"
    gdal_driver_path = library / "lib" / "gdalplugins"
    if gdal_data.exists():
        env["GDAL_DATA"] = str(gdal_data)
    if proj_lib.exists():
        env["PROJ_LIB"] = str(proj_lib)
    if gdal_driver_path.exists():
        env["GDAL_DRIVER_PATH"] = str(gdal_driver_path)
    return env


def gdal_check_code(required_drivers: Iterable[str] = REQUIRED_GDAL_DRIVERS) -> str:
    """Return Python code that validates GDAL imports and required drivers."""
    drivers = list(required_drivers)
    return textwrap.dedent(
        f"""
        from osgeo import gdal
        import sys
        gdal.UseExceptions()
        required = {drivers!r}
        missing = [name for name in required if gdal.GetDriverByName(name) is None]
        print("python=" + sys.version.split()[0])
        print("gdal=" + gdal.VersionInfo("--version"))
        print("required_drivers=" + ",".join(required))
        if missing:
            print("missing_drivers=" + ",".join(missing))
            raise SystemExit(2)
        print("missing_drivers=")
        """
    ).strip()


def check_gdal_runtime(
    python_path: str | Path,
    required_drivers: Sequence[str] = REQUIRED_GDAL_DRIVERS,
    timeout_seconds: int = 30,
) -> GdalRuntimeCheck:
    """Run a small subprocess that verifies GDAL and required drivers."""
    python = Path(python_path)
    if not python.exists():
        return GdalRuntimeCheck(
            ok=False,
            python=python,
            stdout="",
            stderr=f"GDAL Python executable does not exist: {python}",
            returncode=127,
        )
    try:
        result = subprocess.run(
            [str(python), "-c", gdal_check_code(required_drivers)],
            cwd=str(Path(__file__).resolve().parent),
            env=build_gdal_runtime_env(python),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return GdalRuntimeCheck(
            ok=False,
            python=python,
            stdout=exc.stdout or "",
            stderr=f"GDAL runtime check timed out after {timeout_seconds} seconds.",
            returncode=124,
        )
    return GdalRuntimeCheck(
        ok=result.returncode == 0,
        python=python,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
    )


def current_process_gdal_check(
    required_drivers: Sequence[str] = REQUIRED_GDAL_DRIVERS,
) -> str:
    """Validate GDAL in the current Python process and return a short summary."""
    from osgeo import gdal

    gdal.UseExceptions()
    missing = [name for name in required_drivers if gdal.GetDriverByName(name) is None]
    if missing:
        raise RuntimeError(f"Missing GDAL drivers: {', '.join(missing)}")
    return (
        f"python={sys.version.split()[0]}\n"
        f"gdal={gdal.VersionInfo('--version')}\n"
        f"required_drivers={','.join(required_drivers)}"
    )
