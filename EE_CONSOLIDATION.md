# Earth Engine consolidation — v0.6.1

This post-ingestion tool combines already stored SWOT observations to reduce image-asset count. It runs independently of Download, Extraction, local Mosaic, Upload and the audit workers. It uses the existing Earth Engine API connection; no NASA login or source download is required for consolidation itself.

**New in v0.5.0:** use **Update coverage register** to connect loaded full inventories and saved plans to **Statistics → Consolidation** and **Status Map → Consolidation Coverage**. This adds a local replacement register, duplicate checks and explicit comparison/approval records. Read [the register operating guide](EE_REPLACEMENT_REGISTER.md) for the workflow, coverage scope and evidence requirements.

**Before production retirement:** downstream publication and replacement-aware upload/resume/cleanup remain unimplemented. Follow the [asset lifecycle design](EE_ASSET_LIFECYCLE.md). Local approval does not change external analysis scripts, and no cloud deletion is provided.

## Create or update

**Create new mosaics** groups original stored `b1`/`b2` assets by UTC acquisition start date, cycle, pass and the chosen output grid. Native UTM zone + hemisphere is the default; retaining latitude-band tiles and an experimental common 100 m EPSG:6933 grid are available.

**Update existing mosaics** extends the corresponding group when new tiles arrive later, for example European tiles joining an earlier African mosaic. Upload dates do not control grouping. The same date/cycle/pass and compatible native zone, hemisphere and grid alignment are required in native mode. Different acquisition dates remain separate. Disjoint coverage is allowed; gaps stay masked.

1. Choose **Operation → Update existing mosaics**.
2. Set **Source collection** to the collection containing newly ingested/original SWOT assets and **Existing mosaic collection** to the consolidated outputs. These must be separate collections.
3. Set **Previous plans folder** to the saved plans from previous consolidation runs. For new histories, the normal Windows location is `%LOCALAPPDATA%/SWOTFlow/ee_consolidation/`. Existing legacy history is preserved; see [storage rules](DEVELOPMENT.md#durable-application-data). A path to one `plan.json` also works. Retain these files permanently with a backup; source-index meaning and full history depend on them.
4. Select the same grouping, hemisphere, version-compatibility, overlap and maximum-quality settings as before. Date filters can change. To change scientific selection settings, rebuild from source assets instead.
5. **Read EE inventory** reads both collections. Offline alternative: **Load inventory JSON** for original/new assets, then **Load existing inventory JSON** for consolidated assets.
6. **Calculate preview**, inspect rows and **Preview accounting / blocked groups**, then **Save plan**. A missing or inconsistent previous plan stops preview rather than guessing history.
7. Select up to ten groups. The **Export collection** may equal the existing consolidated collection or be another collection separate from the original inputs. Confirm the concrete batch. Every output has a new revision ID; existing assets are never overwritten.
8. **Refresh export status** reports EE task completion. Compare outputs scientifically before any separate decision to retire earlier assets. After restarting the app, **Open saved task folder** reconnects status tracking without resubmitting jobs.

## What the preview means

- **Extend:** previous mosaic plus genuinely new inputs in its group.
- **Create:** new group with multiple incoming source mosaics and no previous consolidated image.
- **Unchanged:** nothing new for an existing group; no export.
- **Already represented:** an unchanged original source asset is already in the previous mosaic's recorded history. It is skipped and does not inflate savings.
- **Singleton:** one unmatched input remains as it is.
- **Blocked:** input histories overlap, a version/timestamp changed, lineage is incomplete, or an earlier source changed in place. Inspect the saved reasons and rebuild the affected source group.

One previous mosaic plus four new tile assets can become one updated mosaic: five input image assets, one proposed output, four potential savings. Keeping all originals temporarily means six image assets. The tool does not delete any of them. Counts concern the inventoried collections and proposed outputs, not all project assets; the fresh project quota check includes other project use. Counts do not estimate storage bytes or valid water area.

Only immediate raster contributors are potential retirement candidates. Retained leaf sources already represented in the old mosaic and earlier superseded revisions remain in the inventory count and are not counted again as savings. No automatic retirement or active-product switch is included.

## Pixel and history contract

The default winner is the lowest valid quality code. Equal-quality ties use the original stored source's earliest acquisition start, then its full asset ID. Elevation, quality and source index are selected together. First-valid selection remains available as a paired comparison rule. The valid-quality limit is 0–3; fractional quality, fill and incomplete elevation/quality pairs are excluded.

An old mosaic may contain pixels won by sources acquired at different times. Using its overall earliest time for every pixel would alter ties when a new source arrives. Incremental processing therefore remaps each old `source_index` into the combined, sorted list of original stored sources before applying the selector. This makes an additive update equivalent to processing those same eligible inputs together under unchanged rules. Source indices refer to stored source mosaics, not necessarily individual original NetCDF granules within them.

The new plan keeps flattened source metadata, original filenames and processing identifiers, source acquisition intervals, actual immediate contributors and predecessor revision IDs. Overall acquisition time is earliest source start to latest source end. Inputs starting on different UTC dates are never silently combined. Native output preserves the 100 m lattice; all output bands use SAMPLE pyramids. Common-grid updates keep the same explicitly defined EPSG:6933 lattice and nearest sampling.

Saved v1 consolidation plans are accepted when their matching output metadata and three-band schema are intact. New plans use `swot-ee-consolidation-v2`. Changed metadata, missing source-index mappings, incompatible grid/pyramid policies and unsupported algorithms are rejected. An ancestor remains stored but is not selected alongside its descendant. Multiple unresolved heads for one group stop preview. The downstream analysis must explicitly select the intended revision and must not analyse every old/new revision as separate acquisitions.

## Cases requiring reconstruction

An earlier mosaic contains only winning observations. It cannot supply discarded alternatives when changing the overlap rule, relaxing quality limits, removing a source or replacing a processing version. A filename list does not recover those values. Rebuild the affected group from retained original source assets, or reacquire/reprocess original granules where necessary. This release detects and blocks these cases; it does not implement automatic version replacement or raw-data recovery.

The update planner also blocks revised timestamps for the same original scene and overlapping original observation identities inside different incoming/previous source assets. This is intentionally conservative when a new stored mosaic partly repeats an earlier one. Existing processing-version priority rules are unchanged; alternatives are not treated as new independent observations.

Consolidation preserves the selected stored values. It does not repair historical shifts, fill missing NASA inputs or establish that an older local mosaic paired its bands correctly. New exports retain an unverified status until compared. Pixel verification and investigation of the original data remain independent.

## Reproducible checks and operational limits

Offline checks (no network or production changes):

```powershell
.venv/Scripts/python.exe -m unittest tests.test_ee_consolidation tests.test_ee_consolidation_update tests.test_gui_layout
```

One-off live pilots and their evidence are maintained outside this repository. They have tested bounded incremental/one-shot pixel equivalence, including quality ties, masks and source-index remapping. They do not establish the integrity of the entire source collection or a completed asset-ingestion round trip. The common-grid option remains experimental pending wider seam/mask validation.

The running desktop app and audit workers are not reloaded by editing these files. Use the current v0.6.1 release on the next normal application launch after the current update finishes. Plans and evidence are outside the live processing manifests. Export requests retain the existing ten-job batch limit, low priority, fresh contributor fingerprints, quota checks and durable request/task journals. A detected newer revision requires a new preview; ambiguous submission responses are never blindly retried.
