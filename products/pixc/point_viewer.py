"""Browser-based PIXC point map viewer."""

from __future__ import annotations

import json
import csv
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .visualize import PixcPointMapData


@dataclass
class PointMapSession:
    """Serve one PIXC point map in a local browser."""

    map_data: PixcPointMapData
    reference_layers: list[dict[str, object]] = field(default_factory=list)
    reference_log_csv: str | Path | None = None
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """Return the local point viewer URL."""
        if self._server is None:
            raise RuntimeError("Point map session has not been started.")
        host, port = self._server.server_address
        return f"http://{host}:{port}/"

    def start(self) -> str:
        """Start the local point viewer server and return its URL."""
        if self._server is not None:
            return self.url
        handler = make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        """Stop the local point viewer server."""
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def render_html(self) -> str:
        """Render the point viewer HTML page."""
        return POINT_VIEWER_HTML

    def point_payload(self) -> dict[str, object]:
        """Return browser-ready point map data."""
        payload = self.map_data.to_dict()
        payload["reference_layers"] = list(self.reference_layers)
        file_paths = list(payload.get("file_paths", []) or [])
        if not file_paths and payload.get("file_path"):
            file_paths = [str(payload.get("file_path", ""))]
        try:
            from .earth_engine_imagery import infer_reference_date_from_paths

            date_text, warnings = infer_reference_date_from_paths(file_paths)
        except Exception:
            date_text, warnings = "", []
        payload["swot_reference_date"] = date_text
        payload["swot_reference_date_warnings"] = warnings
        return payload

    def log_reference_event(self, event: str, row: dict[str, object]) -> None:
        """Append one viewer-side reference imagery event to the project CSV log."""
        if not self.reference_log_csv:
            return
        try:
            log_path = Path(self.reference_log_csv)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fields = [
                "timestamp",
                "event",
                "source",
                "start_date",
                "end_date",
                "image_id",
                "band_preset",
                "status",
                "message",
            ]
            write_header = not log_path.exists() or log_path.stat().st_size == 0
            payload = {
                "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "event": event,
                **row,
            }
            with log_path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                if write_header:
                    writer.writeheader()
                writer.writerow({field: payload.get(field, "") for field in fields})
        except Exception:
            return

    @property
    def reference_bbox(self) -> tuple[float, float, float, float] | None:
        """Return the full-coordinate bbox to use for EE reference searches."""
        payload = self.map_data.to_dict()
        bbox = payload.get("reference_bbox") or payload.get("bbox")
        if not bbox:
            return None
        values = tuple(float(item) for item in bbox)  # type: ignore[iteration-over-optional]
        return values if len(values) == 4 else None


