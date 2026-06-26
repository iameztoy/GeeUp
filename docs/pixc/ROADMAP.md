# SWOTFlow PIXC Roadmap

This roadmap records the staged PIXC development plan so spatial-selection and
download decisions are tracked in the repository.

## Stage 1: Single-File Inspection

Status: implemented as the first local-inspection version.

Current scope:

- Open the PIXC workflow from the product selector.
- Inspect one local PIXC NetCDF file.
- Summarize groups, dimensions, variables, key value counts, coordinate ranges,
  height ranges, and missing/fill values.
- Write inspection JSON/CSV reports.

## Stage 2: CMR Preview And Safe Download

Status: implemented as a cautious V1; future refinements remain expected.

Current scope:

- Add a PIXC download backend using CMR/Earthdata.
- Search `SWOT_L2_HR_PIXC_D` by date and spatial filter.
- Support manual bounding box input:

```text
west longitude
south latitude
east longitude
north latitude
```

- Always preview before download.
- Show matched granule count and known size when available.
- Add `max_granules`, skip-existing, manifest/report CSVs, and download limits.
- Avoid one-click broad downloads over large date ranges or large AOIs.

V1 notes:

- PIXC download state is standalone CSV under the active PIXC project folder.
- UTM tile selection is a single-tile convenience that converts to an
  approximate WGS84 bounding box.
- The HR Raster project database and Earth Engine upload path are not used by
  PIXC downloads.

## Stage 2.5: PIXC Project Mode And Date Picker

Status: implemented as a PIXC-specific YAML/CSV project mode.

Current scope:

- Create, open, save, and save-as PIXC projects from the Home tab.
- Store `product_family: pixc` in each `project.yaml`.
- Default new project browsing to `D:\SWOTFlow_Projects`.
- Keep download files, preview CSV, manifest CSV, and inspection reports inside
  the active project folder.
- Add calendar buttons that write dates as `YYYY-MM-DD`.
- Use a direct year selector, month selector, and day grid so users can jump to
  older SWOT dates without clicking month-by-month.
- Keep manual `YYYY-MM-DD` and ISO datetime entry valid.

Deferred:

- SQLite indexing for PIXC project records.
- Named AOI presets and reusable PIXC filter profiles.

## Stage 3: Browser AOI Picker

Status: implemented as a V1 rectangle-to-bbox picker.

Current scope:

- Open an external browser-based AOI picker from the Tkinter PIXC workflow.
- Use Leaflet with Leaflet.draw for rectangle drawing, and polygon drawing when
  selecting SWOT reference tiles.
- Return the selected rectangle bbox to the PIXC Download tab through a local
  `127.0.0.1` helper server.
- Use CMR bounding-box search first.

Rationale:

- Keeps the Tkinter app lightweight.
- Avoids embedding a web runtime inside the desktop app too early.
- Gives users a precise visual way to select the download area.

Deferred:

- Polygon drawing and direct CMR polygon search.
- Persisting recent AOIs as named project presets.

## Stage 4: Basemap Switching And Satellite Imagery

Status: implemented as V1 basemap switching in the browser AOI picker.

Current scope:

- Satellite imagery layer using Esri World Imagery.
- OpenStreetMap reference layer.
- Basemap switcher so users can move between imagery and reference views.

Google Satellite:

- Do not use unofficial Google tile URLs.
- Add Google Satellite only as an optional provider through the official Google
  Maps JavaScript API.
- Require a user-supplied API key and clear documentation about Google API
  billing and terms.

## Stage 4.5: SWOT Track/Tile Filters And Preview Footprint Selection

Status: implemented as a V1 metadata and preview-selection workflow.

Current scope:

- Parse official-style PIXC filenames into cycle, pass, along-track tile, and
  swath side fields.
- Add optional Download-tab fields for `Cycle`, `Pass`, and comma-separated
  `Tile(s)` such as `102R` or `001L`.
- Send CMR cycle/pass/tile query parameters when provided.
- Enforce CMR's practical constraints: pass filters require one cycle, and tile
  filters require one cycle plus one pass.
- Preserve unparsed granules rather than dropping them when local filename
  parsing cannot safely identify track metadata.
- Add cycle, pass, tile, side, and footprint fields to PIXC preview/download
  CSV reports.
- After preview, open available CMR granule footprints on the AOI map and allow
  users to click one or more granules for download.

## Stage 4.75: SWOT Reference Tile Grid

Status: implemented as a V1 compact reference-tile workflow.

Current scope:

- Generate compact runtime caches from the provided `refs.zip` shapefiles under
  `spatial_presets`.
- Use `Tiles_poly` as the main PIXC spatial reference layer with 90,249
  pass/tile/side polygons.
- Add a **SWOT reference tile(s)** spatial mode in the PIXC Download tab.
- Open the browser AOI map, draw an AOI, load only intersecting SWOT tiles, and
  click tiles to select/unselect them.
- Persist selected reference tile names such as `001_164L` in PIXC
  `project.yaml`.
- If `Cycle` is set, selected reference tiles are converted into exact CMR
  cycle/pass/tile query parameters grouped by pass.
- If `Cycle` is blank, date and spatial search still run first, then parsed PIXC
  filenames are filtered locally by selected pass/tile/side.
- Handle antimeridian tiles in the runtime spatial index instead of using raw
  world-spanning shapefile bboxes.

Notes:

- `Passes_polL` is generated as an optional compact pass-line cache for future
  visual context only.
- CMR's generic `/tiles` endpoint is not used because it is documented for MODIS
  sinusoidal tiles, not SWOT PIXC pass/tile geometry.

## Stage 4.9: Single-File Point Visualization

Status: implemented as a V1 sampled browser-map viewer with attribute-value filtering.

Current scope:

- Add a **Visualize Points** tab with downloaded-file selection, variable
  details, attribute-value filtering, and sampled map display.
- Discover NetCDF variables, detect latitude/longitude variables, and choose a
  useful default attribute such as classification when available.
- List distinct values for a selected attribute when the variable is suitable
  for value filtering, then use selected values to filter the mapped points.
- Open sampled PIXC points from one or more files in a browser map with
  satellite imagery as the default basemap and OpenStreetMap as an alternate
  layer.
- Expose each file as a browser overlay layer so users can load/unload files.
- Add browser-side **Earth Engine Reference** controls for dated Sentinel-2,
  Sentinel-1, Landsat 8, and Landsat 9 imagery. Searches use the full valid
  PIXC coordinate bbox and selected images can be added with different band
  combinations.
- Color points by the selected attribute using categorical colors or a
  continuous ramp, using NetCDF flag metadata and PIXC classification fallback
  labels when available.
- Keep a configurable max-point-per-file cap so large PIXC files do not overload
  the browser.

Deferred:

- Exporting filtered point outputs.
- Chunked/vector-tile rendering for very large point sets.

## Stage 5: Batch Inspection And Export Planning

Planned after download and AOI selection.

Likely scope:

- Inspect batches of downloaded PIXC files.
- Compare variable availability and quality distributions across files.
- Define high-quality water-point filters from observed data.
- Export selected points to agreed formats such as CSV, GeoPackage,
  GeoParquet/Parquet, or small GeoJSON samples.
- Defer rasterized GeoTIFF derivatives until filtering rules are validated.
