# Optional mosaic cleanup — v0.8.17

In Automation, these controls are independent:

- **Clean downloaded and extracted files** removes eligible raw files after extraction and extracted TIFFs after mosaicking, using the existing downstream proof checks.
- **Remove uploaded mosaics** removes eligible local mosaics after EE synchronization. It is off by default and is also available in the Cleanup tab. The selected policy applies in both ordinary and tile/year Automation.

For routine storage management, select **Confirmed EE ingestion + matching provenance**. An asset with the same name is not enough: the recent complete EE inventory must match the upload revision, local file checksum and source history, and the upload record must identify the same destination with a successful status. Pending, failed, uncertain, changed or ambiguously linked files stay local. A recorded pixel-check failure for the current EE revision also prevents removal.

This policy checks upload identity and ingestion, not every cloud pixel. **Full pixel verification (strict)** instead requires the existing full comparison evidence. That comparison checks every elevation, quality and valid/missing cell against the local mosaic. It is slower and remains a separate optional scientific check; choosing ingestion-based cleanup never creates a pixel-verification certificate. Without retained mosaics, later full local-to-EE comparison requires recreating them from the recorded sources.

## When files are removed

Automation evaluates cleanup at its cleanup stages after EE synchronization, including a final sweep for eligible delayed ingestions. It does not wait for a whole tile to finish all years. In **Submit and continue**, unfinished EE ingestions remain local; a later sync and cleanup can remove them after confirmation. This is not a background file watcher, and it does not delete files immediately when a task changes state.

To enable removal for a continuing project:

1. Let the current run finish or reach a normal pause; update and reopen the app.
2. Open the same project. Select **Remove uploaded mosaics**, keeping **Confirmed EE ingestion + matching provenance** selected. Enable downloaded/extracted cleanup separately if wanted.
3. Choose **Resume Run**. Eligible cleanup in completed years is revisited without repeating verified scientific work. For manual review while Automation is idle, use **Cleanup → Sync EE Assets + Preview Cleanup**, inspect the candidates, then delete the chosen rows.

Selecting a checkbox or installing a release alone deletes nothing. Changing a preference during execution affects the next Start/Resume; a running worker retains its captured settings. Old assets without sufficient receipts remain for review, even if visible in EE. Cleanup only removes local files and proven raster sidecars, never EE assets.

## Archived rejected names and the v0.8.17 correction

Earlier upload retries sometimes retain two records for one local file: the rejected overlength asset name, marked `REPLACED_INVALID_ASSET_ID`, and its valid uploaded destination. v0.8.16 mistakenly treated these as conflicting current uploads and retained the mosaic. v0.8.17 keeps the rejected attempt as history but excludes it from cleanup ownership, as Statistics already does. A second genuinely active, failed or uncertain destination still blocks deletion; the archive alone never proves successful ingestion.

After updating at a normal pause, keep **Remove uploaded mosaics** and **Confirmed EE ingestion + matching provenance** selected and Resume. The new cleanup-rule version causes one revisit of earlier successful cleanup attempts, including zero-file results in completed years. Only cleanup repeats; completed downloads, mosaicking and uploads are not invalidated. The recorded rule version prevents that same migration from repeating on subsequent Resumes. Existing hash, sidecar, inventory and provenance checks still apply. Updating while a worker is active is unnecessary and should be avoided.

## Resume and Statistics

Keep the project database and `00_logs`: NASA catalogue observations, downloaded filenames, processing versions, original checksums, mosaic memberships and upload receipts remain recorded after raster deletion. Statistics decreases current files/bytes on disk while retaining historical download, processing and upload counts. It shows the mosaic cleanup policy separately from pixel-integrity results; run JSON/CSV records the policy and deletion totals.

Resume reuses cleaned outputs only when current EE evidence still identifies the same uploaded revision and preferred sources. New inputs or revised source bytes rebuild the affected local grouping; missing/changed cloud revisions or changed scientific settings cannot be skipped merely because an earlier run succeeded. Deferred files remain available for recovery. Consolidation replacement chains keep their existing independent proof requirements.

## Configuration and compatibility

```yaml
cleanup:
  mosaics_enabled: false
  mosaic_policy: confirmed_ingestion  # or full_pixels
```

The GUI writes the explicit choice. Older non-GUI configurations without this section retain the prior strict policy and master cleanup behavior; there is no automatic deletion-policy migration in a running process. Confirmed-ingestion cleanup requires the verification receipts even when the surrounding project uses legacy download mode.

Validation uses isolated application regression fixtures, including real local deletions and SQLite histories, both scheduling modes, sidecars, changed evidence, Resume and Statistics. No production imagery is deleted during release testing.

## Cleanup review — v0.8.18

Each cleanup operation hashes an unchanged downstream file once and reuses that result for its dependent inputs/companions. File identity, size, modification and change times are checked again; sidecar membership and the current proof remain checked. On Windows this uses the actual change timestamp, not creation time. If change metadata is unavailable, the file is hashed again. The cache is bounded and never persisted or reused between cleanup operations. Each deletion target is checked freshly, including retries after a file lock.

An explicitly selected full-pixel policy is enforced even with verified downloads disabled. Both explicit policies bind the exact local mosaic hash/sidecars to its current cloud owner and terminal upload record. Genuine conflicting, pending or failed records retain the file. Legacy non-GUI configurations without an explicit policy preserve their earlier behavior and Statistics labels it as legacy.

Statistics refresh no longer calculates deletion eligibility. Use Cleanup's preview/synchronization controls for proof-checked candidate counts; this avoids reading large TIFFs during routine Statistics refreshes.
