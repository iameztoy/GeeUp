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
                ("Export version comparison", self.export_comparison))):
            ttk.Button(actions, text=label, command=command).grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 6), pady=2)
        ttk.Checkbutton(self, text="Record NASA history for future Download/Automation searches (save project to retain setting)",
                        variable=app.catalog_history_enabled_var).grid(row=2, column=0, sticky="w")
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
        for variable in (self.start, self.end, self.tiles, self.collections, self.summary):
            variable.set("")
        for tree in (self.searches, self.versions):
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
        self.report = {}
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
