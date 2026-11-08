# Calendar-year Automation

The optional **Process each tile year by year** setting limits each pass
through Automation to one tile and one calendar year. It is off by default. Leaving it off
preserves the existing workflow.

## Enable the option

On smaller screens, use the Automation tab's right-hand scrollbar to reach the
queue and results. The mouse wheel scrolls the page over settings/background;
over the tile list or results table it scrolls that list/table instead.

1. Open a project and configure its download, extraction, mosaic and upload
   settings in the corresponding tabs. Check their input and output folders.
2. In **Automation**, select the full date range and UTM tiles. Enable
   **Process each tile year by year**.
3. Choose whether to upload. Select **Clean downloaded and extracted files**
   and **Remove uploaded mosaics** independently; choose the mosaic cleanup policy.
   Set the minimum free space reserve for the output volumes.
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

From v0.8.15, yearly Automation uses the **Upload completion mode** selected
in Automation/Upload; it does not override that choice. Start and Resume both
apply the selected mode to subsequent workers. A running worker is unchanged.

- **Wait for EE verification before next tile** (`wait_for_ee`) keeps the
  existing cloud-task wait and bounded recovery behavior.
- **Submit and continue; verify later** (`submit_and_continue`) finishes the
  local browser transfers, then reconciles once without waiting for EE ingestion
  to finish. It can advance to the next year while acknowledged tasks run in EE.
  Pending verification is not counted as a failed upload. Two additional recovery
  rounds are reserved for confirmed failures or unsubmitted files; active and
  uncertain submissions are retained instead of being blindly retried.

Both modes retain the exact-input boundary check. Verified workflow can classify
a year as **upload pending** only if each missing upload has an unchanged local
mosaic and all remaining inputs are accounted for in EE. That allows later years
to run, even with Continue on tile failure off, while the earlier year stays
incomplete. Resume refreshes EE status, skips verified/active submissions and
retries eligible failures. Legacy runs without verification receipts retain their
conservative input-accounting gate; selecting throughput does not manufacture
missing lineage or bypass that gate.

Missing inputs, failed processing, changed local bytes, uncertain source lineage
or a failed batch-boundary check still prevent later years of the affected tile
from starting. Continue on tile failure can then allow other tiles to proceed.
Low storage and user stop requests always stop further batches for all tiles.

Cleanup is optional, with separate controls for downloaded/extracted files and
uploaded mosaics. Raw/extracted cleanup keeps its existing downstream proof rules.
From v0.8.16, **Remove uploaded mosaics** can use confirmed EE ingestion with
matching provenance, or the stricter full pixel policy. Pending/failed/uncertain
files remain local. The same rules apply without yearly scheduling; see
[mosaic cleanup](MOSAIC_CLEANUP.md). Disabled categories accumulate on disk.

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

**Stop after current step** means finish the current stage, then pause before
starting the next one. From v0.8.8, this is reported as **Automation paused**;
it does not create a failed year or discard outputs. The stop reason is retained
in the run checkpoint. A genuine failed stage or low-disk-space gate remains a
separate error requiring review. **Resume Run** continues pending stages.

If cleanup was initially disabled, enable the desired **Clean downloaded and
extracted files** and/or **Remove uploaded mosaics** control before Resume. Completed years are revisited for their skipped cleanup only,
without downloading, mosaicking or uploading those years again. Remaining years
use the new setting. Changing it during execution takes effect on the next
Start/Resume, not in a running worker. Current verification guards still decide
which files are eligible under the selected mosaic policy. Logs,
filename/version histories and source memberships remain, and Statistics updates
on-disk counts after cleanup without subtracting historical processing totals.

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

From v0.8.17, Resume also revisits successful mosaic cleanup recorded under the
older confirmed-ingestion rule once. This recovers files retained because of
archived rejected asset names, without repeating completed processing or upload
stages. The cleanup-rule version is retained in stage JSON/CSV; it does not mark
a completed year incomplete or enable a disabled cleanup option.

## Upload concurrency on resume

In **Upload → Max pending browser uploads**, choose the number of simultaneous local browser transfers (for example 1 or 2). This is separate from batch size and the limit on Earth Engine ingestion tasks. Start/Resume reads the current setting, including when it loads a saved run. Changing this throughput setting alone does not invalidate completed scientific checkpoints. Already-verified uploads are reconciled and skipped.

A running uploader retains the configuration it started with. For a future change, let it reach a normal pause/completion boundary, set the new value before **Resume Run**, and continue. Editing the field does not retune a running worker; closing the app during active transfers can leave their outcome uncertain until reconciliation. Fewer transfers can shorten each individual transfer without necessarily improving total throughput.

Verified upload reconciliation automatically renews aged catalogue metadata in v0.8.11. See [the freshness and history rules](WORKFLOW_VERIFICATION.md#long-runs-and-catalogue-freshness--v0811).

## Deferred upload records

`upload_pending` is distinct from `complete`, `incomplete`, and a user pause.
The parent and child checkpoints retain the pending files, asset IDs, exact source
checksums and last upload errors. Statistics distinguishes **verification pending**,
**retry pending** and **unknown outcome** in its counts and pending-upload CSV.
These yearly backlog counts describe the last yearly reconciliation, not a live
EE task poll. Current upload/source totals use the cumulative upload records;
Resume refreshes a pending year's boundary checkpoint. Neither pending submissions
nor their retained bytes count as completed EE coverage. Completed years stay reusable on Resume; deferred years
are revisited without calling them complete. Recovery rounds have separate logs
and checkpoint every stage so interruption preserves earlier attempts.
