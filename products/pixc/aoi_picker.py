"""Browser-based AOI picker for PIXC bounding-box searches."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Sequence
from urllib.parse import urlparse

from .download import BBox, validate_bbox


DEFAULT_CENTER = (0.0, 0.0)
DEFAULT_ZOOM = 2


@dataclass
class AoiSelection:
    """One AOI selection returned from the browser map."""

    bbox: Optional[BBox] = None
    basemap: str = ""
    saved_at: str = ""
    selected_granule_ids: list[str] = field(default_factory=list)
    selected_reference_tile_names: list[str] = field(default_factory=list)


class AoiPickerSession:
    """Serve a local browser map and receive one bbox selection."""

    def __init__(
        self,
        *,
        initial_bbox: Optional[Sequence[float]] = None,
        preview_features: Optional[Sequence[dict[str, object]]] = None,
        enable_reference_tiles: bool = False,
        selected_reference_tile_names: Optional[Sequence[str]] = None,
        max_reference_tiles: int = 1000,
        center: tuple[float, float] = DEFAULT_CENTER,
        zoom: int = DEFAULT_ZOOM,
    ) -> None:
        self.initial_bbox = validate_bbox(initial_bbox) if initial_bbox is not None else None
        self.preview_features = list(preview_features or [])
        self.enable_reference_tiles = bool(enable_reference_tiles)
        self.selected_reference_tile_names = [str(name) for name in selected_reference_tile_names or [] if str(name)]
        self.max_reference_tiles = max(1, int(max_reference_tiles))
        self.center = center
        self.zoom = zoom
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._selection: AoiSelection | None = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        """Return the local map URL."""
        if self._server is None:
            raise RuntimeError("AOI picker session has not been started.")
        host, port = self._server.server_address
        return f"http://{host}:{port}/"

    def start(self) -> str:
        """Start the local AOI picker server and return its URL."""
        if self._server is not None:
            return self.url
        handler = make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        """Stop the local AOI picker server."""
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def set_selection(
        self,
        bbox: Optional[Sequence[float]] = None,
        basemap: str = "",
        selected_granule_ids: Optional[Sequence[str]] = None,
        selected_reference_tile_names: Optional[Sequence[str]] = None,
    ) -> AoiSelection:
        """Store a bbox selection from the browser."""
        ids = [str(item) for item in selected_granule_ids or [] if str(item)]
        tile_names = [str(item) for item in selected_reference_tile_names or [] if str(item)]
        parsed_bbox = validate_bbox(bbox) if bbox is not None else None
        if parsed_bbox is None and not ids and not tile_names:
            raise ValueError("Select an AOI rectangle, preview granules, or SWOT reference tiles.")
        selection = AoiSelection(
            bbox=parsed_bbox,
            basemap=str(basemap or ""),
            saved_at=datetime.now().replace(microsecond=0).isoformat(),
            selected_granule_ids=ids,
            selected_reference_tile_names=tile_names,
        )
        with self._lock:
            self._selection = selection
        return selection

    def get_selection(self) -> AoiSelection | None:
        """Return the latest browser selection, if any."""
        with self._lock:
            return self._selection

    def render_html(self) -> str:
        """Render the AOI picker HTML page."""
        initial_bbox = json.dumps(list(self.initial_bbox) if self.initial_bbox else None)
        center = json.dumps(list(self.center))
        preview_features = json.dumps(self.preview_features)
        selected_reference_tiles = json.dumps(self.selected_reference_tile_names)
        return AOI_PICKER_HTML.replace("__INITIAL_BBOX__", initial_bbox).replace(
            "__PREVIEW_FEATURES__",
            preview_features,
        ).replace(
            "__ENABLE_REFERENCE_TILES__",
            "true" if self.enable_reference_tiles else "false",
        ).replace(
            "__SELECTED_REFERENCE_TILES__",
            selected_reference_tiles,
        ).replace(
            "__CENTER__",
            center,
        ).replace("__ZOOM__", str(int(self.zoom)))

    def reference_tile_features(self, payload: dict[str, object]) -> dict[str, object]:
        """Return reference tile GeoJSON features intersecting a browser AOI."""
        if not self.enable_reference_tiles:
            raise ValueError("SWOT reference tile selection is not enabled for this map.")
        from .reference_tiles import load_pixc_reference_tiles

        index = load_pixc_reference_tiles()
        limit = self.max_reference_tiles
        if payload.get("polygon"):
            tiles = index.find_tiles_intersecting_polygon(payload["polygon"], limit=limit + 1)  # type: ignore[arg-type]
        else:
            bbox = payload.get("bbox")
            if not bbox:
                raise ValueError("Reference tile query requires bbox or polygon.")
            tiles = index.find_tiles_intersecting_bbox(bbox, limit=limit + 1)  # type: ignore[arg-type]
        truncated = len(tiles) > limit
        tiles = tiles[:limit]
        selected = set(self.selected_reference_tile_names)
        return {
            "ok": True,
            "count": len(tiles),
            "truncated": truncated,
            "features": [tile.geojson_feature(tile.name in selected) for tile in tiles],
        }


def make_handler(session: AoiPickerSession) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one AOI picker session."""

    class AoiPickerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in {"/", "/index.html"}:
                self.send_json({"ok": False, "error": "not found"}, status=404)
                return
            body = session.render_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/selection":
                if parsed.path == "/reference_tiles":
                    self.handle_reference_tiles()
                    return
                self.send_json({"ok": False, "error": "not found"}, status=404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                selection = session.set_selection(
                    bbox=payload.get("bbox"),
                    basemap=str(payload.get("basemap", "")),
                    selected_granule_ids=payload.get("selectedGranuleIds", []),
                    selected_reference_tile_names=payload.get("selectedReferenceTileNames", []),
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self.send_json(
                {
                    "ok": True,
                    "bbox": list(selection.bbox) if selection.bbox is not None else None,
                    "basemap": selection.basemap,
                    "saved_at": selection.saved_at,
                    "selectedGranuleIds": selection.selected_granule_ids,
                    "selectedReferenceTileNames": selection.selected_reference_tile_names,
                }
            )

        def handle_reference_tiles(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                response = session.reference_tile_features(payload)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self.send_json(response)

        def send_json(self, payload: dict[str, object], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return AoiPickerHandler


AOI_PICKER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SWOTFlow PIXC AOI Picker</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css">
  <style>
    html, body, #map { height: 100%; margin: 0; }
    body { font-family: Segoe UI, Arial, sans-serif; }
    .panel {
      position: absolute;
      z-index: 1000;
      left: 12px;
      bottom: 18px;
      width: min(520px, calc(100vw - 24px));
      background: #ffffff;
      border: 1px solid #bfc7d5;
      border-radius: 6px;
      box-shadow: 0 8px 24px rgba(18, 29, 43, 0.18);
      padding: 10px;
      box-sizing: border-box;
    }
    .row { display: flex; gap: 8px; align-items: center; }
    #bboxText {
      flex: 1;
      min-width: 0;
      border: 1px solid #c6ceda;
      border-radius: 4px;
      padding: 7px 8px;
      font-size: 12px;
      color: #142033;
    }
    button {
      border: 1px solid #29527a;
      background: #184a8b;
      color: #ffffff;
      border-radius: 4px;
      padding: 8px 10px;
      font-size: 13px;
      cursor: pointer;
    }
    button:disabled {
      background: #8d9aaa;
      border-color: #8d9aaa;
      cursor: default;
    }
    #status { margin-top: 8px; color: #35445a; font-size: 12px; }
    #previewTools {
      display: none;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid #d9dee8;
    }
    #tileTools {
      display: none;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid #d9dee8;
    }
    #selectedText {
      flex: 1;
      min-width: 0;
      font-size: 12px;
      color: #142033;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <div class="row">
      <input id="bboxText" aria-label="Selected bbox" readonly value="">
      <button id="saveButton" type="button" disabled>Use This AOI</button>
    </div>
    <div id="previewTools" class="row">
      <span id="selectedText">No preview granules selected.</span>
      <button id="saveGranulesButton" type="button" disabled>Use Selected Granules</button>
    </div>
    <div id="tileTools" class="row">
      <span id="selectedTileText">Draw an AOI to load SWOT tiles.</span>
      <button id="saveTilesButton" type="button" disabled>Use Selected Tiles</button>
    </div>
    <div id="status">Draw or edit one rectangle, then save it.</div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
  <script>
    const initialBbox = __INITIAL_BBOX__;
    const previewFeatures = __PREVIEW_FEATURES__;
    const enableReferenceTiles = __ENABLE_REFERENCE_TILES__;
    const initialReferenceTiles = __SELECTED_REFERENCE_TILES__;
    const center = __CENTER__;
    const defaultZoom = __ZOOM__;
    let selectedBbox = null;
    let selectedPolygon = null;
    let activeBasemap = 'Satellite';
    const selectedGranuleIds = new Set();
    const selectedReferenceTileNames = new Set(initialReferenceTiles || []);
    let referenceTileLayer = null;

    const map = L.map('map', { zoomControl: true });
    const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    });
    const imagery = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 19,
      attribution: 'Tiles &copy; Esri'
    });
    imagery.addTo(map);
    L.control.layers({ 'Satellite': imagery, 'OpenStreetMap': osm }, null, { collapsed: false }).addTo(map);
    map.on('baselayerchange', function(event) {
      activeBasemap = event.name;
    });

    const drawnItems = new L.FeatureGroup();
    map.addLayer(drawnItems);
    const drawControl = new L.Control.Draw({
      draw: {
        polyline: false,
        polygon: enableReferenceTiles ? {
          allowIntersection: false,
          showArea: true,
          shapeOptions: {
            color: '#f28c28',
            weight: 2,
            fillOpacity: 0.12
          }
        } : false,
        circle: false,
        marker: false,
        circlemarker: false,
        rectangle: {
          shapeOptions: {
            color: '#f28c28',
            weight: 2,
            fillOpacity: 0.12
          }
        }
      },
      edit: {
        featureGroup: drawnItems
      }
    });
    map.addControl(drawControl);

    function bboxFromLayer(layer) {
      const bounds = layer.getBounds();
      return [
        bounds.getWest(),
        bounds.getSouth(),
        bounds.getEast(),
        bounds.getNorth()
      ];
    }

    function polygonFromLayer(layer) {
      const geometry = layer.toGeoJSON().geometry;
      if (!geometry || geometry.type !== 'Polygon' || !geometry.coordinates.length) {
        return null;
      }
      return geometry.coordinates[0];
    }

    function renderBbox(bbox) {
      return bbox.map(function(value) { return Number(value).toFixed(6); }).join(', ');
    }

    function updateSelectedGranules() {
      const count = selectedGranuleIds.size;
      document.getElementById('selectedText').textContent = count
        ? count + ' preview granule(s) selected.'
        : 'No preview granules selected.';
      document.getElementById('saveGranulesButton').disabled = count === 0;
    }

    function updateSelectedReferenceTiles() {
      const count = selectedReferenceTileNames.size;
      document.getElementById('selectedTileText').textContent = count
        ? count + ' SWOT reference tile(s) selected.'
        : 'No SWOT reference tiles selected.';
      document.getElementById('saveTilesButton').disabled = count === 0;
    }

    function setSelectionFromLayer(layer) {
      selectedBbox = bboxFromLayer(layer);
      selectedPolygon = polygonFromLayer(layer);
      document.getElementById('bboxText').value = renderBbox(selectedBbox);
      document.getElementById('saveButton').disabled = false;
      document.getElementById('status').textContent = 'AOI selected.';
      if (enableReferenceTiles) {
        loadReferenceTilesForSelection();
      }
    }

    async function loadReferenceTilesForSelection() {
      if (!enableReferenceTiles || !selectedBbox) {
        return;
      }
      document.getElementById('status').textContent = 'Loading intersecting SWOT reference tiles...';
      try {
        const response = await fetch('/reference_tiles', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bbox: selectedBbox, polygon: selectedPolygon })
        });
        const payload = await response.json();
        if (!payload.ok) {
          throw new Error(payload.error || 'Reference tile query failed.');
        }
        renderReferenceTiles(payload.features || []);
        const suffix = payload.truncated ? ' Result limited; draw a smaller AOI.' : '';
        document.getElementById('status').textContent =
          'Loaded ' + payload.count + ' intersecting SWOT reference tile(s).' + suffix;
      } catch (error) {
        document.getElementById('status').textContent = error.message;
      }
    }

    function renderReferenceTiles(features) {
      if (referenceTileLayer) {
        map.removeLayer(referenceTileLayer);
      }
      referenceTileLayer = L.geoJSON(features, {
        style: function(feature) {
          const name = String(feature.id || feature.properties.name || '');
          const selected = selectedReferenceTileNames.has(name) || feature.properties.selected;
          return {
            color: selected ? '#007c89' : '#7445a0',
            weight: selected ? 3 : 1.5,
            fillOpacity: selected ? 0.26 : 0.08
          };
        },
        onEachFeature: function(feature, layer) {
          const props = feature.properties || {};
          const name = String(feature.id || props.name || '');
          const label = [
            name,
            props.pass_num ? 'pass ' + props.pass_num : '',
            props.tile_num ? 'tile ' + props.tile_num + props.tile_side : '',
            props.scene_id ? 'scene ' + props.scene_id : ''
          ].filter(Boolean).join('<br>');
          layer.bindPopup(label);
          layer.on('click', function() {
            if (!name) {
              return;
            }
            if (selectedReferenceTileNames.has(name)) {
              selectedReferenceTileNames.delete(name);
            } else {
              selectedReferenceTileNames.add(name);
            }
            referenceTileLayer.resetStyle(layer);
            updateSelectedReferenceTiles();
          });
        }
      }).addTo(map);
      updateSelectedReferenceTiles();
    }

    map.on(L.Draw.Event.CREATED, function(event) {
      drawnItems.clearLayers();
      drawnItems.addLayer(event.layer);
      setSelectionFromLayer(event.layer);
    });

    map.on(L.Draw.Event.EDITED, function(event) {
      event.layers.eachLayer(function(layer) {
        setSelectionFromLayer(layer);
      });
    });

    map.on(L.Draw.Event.DELETED, function() {
      selectedBbox = null;
      selectedPolygon = null;
      document.getElementById('bboxText').value = '';
      document.getElementById('saveButton').disabled = true;
      if (referenceTileLayer) {
        map.removeLayer(referenceTileLayer);
        referenceTileLayer = null;
      }
      document.getElementById('status').textContent = 'AOI cleared.';
    });

    if (initialBbox) {
      const rectangle = L.rectangle(
        [[initialBbox[1], initialBbox[0]], [initialBbox[3], initialBbox[2]]],
        { color: '#f28c28', weight: 2, fillOpacity: 0.12 }
      );
      drawnItems.addLayer(rectangle);
      setSelectionFromLayer(rectangle);
      map.fitBounds(rectangle.getBounds(), { padding: [20, 20] });
    } else {
      map.setView(center, defaultZoom);
    }

    if (enableReferenceTiles) {
      document.getElementById('tileTools').style.display = 'flex';
      updateSelectedReferenceTiles();
      document.getElementById('status').textContent =
        'Draw an AOI to load intersecting SWOT reference tiles, then click tiles to select.';
    }

    if (previewFeatures && previewFeatures.length) {
      document.getElementById('previewTools').style.display = 'flex';
      document.getElementById('status').textContent =
        'Draw an AOI or click preview footprints to select granules.';
      const previewLayer = L.geoJSON(previewFeatures, {
        style: function(feature) {
          const id = String(feature.id || feature.properties.granule_id || '');
          const selected = selectedGranuleIds.has(id);
          return {
            color: selected ? '#00a1b3' : '#d7442e',
            weight: selected ? 3 : 2,
            fillOpacity: selected ? 0.28 : 0.12
          };
        },
        onEachFeature: function(feature, layer) {
          const props = feature.properties || {};
          const id = String(feature.id || props.granule_id || '');
          const label = [
            props.file_name || id,
            props.cycle_id ? 'cycle ' + props.cycle_id : '',
            props.pass_id ? 'pass ' + props.pass_id : '',
            props.tile_id || ''
          ].filter(Boolean).join('<br>');
          layer.bindPopup(label);
          layer.on('click', function() {
            if (!id) {
              return;
            }
            if (selectedGranuleIds.has(id)) {
              selectedGranuleIds.delete(id);
            } else {
              selectedGranuleIds.add(id);
            }
            previewLayer.resetStyle(layer);
            updateSelectedGranules();
          });
        }
      }).addTo(map);
      if (!initialBbox) {
        try {
          map.fitBounds(previewLayer.getBounds(), { padding: [20, 20] });
        } catch (error) {
          map.setView(center, defaultZoom);
        }
      }
    }

    document.getElementById('saveButton').addEventListener('click', async function() {
      if (!selectedBbox) {
        return;
      }
      const button = document.getElementById('saveButton');
      button.disabled = true;
      document.getElementById('status').textContent = 'Saving AOI...';
      try {
        const response = await fetch('/selection', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bbox: selectedBbox, basemap: activeBasemap })
        });
        const payload = await response.json();
        if (!payload.ok) {
          throw new Error(payload.error || 'AOI save failed.');
        }
        document.getElementById('status').textContent = 'AOI sent to SWOTFlow. You can return to the desktop app.';
      } catch (error) {
        button.disabled = false;
        document.getElementById('status').textContent = error.message;
      }
    });

    document.getElementById('saveTilesButton').addEventListener('click', async function() {
      if (!selectedReferenceTileNames.size) {
        return;
      }
      const button = document.getElementById('saveTilesButton');
      button.disabled = true;
      document.getElementById('status').textContent = 'Saving SWOT tile selection...';
      try {
        const response = await fetch('/selection', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            selectedReferenceTileNames: Array.from(selectedReferenceTileNames),
            basemap: activeBasemap
          })
        });
        const payload = await response.json();
        if (!payload.ok) {
          throw new Error(payload.error || 'SWOT tile selection failed.');
        }
        document.getElementById('status').textContent =
          'Selected SWOT reference tiles sent to SWOTFlow. You can return to the desktop app.';
      } catch (error) {
        button.disabled = false;
        document.getElementById('status').textContent = error.message;
      }
    });

    document.getElementById('saveGranulesButton').addEventListener('click', async function() {
      if (!selectedGranuleIds.size) {
        return;
      }
      const button = document.getElementById('saveGranulesButton');
      button.disabled = true;
      document.getElementById('status').textContent = 'Saving granule selection...';
      try {
        const response = await fetch('/selection', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            selectedGranuleIds: Array.from(selectedGranuleIds),
            basemap: activeBasemap
          })
        });
        const payload = await response.json();
        if (!payload.ok) {
          throw new Error(payload.error || 'Granule selection failed.');
        }
        document.getElementById('status').textContent =
          'Selected granules sent to SWOTFlow. You can return to the desktop app.';
      } catch (error) {
        button.disabled = false;
        document.getElementById('status').textContent = error.message;
      }
    });
  </script>
</body>
</html>
"""
