"""Tkinter UTM tile map selector backed by precomputed JSON geometry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import ttk

from geeup_project import TilePreset
from swot_download_tool import normalize_utm_tiles


PROJECT_ROOT = Path(__file__).resolve().parent
DISPLAY_GEOMETRY_PATH = PROJECT_ROOT / "spatial_presets" / "utm_display_geometries.json"
Point = Tuple[float, float]
Bounds = Tuple[float, float, float, float]
Ring = List[Point]


@dataclass
class DisplayTile:
    """One UTM tile display geometry."""

    token: str
    bounds: Bounds
    polygons: List[Ring]


@dataclass
class DisplayContinent:
    """One continent outline display geometry."""

    name: str
    bounds: Bounds
    polygons: List[Ring]


@dataclass
class UTMDisplayGeometry:
    """Precomputed geometries used by the visual selector."""

    bounds: Bounds
    tiles: Dict[str, DisplayTile]
    continents: List[DisplayContinent]


def parse_bounds(value: Sequence[float]) -> Bounds:
    """Parse JSON bounds stored as minx, miny, maxx, maxy."""
    if len(value) != 4:
        raise ValueError("Display geometry bounds must contain four numbers.")
    minx, miny, maxx, maxy = [float(item) for item in value]
    return minx, miny, maxx, maxy


def parse_polygons(value: Iterable[Iterable[Iterable[float]]]) -> List[Ring]:
    """Parse JSON polygon exterior rings."""
    polygons: List[Ring] = []
    for raw_ring in value:
        ring: Ring = []
        for raw_point in raw_ring:
            point = list(raw_point)
            if len(point) != 2:
                continue
            ring.append((float(point[0]), float(point[1])))
        if len(ring) >= 4:
            polygons.append(ring)
    return polygons


def load_display_geometry(path: str | Path = DISPLAY_GEOMETRY_PATH) -> UTMDisplayGeometry:
    """Load the precomputed UTM selector display geometry JSON."""
    geometry_path = Path(path)
    with geometry_path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)

    tiles = {
        token: DisplayTile(
            token=token,
            bounds=parse_bounds(raw_tile["bounds"]),
            polygons=parse_polygons(raw_tile.get("polygons", [])),
        )
        for token, raw_tile in (document.get("tiles", {}) or {}).items()
    }
    continents = [
        DisplayContinent(
            name=str(raw_continent.get("name", "")),
            bounds=parse_bounds(raw_continent["bounds"]),
            polygons=parse_polygons(raw_continent.get("polygons", [])),
        )
        for raw_continent in (document.get("continents", []) or [])
    ]
    return UTMDisplayGeometry(
        bounds=parse_bounds(document["bounds"]),
        tiles=tiles,
        continents=continents,
    )


def bounds_contains(bounds: Bounds, point: Point) -> bool:
    """Return True when point is inside bounds."""
    minx, miny, maxx, maxy = bounds
    x, y = point
    return minx <= x <= maxx and miny <= y <= maxy


def point_in_ring(point: Point, ring: Ring) -> bool:
    """Return True when a point is inside a polygon exterior ring."""
    x, y = point
    inside = False
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        if ((current_y > y) != (previous_y > y)) and (
            x
            < (previous_x - current_x) * (y - current_y) / (previous_y - current_y)
            + current_x
        ):
            inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def hit_test_tile(geometry: UTMDisplayGeometry, point: Point) -> Optional[str]:
    """Return the token for the tile containing a world-coordinate point."""
    for token, tile in geometry.tiles.items():
        if not bounds_contains(tile.bounds, point):
            continue
        if any(point_in_ring(point, ring) for ring in tile.polygons):
            return token
    return None


class CanvasTransform:
    """Convert EPSG:3857 world coordinates to canvas coordinates."""

    def __init__(self, bounds: Bounds, width: int, height: int, padding: int = 16) -> None:
        self.bounds = bounds
        self.width = width
        self.height = height
        self.padding = padding
        minx, miny, maxx, maxy = bounds
        available_width = max(1, width - padding * 2)
        available_height = max(1, height - padding * 2)
        self.scale = min(
            available_width / max(1.0, maxx - minx),
            available_height / max(1.0, maxy - miny),
        )
        drawn_width = (maxx - minx) * self.scale
        drawn_height = (maxy - miny) * self.scale
        self.offset_x = padding + (available_width - drawn_width) / 2
        self.offset_y = padding + (available_height - drawn_height) / 2

    def world_to_canvas(self, point: Point) -> Tuple[float, float]:
        minx, _miny, _maxx, maxy = self.bounds
        x, y = point
        return (
            self.offset_x + (x - minx) * self.scale,
            self.offset_y + (maxy - y) * self.scale,
        )

    def canvas_to_world(self, point: Point) -> Point:
        minx, _miny, _maxx, maxy = self.bounds
        x, y = point
        return (
            minx + (x - self.offset_x) / self.scale,
            maxy - (y - self.offset_y) / self.scale,
        )


def ring_to_canvas_points(transform: CanvasTransform, ring: Ring) -> List[float]:
    """Flatten a world-coordinate ring into Tkinter canvas coordinate values."""
    points: List[float] = []
    for point in ring:
        canvas_x, canvas_y = transform.world_to_canvas(point)
        points.extend([canvas_x, canvas_y])
    return points


class UTMMapSelectorDialog(tk.Toplevel):
    """Visual UTM selector dialog with click-to-toggle tile selection."""

    canvas_width = 940
    canvas_height = 520

    def __init__(
        self,
        master: tk.Misc,
        geometry: UTMDisplayGeometry,
        selected_tiles: Sequence[str],
        apply_callback: Callable[[List[str]], None],
        preset_choices: Mapping[str, TilePreset] | None = None,
        coverage_tiles: Sequence[str] | None = None,
    ) -> None:
        super().__init__(master)
        self.title("UTM Tile Map Selector")
        self.geometry("980x680")
        self.minsize(860, 580)
        self.geometry_data = geometry
        self.apply_callback = apply_callback
        self.preset_choices = dict(preset_choices or {})
        self.selected_tiles = set(normalize_utm_tiles(selected_tiles))
        self.coverage_tiles = set(normalize_utm_tiles(coverage_tiles or []))
        self.transform = CanvasTransform(
            self.geometry_data.bounds,
            self.canvas_width,
            self.canvas_height,
            padding=16,
        )
        self.status_var = tk.StringVar()
        self.show_labels_var = tk.BooleanVar(value=True)
        self.preset_var = tk.StringVar(
            value=sorted(self.preset_choices)[0] if self.preset_choices else ""
        )
        self.build_layout()
        self.draw_map()
        self.update_status()
        self.transient(master)
        self.grab_set()

    def build_layout(self) -> None:
        """Build selector controls and canvas."""
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        controls = ttk.Frame(self, padding=10)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Tile preset").grid(row=0, column=0, sticky="w")
        self.preset_combo = ttk.Combobox(
            controls,
            textvariable=self.preset_var,
            values=sorted(self.preset_choices),
            state="readonly",
        )
        self.preset_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(
            controls,
            text="Load Preset",
            command=self.load_selected_preset,
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Button(
            controls,
            text="Clear Selection",
            command=self.clear_selection,
        ).grid(row=0, column=3, sticky="w", padx=(8, 0))
        ttk.Checkbutton(
            controls,
            text="Show UTM labels",
            variable=self.show_labels_var,
            command=self.draw_map,
        ).grid(row=0, column=4, sticky="w", padx=(12, 0))

        self.canvas = tk.Canvas(
            self,
            width=self.canvas_width,
            height=self.canvas_height,
            background="#f7f8f5",
            highlightthickness=1,
            highlightbackground="#b7b7b7",
        )
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Configure>", self.on_canvas_configure)

        footer = ttk.Frame(self, padding=(10, 0, 10, 10))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            textvariable=self.status_var,
            foreground="#184a8b",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Cancel", command=self.destroy).grid(
            row=0,
            column=1,
            sticky="e",
            padx=(8, 0),
        )
        ttk.Button(footer, text="Apply", command=self.apply_selection).grid(
            row=0,
            column=2,
            sticky="e",
            padx=(8, 0),
        )

    def draw_map(self) -> None:
        """Redraw continents, tile outlines, and selected tile highlights."""
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("all")
        self.draw_continents(fill="#e1e5dc", outline="#b8beb2", width=0.7)

        for token, tile in self.geometry_data.tiles.items():
            is_selected = token in self.selected_tiles
            is_covered = token in self.coverage_tiles
            if is_selected and is_covered:
                fill = "#2f9e7e"
                outline = "#12614c"
                width = 1.3
            elif is_selected:
                fill = "#4a90d9"
                outline = "#1d5f9f"
                width = 1.2
            elif is_covered:
                fill = "#77b66e"
                outline = "#3f7b38"
                width = 0.9
            else:
                fill = ""
                outline = "#b7bec8"
                width = 0.6
            for ring in tile.polygons:
                points = ring_to_canvas_points(self.transform, ring)
                if len(points) >= 8:
                    self.canvas.create_polygon(
                        points,
                        fill=fill,
                        outline=outline,
                        width=width,
                    )

        self.draw_continents(fill="", outline="#69715f", width=1.1)
        if self.show_labels_var.get():
            self.draw_utm_labels()

    def draw_continents(self, fill: str, outline: str, width: float) -> None:
        """Draw continent polygons on the map canvas."""
        for continent in self.geometry_data.continents:
            for ring in continent.polygons:
                points = ring_to_canvas_points(self.transform, ring)
                if len(points) >= 8:
                    self.canvas.create_polygon(
                        points,
                        fill=fill,
                        outline=outline,
                        width=width,
                    )

    def draw_utm_labels(self) -> None:
        """Draw compact UTM zone and latitude-band reference labels."""
        zone_bounds: Dict[int, List[float]] = {}
        band_bounds: Dict[str, List[float]] = {}
        for token, tile in self.geometry_data.tiles.items():
            zone = int(token[3:5])
            band = token[5]
            minx, miny, maxx, maxy = tile.bounds
            if zone not in zone_bounds:
                zone_bounds[zone] = [minx, maxx]
            else:
                zone_bounds[zone][0] = min(zone_bounds[zone][0], minx)
                zone_bounds[zone][1] = max(zone_bounds[zone][1], maxx)
            if band not in band_bounds:
                band_bounds[band] = [miny, maxy]
            else:
                band_bounds[band][0] = min(band_bounds[band][0], miny)
                band_bounds[band][1] = max(band_bounds[band][1], maxy)

        label_color = "#3f4750"
        top_y = self.transform.offset_y + 9
        for zone, (minx, maxx) in sorted(zone_bounds.items()):
            x, _y = self.transform.world_to_canvas(((minx + maxx) / 2.0, self.geometry_data.bounds[3]))
            self.canvas.create_text(
                x,
                top_y,
                text=f"{zone:02d}",
                fill=label_color,
                font=("Segoe UI", 6),
            )

        left_x = self.transform.offset_x + 8
        right_x = self.transform.offset_x + (
            self.geometry_data.bounds[2] - self.geometry_data.bounds[0]
        ) * self.transform.scale - 8
        for band, (miny, maxy) in sorted(band_bounds.items(), key=lambda item: item[1][0]):
            _x, y = self.transform.world_to_canvas((self.geometry_data.bounds[0], (miny + maxy) / 2.0))
            self.canvas.create_text(
                left_x,
                y,
                text=band,
                fill=label_color,
                font=("Segoe UI", 7, "bold"),
            )
            self.canvas.create_text(
                right_x,
                y,
                text=band,
                fill=label_color,
                font=("Segoe UI", 7, "bold"),
            )

    def update_status(self, last_tile: str = "") -> None:
        """Refresh the selected-count status line."""
        suffix = f" Last tile: {last_tile}." if last_tile else ""
        self.status_var.set(
            "Selected UTM tiles: "
            f"{len(self.selected_tiles)}. Project coverage tiles: {len(self.coverage_tiles)}. "
            f"Blue = active, green = covered, teal = both. Click tiles to select or unselect.{suffix}"
        )

    def on_canvas_configure(self, event: tk.Event) -> None:
        """Re-fit the map when the canvas is resized."""
        if event.width < 20 or event.height < 20:
            return
        self.transform = CanvasTransform(
            self.geometry_data.bounds,
            int(event.width),
            int(event.height),
            padding=16,
        )
        self.draw_map()

    def load_selected_preset(self) -> None:
        """Load the selected built-in/project preset into the map selection."""
        preset = self.preset_choices.get(self.preset_var.get())
        if preset is None:
            return
        self.selected_tiles = set(normalize_utm_tiles(preset.tiles))
        self.draw_map()
        self.update_status(f"{preset.name} preset")

    def clear_selection(self) -> None:
        """Clear all selected map tiles."""
        self.selected_tiles.clear()
        self.draw_map()
        self.update_status()

    def on_canvas_click(self, event: tk.Event) -> None:
        """Toggle the UTM tile under the click location."""
        world_point = self.transform.canvas_to_world((float(event.x), float(event.y)))
        token = hit_test_tile(self.geometry_data, world_point)
        if not token:
            self.update_status("none")
            return
        if token in self.selected_tiles:
            self.selected_tiles.remove(token)
        else:
            self.selected_tiles.add(token)
        self.draw_map()
        self.update_status(token)

    def apply_selection(self) -> None:
        """Send selected tiles back to the Download tab."""
        self.apply_callback(sorted(self.selected_tiles))
        self.destroy()
