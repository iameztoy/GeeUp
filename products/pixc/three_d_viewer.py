"""Browser-based PIXC 3D attribute-space viewer."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .visualize import Pixc3DViewConfig, Pixc3DViewData, build_pixc_3d_view


@dataclass
class ThreeDViewerSession:
    """Serve one PIXC 3D view in a local browser."""

    view_data: Pixc3DViewData
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """Return the local 3D viewer URL."""
        if self._server is None:
            raise RuntimeError("3D viewer session has not been started.")
        host, port = self._server.server_address
        return f"http://{host}:{port}/"

    def start(self) -> str:
        """Start the local 3D viewer server and return its URL."""
        if self._server is not None:
            return self.url
        handler = make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        """Stop the local 3D viewer server."""
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def render_html(self) -> str:
        """Render the 3D viewer HTML page."""
        return THREE_D_VIEWER_HTML

    def data_payload(self) -> dict[str, object]:
        """Return browser-ready 3D point data."""
        return self.view_data.to_dict()


def make_handler(session: ThreeDViewerSession) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one 3D viewer session."""

    class ThreeDViewerHandler(BaseHTTPRequestHandler):
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
            if parsed.path == "/data.json":
                self.send_json(session.data_payload())
                return
            self.send_error(404, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/rebuild":
                self.handle_rebuild()
                return
            self.send_json({"ok": False, "error": "not found"}, status=404)

        def handle_rebuild(self) -> None:
            try:
                payload = self.read_json_body()
                config = Pixc3DViewConfig(
                    file_path=Path(session.view_data.file_path),
                    x_path=str(payload.get("x_path", "") or session.view_data.x_path),
                    y_path=str(payload.get("y_path", "") or session.view_data.y_path),
                    z_path=str(payload.get("z_path", "") or session.view_data.z_path),
                    color_attribute_path=str(
                        payload.get("color_attribute_path", "") or session.view_data.color_attribute_path
                    ),
                    max_points=int(payload.get("max_points", session.view_data.max_points) or session.view_data.max_points),
                    sampling_mode=str(payload.get("sampling_mode", "") or session.view_data.sampling_mode),
                )
                session.view_data = build_pixc_3d_view(config)
                self.send_json({"ok": True, **session.data_payload()})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)

        def read_json_body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            data = self.rfile.read(length) if length else b"{}"
            value = json.loads(data.decode("utf-8") or "{}")
            if not isinstance(value, dict):
                raise ValueError("JSON object body is required.")
            return value

        def send_json(self, payload: dict[str, object], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreeDViewerHandler


THREE_D_VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SWOTFlow PIXC 3D Viewer</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    html, body { height: 100%; margin: 0; }
    body {
      font-family: Segoe UI, Arial, sans-serif;
      color: #152033;
      background: #f6f8fb;
      overflow: hidden;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(220px, 310px) 1fr minmax(210px, 280px);
      grid-template-rows: 1fr;
      height: 100vh;
      min-width: 0;
    }
    .side, .query {
      background: #ffffff;
      border-right: 1px solid #d3dae6;
      box-sizing: border-box;
      overflow-y: auto;
      padding: 12px;
    }
    .query { border-left: 1px solid #d3dae6; border-right: 0; }
    .main {
      display: grid;
      grid-template-rows: 1fr 210px;
      min-width: 0;
      min-height: 0;
    }
    #plot3d, .projection { min-width: 0; min-height: 0; }
    #plot3d { background: #ffffff; }
    .projections {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 1px;
      border-top: 1px solid #d3dae6;
      background: #d3dae6;
      min-height: 0;
    }
    .projection { background: #ffffff; }
    h1 {
      font-size: 16px;
      line-height: 1.25;
      margin: 0 0 8px;
    }
    h2 {
      font-size: 13px;
      margin: 16px 0 8px;
    }
    .meta, .row, .empty {
      color: #3b4a5f;
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .row { margin: 5px 0; }
    .label { color: #647289; }
    .legend {
      display: grid;
      gap: 6px;
      margin-top: 8px;
    }
    .tools {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 10px 0 4px;
    }
    button.toolButton {
      border: 1px solid #aab5c6;
      background: #f7f9fc;
      color: #142033;
      border-radius: 4px;
      cursor: pointer;
      font: 12px Segoe UI, Arial, sans-serif;
      padding: 5px 8px;
    }
    button.toolButton:hover {
      background: #eef3fa;
    }
    .legendItem {
      display: grid;
      grid-template-columns: 18px 14px 1fr;
      gap: 7px;
      align-items: center;
      font-size: 12px;
      color: #26364d;
    }
    .controlGrid {
      display: grid;
      grid-template-columns: 44px 1fr;
      gap: 6px 8px;
      align-items: center;
      margin: 8px 0 4px;
    }
    .controlGrid label {
      color: #647289;
      font-size: 12px;
    }
    .controlGrid select {
      min-width: 0;
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #aab5c6;
      border-radius: 4px;
      padding: 4px 6px;
      font: 12px Segoe UI, Arial, sans-serif;
      background: #ffffff;
    }
    .swatch {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      border: 1px solid rgba(0,0,0,0.25);
    }
    .hint {
      margin-top: 10px;
      border-top: 1px solid #e0e5ee;
      padding-top: 10px;
      color: #5a687c;
      font-size: 12px;
      line-height: 1.4;
    }
    @media (max-width: 980px) {
      body { overflow: auto; }
      .layout { grid-template-columns: 1fr; grid-template-rows: auto 80vh auto; height: auto; }
      .side, .query { border: 0; border-bottom: 1px solid #d3dae6; }
      .main { min-height: 80vh; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside class="side">
      <h1 id="title">PIXC 3D Viewer</h1>
      <div class="meta" id="meta">Loading points...</div>
      <div class="tools">
        <button type="button" class="toolButton" id="resetViewButton">Reset View</button>
        <button type="button" class="toolButton" id="showAllValuesButton">Show All Values</button>
        <button type="button" class="toolButton" id="hideAllValuesButton">Hide All Values</button>
      </div>
      <h2>Variables</h2>
      <div class="controlGrid">
        <label for="xSelect">X</label>
        <select id="xSelect"></select>
        <label for="ySelect">Y</label>
        <select id="ySelect"></select>
        <label for="zSelect">Z</label>
        <select id="zSelect"></select>
        <label for="colorSelect">Color</label>
        <select id="colorSelect"></select>
      </div>
      <div class="tools">
        <button type="button" class="toolButton" id="applyVariablesButton">Apply Variables</button>
      </div>
      <div class="meta" id="viewerStatus"></div>
      <h2>Axes</h2>
      <div class="row"><span class="label">X</span> <span id="xAxis"></span></div>
      <div class="row"><span class="label">Y</span> <span id="yAxis"></span></div>
      <div class="row"><span class="label">Z</span> <span id="zAxis"></span></div>
      <div class="row"><span class="label">Color</span> <span id="colorAxis"></span></div>
      <h2>Legend</h2>
      <div class="legend" id="legend"></div>
      <div class="hint">Rotate, pan, zoom, hover, or click any point. The small plots show X/Y, X/Z, and Y/Z projections using the same colors.</div>
    </aside>
    <main class="main">
      <div id="plot3d"></div>
      <div class="projections">
        <div class="projection" id="plotXY"></div>
        <div class="projection" id="plotXZ"></div>
        <div class="projection" id="plotYZ"></div>
      </div>
    </main>
    <aside class="query">
      <h1>Point Query</h1>
      <div id="queryDetails" class="empty">Click a point to inspect its values.</div>
    </aside>
  </div>
  <script>
    const DEFAULT_CAMERA = { eye: { x: 1.65, y: 1.55, z: 1.1 } };
    let currentPayload = null;
    let activeColorKeys = new Set();
    let traceIndexByColorKey = {};
    let pointLookup = new Map();
    let plotsReady = false;
    const PLOT_IDS = ['plot3d', 'plotXY', 'plotXZ', 'plotYZ'];

    function formatValue(value) {
      const number = Number(value);
      return Number.isFinite(number) ? number.toPrecision(7) : String(value);
    }

    function renderLegend(payload) {
      const container = document.getElementById('legend');
      container.innerHTML = '';
      activeColorKeys = new Set();
      (payload.legend || []).forEach(function(item) {
        const row = document.createElement('div');
        row.className = 'legendItem';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = true;
        checkbox.dataset.valueKey = item.value || '';
        if (item.value !== undefined) {
          activeColorKeys.add(String(item.value));
        } else {
          checkbox.disabled = true;
        }
        checkbox.addEventListener('change', function() {
          setColorVisibility(String(item.value || ''), checkbox.checked);
        });
        const swatch = document.createElement('span');
        swatch.className = 'swatch';
        swatch.style.background = item.color || '#1f77b4';
        const label = document.createElement('span');
        label.textContent = item.count !== undefined ? item.label + ' (' + item.count + ')' : item.label;
        row.appendChild(checkbox);
        row.appendChild(swatch);
        row.appendChild(label);
        container.appendChild(row);
      });
    }

    function hasToggleableLegend(payload) {
      return (payload.legend || []).some(function(item) { return item.value !== undefined; });
    }

    function filteredPoints(payload) {
      const points = payload.points || [];
      if (!hasToggleableLegend(payload)) return points;
      return points.filter(function(point) {
        return activeColorKeys.has(String(point.color_key));
      });
    }

    function updateMeta(payload) {
      const points = filteredPoints(payload);
      document.getElementById('meta').textContent =
        points.length + ' shown from ' + payload.rendered_points + ' rendered / ' + payload.valid_points + ' valid point(s)'
        + (payload.sampled ? ' (sampled)' : '') + ' | color mode: ' + payload.color_mode;
      if (payload.sampling_summary) {
        document.getElementById('meta').textContent += ' | ' + payload.sampling_summary;
      }
    }

    function setPlotTraceVisibility(key, visible) {
      const traces = traceIndexByColorKey[String(key)] || {};
      PLOT_IDS.forEach(function(plotId) {
        const indices = traces[plotId] || [];
        if (indices.length) Plotly.restyle(plotId, { visible: visible }, indices);
      });
    }

    function setColorVisibility(key, visible) {
      key = String(key);
      if (!key) return;
      if (visible) activeColorKeys.add(key);
      else activeColorKeys.delete(key);
      if (currentPayload && plotsReady) {
        setPlotTraceVisibility(key, visible);
        updateMeta(currentPayload);
      }
    }

    function setAllColorVisibility(visible) {
      if (!currentPayload || !hasToggleableLegend(currentPayload)) return;
      document.querySelectorAll('#legend input[type="checkbox"]').forEach(function(checkbox) {
        if (checkbox.disabled) return;
        checkbox.checked = visible;
        const key = String(checkbox.dataset.valueKey || '');
        if (visible) activeColorKeys.add(key);
        else activeColorKeys.delete(key);
      });
      if (plotsReady) {
        Object.keys(traceIndexByColorKey).forEach(function(key) {
          setPlotTraceVisibility(key, visible);
        });
        updateMeta(currentPayload);
      }
    }

    function fillVariableSelect(id, payload, selectedPath) {
      const select = document.getElementById(id);
      select.innerHTML = '';
      (payload.variable_options || []).forEach(function(option) {
        const node = document.createElement('option');
        node.value = option.path;
        node.textContent = option.label || option.path;
        if (option.path === selectedPath) node.selected = true;
        select.appendChild(node);
      });
    }

    function populateVariableControls(payload) {
      fillVariableSelect('xSelect', payload, payload.x_path);
      fillVariableSelect('ySelect', payload, payload.y_path);
      fillVariableSelect('zSelect', payload, payload.z_path);
      fillVariableSelect('colorSelect', payload, payload.color_attribute_path);
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

    async function applyVariables() {
      if (!currentPayload) return;
      const button = document.getElementById('applyVariablesButton');
      button.disabled = true;
      document.getElementById('viewerStatus').textContent = 'Rebuilding plot...';
      try {
        const payload = await postJson('/rebuild', {
          x_path: document.getElementById('xSelect').value,
          y_path: document.getElementById('ySelect').value,
          z_path: document.getElementById('zSelect').value,
          color_attribute_path: document.getElementById('colorSelect').value,
          max_points: currentPayload.max_points,
          sampling_mode: currentPayload.sampling_mode
        });
        applyPayload(payload);
        document.getElementById('viewerStatus').textContent = 'Variables updated.';
      } catch (error) {
        document.getElementById('viewerStatus').textContent = error.message;
      } finally {
        button.disabled = false;
      }
    }

    function pointText(payload, point) {
      return [
        'Index: ' + point.index,
        payload.x_label + ': ' + formatValue(point.x),
        payload.y_label + ': ' + formatValue(point.y),
        payload.z_label + ': ' + formatValue(point.z),
        payload.color_label + ': ' + point.display_color_value
      ].join('<br>');
    }

    function showQuery(payload, point) {
      document.getElementById('queryDetails').innerHTML = [
        '<div class="row"><span class="label">Index</span> ' + point.index + '</div>',
        '<div class="row"><span class="label">X</span> ' + formatValue(point.x) + '</div>',
        '<div class="row"><span class="label">Y</span> ' + formatValue(point.y) + '</div>',
        '<div class="row"><span class="label">Z</span> ' + formatValue(point.z) + '</div>',
        '<div class="row"><span class="label">Color value</span> ' + point.display_color_value + '</div>',
        '<div class="row"><span class="label">Color meaning</span> ' + (point.color_meaning || '') + '</div>'
      ].join('');
    }

    function buildMarker(points) {
      return {
        size: 3,
        color: points.map(function(point) { return point.color; }),
        opacity: 0.82,
        line: { width: 0 }
      };
    }

    function traceLabel(group) {
      return group.label + ' (' + group.points.length + ')';
    }

    function pointGroups(payload) {
      const points = payload.points || [];
      if (!hasToggleableLegend(payload)) {
        return [{ key: '__all__', label: payload.color_label || 'Points', points: points }];
      }
      const legendLabels = {};
      (payload.legend || []).forEach(function(item) {
        if (item.value !== undefined) legendLabels[String(item.value)] = item.label || String(item.value);
      });
      const buckets = {};
      points.forEach(function(point) {
        const key = String(point.color_key);
        if (!buckets[key]) buckets[key] = [];
        buckets[key].push(point);
      });
      const orderedKeys = (payload.legend || [])
        .filter(function(item) { return item.value !== undefined && buckets[String(item.value)]; })
        .map(function(item) { return String(item.value); });
      Object.keys(buckets).forEach(function(key) {
        if (!orderedKeys.includes(key)) orderedKeys.push(key);
      });
      return orderedKeys.map(function(key) {
        return {
          key: key,
          label: legendLabels[key] || key,
          points: buckets[key] || []
        };
      });
    }

    function recordTrace(plotId, groupKey, traceIndex) {
      if (!traceIndexByColorKey[groupKey]) traceIndexByColorKey[groupKey] = {};
      if (!traceIndexByColorKey[groupKey][plotId]) traceIndexByColorKey[groupKey][plotId] = [];
      traceIndexByColorKey[groupKey][plotId].push(traceIndex);
    }

    function projectionTrace(payload, group, xKey, yKey, name) {
      return {
        type: 'scattergl',
        mode: 'markers',
        name: name + ' | ' + traceLabel(group),
        x: group.points.map(function(point) { return point[xKey]; }),
        y: group.points.map(function(point) { return point[yKey]; }),
        text: group.points.map(function(point) { return pointText(payload, point); }),
        customdata: group.points.map(function(point) { return point.index; }),
        visible: isColorVisible(group.key),
        hovertemplate: '%{text}<extra></extra>',
        marker: buildMarker(group.points)
      };
    }

    function isColorVisible(key) {
      return key === '__all__' || activeColorKeys.has(String(key));
    }

    function resetPlots() {
      if (!plotsReady) return;
      Plotly.relayout('plot3d', { 'scene.camera': DEFAULT_CAMERA });
      ['plotXY', 'plotXZ', 'plotYZ'].forEach(function(plotId) {
        Plotly.relayout(plotId, { 'xaxis.autorange': true, 'yaxis.autorange': true });
      });
    }

    function renderPlots(payload) {
      const groups = pointGroups(payload);
      traceIndexByColorKey = {};
      pointLookup = new Map();
      (payload.points || []).forEach(function(point) {
        pointLookup.set(String(point.index), point);
      });
      document.getElementById('title').textContent = payload.file_name;
      updateMeta(payload);
      document.getElementById('xAxis').textContent = payload.x_path;
      document.getElementById('yAxis').textContent = payload.y_path;
      document.getElementById('zAxis').textContent = payload.z_path;
      document.getElementById('colorAxis').textContent = payload.color_attribute_path;

      const traces3d = groups.map(function(group, traceIndex) {
        recordTrace('plot3d', group.key, traceIndex);
        return {
          type: 'scatter3d',
          mode: 'markers',
          name: traceLabel(group),
          x: group.points.map(function(point) { return point.x; }),
          y: group.points.map(function(point) { return point.y; }),
          z: group.points.map(function(point) { return point.z; }),
          text: group.points.map(function(point) { return pointText(payload, point); }),
          customdata: group.points.map(function(point) { return point.index; }),
          visible: isColorVisible(group.key),
          hovertemplate: '%{text}<extra></extra>',
          marker: buildMarker(group.points)
        };
      });
      const mainLayout = {
        margin: { l: 0, r: 0, t: 18, b: 0 },
        paper_bgcolor: '#ffffff',
        plot_bgcolor: '#ffffff',
        scene: {
          xaxis: { title: payload.x_label, zeroline: false },
          yaxis: { title: payload.y_label, zeroline: false },
          zaxis: { title: payload.z_label, zeroline: false },
          camera: DEFAULT_CAMERA
        },
        showlegend: false
      };
      const config = { responsive: true, displaylogo: false };
      Plotly.newPlot('plot3d', traces3d, mainLayout, config);
      const xyTraces = groups.map(function(group, traceIndex) {
        recordTrace('plotXY', group.key, traceIndex);
        return projectionTrace(payload, group, 'x', 'y', 'X/Y');
      });
      const xzTraces = groups.map(function(group, traceIndex) {
        recordTrace('plotXZ', group.key, traceIndex);
        return projectionTrace(payload, group, 'x', 'z', 'X/Z');
      });
      const yzTraces = groups.map(function(group, traceIndex) {
        recordTrace('plotYZ', group.key, traceIndex);
        return projectionTrace(payload, group, 'y', 'z', 'Y/Z');
      });
      Plotly.newPlot('plotXY', xyTraces, {
        margin: { l: 45, r: 10, t: 18, b: 42 },
        xaxis: { title: payload.x_label, zeroline: false },
        yaxis: { title: payload.y_label, zeroline: false },
        showlegend: false
      }, config);
      Plotly.newPlot('plotXZ', xzTraces, {
        margin: { l: 45, r: 10, t: 18, b: 42 },
        xaxis: { title: payload.x_label, zeroline: false },
        yaxis: { title: payload.z_label, zeroline: false },
        showlegend: false
      }, config);
      Plotly.newPlot('plotYZ', yzTraces, {
        margin: { l: 45, r: 10, t: 18, b: 42 },
        xaxis: { title: payload.y_label, zeroline: false },
        yaxis: { title: payload.z_label, zeroline: false },
        showlegend: false
      }, config);
      document.getElementById('plot3d').on('plotly_click', function(event) {
        const pointId = event.points && event.points.length ? event.points[0].customdata : '';
        const point = pointLookup.get(String(pointId));
        if (point) showQuery(payload, point);
      });
      plotsReady = true;
    }

    function applyPayload(payload) {
      currentPayload = payload;
      renderLegend(payload);
      populateVariableControls(payload);
      renderPlots(payload);
      document.getElementById('queryDetails').textContent = 'Click a point to inspect its values.';
      document.getElementById('queryDetails').className = 'empty';
    }

    async function loadData() {
      const response = await fetch('/data.json');
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Could not load 3D payload.');
      applyPayload(payload);
    }

    document.getElementById('resetViewButton').addEventListener('click', resetPlots);
    document.getElementById('showAllValuesButton').addEventListener('click', function() {
      setAllColorVisibility(true);
    });
    document.getElementById('hideAllValuesButton').addEventListener('click', function() {
      setAllColorVisibility(false);
    });
    document.getElementById('applyVariablesButton').addEventListener('click', applyVariables);

    loadData().catch(function(error) {
      document.getElementById('meta').textContent = error.message;
    });
  </script>
</body>
</html>
"""
