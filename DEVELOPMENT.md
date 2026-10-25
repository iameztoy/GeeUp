# Application source and local investigations

## Package layout and entry points

`swotflow_gui.py` is the small top-level GUI launcher. The implementation lives
in `swotflow/`, grouped by responsibility:

```text
swotflow/
  catalog/        NASA catalogue history and its interface
  consolidation/ EE mosaic planning, updates, replacement register and panels
  core/           filename metadata, storage, GDAL runtime and power helpers
  earthengine/    browser uploader and selectors
  processing/     download, duplicate removal, extraction, mosaicking, Automation
  project/        project state, database, updates, statistics, relocation, cleanup
  tools/          optional maintenance and preset/asset builders
  ui/             main window and UTM map widgets
  verification/   download receipts, provenance and independent pixel checks
  paths.py        application-resource paths and child-process commands
  version.py      shared application version
```

Launch from the checkout with `python swotflow_gui.py` or `python -m swotflow`.
Run individual workers as modules, for example:

```powershell
python -m swotflow.processing.download --config config.yaml --dry-run
.\.conda\swot_gdal\python.exe -m swotflow.processing.extract --check-gdal
python -m swotflow.earthengine.uploader --config config.yaml --dry-run
```

The former standalone worker file paths and flat Python imports have moved.
Update custom command lines/imports to their package locations; no duplicate
implementation copies or legacy worker stubs remain at the top level. The GUI
launch command, project formats, configuration locations and processing rules
remain compatible. Package imports do not run workers automatically.

Use `swotflow.paths.APP_ROOT` for bundled assets, presets and application files.
Use `script_command()` with `cwd=APP_ROOT` for Python/GDAL subprocesses so they
resolve the package even when the user's project is on another drive. Scientific
input/output paths continue to come from the project configuration.

For an investigation's frozen code snapshot, preserve the complete `swotflow/`
package and launcher, not just top-level Python files. Historical investigations
retain their own earlier snapshots and are not automatically migrated or rerun.

## Source and investigation separation

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

Create investigation scripts and their outputs directly in an external folder
(for example, a sibling `GeeUp-investigations/` directory), with a separate folder
for each pilot. Do not use the application checkout as temporary investigation
storage. Keep real project folders on the user's selected data drive or another
external location. Maintained application regression tests stay in `tests/` and
use isolated temporary fixtures.

`outputs/` and known investigation filenames are ignored as a second safeguard.
A local compatibility junction may preserve old report addresses after moving
files; it must remain ignored and must not be included in release archives.
Use the external paths directly for all new work; the junction is only for old
evidence references and does not store a second copy of the investigation data.
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

For v0.8.0, `environments/requirements-validated.txt` and
`environments/swot-gdal-validated.yml` pin the direct dependencies/core libraries
used for Windows validation. They are optional reproducibility references, not
full transitive lockfiles. NumPy is explicitly required by native pixel checks in
the GUI environment; GDAL retains its separate NumPy installation. Do not update
either environment while a worker is running.

Before publication, inspect the exact staged file list and test the application
from those files. Stage intended source files explicitly. Generated data and
private paths must not enter the release. Publish source archives from Git's
tracked files, never by zipping the working directory.

Keep `APP_VERSION`, README and the changelog consistent. Use all three version
components. **Every GitHub release title and release description must explicitly
contain its version**, for example `SWOTFlow v0.6.1`. Release notes describe the
application changes, validation and limitations; they do not attach investigation
datasets or reports. Do not restart a running update to display a new version.
