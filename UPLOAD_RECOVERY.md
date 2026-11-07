# Earth Engine upload naming and recovery — v0.8.7

## Asset-name rejection

Earth Engine's image upload dialog accepts 1–100 characters per slash-separated
asset-ID component. The optional processing-range suffix introduced in v0.8.6
could extend a normal mosaic stem to 111 characters. The dialog then disabled
Upload; filling properties did not submit an ingestion task.

The uploader now removes only that optional suffix from an oversized recognized
mosaic's cloud name. A typical 111-character stem becomes 90 characters. The full
local filename and GeoTIFF bytes remain intact. Metadata still records minimum
and maximum contributing versions. The local mosaic manifest retains every input
filename and version; the upload report binds the full local path to the cloud ID.

Names already within the limit are unchanged. Invalid parent components, names
still too long after compaction, and two selected files mapping to the same asset
ID fail before browser submission. Nothing is overwritten or renamed in EE.
Custom naming prefixes/suffixes still apply and must fit within the limit.

## Metadata retained and omitted

For mosaics, the dialog omits redundant default descriptor, overlap and MOSA scene
rows. When the source-version endpoints exist, the synthetic mosaic CRID/counter
rows and full version-list row are also omitted. Custom property values that
differ from the parsed filename are preserved. Local upload reports retain the
complete descriptive metadata and source lineage, including intermediate versions.

The dialog retains acquisition start/end, resolution, coordinate system,
cycle/pass and the contributing version endpoints. Verified uploads also retain:

- `swotflow_revision`: unique submission revision, preventing stale-upload reuse.
- `swotflow_sha256`: identity of the exact local mosaic bytes.
- `swotflow_sources_sha256`: identity of the original-input checksum set.

These three properties support ingestion verification, resume and cleanup. They
are intentionally retained; merely finding an asset with the expected name is
not sufficient evidence that it contains the current file.

## Resume and Statistics

Close the old app normally, reopen the current release, open the existing project, and use
Automation → Resume Run. Authenticate Earthdata when the continuing campaign
requires downloads for later batches. Retained local outputs are checked against
the saved source selection and byte proofs; unchanged mosaics can be uploaded
without downloading or recreating them.

Before retrying, the uploader refreshes EE inventory and operations. Invalid old
names with only unsubmitted/failed attempts become `REPLACED_INVALID_ASSET_ID`.
Their rows and status events remain available as history, with the corrected ID
in the explanation. Contradictory task/asset evidence stops the repair for review.
No existing valid cloud ID is migrated automatically.

Statistics excludes those replaced names from current upload totals and reports
their separate historical count. NASA accounting and EE Consolidation resolve the
short cloud names to full local lineage; ambiguous mappings stay unresolved.
Successful form preparation is never counted as a completed upload.
Planned, dry-run and active tasks are not counted as upload failures.

Validation includes maintained offline regressions and an isolated upload-plan
check against retained local mosaics. A successful new live upload must still be
confirmed by the actual EE task/inventory when the user resumes.


## Browser transfer errors — v0.8.10

An upload has two phases: Chrome transfers the local file, then Earth Engine creates an ingestion operation. A browser-side **File upload unsuccessful / HTTP 400** can happen before an operation exists. Checking the operations API alone cannot detect that failure.

The uploader now reads the local upload cards in the task pane as well as remote operations. It expands only failed cards when their destination details are collapsed. A newly observed failed card for the exact current upload is recorded as ERROR and released from the transfer queue. Existing cards are remembered immediately before submission so a prior failure cannot poison a retry. Unknown or ambiguous outcomes still use the bounded transfer wait; they are not assumed failed merely because an asset is absent.

