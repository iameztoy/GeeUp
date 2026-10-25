"""Statistics controls for immutable NASA history and metadata-only backfill checks."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

import nasa_catalog_history as history
from ee_consolidation_coverage_ui import ConsolidationCoveragePanel


STAGE_EVIDENCE = {
    "download_missing_or_unverified": (
        "No verified download receipt",
        "No valid receipt matches this preferred filename and NASA checksum. This does not prove that the file was never downloaded or is absent from NASA.",
    ),
    "download_pending_extraction": ("Downloaded; extraction pending", "A verified download is recorded; a matching extraction receipt is still needed."),
    "extraction_pending_mosaic": ("Extracted; mosaic pending", "A matching extraction receipt is recorded; mosaic membership is still needed."),
    "mosaic_pending_upload": ("Mosaicked; EE membership pending", "A local mosaic receipt is recorded, but this input is not represented in the loaded EE lineage."),
    "ee_present_needs_pixel_or_provenance_check": (
        "EE membership recorded; checks pending",
        "Recorded lineage associates this input with an EE asset. Presence alone does not establish that EE contains the current local revision or matching pixels.",
    ),
    "verified_active": (
        "Full local-to-EE evidence recorded",
        "Full native-pixel evidence links the local mosaic to its EE representation. Correctness against original NetCDF data is checked separately; this is not a collection-wide completeness certificate.",
    ),
    "ee_revision_bound_pixels_pending": (
        "EE revision matched; pixel checks pending",
        "Submission evidence binds this source to the observed EE revision. Full native-pixel comparison and original-source replay are separate checks.",
    ),
    "duplicate_active_inputs": ("Duplicate membership flagged", "This input has conflicting recorded active memberships. Review the affected assets before replacement or cleanup."),
}


def stage_evidence_label(state: str) -> str:
    """Translate a saved state without changing the machine-readable report."""
    return STAGE_EVIDENCE.get(state, (state.replace("_", " "), ""))[0]


def verification_view_text(report: dict) -> tuple[str, str]:
    """Describe one saved scope; never turn historical counts into current certification."""
    scope = report.get("scope", {})
    tiles = scope.get("tiles", [])
    tile_text = ", ".join(tiles[:6]) or "grids not recorded"
    if len(tiles) > 6:
        tile_text += f" (+{len(tiles) - 6} more; full list in export)"
    scope_text = f"{scope.get('start', '?')} to {scope.get('end', '?')} UTC | {tile_text}"
    summary = (f"Selected search only: {scope_text}\n"
               f"{report['expected_preferred_inputs']:,} preferred inputs (not mosaic assets). "
               + "; ".join(f"{stage_evidence_label(k)}: {v:,}" for k, v in report["counts"].items()))
    def evidence_date(key: str) -> str:
        value = report.get(key)
        if not value:
            return "not recorded — reconcile again"
        return str(value) + (" (recent)" if history.recent(value) else " (refresh needed)")
    status = (f"Saved reconciliation: {report.get('utc') or 'not recorded'}. "
              f"EE collection: {report.get('collection') or 'not recorded'}.\n"
              f"NASA metadata: {evidence_date('catalogue_finished_utc')}; "
              f"EE inventory: {evidence_date('inventory_finished_utc')}.\n"
              f"Recorded input membership: {'accounted for' if report.get('input_accounting_complete') else 'pending/review'}. "
              "Counts describe the saved evidence, not a guarantee of a complete or duplicate-free collection. "
              "Local-to-EE pixel checks and original-source checks are separate. "
              + " ".join(report.get("blockers", [])) + " "
              + report.get("gap_note", "Input-accounting gaps are not measured spatial holes."))
    return summary, status


class NasaHistoryPanel(ttk.Frame):
    """Keep catalogue checks separate from download settings and active pipelines."""

    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent, padding=6)
        self.app = app
        self.busy = False
        self.generation = 0
        self.events: queue.Queue = queue.Queue()
        self.poll_id: str | None = None
        self.inventory: dict = {}
        self.report: dict = {}
        self.scan_rows: dict = {}
        self.verification_report: dict = {}
        self.pixel_stop = threading.Event()
        self.pixel_mode = tk.StringVar(value="sampled")
        self.start, self.end, self.tiles, self.collections = (tk.StringVar() for _ in range(4))
        self.status = tk.StringVar(value="Use project scope, then choose historical dates. Checks retrieve metadata only.")
        self.summary = tk.StringVar()
        self.grid(row=0, column=0, sticky="nsew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        settings = ttk.Frame(self)
        settings.grid(row=0, column=0, sticky="ew")
        settings.columnconfigure(3, weight=1)
        for row, (label, variable) in enumerate((("From UTC date", self.start), ("Through UTC date", self.end),
                                                ("UTM tiles (comma separated)", self.tiles), ("NASA collections (comma separated)", self.collections))):
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6))
            ttk.Entry(settings, textvariable=variable, width=65).grid(row=row, column=1, columnspan=3, sticky="ew", pady=2)
        actions = ttk.Frame(self)
        actions.grid(row=1, column=0, sticky="ew", pady=6)
        for index, (label, command) in enumerate((("Use project scope", self.use_project), ("Check historical dates", self.scan),
                ("Refresh history", self.refresh), ("Import retained filenames", self.import_legacy),
                ("Read EE inventory", self.read_inventory), ("Load EE inventory JSON", self.load_inventory),
                ("Compare selected search", self.compare), ("Export selected history", self.export_history),
                ("Export version comparison", self.export_comparison),
                ("Reconcile all stages", self.reconcile_stages),
                ("Export gaps / repair queue", self.export_verification),
                ("Check selected EE pixels", self.check_pixels),
                ("Check originals against local mosaics", self.check_originals))):
            ttk.Button(actions, text=label, command=command).grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 6), pady=2)
        ttk.Label(actions, text="Pixel check:").grid(row=5, column=0, sticky="w")
        ttk.Combobox(actions, textvariable=self.pixel_mode, values=("sampled", "full"), state="readonly", width=12).grid(row=5, column=1, sticky="w")
        ttk.Button(actions, text="Stop pixel check", command=self.pixel_stop.set).grid(row=5, column=2, sticky="w")
        checks = ttk.Frame(self)
        checks.grid(row=2, column=0, sticky="ew")
        ttk.Checkbutton(checks, text="Record NASA history for future Download/Automation searches (save project to retain setting)",
                        variable=app.catalog_history_enabled_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(checks, text="Verified downloads: both Version D catalogues, mandatory history and exact NASA checksums",
                        variable=app.verified_downloads_var).grid(row=1, column=0, sticky="w")
        ttk.Label(checks, text="Verified mode requires a complete search and the preferred version filter; historical download flags alone never skip a file.",
                  wraplength=850).grid(row=2, column=0, sticky="w")
        ttk.Label(self, textvariable=self.summary, wraplength=850, justify="left").grid(row=3, column=0, sticky="ew", pady=5)
        book = ttk.Notebook(self)
        book.grid(row=4, column=0, sticky="nsew")
        self.searches = ConsolidationCoveragePanel.table(book, "Search history", (
            ("started", "Checked UTC", 190), ("status", "State", 135), ("candidates", "Catalogue records", 120),
            ("dates", "Acquisition dates", 195), ("collections", "Collections", 320)))
        self.versions = ConsolidationCoveragePanel.table(book, "Version comparison", (
            ("tile", "Tile", 85), ("date", "Acquisition", 100), ("status", "Finding", 220),
            ("preferred_downloaded", "New file downloaded", 135), ("preferred_nasa", "NASA preferred filename", 450),
            ("best_recorded_ee", "Recorded EE input", 450)))
        self.versions.bind("<Double-1>", self.details)
        self.stage_table = ConsolidationCoveragePanel.table(book, "Completeness and gaps", (
            ("tile", "Tile", 80), ("state", "Current evidence / next stage", 260),
            ("version_finding", "Processing-version finding", 220), ("filename", "Exact preferred NASA input", 520)))
        self.stage_table.bind("<Double-1>", lambda _e: self.show_stage_details())
        ttk.Label(self, textvariable=self.status, wraplength=850, justify="left").grid(row=5, column=0, sticky="ew", pady=5)
        self.bind("<Destroy>", self.destroyed, add="+")

    def context(self) -> tuple[str, str, str]:
        """Capture project identity on the Tk thread to reject stale worker results."""
        return (self.app.current_project_root_var.get().strip(), self.app.download_manifest_var.get().strip(),
                self.app.destination_var.get().strip())

    def ledger(self) -> history.CatalogHistory:
        """Require an explicitly opened project before any history operation."""
        root, manifest, _ = self.context()
        if not root or not manifest:
            raise ValueError("Open a project first")
        return history.CatalogHistory(history.history_path(Path(manifest)))

    def work(self, label: str, job: Callable, done: Callable) -> None:
        """Run metadata work off Tk; errors clear potentially misleading results."""
        if self.busy:
            return
        self.busy = True
        context, generation = self.context(), self.generation
        self.status.set(label)
        def run() -> None:
            try:
                self.events.put((context, generation, done, job(), None))
            except Exception as exc:
                self.events.put((context, generation, done, None, str(exc)))
        threading.Thread(target=run, daemon=True, name="nasa-catalog-history").start()
        self.poll_id = self.after(150, self.poll)

    def poll(self) -> None:
        """Apply worker results only to their originating project/view generation."""
        self.poll_id = None
        try:
            context, generation, done, result, error = self.events.get_nowait()
        except queue.Empty:
            self.poll_id = self.after(150, self.poll)
            return
        self.busy = False
        if context != self.context() or generation != self.generation:
            return
        if error:
            self.report = {}
            self.verification_report = {}
            self.stage_table.delete(*self.stage_table.get_children())
            self.versions.delete(*self.versions.get_children())
            self.summary.set("")
            self.status.set("Check stopped: " + error + ". Saved search pages remain in history; refresh to inspect.")
        else:
            done(result)

    def destroyed(self, event: tk.Event) -> None:
        """Cancel this view's poll, never a production worker."""
        if event.widget == self and self.poll_id:
            self.after_cancel(self.poll_id)
            self.poll_id = None

    def clear(self) -> None:
        """Discard old-project scope, inventory and comparison display."""
        self.generation += 1
        self.inventory, self.report, self.scan_rows = {}, {}, {}
        self.verification_report = {}
        self.pixel_stop.set()
        for variable in (self.start, self.end, self.tiles, self.collections, self.summary):
            variable.set("")
        for tree in (self.searches, self.versions, self.stage_table):
            tree.delete(*tree.get_children())
        self.status.set("Use project scope to load this project's history.")

    def use_project(self) -> None:
        """Copy settings; do not change acquisition windows used by the pipeline."""
        self.start.set(self.app.download_start_date_var.get())
        self.end.set(self.app.download_end_date_var.get())
        self.tiles.set(", ".join(self.app.current_download_tiles()))
        collection = self.app.selected_download_collection_short_name()
        names = [collection]
        if collection == "SWOT_L2_HR_Raster_100m_D":
            names.append("SWOT_L2_HR_Raster_D")
        self.collections.set(", ".join(names))
        self.refresh()

    def refresh(self) -> None:
        """Read the separate ledger without network access or creating it."""
        try:
            ledger = self.ledger()
            self.work("Reading NASA search history...", ledger.scans, self.show_history)
        except ValueError as exc:
            self.status.set(str(exc))

    def show_history(self, rows: list[dict]) -> None:
        """Render every retained search, including failed and interrupted scans."""
        self.searches.delete(*self.searches.get_children())
        self.scan_rows = {r["id"]: r for r in rows}
        for r in rows:
            scope = json.loads(r["scope"])
            self.searches.insert("", "end", iid=r["id"], values=(r["started"], r["status"], r["candidates"],
                scope.get("start", "") + " — " + scope.get("end", ""), ", ".join(scope.get("collections", [scope.get("origin", "")]))))
        if rows:
            self.searches.selection_set(rows[0]["id"])
        self.status.set(f"{len(rows)} retained snapshots. Select a search to compare or export its complete filenames and decisions.")

    def scan(self) -> None:
        """Check historical metadata independently, with no raster transfer or config write."""
        try:
            from swot_download_tool import DownloadConfig, normalize_utm_tiles, validate_config, PRODUCT_FILTER_BEST
            ledger = self.ledger()
            collections = [n.strip() for n in self.collections.get().split(",") if n.strip()]
            if not collections or any(not n.startswith("SWOT_L2_HR_Raster") for n in collections):
                raise ValueError("Specify SWOT Raster NASA collection short names")
            config = DownloadConfig(collection_short_name=collections[0], start_date=self.start.get().strip(),
                end_date=self.end.get().strip(), utm_tiles=normalize_utm_tiles(self.tiles.get().split(",")),
                manifest_csv=Path(self.app.download_manifest_var.get()), report_csv=ledger.path.with_suffix(".unused.csv"),
                output_folder=Path(self.app.download_output_var.get()), product_version_filter=PRODUCT_FILTER_BEST)
            validate_config(config)
            def job() -> list[dict]:
                history.search_with_history(config, collections=collections)
                return ledger.scans()
            self.work("Checking NASA metadata for the specified historical dates; no files are downloaded...", job, self.show_history)
        except ValueError as exc:
            self.status.set(str(exc))

    def import_legacy(self) -> None:
        """Import retained provenance without pretending it is an original NASA snapshot."""
        try:
            ledger, root = self.ledger(), Path(self.context()[0])
            def job() -> list[dict]:
                history.import_legacy(ledger, history.read_project_snapshot(root))
                return ledger.scans()
            self.work("Importing retained filenames from a read-only project snapshot...", job, self.show_history)
        except ValueError as exc:
            self.status.set(str(exc))

    def inventory_ready(self, inventory: dict) -> None:
        """Bind the metadata snapshot to the original collection selected in the app."""
        if inventory.get("collection") != self.context()[2]:
            self.status.set("Inventory collection does not match this project's original EE collection.")
            self.inventory = {}
            return
        self.inventory = inventory
        from pipeline_verification import VerificationStore, save_inventory
        save_inventory(VerificationStore(self.ledger().path.parent), inventory)
        self.report = {}
        self.verification_report = {}
        self.stage_table.delete(*self.stage_table.get_children())
        self.versions.delete(*self.versions.get_children())
        self.summary.set("")
        self.status.set(f"Loaded {len(inventory.get('assets', [])):,} EE asset records. Select a historical search and compare.")

    def read_inventory(self) -> None:
        """Read original EE asset metadata without submitting or changing any asset."""
        import ee_consolidation as c
        try:
            self.ledger()
            collection = c.collection_id(self.context()[2])
            self.work("Reading EE metadata...", lambda: c.fetch_inventory(c.connect(collection.split("/")[1]), collection), self.inventory_ready)
        except ValueError as exc:
            self.status.set(str(exc))

    def load_inventory(self) -> None:
        """Load a previously saved full inventory for an offline comparison."""
        name = filedialog.askopenfilename(parent=self, title="Full original EE inventory", filetypes=[("JSON", "*.json")])
        if name:
            self.work("Loading EE inventory...", lambda: json.loads(Path(name).read_text(encoding="utf-8")), self.inventory_ready)

    def selected_scan(self) -> str:
        """Require an explicit snapshot, never silently mix different date scopes."""
        selected = self.searches.selection()
        if not selected:
            raise ValueError("Select a catalogue search in Search history")
        return selected[0]

    def compare(self) -> None:
        """Compare selected NASA history to loaded EE metadata and local lineage."""
        try:
            ledger, scan, root = self.ledger(), self.selected_scan(), Path(self.context()[0])
            inventory = self.inventory
            if not inventory or inventory.get("collection") != self.context()[2]:
                raise ValueError("Read or load this project's full EE inventory first")
            def job() -> dict:
                project = history.read_project_snapshot(root)
                represented, unresolved = history.represented_inputs(inventory, project["mosaic_manifest"])
                snapshot = ledger.snapshot(scan)
                scope = snapshot["scope"]
                consolidated, consolidation_warnings = history.approved_consolidated_inputs(
                    inventory["collection"], scope.get("start", ""), scope.get("end", ""))
                represented.extend(consolidated)
                downloaded = [r["file_name"] for r in project["download_manifest"] if r.get("downloaded", "").lower() == "yes"]
                report = history.compare_versions(snapshot, represented, inventory.get("finished_utc", ""), downloaded)
                report["unresolved_assets"] = unresolved + consolidation_warnings
                report["approved_consolidated_assets"] = sorted({p["asset_id"] for p in consolidated})
                report["consolidation_inventory_times"] = sorted({p["observed_utc"] for p in consolidated})
                if report["unresolved_assets"]:
                    report["current_complete_metadata"] = False
                report["collection"] = inventory["collection"]
                return report
            self.work("Comparing catalogue versions with recorded EE inputs...", job, self.show_comparison)
        except ValueError as exc:
            self.status.set(str(exc))

    def show_comparison(self, report: dict) -> None:
        """Keep incomplete/stale scope and unresolved lineage visible beside findings."""
        self.report = report
        self.versions.delete(*self.versions.get_children())
        for i, row in enumerate(report["rows"]):
            self.versions.insert("", "end", iid=str(i), values=[row.get(k, "") for k in self.versions["columns"]])
        self.summary.set("; ".join(f"{key.replace('_', ' ')}: {value:,}" for key, value in report["summary"].items()))
        qualifier = "Recent complete metadata for this scope" if report["current_complete_metadata"] else "Partial, stale, undated or unresolved metadata: review before acting"
        self.status.set(f"{qualifier}. Unresolved assets: {len(report.get('unresolved_assets', []))}. "
                        "These findings describe recorded inputs, not pixel correctness. Double-click a row for filenames and affected assets.")

    def details(self, event: Any = None) -> None:
        """Show the exact products and affected assets behind a version finding."""
        selected = self.versions.selection()
        if selected:
            row = self.report["rows"][int(selected[0])]
            ledger = self.ledger()
            self.work("Reading acquisition version history...", lambda: {**row, "retained_history": ledger.observation_history(row["identity"])},
                      lambda value: self.show_details(value))

    def show_details(self, value: dict) -> None:
        """Use a scrollable window for long original-product histories."""
        window = tk.Toplevel(self)
        window.title("Acquisition and processing-version history")
        window.geometry("1000x620")
        text = tk.Text(window, wrap="none")
        y = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        x = ttk.Scrollbar(window, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        y.pack(side="right", fill="y")
        x.pack(side="bottom", fill="x")
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(value, indent=2))
        text.configure(state="disabled")

    def export_history(self) -> None:
        """Export all candidate names, metadata revisions and version decisions."""
        try:
            ledger, scan = self.ledger(), self.selected_scan()
            name = filedialog.asksaveasfilename(parent=self, defaultextension=".json", initialfile="nasa_history.json")
            if name:
                def job() -> str:
                    Path(name).write_text(json.dumps(ledger.snapshot(scan), indent=2), encoding="utf-8")
                    return "Saved " + name
                self.work("Exporting retained history...", job, self.status.set)
        except ValueError as exc:
            self.status.set(str(exc))

    def export_comparison(self) -> None:
        """Export the full version report, including unknown assets and scope limits."""
        if not self.report:
            self.status.set("Run a version comparison first")
            return
        name = filedialog.asksaveasfilename(parent=self, defaultextension=".json", initialfile="nasa_version_comparison.json")
        if name:
            Path(name).write_text(json.dumps(self.report, indent=2), encoding="utf-8")
            self.status.set("Saved " + name)

    def reconcile_stages(self) -> None:
        """Persist exact-input accounting separately from spatial source-data gaps."""
        from pipeline_verification import VerificationStore, project_report, save_report
        try:
            ledger, scan, root = self.ledger(), self.selected_scan(), Path(self.context()[0])
            inventory = self.inventory
            if not inventory or inventory.get("collection") != self.context()[2]:
                raise ValueError("Read this project's EE inventory first")
            def job() -> dict:
                store = VerificationStore(ledger.path.parent)
                report = project_report(root, store, scan, inventory)
                report["saved_report"] = str(save_report(store, report))
                return report
            self.work("Reconciling exact NASA inputs through processing and active EE representations...", job, self.show_verification)
        except ValueError as exc:
            self.status.set(str(exc))

    def show_verification(self, report: dict) -> None:
        """Show acquisition accounting without claiming that every source pixel is valid."""
        self.verification_report = report
        self.stage_table.delete(*self.stage_table.get_children())
        for index, row in enumerate(report["rows"]):
            display = {**row, "state": stage_evidence_label(row.get("state", ""))}
            self.stage_table.insert("", "end", iid=str(index), values=[display.get(k, "") for k in self.stage_table["columns"]])
        summary, status = verification_view_text(report)
        self.summary.set(summary)
        self.status.set(status)

    def show_stage_details(self) -> None:
        """Explain the selected exact input and its retained evidence."""
        selected = self.stage_table.selection()
        if selected and self.verification_report:
            row = self.verification_report["rows"][int(selected[0])]
            explanation = STAGE_EVIDENCE.get(row.get("state"), ("", "Unrecognized evidence state; inspect the saved report."))[1]
            self.show_details({**row, "evidence_explanation": explanation})

    def export_verification(self) -> None:
        """Export scope, gaps, checks and a reviewable targeted repair queue."""
        if not self.verification_report:
            self.status.set("Reconcile all stages first")
            return
        name = filedialog.asksaveasfilename(parent=self, defaultextension=".json", initialfile="completeness_and_repair_queue.json")
        if name:
            Path(name).write_text(json.dumps(self.verification_report, indent=2), encoding="utf-8")
            self.status.set("Saved " + name + ". This queue does not automatically replace or delete assets.")

    def check_pixels(self) -> None:
        """Launch explicit native comparisons off the UI thread; sampled is never full."""
        from pipeline_verification import VerificationStore
        from native_pixel_verification import check_native_pixels
        if self.busy:
            return
        try:
            store = VerificationStore(self.ledger().path.parent)
            selected = self.stage_table.selection()
            if not selected or not self.verification_report:
                raise ValueError("Select inputs in Completeness and gaps after reconciliation")
            assets = {aid for i in selected for aid in self.verification_report["rows"][int(i)]["asset_ids"]}
            if not assets:
                raise ValueError("Selected inputs have no recorded EE asset to compare")
            config = self.app.build_config()
            python = Path(config.get("gdal", {}).get("python", ""))
            proofs = {p["path"]: p for p in store.artifacts().values() if p["stage"] == "mosaic"}
            local = {Path(path).stem: Path(path) for path in proofs}
            # Prefixes/suffixes and sanitized IDs need the actual upload mapping,
            # not an assumption that the EE basename equals the local filename.
            from project_database import read_project_rows
            upload_path = Path(config.get("artifacts", {}).get("report_csv", store.logs / "upload_report.csv"))
            uploaded = {r.get("asset_id"): Path(r["local_file"]) for r in read_project_rows(upload_path,"upload_report")
                        if r.get("local_file") in proofs}
            jobs = [(uploaded.get(aid,local.get(aid.rsplit("/", 1)[-1])), aid) for aid in sorted(assets)]
            if any(path is None for path, _ in jobs):
                raise ValueError("A selected asset has no matching local mosaic proof. Recreate its exact local reference first.")
            mode = self.pixel_mode.get()
            self.pixel_stop.clear()
            def job() -> dict:
                results = []
                for path, aid in jobs:
                    if self.pixel_stop.is_set():
                        break
                    results.append(check_native_pixels(store, path, aid, python, mode, stop=self.pixel_stop))
                return {"mode": mode, "checked": len(results), "planned": len(jobs), "results": results,
                        "note": "Local-to-EE comparison. Original-NetCDF extraction/replay is a separate acceptance test."}
            self.work(f"Running {mode} native-pixel checks on {len(jobs)} assets; EE assets are read-only...", job,
                      lambda result: (self.show_details(result), self.status.set("Pixel receipts saved. Refresh EE inventory and reconcile to update coverage.")))
        except ValueError as exc:
            self.status.set(str(exc))

    def check_originals(self) -> None:
        """Replay selected mosaics from original NetCDFs without changing any raster."""
        if self.busy:
            return
        from pipeline_verification import VerificationStore
        from gdal_runtime import build_gdal_runtime_env
        import subprocess
        import tempfile
        try:
            store = VerificationStore(self.ledger().path.parent)
            selected = self.stage_table.selection()
            if not selected or not self.verification_report:
                raise ValueError("Select inputs in Completeness and gaps first")
            names = {self.verification_report["rows"][int(i)]["filename"] for i in selected}
            outputs = sorted({p["path"] for p in store.artifacts().values() if p["stage"] == "mosaic" and names & set(p["sources"])})
            if not outputs:
                raise ValueError("No local mosaic receipts cover these inputs")
            python = Path(self.app.build_config().get("gdal", {}).get("python", ""))
            mode = self.pixel_mode.get()
            self.pixel_stop.clear()
            def job() -> dict:
                results = []
                for output in outputs:
                    if self.pixel_stop.is_set():
                        break
                    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log:
                        command = [str(python), str(Path(__file__).with_name("source_pixel_verification.py")),
                                   "--logs", str(store.logs), "--output", output, "--mode", mode]
                        child = subprocess.Popen(command, stdout=log, stderr=log, env=build_gdal_runtime_env(python),
                                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        while child.poll() is None:
                            if self.pixel_stop.wait(0.25):
                                child.terminate()
                                child.wait(timeout=10)
                                break
                        log.seek(0)
                        text = log.read()
                    results.append({"output": output, "return_code": child.returncode, "result": text[-12000:]})
                return {"planned": len(outputs), "checked": len(results), "mode": mode, "results": results,
                        "note": "Retain/reacquire the exact original NetCDF versions for this check. No rasters or EE assets are modified."}
            self.work(f"Comparing {len(outputs)} local mosaics against their original source variables...", job, self.show_details)
        except ValueError as exc:
            self.status.set(str(exc))
