"""Locations for durable application state, separate from disposable experiments."""
from __future__ import annotations

import os
import sys
from pathlib import Path

LEGACY_CONSOLIDATION_ROOT = Path(__file__).resolve().parent / "outputs" / "ee_consolidation"


def data_home() -> Path:
    """Return the user data directory without creating it or changing any files."""
    override = os.environ.get("SWOTFLOW_DATA_HOME")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("SWOTFLOW_DATA_HOME must be an absolute path")
        return path
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "SWOTFlow"


def consolidation_root() -> Path:
    """Keep existing history discoverable; use user data for new installations.

    An explicit data-home override wins. Legacy history is never automatically
    moved or merged: a user can copy the complete folder and set the override.
    Merely constructing the GUI must not create a database or a directory.
    """
    if not os.environ.get("SWOTFLOW_DATA_HOME") and LEGACY_CONSOLIDATION_ROOT.is_dir():
        return LEGACY_CONSOLIDATION_ROOT
    return data_home() / "ee_consolidation"
