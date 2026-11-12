# Release history

## 0.8.21 — 12 November 2026

- Keep a retained uncertain upload from an earlier worker out of the current browser-transfer capacity count, after all existing browser transfers have settled and EE inventory/operations have been reconciled. This prevents an old `UNKNOWN_AFTER_CLICK` from imposing repeated transfer timeouts on later recovery batches.
- Preserve the uncertain report row, submission time, provenance and local mosaic. Do not resubmit it merely because an asset or task is absent; late EE acknowledgement remains eligible for normal reconciliation.
- Keep current-worker uncertainty, active/unreadable browser transfers, revision checks and duplicate-upload protection unchanged. Statistics still reports the unresolved outcome; cleanup retains the file until the existing proof requirements are met.
- Add regression checks for multiple recovery batches, current versus historical uncertainty, the startup transfer gate and late cloud acknowledgement. Include the v0.8.20 new-project defaults while preserving existing project choices; show v0.8.21 on Home and in the window title.

## 0.8.20 — 11 November 2026

- Apply editable recommended settings only when creating a new project: verified NASA downloads and filename/version history, preferred source versions without test limits, original projection, yearly per-tile processing, and fresh catalogue checks.
- Use 10 download threads, batches of 50, 4 extraction/mosaic workers, 3 browser transfers and Submit and continue. Keep existing bounded upload recovery and enable continuing with the next independent tile after failure.
- Enable downloaded/extracted cleanup and the separate mosaic-removal choice using confirmed EE ingestion + matching provenance. Keep all proof, pending-file retention, Resume and Statistics rules unchanged.
- Preserve saved settings on Open/Resume/Save As, including explicit disabled options and legacy configurations. Reject New Project over an existing project file. Keep first-run template and project preset aligned, with no automatic start or new scientific/pyramiding policy.
- Add first-launch, new-project, settings persistence and GUI-to-Automation regression checks; show v0.8.20 on Home and the window title.

## 0.8.19 — 11 November 2026

- Use WAL and short, explicit writer transactions for verification receipts, with FULL durability. Bound retries to temporary SQLite lock contention during setup, writer acquisition and commit; do not replay the transaction body or retry disk/permission/invalid-data errors as if they were contention.
- On Resume, rebuild only an exact unchanged mosaic group whose previous verification failed with a recorded database-lock error. Recompute pixels and establish new source/checksum proof; do not approve an unverified existing TIFF or enable blanket overwrite. Other stale outputs remain protected.
- Preserve source filenames, processing history, completed output checkpoints, upload recovery and cleanup guards. No change to raster selection, projections, quality pairing or mosaic values.
- Verify actual parallel writer contention, bounded failure, rollback, legacy receipt migration, unchanged-group recovery and existing Resume/Statistics behavior. Home and the window title show v0.8.19.
- Clarify **Continue with next tile after failure**: independent tiles may continue after a failed tile/year; that tile's later years remain pending. It does not mark bad outputs complete or bypass disk-space, authentication, provenance or user-stop safeguards.

## 0.8.18 — 8 November 2026

- Avoid repeatedly hashing one large downstream mosaic for each input and companion during cleanup. Reuse hashes only within the current operation, with file identity/change metadata, sidecar membership and current proof checks; revalidate after a locked-file retry. Unsupported change metadata falls back to fresh hashing.
- Enforce explicitly selected full-pixel cleanup even when verified downloads are off. Both mosaic cleanup policies require the exact local revision, current cloud proof and an unambiguous successful upload owner. Keep pending, failed, changed and conflicting files.
- Make Statistics refresh metadata-only: retain cumulative NASA/download/extraction/mosaic/upload counts, refresh disk counts, and show cleanup eligibility as not evaluated until explicitly previewed in Cleanup.
- Checkpoint each completed extraction and mosaic in SQLite before the next output. Recover the narrow receipt-before-manifest crash interval from current source/settings/byte proof. Remove stale `.aux`, mask, overview and world-file companions when replacing rasters; leave unrelated files alone.
- Flush an uncertain upload-attempt record to disk before clicking Upload. A lost browser response or process death cannot silently become an unsubmitted file and trigger a duplicate click; a later exact revision echo from EE can still confirm the attempted ingestion. Existing task/inventory reconciliation and bounded recovery remain in place.
- Record worker launch failures as failed stages, and recompute current ordinary-run errors on Resume while retaining historical attempts. Reject scientific/source/output changes to saved Resume runs; cleanup and upload execution preferences remain adjustable.
- Correct projected extraction nodata in the existing Africa LAEA and WGS84 modes: preserve independent source masks and use one shared WSE fill in the GeoTIFF. Invalidate old projected local receipts; native extraction is unchanged. Previously uploaded projected products require explicit review rather than automatic replacement.
- Add workflow option matrices, process-exit/reopen checks, cleanup mutation tests and real-GDAL regressions. Home and the window title show v0.8.18. No production collection or active run is changed by installing this release; reopen after a normal stop or completion.

