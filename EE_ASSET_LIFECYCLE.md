# Managing original, consolidated and updated Earth Engine assets

Design recorded 24 October 2026; implementation status updated for v0.5.0. The separate local replacement register, explicit local approval, duplicate checks, Statistics table and consolidation map mode are implemented. Cloud publication/deletion and replacement-aware upload/resume/cleanup remain future work. Current originals, audit workers, update jobs and external analysis inputs are unchanged. See [the operating guide](EE_REPLACEMENT_REGISTER.md).

## What runs where

The initial NASA download, extraction and local mosaicking remain local. The resulting upload-ready mosaics are ingested into the original EE collection. EE Consolidation then performs pixel selection, mosaicking and export on Earth Engine servers. The desktop app reads metadata, selects groups, saves lineage/plans, submits tasks and reads status. Accepted EE batch tasks can continue after the desktop app closes; local planning, monitoring and the independent integrity audits cannot.

Consolidation does not require downloading and uploading all of those rasters again. Its native grouping still requires the same acquisition start date, cycle, pass, UTM zone/hemisphere and compatible 100 m grid. A genuinely different UTM zone stays in a different native group. A later tile in an already covered zone can extend its corresponding group.

## Recommended collection structure

Use two physical raster collections initially:

| Role | Contents | How it is used |
|---|---|---|
| Original/upload collection | Existing upload-ready mosaics and later additions from the normal app pipeline | Keep the ongoing uploader's destination unchanged. New tiles go here; there is no need for a new collection per date, region or update run. |
| Consolidated revisions collection | First consolidations and later revision assets, including candidates awaiting verification and superseded revisions retained temporarily | Export new IDs without overwriting. Never assume every image in this collection is an independent current observation. |

Select the original collection explicitly, for example `projects/MY_PROJECT/assets/SWOT_SOURCE`. The consolidated collection is chosen explicitly in the tab; this document does not create or designate one automatically.

Provide a third **logical analysis selection**, defined by a versioned manifest of approved asset IDs. This is a selection of existing assets, not another copy of their pixels. EE can construct an ImageCollection from that explicit image list. It selects one approved representation per acquisition group, using approved original sources for groups not yet consolidated when needed. Partially overlapping source groups require review/reconstruction; whole-asset selection cannot safely remove a duplicated subset of observations by merely dropping an asset.

Keep the active selection stable while an export or validation is pending. Publish a new selection only after coverage, processing-version compatibility, pixel verification and duplicate checks pass. A local manifest alone does not update independent EE scripts or other projects: consumers must explicitly load the published manifest/list or use a generated EE loader tied to its revision. Design this interface before connecting the separate water-classification project. v0.5.0 provides explicit local approval after full-comparison evidence; there is no automatic promotion or downstream publication.

Do not use `original.merge(consolidated)` as the scientific dataset. Nor should a consumer read the consolidated collection without selecting the intended revisions. Different asset IDs can contain the same original observations; uniqueness of `system:index` is not enough to detect that duplication.

## Example lifecycle

For one date/cycle/pass/native grid:

| Stage | Stored image assets in the example | Intended analysis representation |
|---|---|---|
| Initial upload | A, B, C | A, B, C, if approved and their source observations do not duplicate one another |
| First consolidation exported | A, B, C, M1 | Keep A, B, C until M1 is verified |
| M1 approved and selection published | A, B, C, M1 | M1 only |
| Later tiles uploaded | A, B, C, M1, D, E | M1 remains the published representation; D/E are visible as pending additions |
| Incremental update exported | A, B, C, M1, D, E, M2 | Keep M1 until M2 is verified |
| M2 approved and selection published | A, B, C, M1, D, E, M2 | M2 only |
| Separate retirement completed | M2, plus any explicitly retained rollback files | M2 only |

The diagram is conceptual: A–E must satisfy the eligibility and original-observation identity rules. Temporarily retaining inputs and candidates supports validation and rollback. Excluding them from analysis prevents duplicate observations, but does not free project asset slots. To reduce the project count, redundant image assets must eventually be deleted from the project after verified replacement. Moving them into another collection/folder in the same project or marking them superseded does not free their slots. Original filenames, processing versions, time intervals and lineage receipts remain retained even after the raster is retired.

## Tracking model

Do not collapse these independent dimensions into a single “done” flag:

| Dimension | Examples | Evidence |
|---|---|---|
| Cloud availability | Present, absent, not checked, stale snapshot | Fresh paginated EE inventory and asset metadata |
| Export execution | Planned, submitted, running, succeeded, failed, uncertain | Saved request ID and EE operation/task status |
| Verification | Not checked, structural checks passed, sampled pixels passed, full comparison passed, failed | Receipt tied to output fingerprint, input set, grid and tested scope |
| Product role | Incoming, candidate, active, superseded, retirement candidate, retired | Explicit publication/replacement register and approved manifest revision |
| Source coverage | Represented in active product, represented only in a candidate, pending addition, unresolved, superseded product version | Original-observation identities and exact eligible product versions linked through all mosaic levels |

EE task success establishes execution, not scientific correctness or inclusion in the published analysis selection. A consolidated image's source list establishes recorded input coverage, not valid water coverage at every pixel. An input can be accounted for while losing all overlap selections or containing no valid pixels.

The replacement register should store stable group key, output revision/fingerprint, predecessor IDs, flattened original observation/product identities, source assets, full acquisition intervals, verification receipt, publication-manifest revision, and retirement events. Retired records must resolve through replacement chains to a currently present approved asset. Repeated retirement attempts must be idempotent and failures visible.

