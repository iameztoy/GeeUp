# Workflow verification — v0.8.0

Verified mode connects exact preferred NASA filenames and checksums to local outputs and current Earth Engine assets. It applies to initial runs and updates in the same project. Preserve the project logs and databases when cleaning rasters; independent projects do not automatically share history.

## Routine processing

- A complete search queries both relevant Version D catalogues and keeps every returned filename/version. Existing processing priority chooses one preferred product per observation; lower versions and catalogue aliases remain recorded, without counting as missing acquisitions.
- Downloads require the NASA size and checksum. Extraction and mosaicking record source/output hashes, mask/georeferencing sidecar hashes, grid metadata, run settings, code fingerprints and effective workers. Reuse checks the current bytes and scientific settings. A changed source, output or sidecar cannot pass on a historical success flag.
- Lowest eligible quality selects WSE and QA together. Both bands share a no-data value in the final standalone GeoTIFF. Native misalignment and excluded incompatible inputs stop verified mosaicking.
- Upload refreshes the actual collection inventory. A missing asset is not skipped merely because an old task succeeded. An existing same-ID asset without full pixel evidence remains pending verification.
- Verified Automation ends with exact preferred-input accounting. Missing or ambiguous inputs fail reconciliation; complete recorded membership with untested pixels is a warning, not a scientific pixel certificate.

Full source replays and full EE reads are **optional maintenance operations**, not automatically repeated for every processed file. Checksums do add local disk reads. Catalogue searches, environment capture and EE inventory refreshes operate at run/batch level.

## Statistics → NASA History

Overview keeps the existing processing totals and offers shortcuts to these controls and consolidation coverage. Reconciliation displays the selected date/grid scope, preferred-input counts and NASA/EE metadata dates. Readable state labels are explanations of saved evidence; the exported raw state codes stay unchanged. Counts are not a collection-wide completeness or duplicate-free certificate. Missing receipts and spatial source-data gaps remain distinct.

1. Set a bounded grid/date scope and **Check historical dates**. This also finds newly published preferred versions of old acquisitions.
2. **Read EE inventory**, select the complete NASA search, then **Reconcile all stages**.
3. Inspect **Completeness and gaps**; double-click for exact filenames and supporting evidence. **Export gaps / repair queue** saves a reviewable JSON report. It does not start transfers or replace assets.
4. Select rows and choose **Check originals against local mosaics** or **Check selected EE pixels**. `sampled` screens a deterministic selection of native windows; `full` compares every native cell. The stop control affects this verification only.
5. Reconcile again after checks or repairs. Snapshots older than 24 hours, unresolved lineage, conflicting versions and duplicate active membership block current completeness.

| State | Meaning |
|---|---|
| `download_missing_or_unverified` | No valid transfer receipt for the preferred filename and current NASA checksum. This is not proof that a legacy file was never downloaded. |
| `download_pending_extraction` | Exact bytes were verified; no matching extraction receipt. |
| `extraction_pending_mosaic` | Extraction is recorded; no matching mosaic receipt. |
| `mosaic_pending_upload` | Mosaic membership is recorded; no current recorded EE representation. |
| `ee_present_needs_pixel_or_provenance_check` | Recorded EE membership exists; current full pixel/checksum evidence is incomplete. |
| `verified_active` | A current EE asset matches the linked local mosaic at every native cell, with matching source checksums. |
| `duplicate_active_inputs` | More than one active recorded asset includes this preferred input; resolve the replacement before declaring completion. |

These states count **input identities**, not square kilometres. Included sources may contain nodata, rejected quality cells or observations that lose to a better overlap. A blank map area is not automatically a missing download. A separate spatial investigation is needed to distinguish these causes; an automated continent-wide spatial gap atlas is not provided.

## Two independent comparison stages

