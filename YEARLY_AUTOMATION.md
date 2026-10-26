# Calendar-year Automation

The optional **Process each tile year by year** setting limits each pass
through Automation to one tile and one calendar year. It is off by default. Leaving it off
preserves the existing workflow.

## Enable the option

1. Open a project and configure its download, extraction, mosaic and upload
   settings in the corresponding tabs. Check their input and output folders.
2. In **Automation**, select the full date range and UTM tiles. Enable
   **Process each tile year by year**.
3. Choose whether to upload and whether to enable cleanup. Set the minimum free
   space reserve for the output volumes.
4. Run **Preflight** and review the expected inputs and warnings. Authenticate
   Earthdata when downloads are needed, then start Automation.

The setting is saved with the project as `automation.process_by_year: true`.
Changing the scheduling option requires a new preflight. Configure the complete
run before starting; do not run manual processing against the same project while
Automation is active.

**Refresh remote catalogue during preflight** is a separate setting: it queries
NASA for the current available files instead of reusing a cached planning list.
It does not select the processing order. Verified workflows refresh the catalogue
regardless, and annual batches always obtain a fresh scoped selection.

## Processing order and date ownership

Automation handles each selected UTM tile in turn. For that tile, it handles
years chronologically using the existing pipeline: download, duplicate selection,
extraction, verified raw-file cleanup, mosaicking and verified extracted-file
cleanup. When uploads are enabled, upload, Earth Engine synchronization and
eligible mosaic cleanup follow. It completes all requested years for that tile
before moving to the next tile. For two tiles and 2023–2026, the order is tile A
2023, A 2024, A 2025, A 2026, then tile B 2023 through B 2026. It does not download
every tile for every year before beginning extraction.

The first and last years are clipped to the requested dates. For example,
`2023-07-01` through `2026-09-30` produces these inclusive windows:

| Year | First acquisition date | Last acquisition date |
| --- | --- | --- |
| 2023 | 2023-07-01 | 2023-12-31 |
| 2024 | 2024-01-01 | 2024-12-31 |
| 2025 | 2025-01-01 | 2025-12-31 |
| 2026 | 2026-01-01 | 2026-09-30 |

A source belongs to the window containing its acquisition **start** date in the
SWOT filename. Both boundary dates are included. A granule beginning on
31 December and ending on 1 January belongs to the earlier year. Catalogue
queries can return overlapping records; the date-ownership filter records those
outside the current window as exclusions rather than processing them twice.
Unrecognized filenames cannot silently enter a bounded window.

The configured processing-version priorities, projection and quality handling
still apply. Automation retains its existing local mosaic scope of one UTM tile.
Annual scheduling does not combine different years
into a single mosaic or change the scientific processing algorithms. The child
window overrides the manual extraction year selector so that all acquisitions
inside that window are considered.

If later local mosaicking across UTM tiles is required, disable cleanup and retain
the necessary source rasters. Per-tile processing followed by cleanup cannot
promise a later local mosaic that needs deleted inputs. Earth Engine
Consolidation remains available for combining the uploaded outputs separately.

## Uploads, cleanup and storage

When uploading in this mode, Automation waits for Earth Engine confirmation
(`wait_for_ee`) before advancing. This override applies to the annual run and
does not change the saved manual upload setting. Unresolved uploads, failed
stages or a failed batch-boundary check prevent later years of the affected tile
from starting. With **Continue on tile failure** enabled, Automation may proceed
to the next tile while the affected tile's later years remain pending; otherwise
the run stops. Low storage and a user stop request always stop further batches
for all tiles.

Cleanup remains optional and uses the existing verification requirements. Raw
and extracted files are removed only when their downstream evidence permits it.
Final mosaics may remain locally while full pixel-verification evidence is
pending; Earth Engine asset existence alone is not sufficient for deleting those
files. With cleanup disabled, local files accumulate across tile/year batches.

Before download, extraction and mosaicking stages, Automation checks the free
space on the volume containing that stage's configured output folder. A reserve
failure stops further consuming stages and prevents the next batch from starting;
retained files and checkpoints remain available for resume. This is a check at
stage boundaries, not an estimate of the next stage's peak disk use. One tile-year's
data, or a single large stage, can still exceed available storage. Choose a
reserve with room for downloaded originals, extracted rasters and mosaic working
files, including retained final outputs.

## Resume and records

The parent run retains the full requested date range and unique UTM selection.
Each tile/calendar window has its own checkpoint under the parent run's
`tiles/<UTM tile>/years/YYYY` folder. Resume reuses verified completed-batch checkpoints and
continues the incomplete batch; it does not count the same UTM tile as a different
tile for each year.

Project download, catalogue/version history, extraction, mosaic and upload
records remain cumulative in the project's configured locations. Year-specific
checkpoints organize execution without replacing those records. A stopped or
incomplete batch remains distinct from a completed search that found no eligible
inputs. Use the recorded errors to resolve a blocked batch before resuming.

## Progress and Statistics

Automation stage rows and live activity include the year. The progress bar
counts stage execution across all selected tiles and years. **Tiles through all
years** counts a tile only after its stage work across the full requested range
is accounted for. **Tile/year batches through stages** summarizes each completed
tile/year combination, including when one tile has finished every year while
another tile has not started. Neither label certifies pixel completeness.

Statistics retains the full requested date window and unique tile count, with
completed and remaining tile/year batches and the annual run's status. The default campaign
view refers to the full-range parent campaign; an explicitly selected campaign
remains available. Annual details are included in the statistics snapshot and
CSV exports. Input completeness, upload verification and pixel-integrity evidence
remain separate checks and should be reviewed alongside execution progress.

This option reduces the amount of time-range data awaiting downstream processing.
It does not itself establish that historical gaps or shifts are repaired, or
replace a representative end-to-end acceptance pilot before a large rebuild.