## 0.8.17 — 8 November 2026

- Fix confirmed-ingestion cleanup retaining verified mosaics when their upload history also contains a rejected overlength asset name. Exclude only `REPLACED_INVALID_ASSET_ID` history from current cleanup ownership, matching Statistics; preserve all archived attempts and their errors. Genuine conflicting destinations, pending/failed uploads, changed bytes and mismatched provenance remain protected.
- Version the cleanup eligibility rule in stage JSON/CSV. Resume revisits successful cleanup attempts made with the older rule once, even when they deleted zero files, without invalidating completed download, extraction, mosaic or upload checkpoints. Apply this to ordinary and yearly Automation; disabled cleanup and the strict pixel-verification policy remain unchanged.
- Test real fixture deletion, retained history/Statistics, cache invalidation after status changes, ambiguous destinations and repeated Resume. The read-only check of all 81 retained 2023 project mosaics resolves their metadata conflict; actual removal still requires the normal current-inventory and local-byte checks.
- Keep the active local application and ongoing run unchanged while publishing this correction. Update after a normal pause/completion, keep **Remove uploaded mosaics → Confirmed EE ingestion + matching provenance** enabled, and Resume. Display v0.8.17 on Home and in the title after updating/reopening.

## 0.8.16 — 8 November 2026

- Separate **Clean downloaded and extracted files** from the opt-in **Remove uploaded mosaics** control in Automation. Share the mosaic setting with Cleanup and apply the same eligibility rules in ordinary and calendar-year runs.
- Offer **Confirmed EE ingestion + matching provenance** for mosaic removal without reading every cloud pixel. Require a recent complete inventory, matching asset revision token, local SHA-256, source checksums, recorded destination and successful upload state. Retain pending, failed, unknown, changed, duplicate or unproven files; known current pixel failures block this policy. Full pixel verification remains a separate stricter choice.
- Recheck local bytes and current records immediately before deletion; remove proven sidecars with their parent. Reuse metadata within a deletion batch and invalidate it when inventories or SQLite histories change, including updates not yet exported to CSV.
- Preserve NASA filenames, processing versions, source membership, upload records and receipts. Resume and download reuse accept current confirmed ingestion evidence after cleanup without calling it pixel certification; new/changed sources and changed settings still invalidate reuse.
- Record the selected cleanup policy in run JSON/CSV and Statistics. Refresh on-disk counts without subtracting historical processing totals. Changing cleanup preferences applies on Start/Resume, including eligible completed-year cleanup, without altering an active worker.
- Keep mosaic removal off by default in the GUI. Legacy non-GUI configurations without the new section retain their previous strict/master-cleanup behavior. Add cleanup, scheduling, Resume, sidecar, Statistics and GUI regression coverage; show v0.8.16 on Home and in the window title.

## 0.8.15 — 7 November 2026

