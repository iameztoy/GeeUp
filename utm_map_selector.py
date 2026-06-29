"""Tkinter UTM tile map selector backed by precomputed JSON geometry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import ttk

from swotflow_project import TilePreset
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


PIPELINE_STATUS_STYLES = {
    "none": {
        "label": "No project records",
        "fill": "#f1f1f1",
        "outline": "#d2d2d2",
    },
    "downloaded": {
        "label": "Downloaded only",
        "fill": "#5b8fd6",
        "outline": "#2f5e9e",
    },
    "extracted": {
        "label": "Extracted, not mosaicked",
        "fill": "#31a6a6",
        "outline": "#197272",
    },
    "mosaicked": {
        "label": "Mosaicked, not uploaded",
        "fill": "#e89b33",
        "outline": "#a46212",
    },
    "uploaded": {
        "label": "Uploaded/EE verified",
        "fill": "#4f9f50",
        "outline": "#2d6b2f",
    },
    "attention": {
        "label": "Partially uploaded; missing files",
        "fill": "#d95f5f",
        "outline": "#8f2f2f",
    },
}


UPDATE_COVERAGE_STATUS_STYLES = {
    "no_expected": {
        "label": "No preview matches",
        "fill": "#f1f1f1",
        "outline": "#d2d2d2",
    },
    "not_started": {
        "label": "Not started",
        "fill": "#c9c9c9",
        "outline": "#8b8b8b",
    },
    "pending_download": {
        "label": "Needs download",
        "fill": "#5b8fd6",
        "outline": "#2f5e9e",
    },
    "pending_extract": {
        "label": "Needs extraction",
        "fill": "#31a6a6",
        "outline": "#197272",
    },
    "pending_mosaic": {
        "label": "Needs mosaic",
        "fill": "#e89b33",
        "outline": "#a46212",
    },
    "pending_upload": {
        "label": "Needs upload/verification",
        "fill": "#d6b13f",
        "outline": "#8d7014",
    },
    "complete": {
        "label": "Complete for update preview",
        "fill": "#4f9f50",
        "outline": "#2d6b2f",
    },
    "attention": {
        "label": "Partial or inconsistent",
        "fill": "#d95f5f",
        "outline": "#8f2f2f",
    },
}


@dataclass
class TilePipelineStatus:
    """Per-tile project status used by the Statistics status map."""

    token: str
    downloaded: int = 0
    extracted: int = 0
    mosaic_sources: int = 0
    submitted: int = 0
    uploaded: int = 0
    missing_upload: int = 0
    status: str = "none"

    @property
    def label(self) -> str:
        """Return a human-readable status label."""
        return PIPELINE_STATUS_STYLES.get(
            self.status,
            PIPELINE_STATUS_STYLES["none"],
        )["label"]


@dataclass
class TileUpdateCoverageStatus:
    """Per-tile update-window coverage status used by the Statistics status map."""

    token: str
    expected: int = 0
    downloaded: int = 0
    extracted: int = 0
    mosaic_sources: int = 0
    uploaded_sources: int = 0
    latest_expected_date: str = ""
    latest_downloaded_date: str = ""
    latest_extracted_date: str = ""
    latest_mosaicked_date: str = ""
    latest_uploaded_date: str = ""
    status: str = "no_expected"

    @property
    def label(self) -> str:
        """Return a human-readable update coverage status label."""
        return UPDATE_COVERAGE_STATUS_STYLES.get(
            self.status,
            UPDATE_COVERAGE_STATUS_STYLES["attention"],
        )["label"]


def pipeline_status_key(
    downloaded: int,
    extracted: int,
    mosaic_sources: int,
    uploaded: int,
    missing_upload: int,
) -> str:
    """Classify one UTM tile by the furthest verified pipeline stage reached."""
    values = [downloaded, extracted, mosaic_sources, uploaded, missing_upload]
    downloaded, extracted, mosaic_sources, uploaded, missing_upload = [
        max(0, int(value)) for value in values
    ]
    if not any((downloaded, extracted, mosaic_sources, uploaded, missing_upload)):
        return "none"
    if uploaded > 0 and missing_upload > 0:
        return "attention"
    if uploaded > 0:
        return "uploaded"
    if mosaic_sources > 0:
        return "mosaicked"
    if extracted > 0:
        return "extracted"
    return "downloaded"


def pipeline_status_from_qa_row(row: Sequence[object]) -> TilePipelineStatus:
    """Create a tile pipeline status from a ProjectInsights upload QA row."""
    if len(row) < 6:
        raise ValueError("Pipeline QA row must contain six values.")
    token = str(row[0])
    try:
        normalized = normalize_utm_tiles([token])[0]
    except ValueError:
        normalized = token.strip().upper()
    downloaded = int(row[1])
    extracted = int(row[2])
    mosaic_sources = int(row[3])
    if len(row) >= 7:
        submitted = int(row[4])
        uploaded = int(row[5])
        missing_upload = int(row[6])
    else:
        submitted = 0
        uploaded = int(row[4])
        missing_upload = int(row[5])
    return TilePipelineStatus(
        token=normalized,
        downloaded=downloaded,
        extracted=extracted,
        mosaic_sources=mosaic_sources,
        submitted=submitted,
        uploaded=uploaded,
        missing_upload=missing_upload,
        status=pipeline_status_key(
            downloaded,
            extracted,
            mosaic_sources,
            uploaded,
            missing_upload,
        ),
    )


def update_coverage_status_from_row(row: Sequence[object]) -> TileUpdateCoverageStatus:
    """Create a tile update coverage status from a ProjectInsights row."""
    if len(row) < 12:
        raise ValueError("Update coverage row must contain twelve values.")
    token = str(row[0])
    try:
        normalized = normalize_utm_tiles([token])[0]
    except ValueError:
        normalized = token.strip().upper()
    return TileUpdateCoverageStatus(
        token=normalized,
        expected=int(row[1]),
        downloaded=int(row[2]),
        extracted=int(row[3]),
        mosaic_sources=int(row[4]),
        uploaded_sources=int(row[5]),
        latest_expected_date=str(row[6]),
        latest_downloaded_date=str(row[7]),
        latest_extracted_date=str(row[8]),
        latest_mosaicked_date=str(row[9]),
        latest_uploaded_date=str(row[10]),
        status=str(row[11] or "no_expected"),
    )


class UTMPipelineStatusMap(ttk.Frame):
    """Read-only UTM status map for project pipeline QA/QC."""

    canvas_width = 940
    canvas_height = 520

    def __init__(
        self,
        master: tk.Misc,
        geometry: UTMDisplayGeometry | None = None,
    ) -> None:
        super().__init__(master)
        self.geometry_data = geometry
        self.tile_statuses: Dict[str, TilePipelineStatus] = {}
        self.update_coverage_statuses: Dict[str, TileUpdateCoverageStatus] = {}
        self.update_coverage_campaign_rows: Dict[str, Sequence[Sequence[object]]] = {}
        self.update_campaign_label_by_id: Dict[str, str] = {}
        self.update_campaign_id_by_label: Dict[str, str] = {}
        self.active_update_campaign_id = ""
        self.transform = CanvasTransform(
            geometry.bounds if geometry is not None else (-180.0, -80.0, 180.0, 84.0),
            self.canvas_width,
            self.canvas_height,
            padding=16,
        )
        self.status_var = tk.StringVar(
            value="Refresh statistics to populate the UTM pipeline status map."
        )
        self.missing_upload_rows_by_tile: Dict[str, List[Tuple[str, str, str]]] = {}
        self.show_labels_var = tk.BooleanVar(value=True)
        self.map_mode_var = tk.StringVar(value="Pipeline Status")
        self.update_campaign_var = tk.StringVar(value="")
        self.legend_frame: ttk.Frame | None = None
        self.build_layout()
        self.draw_map()

    def build_layout(self) -> None:
        """Build the read-only map, legend, and status controls."""
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        controls = ttk.Frame(self, padding=(0, 0, 0, 8))
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)
        ttk.Label(
            controls,
            text="UTM Pipeline Status Map",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(controls, text="Mode").grid(row=0, column=1, sticky="e", padx=(8, 4))
        self.map_mode_combo = ttk.Combobox(
            controls,
            textvariable=self.map_mode_var,
            values=("Pipeline Status", "Update Coverage"),
            state="readonly",
            width=18,
        )
        self.map_mode_combo.grid(row=0, column=2, sticky="e")
        self.map_mode_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_mode_changed())
        ttk.Label(controls, text="Update window").grid(row=0, column=3, sticky="e", padx=(10, 4))
        self.update_campaign_combo = ttk.Combobox(
            controls,
            textvariable=self.update_campaign_var,
            values=(),
            state="disabled",
            width=34,
        )
        self.update_campaign_combo.grid(row=0, column=4, sticky="e")
        self.update_campaign_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.on_update_campaign_changed(),
        )
        ttk.Checkbutton(
            controls,
            text="Show UTM labels",
            variable=self.show_labels_var,
            command=self.draw_map,
        ).grid(row=0, column=5, sticky="e", padx=(10, 0))

        self.canvas = tk.Canvas(
            self,
            width=self.canvas_width,
            height=self.canvas_height,
            background="#f7f8f5",
            highlightthickness=1,
            highlightbackground="#b7b7b7",
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Leave>", self.on_canvas_leave)

        self.legend_frame = ttk.Frame(self, padding=(0, 8, 0, 0))
        self.legend_frame.grid(row=2, column=0, sticky="ew")
        self.legend_frame.columnconfigure(0, weight=1)
        self.draw_legend()

        self.status_frame = ttk.Frame(self, height=58, padding=(0, 6, 0, 0))
        self.status_frame.grid(row=3, column=0, sticky="ew")
        self.status_frame.grid_propagate(False)
        self.status_frame.columnconfigure(0, weight=1)
        self.status_label = tk.Label(
            self.status_frame,
            textvariable=self.status_var,
            foreground="#184a8b",
            anchor="nw",
            justify="left",
            wraplength=900,
            height=3,
            borderwidth=0,
            padx=0,
            pady=0,
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.status_frame.bind(
            "<Configure>",
            lambda event: self.status_label.configure(
                wraplength=max(240, int(event.width) - 4)
            ),
        )

    def current_mode_key(self) -> str:
        """Return the selected status-map mode key."""
        return "update" if self.map_mode_var.get() == "Update Coverage" else "pipeline"

    def on_mode_changed(self) -> None:
        """Redraw when the status-map mode changes."""
        self.update_campaign_combo.configure(
            state="readonly"
            if self.current_mode_key() == "update" and self.update_campaign_id_by_label
            else "disabled"
        )
        self.draw_legend()
        self.draw_map()
        self.update_summary_status()

    def on_update_campaign_changed(self) -> None:
        """Switch the Update Coverage map to another persisted date window."""
        campaign_id = self.update_campaign_id_by_label.get(self.update_campaign_var.get(), "")
        if not campaign_id:
            return
        self.active_update_campaign_id = campaign_id
        self.set_update_coverage_statuses(
            self.update_coverage_campaign_rows.get(campaign_id, [])
        )

    def draw_legend(self) -> None:
        """Draw the legend for the active status-map mode."""
        if self.legend_frame is None:
            return
        for child in self.legend_frame.winfo_children():
            child.destroy()
        if self.current_mode_key() == "update":
            styles = UPDATE_COVERAGE_STATUS_STYLES
            keys = (
                "not_started",
                "pending_download",
                "pending_extract",
                "pending_mosaic",
                "pending_upload",
                "complete",
                "no_expected",
            )
        else:
            styles = PIPELINE_STATUS_STYLES
            keys = ("downloaded", "extracted", "mosaicked", "uploaded", "attention", "none")
        for index, key in enumerate(keys):
            style = styles[key]
            item = ttk.Frame(self.legend_frame)
            item.grid(row=0, column=index, sticky="w", padx=(0, 12))
            swatch = tk.Canvas(item, width=16, height=12, highlightthickness=0)
            swatch.grid(row=0, column=0, sticky="w")
            swatch.create_rectangle(
                0,
                0,
                16,
                12,
                fill=style["fill"],
                outline=style["outline"],
            )
            ttk.Label(item, text=style["label"]).grid(row=0, column=1, sticky="w", padx=(4, 0))

    def set_geometry(self, geometry: UTMDisplayGeometry) -> None:
        """Set display geometry and redraw the status map."""
        self.geometry_data = geometry
        self.transform = CanvasTransform(
            geometry.bounds,
            self.canvas_width,
            self.canvas_height,
            padding=16,
        )
        self.draw_map()

    def set_tile_statuses(self, rows: Sequence[Sequence[object]]) -> None:
        """Update map statuses from ProjectInsights upload QA rows."""
        statuses: Dict[str, TilePipelineStatus] = {}
        for row in rows:
            status = pipeline_status_from_qa_row(row)
            statuses[status.token] = status
        self.tile_statuses = statuses
        self.draw_map()
        self.update_summary_status()

    def set_update_coverage_statuses(self, rows: Sequence[Sequence[object]]) -> None:
        """Update map statuses from ProjectInsights update coverage rows."""
        statuses: Dict[str, TileUpdateCoverageStatus] = {}
        for row in rows:
            try:
                status = update_coverage_status_from_row(row)
            except (TypeError, ValueError):
                continue
            statuses[status.token] = status
        self.update_coverage_statuses = statuses
        self.draw_map()
        self.update_summary_status()

    def set_update_campaigns(
        self,
        campaigns: Sequence[Sequence[object]],
        rows_by_campaign: Mapping[str, Sequence[Sequence[object]]],
        active_campaign_id: str = "",
    ) -> None:
        """Load persisted update windows and select the latest/current campaign."""
        self.update_coverage_campaign_rows = {
            str(campaign_id): rows
            for campaign_id, rows in rows_by_campaign.items()
        }
        self.update_campaign_label_by_id = {}
        self.update_campaign_id_by_label = {}
        labels: list[str] = []
        for campaign in campaigns:
            if len(campaign) < 7:
                continue
            campaign_id = str(campaign[0])
            label = f"{campaign[1]} ({campaign[6]} tiles, {campaign[5]} expected)"
            self.update_campaign_label_by_id[campaign_id] = label
            self.update_campaign_id_by_label[label] = campaign_id
            labels.append(label)
        selected_id = (
            active_campaign_id
            if active_campaign_id in self.update_campaign_label_by_id
            else (str(campaigns[0][0]) if campaigns else "")
        )
        self.active_update_campaign_id = selected_id
        selected_label = self.update_campaign_label_by_id.get(selected_id, "")
        self.update_campaign_var.set(selected_label)
        self.update_campaign_combo.configure(
            values=labels,
            state="readonly"
            if self.current_mode_key() == "update" and labels
            else "disabled",
        )
        if selected_id:
            self.set_update_coverage_statuses(
                self.update_coverage_campaign_rows.get(selected_id, [])
            )

    def set_missing_upload_rows(self, rows: Sequence[Sequence[object]]) -> None:
        """Track upload-ready mosaic rows that are not yet uploaded/verified."""
        missing_by_tile: Dict[str, List[Tuple[str, str, str]]] = {}
        for row in rows:
            if len(row) < 4:
                continue
            output_file = str(row[0])
            source_tiles = [
                part.strip().upper()
                for part in str(row[1]).replace(";", ",").split(",")
                if part.strip()
            ]
            date_text = str(row[2])
            grid = str(row[3])
            for tile in source_tiles:
                missing_by_tile.setdefault(tile, []).append((output_file, date_text, grid))
        self.missing_upload_rows_by_tile = missing_by_tile

    def clear_statuses(self) -> None:
        """Clear all project status data from the map."""
        self.tile_statuses = {}
        self.update_coverage_statuses = {}
        self.update_coverage_campaign_rows = {}
        self.update_campaign_label_by_id = {}
        self.update_campaign_id_by_label = {}
        self.active_update_campaign_id = ""
        self.update_campaign_var.set("")
        self.update_campaign_combo.configure(values=(), state="disabled")
        self.missing_upload_rows_by_tile = {}
        self.draw_map()
        self.status_var.set("Refresh statistics to populate the UTM pipeline status map.")

    def tile_status(self, token: str) -> TilePipelineStatus:
        """Return current status for a tile, defaulting to no project records."""
        return self.tile_statuses.get(token, TilePipelineStatus(token=token))

    def tile_update_coverage_status(self, token: str) -> TileUpdateCoverageStatus:
        """Return current update coverage for a tile, defaulting to no expected rows."""
        return self.update_coverage_statuses.get(token, TileUpdateCoverageStatus(token=token))

    def draw_map(self) -> None:
        """Redraw continents, tiles, labels, and status colors."""
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("all")
        if self.geometry_data is None:
            self.canvas.create_text(
                12,
                16,
                anchor="nw",
                text="Status map geometry is not loaded yet.",
                fill="#555555",
            )
            return

        self.draw_continents(fill="#e1e5dc", outline="#b8beb2", width=0.7)
        for token, tile in self.geometry_data.tiles.items():
            if self.current_mode_key() == "update":
                status = self.tile_update_coverage_status(token)
                style = UPDATE_COVERAGE_STATUS_STYLES.get(
                    status.status,
                    UPDATE_COVERAGE_STATUS_STYLES["attention"],
                )
                width = 1.1 if status.status != "no_expected" else 0.5
            else:
                status = self.tile_status(token)
                style = PIPELINE_STATUS_STYLES.get(status.status, PIPELINE_STATUS_STYLES["none"])
                width = 1.1 if status.status != "none" else 0.5
            for ring in tile.polygons:
                points = ring_to_canvas_points(self.transform, ring)
                if len(points) >= 8:
                    self.canvas.create_polygon(
                        points,
                        fill=style["fill"],
                        outline=style["outline"],
                        width=width,
                    )
        self.draw_continents(fill="", outline="#69715f", width=1.1)
        if self.show_labels_var.get():
            self.draw_utm_labels()

    def draw_continents(self, fill: str, outline: str, width: float) -> None:
        """Draw continent polygons on the map canvas."""
        if self.geometry_data is None:
            return
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
        if self.geometry_data is None:
            return
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
            x, _y = self.transform.world_to_canvas(
                ((minx + maxx) / 2.0, self.geometry_data.bounds[3])
            )
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
            _x, y = self.transform.world_to_canvas(
                (self.geometry_data.bounds[0], (miny + maxy) / 2.0)
            )
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

    def on_canvas_configure(self, event: tk.Event) -> None:
        """Re-fit the status map when the canvas is resized."""
        if self.geometry_data is None or event.width < 20 or event.height < 20:
            return
        self.transform = CanvasTransform(
            self.geometry_data.bounds,
            int(event.width),
            int(event.height),
            padding=16,
        )
        self.draw_map()

    def tile_at_canvas_event(self, event: tk.Event) -> str | None:
        """Return the tile token under a canvas event."""
        if self.geometry_data is None:
            return None
        world_point = self.transform.canvas_to_world((float(event.x), float(event.y)))
        return hit_test_tile(self.geometry_data, world_point)

    def on_canvas_motion(self, event: tk.Event) -> None:
        """Show details for the tile under the pointer."""
        token = self.tile_at_canvas_event(event)
        if token is None:
            self.update_summary_status()
            return
        self.update_tile_status(token)

    def on_canvas_click(self, event: tk.Event) -> None:
        """Show details for the clicked tile without changing selection."""
        token = self.tile_at_canvas_event(event)
        if token is None:
            self.update_summary_status()
            return
        self.update_tile_status(token)

    def on_canvas_leave(self, _event: tk.Event) -> None:
        """Restore the summary status when the pointer leaves the map."""
        self.update_summary_status()

    def update_tile_status(self, token: str) -> None:
        """Show a detailed status message for one tile."""
        if self.current_mode_key() == "update":
            self.update_tile_coverage_status(token)
            return
        status = self.tile_status(token)
        self.status_var.set(
            f"{token}: {status.label}. "
            f"Downloaded {status.downloaded}; extracted {status.extracted}; "
            f"mosaic sources {status.mosaic_sources}; sent to upload {status.submitted}; "
            f"uploaded/verified {status.uploaded}; "
            f"missing upload {status.missing_upload}."
        )
        missing_rows = self.missing_upload_rows_by_tile.get(token, [])
        if missing_rows:
            output_file, date_text, grid = missing_rows[0]
            extra = "" if len(missing_rows) == 1 else f" +{len(missing_rows) - 1} more"
            self.status_var.set(
                f"{self.status_var.get()} First missing mosaic: {Path(output_file).name} "
                f"({date_text}, {grid}){extra}. Full path is listed in Uploaded > QA > "
                "Ready Mosaics Not Uploaded/Verified."
            )

    def update_tile_coverage_status(self, token: str) -> None:
        """Show a detailed update-window coverage message for one tile."""
        status = self.tile_update_coverage_status(token)
        date_bits = []
        if status.latest_expected_date:
            date_bits.append(f"latest expected {status.latest_expected_date}")
        if status.latest_uploaded_date:
            date_bits.append(f"latest uploaded/verified {status.latest_uploaded_date}")
        date_text = "; ".join(date_bits)
        if date_text:
            date_text = f" {date_text}."
        self.status_var.set(
            f"{token}: {status.label}. "
            f"Expected {status.expected}; downloaded {status.downloaded}; "
            f"extracted {status.extracted}; mosaic sources {status.mosaic_sources}; "
            f"uploaded/verified sources {status.uploaded_sources}.{date_text}"
        )
        if status.expected == 0:
            self.status_var.set(
                f"{self.status_var.get()} Run Download > Preview Search for the update date window "
                "to populate expected remote matches."
            )

    def update_summary_status(self) -> None:
        """Show a compact project status summary."""
        if self.current_mode_key() == "update":
            self.update_coverage_summary_status()
            return
        counts: Dict[str, int] = {}
        for status in self.tile_statuses.values():
            counts[status.status] = counts.get(status.status, 0) + 1
        if not counts:
            self.status_var.set("No per-tile project records are available yet.")
            return
        ordered = [
            f"{PIPELINE_STATUS_STYLES[key]['label']}: {counts[key]}"
            for key in ("downloaded", "extracted", "mosaicked", "uploaded", "attention")
            if counts.get(key)
        ]
        self.status_var.set("Project tile status. " + "; ".join(ordered) + ".")

    def update_coverage_summary_status(self) -> None:
        """Show a compact update coverage summary."""
        counts: Dict[str, int] = {}
        expected_total = 0
        for status in self.update_coverage_statuses.values():
            counts[status.status] = counts.get(status.status, 0) + 1
            expected_total += status.expected
        if not counts:
            self.status_var.set(
                "No update coverage rows are available. Run Download > Preview Search "
                "for the update window, then Refresh Statistics."
            )
            return
        ordered = [
            f"{UPDATE_COVERAGE_STATUS_STYLES[key]['label']}: {counts[key]}"
            for key in (
                "complete",
                "pending_upload",
                "pending_mosaic",
                "pending_extract",
                "pending_download",
                "not_started",
                "no_expected",
            )
            if counts.get(key)
        ]
        self.status_var.set(
            f"Update coverage for {self.update_campaign_label_by_id.get(self.active_update_campaign_id, 'latest Download preview')}. "
            f"Expected granules: {expected_total}. "
            + "; ".join(ordered)
            + "."
        )


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
        status_rows: Sequence[Sequence[object]] | None = None,
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
        self.tile_statuses: Dict[str, TilePipelineStatus] = {}
        for row in status_rows or []:
            try:
                status = pipeline_status_from_qa_row(row)
            except (TypeError, ValueError):
                continue
            self.tile_statuses[status.token] = status
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
            status = self.tile_statuses.get(token)
            if status is not None:
                style = PIPELINE_STATUS_STYLES.get(status.status, PIPELINE_STATUS_STYLES["none"])
                fill = style["fill"]
                outline = "#184a8b" if is_selected else style["outline"]
                width = 2.0 if is_selected else 0.9
            elif is_selected and is_covered:
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
