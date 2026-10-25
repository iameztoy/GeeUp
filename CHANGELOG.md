# Release history

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