- Respect the selected Upload completion mode in yearly Automation, child configurations and Resume. Remove the implicit `wait_for_ee` override; the existing conservative mode remains available and the default when no mode is configured.
- In Submit and continue, finish local browser transfers and reconcile EE once without waiting for cloud ingestion. Automatic recovery now targets confirmed failures or unsubmitted outputs, with up to two additional rounds. Active/uncertain submissions stay retained; fresh reconciliation and uploader guards prevent blind resubmission.
- Preserve exact-input year-boundary checks. Pending years can advance only with unchanged local mosaic/source evidence; they remain incomplete. Missing inputs, unsafe lineage, processing failures and storage limits still block advancement.
- Distinguish awaiting verification, retry and unknown outcome in Statistics and its pending-upload CSV. Explain that yearly backlog counts are checkpoint snapshots; Resume refreshes them and skips active/verified uploads. Preserve cumulative NASA histories and completed scientific checkpoints.
- Keep cleanup eligibility unchanged: unverified mosaics remain on disk, and local transfers must finish before advancing. Add mode, retry, cleanup, Resume and Statistics regression coverage. Display v0.8.15 on Home and in the window title after updating and reopening.

## 0.8.14 — 7 November 2026

- Add two additional automatic upload recovery rounds after the initial stage, with a 30-second delay between rounds, EE synchronization before and after each round, and independent checkpoint/log paths for each attempt. Stop requests and system/authentication failures remain actionable.
- Preserve active browser transfers: reuse an open editor without reloading, wait for local transfers before recovery, detach the driver without closing attached Chrome, and recheck late EE acknowledgements before another submission. Preserve uncertain submission records; an unknown outcome is not permission for a duplicate upload.
- Let verified yearly Automation continue with explicit `upload_pending` exceptions when all preferred source checksums are represented by verified EE revisions or unchanged local mosaics. Missing inputs, changed bytes, duplicate ownership, failed processing and storage limits still block unsafe advancement. Upload errors alone do not prove corruption.
- Persist pending filenames, destination IDs, source/checksum lineage and last errors in the saved run and verification reports. Statistics counts deferred years separately from completed years and exports a dedicated pending-upload CSV. Resume retries exceptions while reusing completed years; a pause does not erase unvisited pending records.
- Keep cleanup guards intact. Unverified mosaics are retained; safely removed mosaics can justify upstream resume only through the same current full-pixel/source evidence required for deletion. No scientific processing, source selection or projection changes.
- Add regression coverage for bounded retries, late success, active transfers, uncertain submissions, year continuation, cleanup/resume, damaged files and Statistics exports. Show v0.8.14 on Home and in the window title.

## 0.8.13 — 7 November 2026

- Extend asset-name validation recovery to Earth Engine's "Please provide an asset ID" message, observed separately during the same continuing upload. The earlier generic error filter also discarded this wording.
- Cover both observed validation messages with real keyboard recovery tests in isolated Chrome. Keep persistent-error/no-click guards, source history, Statistics and the v0.8.11 catalogue freshness correction intact. Show v0.8.13 on Home and in the window title at the next launch.

## 0.8.12 — 7 November 2026

- Preserve Earth Engine's asset-ID length validation message, including wrapped text. Previously it was discarded because it did not contain generic words such as "error" or "invalid", preventing the existing pre-click recovery from running.
- Retry the planned Asset Name once through keyboard input for that validation. Clear the nested input directly rather than relying on Ctrl+A; never append a second destination path. Stop safely if validation persists or the Upload button disappears. Do not force-enable a disabled button or resubmit an already clicked upload.
- Add unit and isolated Chrome shadow-DOM regression tests for the observed message, exact input replacement, unrelated validation and no-click failure cases. Healthy uploads, scientific settings, metadata/Statistics accounting and duplicate guards remain unchanged.
- Retain the v0.8.11 NASA metadata freshness renewal. Display v0.8.12 on Home and in the window title at the next launch.

## 0.8.11 — 7 November 2026

- Refresh an aged NASA catalogue observation automatically at verified Automation's final upload reconciliation, once per exact tile/date scope. Reuse recent observations without another search. The 24-hour threshold is an application freshness rule, not expiration of files or history.
- Use the existing verified dual-catalogue search, processing-version priority, complete-page accounting and provider-checksum validation. Append a new observation and retain all earlier filenames, selections and transfer history.
- Reconcile current preferred inputs against existing mosaic and EE revision evidence. Added acquisitions, newer processing versions, changed checksums, partial searches and failed refreshes still block the affected year; never download, reprocess, replace or delete imagery from this metadata check.
- Record refresh provenance and current observation dates in the reconciliation report used by Statistics. Preserve historical processing totals, upload recovery, pause/resume, cleanup safeguards and scientific settings.
- Document upload-concurrency changes on Start/Resume. Add regression coverage for unchanged inputs, new/changed/disappeared inputs, incomplete metadata, history retention and year-gate behavior. Show v0.8.11 on Home and in the window title at the next launch.

