# NASA filename and processing-version history — v0.7.1

**Statistics → NASA History** retains catalogue searches and compares preferred NASA products with the original inputs recorded for EE assets. Version 0.7.1 adds stage reconciliation, an exportable repair queue and optional native-pixel checks. See [workflow verification](WORKFLOW_VERIFICATION.md) for controls, evidence requirements and limits.

## Use the feature

1. Open the project on the next normal application launch. Do not interrupt a running update merely to load this release.
2. Choose **Statistics → NASA History → Use project scope**. This copies Download dates and tiles into independent fields. Editing these fields does not change Download or Automation settings.
3. Optionally click **Import retained filenames**. It reads the project SQLite database in read-only mode, or CSV exports if no database exists, and imports existing download, extraction and mosaic input names. Unchanged imports are idempotent. Imported records are labelled `legacy`: the original NASA query/revision history is unknown.
4. Set historical acquisition dates and tiles, then **Check historical dates**. Only public metadata is retrieved. For Version D the default checks both `SWOT_L2_HR_Raster_100m_D` and `SWOT_L2_HR_Raster_D`, restricted to the selected 100 m UTM filenames. The parent catalogue can expose products absent from the subcollection. Collections are editable and retained in each search receipt.
5. **Read EE inventory**, or **Load EE inventory JSON** from a saved full original-collection snapshot. These controls save a dated verification inventory, leave legacy upload reports unchanged and submit no EE tasks.
6. Select a search and **Compare selected search**. The report lists NASA's preferred filename, the best recorded EE input, affected asset IDs and whether the preferred file was merely downloaded. Double-click a row for all retained versions/revisions of that observation. Export the selected history or comparison as JSON evidence.
7. Enable **Verified downloads** in Download or NASA History for both-catalogue production discovery, mandatory retained history and exact NASA byte/checksum checks. New projects enable this mode. Use the preferred version filter and no result limit; narrow dates/grids for pilots. Historical manifest flags alone do not skip missing raw files. A complete group already represented by current checksum-linked full-pixel EE evidence can be reused without transferring its originals again.
8. For legacy mode, to retain future ordinary previews/downloads and fresh Automation searches, enable **Record NASA history for future Download/Automation searches** and save the project. Existing configurations default to disabled so an ongoing update and its subsequent subprocesses retain their current behaviour. Historical checks in this panel always retain history, independently of that setting.
9. Select a complete search and **Reconcile all stages**. Open **Completeness and gaps**, inspect exact input rows, then **Export gaps / repair queue**. Missing, pending, conflicting and duplicate inputs remain separate. A metadata reconciliation does not certify pixel correctness.
10. Select rows for **Check selected EE pixels** or **Check originals against local mosaics**. Choose `sampled` for screening or `full` for every native cell. Originals must still be present for source replay; local mosaics must still be present for EE comparison. **Stop pixel check** stops only this check. Reconcile again to refresh completion evidence.

There is no scheduled monitor in this release. Revisit historical acquisition dates explicitly; new-date searches cannot find reprocessing of old measurements. Verified Automation preflight always refreshes NASA. Legacy cached preflight does not contact NASA: request a remote refresh when needed. Legacy downloads keep one selected catalogue; verified downloads explicitly query both Version D catalogues.

## Evidence retained

The separate database is **`00_logs/nasa_catalog_history.sqlite3`**, beside the configured download manifest. Back it up with the project database and consolidation plans. Raster cleanup does not remove it; reading absent history creates nothing.

