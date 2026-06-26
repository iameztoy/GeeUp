"""Product-family launcher for SWOTFlow."""

from __future__ import annotations

from typing import Any

import tkinter as tk
from tkinter import ttk


APP_TITLE = "SWOTFlow - Product Selector"


def clear_root(root: tk.Tk) -> None:
    """Remove all widgets from a Tk root before mounting a product workflow."""
    for child in root.winfo_children():
        child.destroy()


class ProductSelectorApp:
    """Initial product-family selector for SWOTFlow."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.product_app: Any | None = None
        self.show_selector()

    def show_selector(self) -> None:
        """Show the product-family selection screen."""
        clear_root(self.root)
        self.product_app = None
        self.root.title(APP_TITLE)
        self.root.geometry("760x460")
        self.root.minsize(640, 360)

        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer,
            text="SWOTFlow",
            font=("Segoe UI", 20, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text="Choose a SWOT product family to process.",
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 18))

        choices = ttk.Frame(outer)
        choices.grid(row=2, column=0, sticky="nsew")
        choices.columnconfigure(0, weight=1)
        choices.columnconfigure(1, weight=1)

        self.build_product_choice(
            choices,
            column=0,
            title="HR Raster 100 m",
            description=(
                "Open the current raster workflow for download, duplicate cleanup, "
                "GeoTIFF extraction, mosaicking, upload, statistics, cleanup, and automation."
            ),
            command=self.open_hr_raster,
        )
        self.build_product_choice(
            choices,
            column=1,
            title="Pixel Cloud / PIXC",
            description=(
                "Open the new point-cloud workflow shell for PIXC NetCDF download, "
                "inspection, statistics, and future point exports."
            ),
            command=self.open_pixc,
        )

    def build_product_choice(
        self,
        parent: ttk.Frame,
        *,
        column: int,
        title: str,
        description: str,
        command: Any,
    ) -> None:
        """Create one product selection panel."""
        frame = ttk.LabelFrame(parent, text=title, padding=16)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if column == 0 else (10, 0))
        frame.columnconfigure(0, weight=1)
        ttk.Label(
            frame,
            text=description,
            wraplength=300,
            justify="left",
        ).grid(row=0, column=0, sticky="nw")
        ttk.Button(
            frame,
            text=f"Process {title}",
            command=command,
        ).grid(row=1, column=0, sticky="w", pady=(18, 0))

    def open_hr_raster(self) -> None:
        """Open the existing HR Raster workflow."""
        from products.hr_raster.app import launch

        clear_root(self.root)
        self.product_app = launch(self.root)

    def open_pixc(self) -> None:
        """Open the PIXC workflow shell."""
        from products.pixc.app import launch

        clear_root(self.root)
        self.product_app = launch(self.root, back_command=self.show_selector)


def main() -> int:
    """Launch the SWOTFlow product selector."""
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app = ProductSelectorApp(root)
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