## 0.8.10 — 7 November 2026

- Detect failed local browser transfers in the Earth Engine task pane, including HTTP 400 failures that never produce an ingestion operation. Open only failed cards' details when needed to identify the exact destination.
- Remove confirmed failed transfers from the browser queue so other files continue when fail-fast is disabled. Record the failure and retain the mosaic for normal deferred repair; never resubmit within the monitor.
- Bind failures to newly created upload cards using a pre-submit element baseline. Ignore old failure cards from earlier attempts, unknown baselines and ambiguous active transfers; remote operations/assets take precedence.
- Preserve the existing bounded wait for transfers whose outcome is unknown, inventory/operation reconciliation before retries, yearly completeness gates and local cleanup safeguards.
- Add regression tests, including an isolated Chrome fixture for the observed shadow-DOM layout. Keep current production uploads, imagery, scientific settings and histories unchanged; display v0.8.10 on Home and in the title.

## 0.8.9 — 6 November 2026

- Correct Statistics so empty JSON lists do not become phantom excluded source files or lineage rows.
- Preserve upload submission time and operation ID when an EE inventory refresh confirms an asset. Recover previously blank verified rows from retained events, once per history read; never infer submission from asset presence or attach an old task to a new attempt.
- Count each mosaic/asset once per contributing source processing version, using retained input filenames. Avoid double-counting the same version across source tiles and stop using a synthetic mosaic filename version when source lineage exists.
- Clarify Statistics units: source records versus output mosaics/assets, overlapping mixed-version totals, and pending uploads versus missing NASA acquisitions. Include count definitions in JSON snapshots.
- Check current local presence before showing an output as upload-ready; preserve completed processing/upload history after cleanup.
- Retain NASA selection, pixel processing, verification/deletion safeguards, yearly Automation and pause/resume behavior. Show v0.8.9 on Home and in the window title.

## 0.8.8 — 6 November 2026

- Fix the yearly stop gate: Stop after current step is a resumable pause, not a failed processing year. Record future stop requests with their time and reason; preserve prior history while correcting the current display of legacy stop-only errors. Real stage failures remain failures.
- Apply the Cleanup checkbox on the next Start/Resume, including when Resume first loads an older saved configuration. A currently running worker retains its original settings.
- Revisit disabled cleanup in completed years before advancing, using the existing downstream verification guards. Reuse scientific and upload checkpoints. Interrupted cleanup resumes from its remaining stages; disabling cleanup prevents this catch-up.
- Filter cleanup candidates by stage, tile and date before reading checksums, including exact GeoTIFF companions. Keep the full proof and immediate pre-deletion checks; preserve unverified mosaics and all source histories.
- Validate disk counts after real fixture cleanup while retaining downloaded/extracted/mosaicked history and completion evidence. Preserve NASA selection, projections, pixel processing, EE upload naming and ingestion checks.
- Display v0.8.8 on Home and in the window title.

## 0.8.7 — 6 November 2026

- Fix the v0.8.6 processing-range suffix exceeding Earth Engine's 100-character asset-name component limit. For oversized recognized mosaics, omit that suffix only from the cloud name; keep the parseable SWOT stem, full local filename, processing endpoints and original-input history. Leave existing valid asset names unchanged.
- Validate destination components and generated names before opening upload dialogs. Reject unresolved length errors and cloud-name collisions with an actionable message.
- Reduce redundant mosaic dialog properties while preserving complete metadata in local upload/mosaic manifests. Keep acquisition times, grid, cycle/pass, minimum/maximum source versions and all three revision/checksum binding properties in verified uploads.
- Resolve shortened cloud names in NASA input reconciliation and EE Consolidation lineage. Ambiguous lineage remains unresolved rather than choosing one of several revisions.
- Preserve invalid-name attempts and status events as history after a fresh EE inventory confirms no corresponding asset/task. Exclude replaced invalid names from current Statistics totals and expose their separate historical count; never count them as uploaded.
- Correct Statistics so planned, dry-run and active uploads are not counted as failures. Failed and unresolved uploads remain visible.
- Retain scientific algorithms, raw/source filenames, existing mosaic bytes, verification guards, yearly scheduling and resume checkpoints. Show v0.8.7 on Home and in the window title.