def make_handler(session: PointMapSession) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one point map session."""

    class PointMapHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                body = session.render_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/points.json":
                self.send_json(session.point_payload())
                return
            if parsed.path == "/reference_options.json":
                try:
                    from .earth_engine_imagery import reference_imagery_options

                    self.send_json({"ok": True, **reference_imagery_options()})
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)}, status=500)
                return
            self.send_json({"ok": False, "error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                payload = self.read_json_body()
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            if parsed.path == "/reference_search":
                self.handle_reference_search(payload)
                return
            if parsed.path == "/reference_layer":
                self.handle_reference_layer(payload)
                return
            self.send_json({"ok": False, "error": "not found"}, status=404)

        def read_json_body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            data = self.rfile.read(length) if length else b"{}"
            value = json.loads(data.decode("utf-8") or "{}")
            if not isinstance(value, dict):
                raise ValueError("JSON object body is required.")
            return value

        def handle_reference_search(self, payload: dict[str, object]) -> None:
            bbox = session.reference_bbox
            if bbox is None:
                self.send_json({"ok": False, "error": "Point-cloud bbox is unavailable."}, status=400)
                return
            sources = payload.get("sources", [])
            if not isinstance(sources, list):
                sources = []
            start_date = str(payload.get("start_date", "") or "").strip()
            end_date = str(payload.get("end_date", "") or "").strip()
            ee_project = str(payload.get("ee_project", "") or "").strip()
            try:
                max_images = int(str(payload.get("max_images", "50") or "50"))
            except ValueError:
                max_images = 50
            images: list[dict[str, object]] = []
            warnings: list[str] = []
            try:
                from .earth_engine_imagery import EarthEngineImageSearchConfig, search_reference_images

                for source in sources:
                    result = search_reference_images(
                        EarthEngineImageSearchConfig(
                            source=str(source),
                            start_date=start_date,
                            end_date=end_date,
                            bbox=bbox,
                            ee_project=ee_project,
                            max_images=max_images,
                        )
                    )
                    images.extend(record.to_dict() for record in result.images)
                    warnings.extend(result.warnings)
                session.log_reference_event(
                    "search",
                    {
                        "source": ",".join(str(source) for source in sources),
                        "start_date": start_date,
                        "end_date": end_date,
                        "status": "OK" if images else "WARNING",
                        "message": "; ".join(warnings),
                    },
                )
                self.send_json({"ok": True, "images": images, "warnings": warnings, "bbox": list(bbox)})
            except Exception as exc:
                session.log_reference_event(
                    "search",
                    {
                        "source": ",".join(str(source) for source in sources),
                        "start_date": start_date,
                        "end_date": end_date,
                        "status": "ERROR",
                        "message": str(exc),
                    },
                )
                self.send_json({"ok": False, "error": str(exc)}, status=500)

        def handle_reference_layer(self, payload: dict[str, object]) -> None:
            source = str(payload.get("source", "") or "").strip()
            image_id = str(payload.get("image_id", "") or "").strip()
            band_preset = str(payload.get("band_preset", "") or "").strip()
            ee_project = str(payload.get("ee_project", "") or "").strip()
            layer_name = str(payload.get("layer_name", "") or "").strip()
            try:
                from .earth_engine_imagery import EarthEngineTileLayerRequest, build_reference_tile_layer

                layer = build_reference_tile_layer(
                    EarthEngineTileLayerRequest(
                        source=source,
                        image_id=image_id,
                        band_preset=band_preset,
                        ee_project=ee_project,
                        layer_name=layer_name,
                    )
                )
                session.log_reference_event(
                    "layer",
                    {
                        "source": source,
                        "image_id": image_id,
                        "band_preset": band_preset,
                        "status": "OK",
                        "message": layer.name,
                    },
                )
                self.send_json({"ok": True, "layer": layer.to_dict()})
            except Exception as exc:
                session.log_reference_event(
                    "layer",
                    {
                        "source": source,
                        "image_id": image_id,
                        "band_preset": band_preset,
                        "status": "ERROR",
                        "message": str(exc),
                    },
                )
                self.send_json({"ok": False, "error": str(exc)}, status=500)

        def send_json(self, payload: dict[str, object], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return PointMapHandler


POINT_VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SWOTFlow PIXC Point Viewer</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <style>
    html, body, #map { height: 100%; margin: 0; }
    body { font-family: Segoe UI, Arial, sans-serif; color: #142033; }
    .panel, .eePanel {
      position: absolute;
      z-index: 1000;
      background: #ffffff;
      border: 1px solid #bfc7d5;
      border-radius: 6px;
      box-shadow: 0 8px 24px rgba(18, 29, 43, 0.18);
      box-sizing: border-box;
    }
    .panel {
      left: 12px;
      bottom: 18px;
      width: min(560px, calc(100vw - 24px));
      padding: 10px;
    }
    .eePanel {
      left: 56px;
      top: 12px;
      width: min(430px, calc(100vw - 24px));
      max-height: calc(100vh - 24px);
      padding: 10px;
      overflow-y: auto;
    }
    .title, .eeTitle {
      font-size: 13px;
      font-weight: 600;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .meta, #status, #measureStatus, .eeStatus, .resultMeta {
      color: #35445a;
      font-size: 12px;
      line-height: 1.35;
    }
    .meta { margin-top: 5px; }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 10px;
      margin-top: 8px;
      max-height: 92px;
      overflow-y: auto;
    }
    .legendItem {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 12px;
      color: #25344d;
    }
    .swatch {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      border: 1px solid rgba(0, 0, 0, 0.25);
      flex: 0 0 auto;
    }
    .tools, .eeActions, .eeChecks, .resultActions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    button, .toolButton {
      border: 1px solid #aab5c6;
      background: #f7f9fc;
      color: #142033;
      border-radius: 4px;
      cursor: pointer;
      font: 12px Segoe UI, Arial, sans-serif;
      padding: 5px 8px;
    }
    button.primary {
      background: #184a8b;
      border-color: #184a8b;
      color: #ffffff;
    }
    button:disabled {
      cursor: default;
      opacity: 0.55;
    }
    .toolButton.active {
      background: #184a8b;
      border-color: #184a8b;
      color: #ffffff;
    }
    label {
      font-size: 12px;
      color: #25344d;
    }
    input, select {
      border: 1px solid #aab5c6;
      border-radius: 4px;
      box-sizing: border-box;
      color: #142033;
      font: 12px Segoe UI, Arial, sans-serif;
      padding: 5px 6px;
    }
    .eeGrid {
      display: grid;
      grid-template-columns: 80px 1fr 1fr;
      gap: 6px;
      align-items: center;
      margin-top: 8px;
    }
    .eeGrid label { grid-column: 1; }
    .eeGrid input { width: 100%; }
    .fullRow { grid-column: 2 / 4; }
    .resultList {
      display: grid;
      gap: 8px;
      margin-top: 10px;
      max-height: 260px;
      overflow-y: auto;
    }
    .resultCard {
      border: 1px solid #d5dbe6;
      border-radius: 5px;
      padding: 8px;
      background: #fbfcfe;
    }
    .resultTitle {
      font-size: 12px;
      font-weight: 600;
      color: #142033;
      overflow-wrap: anywhere;
    }
    .resultActions select { flex: 1 1 210px; min-width: 0; }
    #measureStatus { margin-top: 6px; }
    @media (max-width: 780px) {
      .eePanel {
        left: 12px;
        top: 12px;
        width: auto;
        max-height: 48vh;
      }
      .panel {
        bottom: 12px;
      }
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel" id="pointPanel">
    <div class="title" id="title">PIXC points</div>
    <div class="meta" id="meta">Loading points...</div>
    <div class="tools">
      <button type="button" class="toolButton" id="measureButton">Measure Distance</button>
      <button type="button" class="toolButton" id="clearMeasureButton">Clear Measurement</button>
    </div>
    <div id="measureStatus">Measurement: inactive</div>
    <div class="legend" id="legend"></div>
    <div id="status"></div>
  </div>
  <div class="eePanel" id="eePanel">
    <div class="eeTitle">Earth Engine Reference</div>
    <div class="eeGrid">
      <label for="eeStartDate">Start</label>
      <input id="eeStartDate" type="date">
      <button type="button" id="useSwotDateButton">Use SWOT Date</button>
      <label for="eeEndDate">End</label>
      <input id="eeEndDate" type="date">
      <input id="eeProject" class="fullRow" placeholder="EE project id (optional)">
      <label for="eeMaxImages">Max/source</label>
      <input id="eeMaxImages" type="number" min="1" max="200" value="30">
      <button type="button" id="allSourcesButton">All Sources</button>
    </div>
    <div class="eeChecks">
      <label><input type="checkbox" class="sourceCheck" value="sentinel2" checked> Sentinel-2</label>
      <label><input type="checkbox" class="sourceCheck" value="sentinel1"> Sentinel-1</label>
      <label><input type="checkbox" class="sourceCheck" value="landsat8"> Landsat 8</label>
      <label><input type="checkbox" class="sourceCheck" value="landsat9"> Landsat 9</label>
    </div>
    <div class="eeActions">
      <button type="button" class="primary" id="searchReferenceButton">Search Images</button>
      <button type="button" id="clearEeLayersButton">Clear EE Layers</button>
    </div>
    <div class="eeStatus" id="eeStatus">Search uses the full valid PIXC coordinate bbox.</div>
    <div class="resultList" id="referenceResults"></div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
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
    const baseLayers = { 'Satellite': imagery, 'OpenStreetMap': osm };
    const overlayLayers = {};
    const layerControl = L.control.layers(baseLayers, overlayLayers, { collapsed: false }).addTo(map);
    const pointRenderer = L.canvas({ padding: 0.5 });
    let pointPayload = null;
    let referenceOptions = null;
    let measureActive = false;
    let measurePoints = [];
    let measurementLine = null;
    let measurementMarkers = [];
    let eeLayerCounter = 0;
    const eeDynamicLayers = {};

    L.DomEvent.disableClickPropagation(document.getElementById('pointPanel'));
    L.DomEvent.disableClickPropagation(document.getElementById('eePanel'));

    function fileName(path) {
      const text = String(path || '');
      const parts = text.split(/[\\\\/]/);
      return parts[parts.length - 1] || text;
    }

    function formatNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) ? number.toFixed(6) : String(value);
    }

    function renderLegend(legend) {
      const container = document.getElementById('legend');
      container.innerHTML = '';
      (legend || []).forEach(function(item) {
        const row = document.createElement('span');
        row.className = 'legendItem';
        const swatch = document.createElement('span');
        swatch.className = 'swatch';
        swatch.style.background = item.color || '#184a8b';
        const label = document.createElement('span');
        label.textContent = item.count !== undefined ? item.label + ' (' + item.count + ')' : item.label;
        row.appendChild(swatch);
        row.appendChild(label);
        container.appendChild(row);
      });
    }

    function popupText(feature) {
      const props = feature.properties || {};
      const coords = feature.geometry.coordinates || [];
      return [
        'File: ' + (props.file_name || ''),
        'Index: ' + props.index,
        'Value: ' + (props.display_value || props.value_label),
        'Lon: ' + formatNumber(coords[0]),
        'Lat: ' + formatNumber(coords[1])
      ].join('<br>');
    }

    function formatDistance(meters) {
      if (!Number.isFinite(meters)) return '';
      return meters >= 1000 ? (meters / 1000).toFixed(3) + ' km' : meters.toFixed(1) + ' m';
    }

    function setMeasureActive(active) {
      measureActive = active;
      const button = document.getElementById('measureButton');
      button.classList.toggle('active', measureActive);
      button.textContent = measureActive ? 'Measuring...' : 'Measure Distance';
      if (measureActive) {
        clearMeasurement(false);
        document.getElementById('measureStatus').textContent = 'Measurement: click two points or map locations.';
      } else if (!measurementLine) {
        document.getElementById('measureStatus').textContent = 'Measurement: inactive';
      }
    }

    function clearMeasurement(resetStatus = true) {
      if (measurementLine) {
        map.removeLayer(measurementLine);
        measurementLine = null;
      }
      measurementMarkers.forEach(function(marker) { map.removeLayer(marker); });
      measurementMarkers = [];
      measurePoints = [];
      if (resetStatus) document.getElementById('measureStatus').textContent = 'Measurement: inactive';
    }

    function addMeasurePoint(latlng) {
      if (!measureActive) return;
      if (measurePoints.length >= 2) clearMeasurement(false);
      measurePoints.push(latlng);
      const marker = L.circleMarker(latlng, {
        radius: 5,
        color: '#ffffff',
        weight: 2,
        fillColor: '#d62728',
        fillOpacity: 1
      }).addTo(map);
      measurementMarkers.push(marker);
      if (measurePoints.length === 1) {
        document.getElementById('measureStatus').textContent = 'Measurement: first point set. Click a second point.';
        return;
      }
      const distance = map.distance(measurePoints[0], measurePoints[1]);
      measurementLine = L.polyline(measurePoints, { color: '#d62728', weight: 3, opacity: 0.95 }).addTo(map);
      measurementLine.bindTooltip(formatDistance(distance), { permanent: true, direction: 'center' }).openTooltip();
      document.getElementById('measureStatus').textContent = 'Measurement: ' + formatDistance(distance);
      setMeasureActive(false);
    }

    function addNamedOverlay(name, layer, visible) {
      overlayLayers[name] = layer;
      layerControl.addOverlay(layer, name);
      if (visible) layer.addTo(map);
    }

    function addEeTileLayer(layerPayload, visible = true) {
      if (!layerPayload || !layerPayload.tile_url) return null;
      const layer = L.tileLayer(layerPayload.tile_url, {
        opacity: layerPayload.opacity || 0.78,
        attribution: layerPayload.attribution || 'Google Earth Engine',
        maxZoom: 19
      });
      const name = layerPayload.name || 'Earth Engine reference imagery';
      addNamedOverlay(name, layer, visible);
      return layer;
    }

    async function postJson(path, payload) {
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || 'Request failed.');
      }
      return data;
    }

    function checkedSources() {
      return Array.from(document.querySelectorAll('.sourceCheck'))
        .filter(function(item) { return item.checked; })
        .map(function(item) { return item.value; });
    }

    function setAllSources() {
      document.querySelectorAll('.sourceCheck').forEach(function(item) { item.checked = true; });
    }

    function setReferenceDatesFromSwot() {
      if (!pointPayload || !pointPayload.swot_reference_date) {
        document.getElementById('eeStatus').textContent = 'Could not infer a SWOT acquisition date from the PIXC filename.';
        return;
      }
      document.getElementById('eeStartDate').value = pointPayload.swot_reference_date;
      document.getElementById('eeEndDate').value = pointPayload.swot_reference_date;
      const warnings = pointPayload.swot_reference_date_warnings || [];
      document.getElementById('eeStatus').textContent = warnings.length ? warnings.join(' ') : 'Using SWOT acquisition date.';
    }

    function sourceLabel(source) {
      if (!referenceOptions) return source;
      const row = (referenceOptions.sources || []).find(function(item) { return item.key === source; });
      return row ? row.label : source;
    }

    function makePresetSelect(source) {
      const select = document.createElement('select');
      const presets = referenceOptions && referenceOptions.band_presets
        ? (referenceOptions.band_presets[source] || [])
        : [];
      presets.forEach(function(preset) {
        const option = document.createElement('option');
        option.value = preset.key;
        option.textContent = preset.name;
        select.appendChild(option);
      });
      return select;
    }

    function renderReferenceResults(images, warnings) {
      const container = document.getElementById('referenceResults');
      container.innerHTML = '';
      if (!images.length) {
        const empty = document.createElement('div');
        empty.className = 'eeStatus';
        empty.textContent = warnings && warnings.length ? warnings.join(' ') : 'No images found.';
        container.appendChild(empty);
        return;
      }
      images.forEach(function(record) {
        const card = document.createElement('div');
        card.className = 'resultCard';
        const title = document.createElement('div');
        title.className = 'resultTitle';
        title.textContent = record.title || record.image_id;
        const meta = document.createElement('div');
        meta.className = 'resultMeta';
        const parts = [sourceLabel(record.source), record.date || '', record.cloud ? 'cloud ' + record.cloud + '%' : '', record.orbit || '', record.polarizations || '']
          .filter(Boolean);
        meta.textContent = parts.join(' | ');
        const actions = document.createElement('div');
        actions.className = 'resultActions';
        const presetSelect = makePresetSelect(record.source);
        const addButton = document.createElement('button');
        addButton.type = 'button';
        addButton.textContent = 'Add Layer';
        addButton.addEventListener('click', function() {
          addReferenceLayer(record, presetSelect.value, addButton);
        });
        actions.appendChild(presetSelect);
        actions.appendChild(addButton);
        card.appendChild(title);
        card.appendChild(meta);
        card.appendChild(actions);
        container.appendChild(card);
      });
    }

    async function searchReferenceImages() {
      const button = document.getElementById('searchReferenceButton');
      const startDate = document.getElementById('eeStartDate').value;
      const endDate = document.getElementById('eeEndDate').value;
      const sources = checkedSources();
      if (!startDate || !endDate) {
        document.getElementById('eeStatus').textContent = 'Choose start and end dates.';
        return;
      }
      if (!sources.length) {
        document.getElementById('eeStatus').textContent = 'Select at least one imagery source.';
        return;
      }
      button.disabled = true;
      document.getElementById('eeStatus').textContent = 'Searching Earth Engine images intersecting the PIXC bbox...';
      try {
        const data = await postJson('/reference_search', {
          start_date: startDate,
          end_date: endDate,
          sources: sources,
          ee_project: document.getElementById('eeProject').value,
          max_images: document.getElementById('eeMaxImages').value
        });
        renderReferenceResults(data.images || [], data.warnings || []);
        const warningText = (data.warnings || []).length ? ' Warnings: ' + data.warnings.join(' ') : '';
        document.getElementById('eeStatus').textContent =
          'Found ' + (data.images || []).length + ' image(s).' + warningText;
      } catch (error) {
        document.getElementById('eeStatus').textContent = error.message;
      } finally {
        button.disabled = false;
      }
    }

    async function addReferenceLayer(record, bandPreset, button) {
      button.disabled = true;
      const layerName = sourceLabel(record.source) + ' | ' + (record.date || '') + ' | ' + fileName(record.image_id);
      document.getElementById('eeStatus').textContent = 'Building Earth Engine tile layer...';
      try {
        const data = await postJson('/reference_layer', {
          source: record.source,
          image_id: record.image_id,
          band_preset: bandPreset,
          ee_project: document.getElementById('eeProject').value,
          layer_name: layerName
        });
        const key = 'ee_' + (++eeLayerCounter);
        eeDynamicLayers[key] = addEeTileLayer(data.layer, true);
        document.getElementById('eeStatus').textContent = 'Layer added: ' + (data.layer.name || layerName);
      } catch (error) {
        document.getElementById('eeStatus').textContent = error.message;
      } finally {
        button.disabled = false;
      }
    }

    function clearEeLayers() {
      Object.keys(eeDynamicLayers).forEach(function(key) {
        const layer = eeDynamicLayers[key];
        if (layer) {
          map.removeLayer(layer);
          layerControl.removeLayer(layer);
        }
        delete eeDynamicLayers[key];
      });
      document.getElementById('eeStatus').textContent = 'Earth Engine layers cleared.';
    }

    function drawReferenceBbox(payload) {
      const bbox = payload.reference_bbox || payload.bbox;
      if (!bbox) return;
      const rectangle = L.rectangle([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], {
        color: '#ff9f1c',
        weight: 2,
        dashArray: '6 4',
        fillOpacity: 0
      });
      addNamedOverlay('EE search AOI', rectangle, false);
    }

    async function loadReferenceOptions() {
      const response = await fetch('/reference_options.json');
      const data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || 'Reference options unavailable.');
      referenceOptions = data;
    }

    async function loadPoints() {
      try {
        const response = await fetch('/points.json');
        const payload = await response.json();
        pointPayload = payload;
        const layers = (payload.layers && payload.layers.length)
          ? payload.layers
          : [{
              layer_id: 'file_1',
              file_path: payload.file_path,
              file_name: fileName(payload.file_path),
              total_points: payload.total_points,
              valid_points: payload.valid_points,
              rendered_points: payload.rendered_points,
              sampled: payload.sampled,
              bbox: payload.bbox,
              reference_bbox: payload.reference_bbox,
              features: payload.features || []
            }];
        const features = payload.features || [];
        document.getElementById('title').textContent =
          (layers.length === 1 ? layers[0].file_name : layers.length + ' PIXC files') + ' - ' + payload.attribute_path;
        document.getElementById('meta').textContent =
          payload.rendered_points + ' rendered from ' + payload.valid_points + ' valid point(s)'
          + (payload.sampled ? ' (sampled)' : '')
          + ' | color mode: ' + payload.color_mode;
        renderLegend(payload.legend || []);
        const activeLayers = [];
        layers.forEach(function(layerPayload) {
          const layer = L.geoJSON({ type: 'FeatureCollection', features: layerPayload.features || [] }, {
            pointToLayer: function(feature, latlng) {
              const color = (feature.properties && feature.properties.color) || '#184a8b';
              return L.circleMarker(latlng, {
                renderer: pointRenderer,
                radius: 3,
                stroke: false,
                fillColor: color,
                fillOpacity: 0.82
              });
            },
            onEachFeature: function(feature, layer) {
              layer.bindPopup(popupText(feature));
              layer.on('click', function(event) {
                if (measureActive) {
                  addMeasurePoint(event.latlng);
                  map.closePopup();
                  L.DomEvent.stopPropagation(event);
                }
              });
            }
          }).addTo(map);
          const layerName = (layerPayload.file_name || fileName(layerPayload.file_path)) + ' (' + (layerPayload.rendered_points || 0) + ')';
          addNamedOverlay(layerName, layer, true);
          activeLayers.push(layer);
        });
        drawReferenceBbox(payload);
        (payload.reference_layers || []).forEach(function(layerPayload) {
          addEeTileLayer(layerPayload, Boolean(layerPayload.default_visible));
        });
        if (payload.bbox) {
          map.fitBounds([[payload.bbox[1], payload.bbox[0]], [payload.bbox[3], payload.bbox[2]]], { padding: [24, 24] });
        } else if (features.length) {
          const group = L.featureGroup(activeLayers);
          map.fitBounds(group.getBounds(), { padding: [24, 24] });
        } else {
          map.setView([0, 0], 2);
        }
        document.getElementById('status').textContent =
          'Use the top-right layer control for basemaps, files, the EE search AOI, and reference imagery.';
        try {
          await loadReferenceOptions();
          setReferenceDatesFromSwot();
        } catch (error) {
          document.getElementById('eeStatus').textContent = error.message;
        }
      } catch (error) {
        document.getElementById('meta').textContent = 'Point loading failed.';
        document.getElementById('status').textContent = error.message;
        map.setView([0, 0], 2);
      }
    }

    document.getElementById('measureButton').addEventListener('click', function() {
      setMeasureActive(!measureActive);
    });
    document.getElementById('clearMeasureButton').addEventListener('click', function() {
      setMeasureActive(false);
      clearMeasurement(true);
    });
    document.getElementById('useSwotDateButton').addEventListener('click', setReferenceDatesFromSwot);
    document.getElementById('allSourcesButton').addEventListener('click', setAllSources);
    document.getElementById('searchReferenceButton').addEventListener('click', searchReferenceImages);
    document.getElementById('clearEeLayersButton').addEventListener('click', clearEeLayers);
    map.on('click', function(event) { addMeasurePoint(event.latlng); });

    loadPoints();
  </script>
</body>
</html>
"""