- **Fail fast on first error / fail_fast = false:** continue with other files after a confirmed file failure. This is the default.
- **Fail fast on first error / fail_fast = true:** stop after recording a confirmed failure, including one detected during transfer monitoring.
- **Retry attempts:** applies to immediate upload-dialog/UI failures; it is not an unlimited retry of later network transfers.
- **Automation deferred repair (v0.8.14):** after the initial stage, make up to two additional recovery rounds, with a 30-second delay between rounds and EE reconciliation before/after each round. Successful outputs are skipped. System/login failures stop retries; user pauses are honored between steps. For one attempt per round, keep Upload → Retry attempts at 1. That setting still controls immediate UI retries independently.
- **Retained exceptions:** verified yearly runs can continue to later years if exact-source accounting finds only upload-pending inputs, each in a local mosaic that still matches its processing receipt. The earlier year remains `upload_pending`, never complete. Resume revisits it and skips already-complete years. Missing inputs, changed/corrupt outputs, ambiguous provenance and scientific processing failures still require intervention.
- **Active or uncertain uploads:** recovery waits without reloading any open editor while local transfers are active or unreadable. It leaves attached Chrome open on exit. Late cloud acknowledgements are reconciled before clicking again. A prior uncertain submission without a matching terminal failure remains pending for review, rather than being blindly submitted again. Keep Chrome open while any transfer is active.

An HTTP 400 message alone does not establish the underlying network/server cause or prove that a GeoTIFF is defective. A successful retry of the same verified local file establishes recovery of that attempt. Errors and later successes remain in upload event history; Statistics reflects the current status separately from historical attempts.

The fix does not cancel or restart a running uploader. A worker already in memory uses its original code until it exits; subsequent uploader processes use the updated files. Restart the GUI between runs to load the new displayed version. No change to projections, pixel values, processing-version selection or deletion safeguards is required.


## Asset-ID validation recovery — v0.8.12–v0.8.13

An intended asset name can be valid while the browser dialog still rejects its field. The app now retains Earth Engine's "Each part of the asset ID ... must contain ..." validation text, including line wrapping, and (in v0.8.13) "Please provide an asset ID", rather than discarding them as an unrecognized message. This allows the existing one-time pre-click correction to replace the field with the planned destination through real keyboard input. Direct element clearing avoids appending the name when Ctrl+A does not select the nested input.

The Upload button must become enabled. If validation persists or the button disappears, the file remains a retryable error and is not submitted. Other files continue when Fail fast is off; any unresolved file keeps the affected year incomplete. After the bounded recovery rounds, eligible intact files can be deferred while later years continue; Resume retries those exceptions. They are not silently counted as uploaded. Source lineage, revision bindings, duplicate prevention and Statistics reconciliation remain intact. No extra correction is performed for an already enabled Upload button.

## Error tracking, cleanup and continuation — v0.8.14

The app detects browser validation/transfer errors, remote ingestion failures and missing or unbound expected assets. No single screenshot or asset-name match is treated as proof of success. A browser layout that cannot be read remains uncertain; it is not a silent pass. HTTP errors and exhausted retries do not establish file corruption. Actual byte/proof failures are reported separately and remain blocking integrity issues.

Each run keeps the exception list in `automation_run.json` (`deferred_uploads`); verification reports retain the corresponding source filenames/checksums and EE completeness remains false. Statistics exports `00_logs/statistics/project_statistics_deferred_uploads.csv` with pending paths, asset IDs and last errors, alongside annual status/counts. Historical failures stay in upload events after a successful retry; current status and the next completed reconciliation remove recovered files from the pending list.

Cleanup runs only when enabled and keeps its existing safeguards: intact downstream extraction/mosaic proofs for intermediates, and current full-pixel/source evidence for final mosaic deletion. Deferred local mosaics must be retained for recovery. This release does not loosen those requirements to save disk space.

To continue an existing run, close the idle older app, reopen v0.8.14, open the same project and choose Automation → Resume Run. Keep Fail fast off for per-file continuation; enable Cleanup if desired before Resume. Do not create a new project or delete pending mosaics. Resume checks retained evidence first, so uploads may not begin immediately. Authenticate when a later year's downloads require it.

Validation uses isolated fixtures and browser DOM tests. Production uploads are not started by the release tests; their recovery must still be observed in the real run.
