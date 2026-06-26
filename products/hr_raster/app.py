"""HR Raster product-family wrapper.

This keeps the existing raster GUI as the source of truth while the platform
launcher grows around multiple SWOT product families.
"""

from __future__ import annotations

import tkinter as tk

from swotflow_gui import LauncherApp


PRODUCT_NAME = "SWOT L2 HR Raster 100 m"


def launch(root: tk.Tk) -> LauncherApp:
    """Mount the existing HR Raster workflow in the given Tk root."""
    return LauncherApp(root)

