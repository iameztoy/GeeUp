# Release history

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