## 0.8.6 — 27 October 2026

- Remove verified GeoTIFF companions during cleanup, including `.tif.aux.xml` and `.tif.aux`. Preserve sidecars of live/unproven rasters; recover identifiable orphaned companions using current downstream evidence. Retain all filename histories and manifests.
- Resume ordinary and tile/year Automation from the saved NASA selection when current local extraction/mosaic proofs match source checksums and settings. Avoid downloading raw files again after successful raw cleanup. Missing/changed evidence falls back to ordinary verification.
- Write all Automation JSON checkpoints atomically; use the latest stage result when deciding whether a stage completed.
- Add lowest/highest contributing processing-version ranges, including product counters, to new mosaic filenames. Repeat homogeneous versions; retain existing output names to prevent upgrade-only duplicates. Add range/all-version fields to manifests and upload metadata without changing acquisition intervals or pixel selection.
- Preserve yearly scheduling, NASA/download histories, Statistics, scientific algorithms, existing cloud assets and the stopped benchmark. Add maintained regression coverage and the resume/lineage guide.
- Display v0.8.6 on Home and in the window title; use v0.8.6 in the GitHub release title and description.

## 0.8.5 — 26 October 2026

- Fix EE Consolidation export rejection on noncommercial projects by omitting the custom task priority and using Earth Engine's default queue priority. Remove the obsolete priority from newly saved plan settings.
- Cover native UTM, common-grid and incremental exports, including legacy plans with a saved priority, in offline regression tests. Preserve image selection, quality pairing, grids, provenance, no-overwrite rules and duplicate-submission safeguards.
- Keep the live benchmark paused; full cloud comparison and pixel/timing acceptance remain pending. This patch does not resume tasks or change existing assets.
- Display v0.8.5 on Home and in the window title; include v0.8.5 in the GitHub release title and description.

## 0.8.4 — 26 October 2026

- Add vertical page scrolling to Automation so the queue and results remain reachable on smaller screens. The content also expands when more vertical space is available.
- Support the mouse wheel over settings/background without global bindings or changes to the tile list, results table and other tabs' scrolling.
- Preserve all processing, tracking and Statistics behavior. Display v0.8.4 on Home and in the window title; include v0.8.4 in the GitHub release title and description.

## 0.8.3 — 26 October 2026

- Fix overlapping Automation controls: the remote-catalogue-refresh checkbox could cover the yearly-processing checkbox in v0.8.2. Place them on separate rows and explain their different purposes.
- Add a UI regression check for non-overlapping placement and independent settings. Preserve the download, processing, history, Statistics and cleanup behavior.
- Display v0.8.3 on Home and in the window title; use v0.8.3 in the GitHub release title and description.

## 0.8.2 — 26 October 2026