**Original → local** reads the exact checksummed NetCDF variables and independently places them on the integer native grid, without calling the production extraction/VRT/warp/mosaic selector. It compares WSE, QA and both masks, including overlap selection. It shares GDAL's NetCDF reader, so this is not independent validation of the reader itself. Original files must remain available or be reacquired. The checker supports unrotated native 100 m grids; common-grid output requires separate validation.

**Local → EE** compares the exact native CRS, transform, dimensions, masks, quality codes and WSE values. It checks the asset fingerprint and local hash before/after. WSE tolerance is 0.00001 m; QA and masks must match exactly. Sampled comparisons cannot certify full equivalence. This check proves ingestion fidelity relative to the local mosaic, not correctness of its original-source processing. Source replay results are retained separately and are not implied by `verified_active`.

The optional checks consume time and EE requests. They do not upload, overwrite or delete assets. Results do not establish the historical cause of any shift.

The browser uploader supports a global pyramiding policy. Per-band entries matching that policy are accepted; differing per-band policies are blocked because this dialog integration cannot apply them. SAMPLE for both bands preserves paired observations in pyramids. Native-grid checks do not certify how a coarser scientific average or display filter should be interpreted.

## Resume, updates and cleanup

Mosaic selection applies the shared processing-priority policy before considering
pixel quality. Cleanup and raw duplicate removal are not substitutes for this
selection. Verified Automation freezes the completed NASA scan ID for mosaicking;
each selected extraction must match that scan's preferred filename and checksum.
Older extracted versions are recorded as excluded and cannot supply pixels.

New verified browser uploads include a unique revision token, local SHA-256 and
source-checksum digest in the EE properties. Append-only ingestion receipts bind
these to the observed EE asset fingerprint and the task identifier when available.
This is inexpensive submission/accounting evidence, not a pixel correctness test.
Legacy assets without a matching revision receipt require a full pixel comparison
or explicit replacement. An existing asset name alone cannot certify a new local
revision. Duplicate checks use acquisition identity across processing versions,
including unverified active owners and repeated inputs inside one mosaic.

Per-file verification uses an indexed latest-receipt lookup. Whole-history reads
are reserved for whole-project reconciliation. Project relocation backs up both
verification databases and adds operational path mappings without rewriting the
original receipt payloads. Combined Cleanup deletes raw, extracted and mosaic
files in that order, rechecking each dependency immediately before deletion.

A download group can skip reacquisition after raw cleanup only when **every** preferred source has checksum-linked full-pixel evidence in a recent active EE inventory. A new member or changed preferred source forces reacquisition of the affected local date/cycle/pass/grid group. Partially overlapping uploads are blocked for explicit replacement, avoiding duplicate observation membership.

Verified cleanup is conservative: raw files require a matching current extraction proof; extracted files require a matching current mosaic proof; local mosaics require full-pixel evidence for their active cloud representation. Target/downstream hashes are rechecked immediately before deletion. Logs, history, proof databases and sidecars are retained. Original-source replay needs the originals, so leave cleanup disabled until the desired acceptance checks finish.

Approved consolidation evidence is reused only when it can be connected to checksum-certified original assets and current replacement fingerprints. Filename-only legacy evidence and unresolved chains remain pending. Physical cloud retirement and publication of an active dataset still require a separately reviewed workflow; this release does not automatically delete superseded assets.

## Evidence and remaining limits

Back up `00_logs/nasa_catalog_history.sqlite3`, `download_verification.sqlite3`, `workflow_verification.sqlite3`, project manifests/database and consolidation plans/register. Dated stage reports are in `verification_reports/`; original-source results in `source_pixel_checks/`. Credentials are excluded from receipts.

Historical refresh and integrity checks are manually launched. Scheduling, automatic repair execution, a stratified collection-wide audit queue, automatic cloud retirement and a spatial gap atlas remain future work. A limited successful pilot supports those tested native cases; it does not certify every grid, date, processing version or optional projection. Investigation reports/data stay outside the application repository. Use **v0.8.0** in any GitHub release title and description. The historical spatial-shift mechanism remains unresolved; these safeguards and independent checks do not establish that cause.