Retirement must also preserve rebuildability. An old mosaic retains winners only. Replacing a source processing version or changing masks/selection can require original inputs. Retain compact provenance permanently, decide a bounded rollback policy, and document that reacquisition/reprocessing may be needed after raw/intermediate rasters are removed.

## Statistics and map integration: implemented scope and remaining design

v0.5.0 adds **Consolidation Coverage** alongside Pipeline Status and Update Coverage. It shares the Statistics consolidation date window, uses fixed categorical colours and text labels, displays snapshot timestamps and treats inventories older than 24 hours as stale. Source-tile counts and asset history are available in Statistics. Its denominator is registered original-input history, not the NASA catalogue; catalogue reconciliation, published downstream selections and confirmed retirement metrics below remain design targets.

The complete lifecycle target is to report per source UTM tile and date/pass/grid group:

- Eligible expected observations under the existing processing-version policy.
- Original observations currently available in EE.
- Observations recorded in the approved active mosaic.
- Newly uploaded observations awaiting consolidation.
- Observations included only in exported candidates awaiting verification/publication.
- Missing or unresolved observations, failed checks and duplicate/ambiguous revision conflicts.
- Superseded rasters still occupying slots, eligible retirement candidates and confirmed retired assets.
- Current stored image count, active-analysis image count, temporary overhead and realized asset reduction.

The implemented view counts distinct registered observations and the best registered product under the existing version-priority policy. The future catalogue-integrated ratio is `distinct eligible observation identities represented in the active product / distinct eligible expected observation identities`, scoped to the chosen catalogue snapshot and version policy. Keep upload coverage and valid-pixel coverage as separate metrics. A newer required processing version cannot be declared fulfilled merely because an older version is present; show acquisition presence and version freshness separately. Counts from different tiles must be deduplicated appropriately when aggregated.

An Africa-plus-Europe mosaic must contribute coverage to every source tile recorded in its flattened lineage, even though it has one output CRS and one asset name. The map should use that source lineage rather than guessing coverage from the new consolidated filename. A tile with new pending observations remains visibly pending even when its older acquisitions are already consolidated. Reducing raster count alone must not make recorded acquisition coverage shrink.

For efficient operation, refresh metadata/task states incrementally and cache both inventories and the lineage index. Bulk inventory reconciliation provides the observation accounting; pixel checks use the separate verification queue. Standard map refresh should not download rasters or rerun continental pixel analysis.

## Required connection to upload, resume and cleanup

The original v0.4.0 inspection found the following limitations; the new consolidation view resolves the separate history/coverage display, while the original pipeline/resume limitations remain:

- v0.5.0 `swotflow_gui.update_statistics_status_map` also refreshes the separate consolidation panel/register. Existing pipeline/update modes still retain their previous meanings; the new mode receives flattened source-tile coverage from the register.
- `project_insights.merge_upload_rows_with_ee_inventory` positively confirms matching inventory assets but retains unmatched historical upload rows. An absent current asset is not automatically a proven data gap or a valid replacement.
- `ee_ui_uploader.should_resume_skip_existing_status` can retain `EE_VERIFIED_EXISTS` as a resume skip. It does not resolve that source through a consolidation replacement record.
- v0.5.0 retains plans/task journals and adds an event-backed local replacement register and explicit full-comparison approval. It still has no published downstream manifest, retirement-aware pipeline accounting or cloud retirement operation.

Before retiring production originals, all three consumers—Statistics, resume/upload and cleanup—must understand “original retired, observations available in approved replacement.” A historical successful-upload flag alone is insufficient. Conversely, disappearance of a deliberately retired original must not trigger a redundant re-upload or mark the corresponding observations missing when the approved replacement is present.

Retirement procedure to implement: freeze an explicit proposed list; verify replacement availability, scope, lineage and required pixel checks; publish the new analysis selection; check rollback policy and references; then perform separately authorized deletions of only listed obsolete images. Never delete based solely on a matching date/UTM label or successful export. Confirm each deletion and preserve its replacement mapping. No automatic or manual cloud deletion was performed while recording this design.

## Acceptance cases before rollout

1. A/B/C → M1 → M2 (with D/E) yields one approved representation for the group after promotion, with unchanged original-observation accounting.
2. A failed export or failed verification leaves the previous approved product active and all inputs retained.
3. Both old and new revisions may exist physically but cannot both enter the same published analysis selection.
4. Partial-overlap and revised-processing conflicts block promotion/retirement until reconstructed or resolved.
5. Retiring A/B/C/M1/D/E after M2 approval preserves source-tile coverage and does not schedule those inputs for redundant upload.
6. If M2 later disappears or changes, the current inventory makes the replacement proof invalid; the map reports unavailable/unresolved rather than reusing a historical green status.
7. Interrupted promotion/deletion can resume from durable records without losing the last approved selection or repeating unsafe deletions.
8. Different native zones/hemispheres/dates remain separate, and mixed Africa-Europe source tiles remain individually traceable.

## Sources

Repository implementation reviewed 24 October 2026: `swotflow/consolidation/core.py`, `swotflow/consolidation/update.py`, `swotflow/consolidation/tab.py`, `swotflow_gui.py`, `swotflow/project/insights.py`, `swotflow/earthengine/uploader.py`, `swotflow/project/database.py` and `swotflow/ui/utm_map.py`.

Official EE documentation checked the same day: [batch processing](https://developers.google.com/earth-engine/guides/processing_environments), [collections from explicit image lists](https://developers.google.com/earth-engine/apidocs/ee-imagecollection-fromimages), and [asset management](https://developers.google.com/earth-engine/guides/manage_assets).
