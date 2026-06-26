# SWOTFlow PIXC Troubleshooting

The PIXC workflow is still early. These notes capture expected first issues.

## PIXC Button Opens The Early Shell

The current PIXC workflow includes project mode, safe preview/download, sampled
point visualization, variable details, and attribute-value filtering. Batch
exports are still planned work.

## NetCDF Dependencies Are Missing

The PIXC shell does not require NetCDF dependencies just to open. Real file
point visualization, attribute-value filtering, and Earth Engine reference
imagery require:

```powershell
python -m pip install -r requirements-pixc.txt
```

## Earth Engine Reference Imagery Is Missing

The point map can still open when Earth Engine imagery fails. Check the
**Earth Engine Reference** status text inside the browser viewer and
`<project>\00_logs\pixc_reference_imagery.csv`.

Common causes:

- `earthengine-api` is not installed in the active Python environment.
- Earth Engine is not authenticated. Run `earthengine authenticate`.
- The selected account needs an Earth Engine project id in the **EE project**
  field inside the browser viewer.
- No Sentinel/Landsat image exists inside the chosen date window for the point
  full PIXC coordinate bbox.

## Earthdata Login During Download

`Download Selected Files` requires Earthdata authentication through
`earthaccess`, using the same standard login strategy as the HR Raster
downloader. If Earthdata asks for credentials, enter them in the terminal window.

On Windows, a reusable option is `%USERPROFILE%\_netrc`, for example
`C:\Users\ibana\_netrc`. If interactive login fails and no valid credential
source is available, the PIXC downloader will stop before transferring data.

The failed attempt is still written to the active PIXC project:

- `<project>\00_logs\pixc_download_preview.csv`
- `<project>\00_logs\pixc_download_manifest.csv`
- `<project>\00_logs\pixc_download_events.csv`

## Keep Test Projects Separate

If PIXC testing appears to affect raster project statistics or cleanup, verify
that the PIXC workflow is using a separate project root. Raster and PIXC
experiments should not share the same `swotflow.sqlite3` during early
development.
