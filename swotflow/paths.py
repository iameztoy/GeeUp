"""Resolve checkout resources and launch package workers consistently."""
from __future__ import annotations

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def module_path(module: str) -> Path:
    """Return a shipped module's source path without importing its side effects."""
    if not module.startswith("swotflow.") or not all(part.isidentifier() for part in module.split(".")):
        raise ValueError("Expected a SWOTFlow package module")
    return APP_ROOT.joinpath(*module.split(".")).with_suffix(".py")


def script_command(python: str | Path, script: Path, *arguments: str) -> list[str]:
    """Use module execution so Windows/GDAL child processes resolve package imports.

    Callers launch the command with ``cwd=APP_ROOT``. User data/configuration
    paths remain explicit arguments and are never rebased to the package folder.
    """
    relative = Path(script).resolve().relative_to(APP_ROOT)
    if relative.suffix != ".py" or relative.parts[0] != "swotflow":
        raise ValueError("Worker must be a Python module within the SWOTFlow package")
    module = ".".join(relative.with_suffix("").parts)
    return [str(python), "-m", module, *map(str, arguments)]