- Add optional **Process each tile year by year** scheduling to Automation, off by default. Complete one tile's requested years before moving to the next tile; leave the existing full-range tile workflow unchanged when disabled.
- Clip the first and last calendar years to the requested range. Assign granules to their inclusive acquisition-start window, including midnight spans, and retain excluded boundary records in the project history.
- Preserve cumulative catalogue, download, extraction, mosaic and upload records while saving independent tile/year checkpoints. Guard completed checkpoint reuse against changed source, processing or output settings. Reconcile input accounting before advancing.
- Require Earth Engine confirmation when uploads are enabled. Block later years of an incomplete tile; other tiles follow the existing continue-on-failure setting. Low-storage and user-stop events pause all further batches.
- Check the actual download, extraction and mosaic output volumes before consuming stages. This reserve is not a peak-space prediction: one tile-year can still exceed capacity, cleanup remains evidence-gated, and uncertified final mosaics may remain local.
- Distinguish unique tiles from tile/year stage batches in Automation and Statistics, retaining the full requested date window. Keep pipeline completion separate from input and pixel certification.
- Retain Automation's per-UTM local mosaic scope. Later local mosaicking across UTM tiles needs retained source rasters; EE Consolidation remains a separate option after upload. See [the yearly Automation guide](YEARLY_AUTOMATION.md).
- Display **v0.8.2** through the shared version source on Home and in the application title. Add maintained scheduling, date-boundary, checkpoint, progress and Statistics regression coverage. No production rebuild or new live-upload acceptance result is claimed by this release.
- Preserve the released projection and mosaic algorithms; the separate v0.8.1 projection candidate remains under evaluation. Use **v0.8.2** in the GitHub release title and description.

## 0.8.0 — 25 October 2026

- Organize application implementation under `swotflow/` packages for processing, consolidation, Earth Engine upload, catalogue history, verification, project management, core helpers, interface and tools. Keep `swotflow_gui.py` as the simple top-level launcher and add `python -m swotflow`.
- Show v0.8.0 on Home and in the application window title, keeping the version visible while other tabs are selected.
- Update internal imports, test targets and worker subprocesses to package/module execution. Resolve bundled assets, presets, configuration and legacy history from one application-root helper.
- This is a minor version increment because documented standalone worker paths/imports change. Update manual CLI examples to `python -m swotflow.<package>.<module>`. Existing standalone worker paths and flat imports have moved; custom scripts need those documented updates.
- Preserve scientific processing, processing-version priority, overlap selection, project formats and existing EE assets. No rebuild, investigation, or cloud mutation is started by this refactor.
- Keep all one-off migration scripts, backups and validation reports outside the application repository. Use **v0.8.0** in the GitHub release title and description.
- Declare NumPy for the GUI environment's native pixel checks and provide pinned validation dependency references under `environments/`. Refresh verification guides to v0.8.0, retaining the live-upload/update acceptance gates.

## 0.7.3 — 25 October 2026

- Select the preferred processing version before overlap selection, even when older extracted TIFFs remain. Automation freezes the completed NASA selection for the mosaic stage; missing preferred versions or checksum conflicts stop that stage. Record excluded versions without deleting them.
- Bind verified browser submissions to exact mosaic/source hashes using a unique revision token and EE metadata. Persist append-only submission and observed-asset receipts. An existing asset name without matching revision or full-pixel evidence cannot skip a changed local upload or certify input accounting.
- Block duplicate acquisition identities across processing versions in completeness/reuse checks and at initial consolidation planning. Existing cloud assets remain unchanged; replacement remains explicit.
- Use an indexed single-filename receipt lookup during extraction instead of decoding the complete download history for every file.
- Relocate both verification databases using consistent backups, transactional path indexes and a versioned path-mapping layer; original receipt payloads remain unchanged. Include them in rollback.
- Run combined cleanup in raw → extracted → mosaic order, retaining the existing immediate hash checks. Clarify the browser uploader's supported pyramiding configuration.
- Keep investigation data, pilots and generated reports outside the application repository. Use **v0.7.3** in the GitHub release title and description.

## 0.7.2 — 25 October 2026

- Keep the Statistics layout and existing totals/charts/maps, adding an Overview explanation and shortcuts to NASA History and consolidation coverage.
- Show the selected reconciliation scope, preferred-input units, NASA/EE metadata timestamps and human-readable evidence states. Retain raw state codes in exported reports and explain them in row details; older reports without timestamps explicitly remain undated.
- Distinguish missing verification receipts from missing catalogue imagery, recorded membership from pixel evidence, and potential asset savings from verified retirement. No automatic scans, processing changes, cloud changes or fixes to the outstanding pipeline review findings are included.

## 0.7.1 — 25 October 2026

