# Application source and local investigations

The repository contains shipped application modules, generic documentation,
configuration templates, bundled UI assets and automated regression tests.
Keep these regression tests in GitHub: they verify supported application behaviour
using isolated fixtures and mocked services.

One-off audits, pilots, investigation scripts, downloaded source data, private
inventories, generated maps, reports and test outputs are **not** application
source. Keep them in a separate directory outside this checkout. Do not commit
them, attach them to GitHub releases, or force-add ignored files. The local
investigation directory may include frozen application source copies for replay;
those copies are evidence, not the development version.

`outputs/` and known investigation filenames are ignored as a second safeguard.
A local compatibility junction may preserve old report addresses after moving
files; it must remain ignored and must not be included in release archives.
Never recursively clean a junction's contents to remove the junction itself.

## Durable application data

Project data, manifests and the NASA filename ledger stay in the selected project.
In particular, keep and back up `swotflow.sqlite3` and
`00_logs/nasa_catalog_history.sqlite3` after cleaning downloaded rasters.

New EE consolidation histories use:

- Windows: `%LOCALAPPDATA%/SWOTFlow/ee_consolidation/`.
- macOS: `~/Library/Application Support/SWOTFlow/ee_consolidation/`.
- Linux: `${XDG_DATA_HOME:-~/.local/share}/SWOTFlow/ee_consolidation/`.

Plans, export journals, source-index mappings and `registers/` are durable
application records. Back them up; they are not disposable test outputs.
`SWOTFLOW_DATA_HOME` can specify an absolute alternative data directory; the
application uses its `ee_consolidation/` child. Set it before launching the app.

For compatibility, when no override is set and an existing
`outputs/ee_consolidation/` folder is present, the app continues using that history.
It does not silently move, merge or abandon existing registers. To relocate it,
close the app, copy the complete folder (including registers and evidence) beneath
the chosen data home, verify the copy, then set `SWOTFLOW_DATA_HOME`. Retain the
old copy until saved plans and coverage are confirmed. Opening the GUI or reading
Statistics does not create these folders or databases.

## Regression checks and releases

Run the maintained suite from the application checkout:

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Use the configured GDAL environment for GDAL-dependent checks. Tests use temporary
folders; no real project or cloud changes are required. Investigation workers are
run separately and are never started as part of the application regression suite.

Before publication, inspect the exact staged file list and test the application
from those files. Stage intended source files explicitly. Generated data and
private paths must not enter the release. Publish source archives from Git's
tracked files, never by zipping the working directory.

Keep `APP_VERSION`, README and the changelog consistent. Use all three version
components. **Every GitHub release title and release description must explicitly
contain its version**, for example `SWOTFlow v0.6.1`. Release notes describe the
application changes, validation and limitations; they do not attach investigation
datasets or reports. Do not restart a running update to display a new version.