- Each search has a unique ID, UTC times, exact date/grid/collection scope, selection policy and ranking-code hash. Repeating a search appends a snapshot.
- Each returned page is committed before the next request, with compressed metadata, SHA-256, hit/page counts, observation time, search-after token and CMR request ID when supplied. Credential fields and URL query strings are removed. The hash covers **sanitized metadata**, not original HTTP bytes. Authentication headers/passwords are never saved.
- Every candidate retains its exact filename, observation identity, CRID/counter, concept ID, revision ID/date, provider, acquisition interval, public links and available `DataGranule` metadata/checksums. Catalogue aliases remain in history; exact filenames are selected once.
- Winners and excluded alternatives retain their decisions and preferred filenames using the existing `swot_metadata` policy. Superseded versions are not additional missing acquisitions.
- Enabled downloads save a plan, batch-start intent, batch results and final/stopped state. Completed batches survive a later crash even before the legacy manifest is saved. A crash inside a transfer batch leaves its items pending; started is not proof of completed. Existing local-file checks reconcile files on retry. The history does not change legacy completion or skip rules.
- Failed/interrupted searches retain pages and cannot claim completeness. Repeated/missing pagination tokens, repeated concepts within one collection, timed-out responses and changed/mismatched hit counts are rejected. Limited searches are labelled `limited`; unpaged providers without accounting are `unverified`. Retrying creates a new snapshot, preserving the failed attempt.

Unrecognized or out-of-scope products stop an enabled search for review. Ordinary downloads preserve CMR temporal-overlap semantics; historical comparisons use the explicit UTC acquisition-start dates in the panel. Storage scales with metadata, not raster volume. Writes are batched transactions; pages are compressed and filename/observation lookups are indexed. No new dependency or raw imagery download is required.

## Comparison meanings and limits

| Finding | Meaning |
|---|---|
| `preferred_version_recorded` | This search's preferred filename appears in recorded inputs of a loaded EE asset. |
| `newer_version_available` | NASA's winner outranks the best recorded EE input. A local download does not establish EE ingestion. |
| `recorded_version_ranks_higher` | EE lineage contains a higher-ranked product than this search returned. Review scope/availability; never downgrade automatically. |
| `timestamp_variant_review` | Grid/cycle/pass/scene match but acquisition timestamps differ; review instead of silently merging identities. |
| `not_in_loaded_ee_lineage` | No matching acquisition in loaded original-asset lineage. Pending work, unknown provenance or retirement may explain this; it is not proof of a download failure. |
| `not_returned_in_search` | An EE-recorded input was not returned. This is not proof of withdrawal or permission to delete it. |
| `equal_rank_variant_review` | Different filenames without a clear priority winner require review. |

The report belongs to one search scope. NASA/EE snapshots older than 24 hours, undated snapshots, incomplete searches and unresolved assets remain qualified. Pagination reconciles counts but is not an atomic NASA snapshot: catalogue records can change while paging. Same-filename CMR revisions remain available for inspection; a metadata revision alone does not establish changed pixels or processing priority.

Original EE membership is full inventory metadata joined to mosaic input records: **recorded lineage plus existence, not pixel verification**. The comparison also reads the project's separate replacement register. A consolidated mosaic can supply retained original input history only when it is locally active, present in a recent inventory, free of conflicts and backed by current full-comparison approval evidence. This preserves processing-version comparisons after original assets are retired. Refresh that register through EE Consolidation when needed. Candidate exports, unknown lineage, stale inventories and invalid approvals cannot establish representation; they are excluded or flagged. The comparison does not change register approvals or certify any additional pixels.

No finding triggers automatic download, replacement or retirement. Replacing a source inside a mosaic may require rebuilding that mosaic; adding both versions as separate acquisitions would create duplicates. Development did not change any cloud asset, audit queue, active configuration or running process.

## Validation, sources and rollback

Statistics Overview now links directly to NASA History and consolidation coverage. In Completeness and gaps, counts refer to **preferred input acquisitions in the selected search**, not mosaic assets or the whole project. Reconciliation retains the NASA and EE snapshot dates; stale dates request a refresh, and older undated reports request reconciliation. Row details explain each evidence state while JSON exports retain the original codes. “No verified download receipt” is not proof of an absent NASA file or a measured spatial hole. Full local-to-EE evidence does not replace the separate original-source replay check.

Isolated tests cover excluded-version retention, changing concepts/revisions, aliases, incomplete paging, interrupted transfers, stale evidence, timestamp variants, read-only project access and the Statistics workflow. Run `python -m unittest tests.test_nasa_catalog_history tests.test_download_tool tests.test_gui_layout` for offline regression coverage. One-off live metadata checks and their retained evidence are kept outside the repository.