- Retain per-run settings/runtime/code hashes and per-output input/output checksums in a separate workflow verification ledger. Verified extraction and mosaicking cannot reuse stale success flags or changed bytes.
- Add Statistics → NASA History controls for exact-input stage reconciliation, an exportable gap/repair queue, and sampled/full native pixel checks against EE or independent placement of original NetCDF data. Sampled matches never certify full pixel equivalence; original-source and local-to-EE checks remain distinct.
- Reconcile verified Automation runs against complete current NASA and EE snapshots; pending inputs fail reconciliation and untested pixels remain visibly pending. Keep these expensive pixel checks outside the ordinary per-file path.
- Reuse a cleaned download group only when all preferred sources have current checksum-linked full-pixel EE evidence. Newly added/revised inputs require affected-group rebuilding; uploads block partial duplicate membership. Cleanup rechecks current downstream proofs immediately before deletion.
- Fix different WSE/quality no-data fills in standalone GeoTIFF mosaics: both bands now share a representable no-data value, preserving paired masks after reopening and upload without sidecars. Bump the overlap signature to invalidate older outputs. This fix does not establish the cause of historical spatial shifts.
- Fix construction of the browser uploader's pyramiding-selection script. Reject differing per-band policies that this browser layout cannot apply, instead of silently using only the global policy. Matching SAMPLE policies are supported.
- Keep existing grouping, version-priority and overlap tie choices. No scheduled monitor, automatic repair execution, automatic cloud retirement or continent-wide rebuild is started by this release. Investigation material remains outside GitHub.

## 0.7.0 — 25 October 2026

- Add verified downloads in Download and Statistics: query both Version D catalogues, preserve all filenames/versions, require complete pagination, and verify exact NASA byte counts and checksums. A run-local cache avoids repeated hashing; every new run and transfer rechecks bytes.
- Reject conflicting catalogue aliases and incomplete searches; stale download-manifest flags cannot skip missing raw inputs in verified mode. Automation refreshes its catalogue preflight in this mode.
- Select local mosaic elevation and quality together using the lowest valid quality code, with deterministic ties; reject misaligned native grids. Verified mode refuses mosaics that would discard incompatible inputs. Mark the new algorithm in source signatures so old outputs are stale.
- Block download, automation and upload reuse of a project marked for collection reset. New projects enable verification/history and disable automatic cleanup for initial validation.
- Document the remaining acceptance gates; these checks do not prove the historical cause of shifted blocks or certify the complete production pipeline. No full rebuild is started by upgrading.

## 0.6.1 — 24 October 2026

- Separate the application and regression suite from local investigation scripts, downloads and reports; exclude investigation paths from Git.
- Save new consolidation plans and registers in the user data folder, preserving discovery of existing legacy history. NASA history remains in the project logs.
- Document repository boundaries and require the version in GitHub release titles and descriptions.
- Include the pending guarded partial-download cleanup and its regression coverage.

## 0.6.0 — 24 October 2026

- Add Statistics → NASA History, persistent catalogue snapshots, candidate filename/version retention, and historical metadata comparisons against recorded EE inputs.
- Add opt-in page accounting and transfer-batch checkpoints for future Download/Automation runs. Existing configurations keep the legacy path.
- Add read-only import of retained filenames, acquisition revision history and JSON evidence exports.
- Include current approved consolidation lineage in version comparisons; candidates, conflicts and stale approval evidence cannot establish representation.
- Move Statistics after EE Consolidation and before Cleanup; include this UI maintenance change in the feature release.
- Validate isolated upgrades, interrupted transfers, aliases, incomplete searches and GUI integration, plus a bounded public NASA metadata check without imagery downloads or EE writes. Investigation evidence is retained separately from the repository.

## 0.5.0 — 24 October 2026

- Add the local replacement register, evidence-based local approval, duplicate checks, and consolidation coverage in Statistics/the map.
- Keep downstream publication and asset retirement as separate work.

## 0.4.0 — 24 October 2026

- Add incremental updates to existing consolidated mosaics, preserving source history and paired pixel selection.
- Compare incremental results with processing all inputs together in a bounded pilot.

This changelog begins with releases documented during the consolidation work; earlier release history is not reconstructed here.
