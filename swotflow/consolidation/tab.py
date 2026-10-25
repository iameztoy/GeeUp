"""Independent Tk panel for post-ingestion consolidation; no workflow mutations."""
from __future__ import annotations

import json
import queue
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import swotflow.consolidation.core as engine
import swotflow.consolidation.update as updates
import swotflow.consolidation.register as registry
from swotflow.core.storage import consolidation_root


class ConsolidationTab:
    """Keep consolidation state and worker events separate from automation."""

    def __init__(self, parent: ttk.Frame, app: Any) -> None:
        # The host app supports 820 x 650 windows. Keep every action reachable.
        host = parent
        host.rowconfigure(0, weight=1)
        host.columnconfigure(0, weight=1)
        canvas = tk.Canvas(host, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(host, orient="horizontal", command=canvas.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        parent = ttk.Frame(canvas, padding=2)
        window = canvas.create_window(0, 0, window=parent, anchor="nw")
        parent.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=max(event.width, parent.winfo_reqwidth())))
        self.parent, self.app = parent, app
        self.busy = False
        self.events: queue.Queue = queue.Queue()
        self.inventory: dict | None = None
        self.existing_inventory: dict | None = None
        self.plan: dict | None = None
        self.plan_settings: tuple | None = None
        self.folder: Path | None = None
        self.status_folder: Path | None = None
        self.poll_id: str | None = None
        self.source = tk.StringVar(value=app.destination_var.get())
        self.project = tk.StringVar(value=self.source.get().split("/")[1] if self.source.get().startswith("projects/") else "")
        self.destination = tk.StringVar(value=self.source.get() + "_consolidated_pilot" if self.source.get() else "")
        self.lineage = tk.StringVar(value=app.mosaic_manifest_var.get())
        self.operation = tk.StringVar(value="Create new mosaics")
        self.existing_collection = tk.StringVar(value=self.destination.get())
        self.previous_plans = tk.StringVar(value=str(consolidation_root()))
        self.mode = tk.StringVar(value=engine.MODES["native"])
        self.rule = tk.StringVar(value=engine.RULES["lowest_quality"])
        self.start, self.end = tk.StringVar(), tk.StringVar()
        self.hemispheres, self.versions = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        self.quality = tk.StringVar(value="3")
        self.status = tk.StringVar(value="Read an EE inventory or load a saved inventory to estimate savings.")
        self.counts = tk.StringVar(value="No preview yet. Existing assets are retained; no deletion is available here.")
        self.comparison = tk.StringVar(value="")
        self.quota = tk.StringVar(value="Project quota: not queried. Asset-count savings do not estimate storage bytes.")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(5, weight=1)
        ttk.Label(parent, text="Earth Engine consolidation", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(parent, text="Combine stored images from the same UTC start date, cycle and pass. Preview first, then export selected groups to a separate collection.",
                  wraplength=1100).grid(row=1, column=0, sticky="w", pady=(3, 8))
        fields = ttk.LabelFrame(parent, text="Collection and grouping", padding=8)
        fields.grid(row=2, column=0, sticky="ew")
        fields.columnconfigure(1, weight=1)
        fields.columnconfigure(3, weight=1)
        self._entry(fields, 0, "Source collection", self.source, span=3)
        self._entry(fields, 1, "Export collection", self.destination, span=3)
        self._entry(fields, 2, "EE project", self.project)
        ttk.Button(fields, text="Use current project settings", command=self.use_project).grid(row=2, column=2, columnspan=2, sticky="w", padx=8)
        self._entry(fields, 3, "Mosaic lineage manifest", self.lineage, span=3)
        ttk.Label(fields, text="Grouping").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Combobox(fields, textvariable=self.mode, values=list(engine.MODES.values()), state="readonly", width=53).grid(row=4, column=1, sticky="ew", padx=6)
        ttk.Checkbutton(fields, text="Common grid: separate hemispheres", variable=self.hemispheres).grid(row=4, column=2, columnspan=2, sticky="w")
        ttk.Label(fields, text="Overlap rule").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Combobox(fields, textvariable=self.rule, values=list(engine.RULES.values()), state="readonly", width=45).grid(row=5, column=1, sticky="ew", padx=6)
        ttk.Checkbutton(fields, text="Require one identical known processing version", variable=self.versions).grid(row=5, column=2, columnspan=2, sticky="w")
        dates = ttk.Frame(fields)
        dates.grid(row=6, column=0, columnspan=4, sticky="ew", pady=4)
        for text, var in (("From UTC date", self.start), ("Through UTC date", self.end)):
            ttk.Label(dates, text=text).pack(side="left", padx=(0, 5))
            ttk.Entry(dates, textvariable=var, width=13).pack(side="left", padx=(0, 15))
        ttk.Label(dates, text="Blank = all dates; YYYY-MM-DD.  Maximum quality code").pack(side="left")
        ttk.Combobox(dates, textvariable=self.quality, values=["0", "1", "2", "3"], state="readonly", width=3).pack(side="left", padx=5)
        ttk.Label(fields, text="100 m output. Equal-quality ties use earliest acquisition start, then asset ID. Elevation, quality and source_index stay together.\n"
                  "Native mode keeps zone, hemisphere and grid alignment. Common grid uses EPSG:6933 and nearest-neighbour sampling; validate seams before scaling up.",
                  wraplength=1100).grid(row=7, column=0, columnspan=4, sticky="w", pady=4)
        ttk.Label(fields, text="Operation").grid(row=8, column=0, sticky="w", pady=3)
        ttk.Combobox(fields, textvariable=self.operation, values=["Create new mosaics", "Update existing mosaics"], state="readonly").grid(row=8, column=1, sticky="ew", padx=6)
        ttk.Button(fields, text="Load existing inventory JSON", command=self.load_existing_inventory).grid(row=8, column=2, columnspan=2, sticky="w")
        self._entry(fields, 9, "Existing mosaic collection", self.existing_collection, span=3)
        self._entry(fields, 10, "Previous plans folder", self.previous_plans)
        ttk.Button(fields, text="Browse plans...", command=self.choose_plans).grid(row=10, column=2, sticky="w", padx=6)
        ttk.Button(fields, text="Read consolidated inventory", command=self.read_consolidated_inventory).grid(row=10, column=3, sticky="w")
        ttk.Label(fields, text="Update mode reads both collections and saved plans. It extends matching date/pass/grid groups; changed rules or processing-version replacements require a rebuild.",
                  wraplength=1100).grid(row=11, column=0, columnspan=4, sticky="w", pady=4)
        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, sticky="ew", pady=8)
        for label, command in (("Read EE inventory", self.read_inventory), ("Load inventory JSON", self.load_inventory),
                               ("Calculate preview", self.preview), ("Save plan", self.save),
                               ("Open saved task folder", self.open_task_folder)):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 6))
        summary = ttk.LabelFrame(parent, text="Estimated asset reduction", padding=8)
        summary.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        for row, var in enumerate((self.counts, self.comparison, self.quota)):
            ttk.Label(summary, textvariable=var, wraplength=1100, justify="left").grid(row=row, column=0, sticky="w", pady=2)
        table = ttk.Frame(parent)
        table.grid(row=5, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        cols = ("action", "date", "cycle", "pass", "crs", "inputs", "saved", "versions", "lineage")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="extended", height=7)
        for name, label, width in zip(cols, ("Action", "UTC date", "Cycle", "Pass", "Output CRS", "Read assets", "Potential saving", "Processing versions", "Unknown lineage"), (65, 95, 55, 55, 110, 80, 115, 150, 110)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=40)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda event: self.details())
        ttk.Label(parent, text="Select groups with Ctrl/Shift; double-click for source IDs, times and processing history. Counts are metadata estimates, not a pixel-integrity check.",
                  wraplength=1100).grid(row=6, column=0, sticky="w", pady=4)
        exports = ttk.Frame(parent)
        exports.grid(row=7, column=0, sticky="ew")
        ttk.Button(exports, text="Export selected (max 10)", command=self.export).pack(side="left", padx=(0, 6))
        ttk.Button(exports, text="Refresh export status", command=self.refresh).pack(side="left", padx=(0, 6))
        ttk.Button(exports, text="View selected details", command=self.details).pack(side="left")
        ttk.Button(exports, text="Preview accounting / blocked groups", command=self.accounting).pack(side="left", padx=6)
        coverage_actions = ttk.Frame(parent)
        coverage_actions.grid(row=10, column=0, sticky="ew", pady=6)
        ttk.Button(coverage_actions, text="Update coverage register", command=self.update_register).pack(side="left")
        ttk.Label(coverage_actions, text="Uses loaded full inventories and saved plans. Local records only; review in Statistics > Consolidation.",
                  wraplength=820).pack(side="left", padx=8)
        ttk.Label(parent, text="Exports retain originals and use additional quota. Outputs remain unverified until compared. Consolidation cannot fix existing shifts, missing granules or inherited band mismatches.",
                  wraplength=1100, foreground="#904000").grid(row=8, column=0, sticky="w", pady=6)
        ttk.Label(parent, textvariable=self.status, wraplength=1100).grid(row=9, column=0, sticky="w")
        for var in (self.source, self.project, self.lineage, self.mode, self.rule, self.start, self.end, self.hemispheres, self.versions, self.quality,
                    self.operation, self.existing_collection, self.previous_plans):
            var.trace_add("write", self.changed)
        parent.bind("<Destroy>", self.destroyed, add="+")

    @staticmethod
    def _entry(parent: ttk.Frame, row: int, label: str, var: tk.StringVar, span: int = 1) -> None:
        """Place a labelled entry."""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, columnspan=span, sticky="ew", padx=6)

    def destroyed(self, event: tk.Event) -> None:
        """Cancel UI polling only; submitted EE jobs continue independently."""
        if event.widget == self.parent and self.poll_id is not None:
            self.parent.after_cancel(self.poll_id)
            self.poll_id = None

    def changed(self, *args: Any) -> None:
        """Mark the preview stale instead of exporting an old selection silently."""
        if self.plan is not None:
            self.status.set("Settings changed. Calculate a new preview before exporting.")

    def settings(self) -> tuple:
        """Capture all plan inputs on the Tk thread."""
        return (self.source.get().strip(), self.project.get().strip(), self.lineage.get().strip(), self.mode.get(),
                self.rule.get(), self.start.get().strip(), self.end.get().strip(), self.hemispheres.get(), self.versions.get(), self.quality.get(),
                self.operation.get(), self.existing_collection.get().strip(), self.previous_plans.get().strip())

    def options(self) -> engine.Options:
        """Translate controls to validated backend options."""
        options = engine.Options(mode=next(k for k, v in engine.MODES.items() if v == self.mode.get()),
                                 overlap=next(k for k, v in engine.RULES.items() if v == self.rule.get()),
                                 start=self.start.get().strip(), end=self.end.get().strip(),
                                 separate_hemispheres=self.hemispheres.get(), separate_versions=self.versions.get(),
                                 max_quality=int(self.quality.get()))
        options.validate()
        return options

    def use_project(self) -> None:
        """Copy context only; do not save or change the active project."""
        source = self.app.destination_var.get().strip()
        self.source.set(source)
        self.project.set(source.split("/")[1] if source.startswith("projects/") else "")
        self.lineage.set(self.app.mosaic_manifest_var.get())
        self.destination.set(source + "_consolidated_pilot" if source else "")
        self.existing_collection.set(self.destination.get())

    def choose_plans(self) -> None:
        """Choose the folder containing saved consolidation plan.json files."""
        path = filedialog.askdirectory(parent=self.parent, title="Previous consolidation plans folder")
        if path:
            self.previous_plans.set(path)

    def load_existing_inventory(self) -> None:
        """Load the previous mosaic inventory separately for offline previews."""
        path = filedialog.askopenfilename(parent=self.parent, title="Existing consolidated mosaic inventory", filetypes=[("JSON", "*.json")])
        if path:
            def done(value: dict) -> None:
                if not isinstance(value.get("assets"), list) or not value.get("collection"):
                    messagebox.showerror("Invalid inventory", "Expected collection and assets fields", parent=self.parent)
                    return
                self.existing_inventory = value
                self.existing_collection.set(value["collection"])
                self.plan = None
                self.tree.delete(*self.tree.get_children())
                self.status.set("Existing mosaic inventory loaded. Calculate a new preview.")
            self.work("Loading existing mosaic metadata...", lambda: json.loads(Path(path).read_text(encoding="utf-8")), done)

    def read_consolidated_inventory(self) -> None:
        """Read output metadata even in create mode, without submitting any task."""
        project, collection = self.project.get().strip(), self.existing_collection.get().strip()
        def done(value: dict) -> None:
            self.existing_inventory = value
            self.status.set("Consolidated inventory loaded. Update coverage register to reconcile Statistics and the map.")
        self.work("Reading consolidated EE metadata...", lambda: engine.fetch_inventory(engine.connect(project), collection, self.progress), done)

    def update_register(self) -> None:
        """Reconcile saved lineage and loaded inventories in a separate local database."""
        if self.busy:
            return
        source = self.source.get().strip()
        if not self.inventory or self.inventory.get("collection") != source:
            messagebox.showinfo("Coverage register", "Load the full source inventory first.", parent=self.parent)
            return
        path = registry.default_path(source)
        inventory = self.inventory
        existing = self.existing_inventory
        if existing and existing["collection"] != self.existing_collection.get().strip():
            messagebox.showinfo("Coverage register", "Reload the inventory for the selected existing mosaic collection.", parent=self.parent)
            return
        plans_path = Path(self.previous_plans.get().strip())
        lineage_path = self.lineage.get().strip()
        destination = self.destination.get().strip()
        previous_destination = self.existing_collection.get().strip()
        current_plan = self.plan if self.plan_settings == self.settings() else None
        def job() -> Path:
            register = registry.ReplacementRegister(path, source)
            # Explicit task destinations take precedence over UI defaults. Other
            # collections in the shared plans directory cannot contaminate scope.
            files = [plans_path] if plans_path.is_file() else sorted(plans_path.rglob("plan.json")) if plans_path.exists() else []
            for file in files:
                plan = json.loads(file.read_text(encoding="utf-8"))
                if plan.get("collection") != source or plan.get("algorithm") not in updates.SUPPORTED:
                    continue
                task_path = file.parent / "tasks.json"
                tasks = json.loads(task_path.read_text(encoding="utf-8")) if task_path.exists() else {}
                register.import_plan(plan, tasks.get("destination") or previous_destination)
            if current_plan:
                register.import_plan(current_plan, destination)
            lineage = engine.read_lineage(Path(lineage_path) if lineage_path else None)
            register.sync_inventory(inventory, lineage)
            if existing:
                register.sync_inventory(existing)
            return path
        def done(saved: Path) -> None:
            self.status.set(f"Replacement register updated: {saved}. No assets or active selections changed.")
            self.app.consolidation_coverage_panel.use_register(saved, source)
        self.work("Reconciling source observations, revisions and duplicate checks locally...", job, done)

    def work(self, label: str, job: Callable, finished: Callable) -> None:
        """Run network/large metadata work off Tk; dispatch results through a queue."""
        if self.busy:
            messagebox.showinfo("Consolidation is busy", "Wait for the current consolidation operation.", parent=self.parent)
            return
        self.busy = True
        self.status.set(label)
        def run() -> None:
            try:
                self.events.put(("done", (finished, job())))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=run, daemon=True, name="ee-consolidation").start()
        self.poll_id = self.parent.after(100, self.poll)

    def poll(self) -> None:
        """Process background events without making Tk calls in workers."""
        self.poll_id = None
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "progress":
                    self.status.set(value)
                else:
                    self.busy = False
                    if event == "error":
                        self.status.set("Consolidation stopped: " + value)
                        messagebox.showerror("Consolidation", value, parent=self.parent)
                    else:
                        callback, result = value
                        callback(result)
        except queue.Empty:
            if self.busy:
                self.poll_id = self.parent.after(100, self.poll)

    def progress(self, text: str) -> None:
        """Queue a status update from the worker."""
        self.events.put(("progress", text))

    def read_inventory(self) -> None:
        """Fetch metadata with the EE API; never start an export here."""
        project, source = self.project.get().strip(), self.source.get().strip()
        update, existing = self.operation.get() == "Update existing mosaics", self.existing_collection.get().strip()
        def job() -> dict:
            ee = engine.connect(project)
            if update:
                engine.validate_destination(source, existing)
            inventory = engine.fetch_inventory(ee, source, self.progress)
            if update:
                inventory["existing_inventory"] = engine.fetch_inventory(ee, existing, self.progress)
            return inventory
        self.work("Reading EE metadata...", job, self.inventory_ready)

    def load_inventory(self) -> None:
        """Load an offline inventory for a network-free estimate."""
        path = filedialog.askopenfilename(parent=self.parent, title="EE inventory JSON", filetypes=[("JSON", "*.json")])
        if path:
            self.work("Loading saved inventory...", lambda: json.loads(Path(path).read_text(encoding="utf-8")), self.inventory_ready)

    def inventory_ready(self, inventory: dict) -> None:
        """Keep inventory scope explicit, clearing previous plan and selection."""
        if not isinstance(inventory.get("assets"), list):
            messagebox.showerror("Invalid inventory", "Expected an assets list in the JSON file", parent=self.parent)
            return
        collection = inventory.get("collection") or self.source.get().strip()
        inventory = dict(inventory, collection=collection)
        # Project inventories may include other collections; select only direct children.
        inventory["assets"] = [a for a in inventory["assets"] if a.get("id", "").rsplit("/", 1)[0] == collection]
        self.source.set(collection)
        if inventory.get("existing_inventory"):
            self.existing_inventory = inventory["existing_inventory"]
            self.existing_collection.set(self.existing_inventory["collection"])
        self.inventory, self.plan, self.folder = inventory, None, None
        self.tree.delete(*self.tree.get_children())
        self.counts.set(f"Loaded {len(inventory['assets']):,} assets. Calculate preview to compare options.")
        self.comparison.set("")
        quota = inventory.get("quota", {}).get("asset_count") if inventory.get("quota") else None
        self.quota.set(f"Project count quota: {quota['usage']:,} / {quota['limit']:,}; {quota['limit'] - quota['usage']:,} free at inventory time."
                       if quota else "Project quota unavailable in this inventory. Export checks fresh destination quota; byte use is not estimated.")
        self.status.set("Inventory loaded. No cloud assets changed.")

    def preview(self) -> None:
        """Compute all grouping counts and a detailed plan for the selected mode."""
        try:
            settings, options = self.settings(), self.options()
            if not self.inventory or self.inventory["collection"] != settings[0]:
                raise ValueError("Load an inventory for the selected source collection first")
            if settings[10] == "Update existing mosaics" and (not self.existing_inventory or self.existing_inventory["collection"] != settings[11]):
                raise ValueError("Read both EE inventories or load an existing-mosaic inventory for the selected collection")
        except ValueError as exc:
            messagebox.showerror("Preview", str(exc), parent=self.parent)
            return
        inventory = self.inventory
        existing_inventory = self.existing_inventory
        def job() -> dict:
            lineage = engine.read_lineage(Path(settings[2]) if settings[2] else None)
            if settings[10] == "Update existing mosaics":
                assert existing_inventory is not None
                plan = updates.plan_updates(inventory["assets"], settings[0], existing_inventory["assets"], settings[11],
                                            updates.read_plans(Path(settings[12])), options, lineage)
                plan["existing_inventory_utc"] = existing_inventory.get("finished_utc", "unknown")
                plan["comparison"] = []
            else:
                plan = engine.plan_assets(inventory["assets"], settings[0], options, lineage)
                plan["comparison"] = engine.option_comparison(inventory["assets"], settings[0], options, lineage)
            plan["inventory_utc"] = inventory.get("finished_utc", "unknown")
            return plan
        def done(plan: dict) -> None:
            self.plan, self.plan_settings, self.folder = plan, settings, None
            self.show_plan()
            self.status.set("Preview ready. No exports submitted." if settings == self.settings() else "Settings changed while calculating. Recalculate before exporting.")
        self.work("Grouping metadata and preserving source history...", job, done)

    def show_plan(self) -> None:
        """Display net and temporary counts, plus all mergeable groups."""
        assert self.plan
        s = self.plan["summary"]
        self.counts.set(f"{s['inventory_assets']:,} current assets → {s['eventual_assets']:,} after verified replacement: potential saving {s['potential_reduction']:,} ({s['reduction_percent']}%).\n"
                        f"{s['new_mosaics']:,} new mosaics; {s['singletons']:,} singletons retained; {s['excluded']:,} excluded; {s['outside_dates']:,} outside dates.\n"
                        f"While keeping originals: {s['temporary_assets_with_originals']:,} image assets in the inventoried inputs plus proposed outputs, plus a destination collection if new.")
        self.comparison.set("Potential savings by grouping: " + " | ".join(
            f"{r['mode']}: {r['potential_reduction']:,} ({r['reduction_percent']}%)" for r in self.plan["comparison"]))
        if self.plan.get("operation") == "update_existing":
            self.comparison.set(f"Extend {s['extended_groups']:,} groups; create {s['created_groups']:,}; unchanged {s['unchanged_groups']:,}; "
                                f"already represented inputs {s['already_represented']:,}; blocked groups {s['blocked_groups']:,}. "
                                "Use Preview accounting for exclusions and rebuild reasons. Existing grouping rules must be retained.")
        quota = (self.inventory or {}).get("quota")
        if quota and quota.get("asset_count"):
            count = quota["asset_count"]
            free = count["limit"] - count["usage"]
            extra = s["new_mosaics"] + 1
            self.quota.set(f"Project quota at inventory time: {count['usage']:,} / {count['limit']:,}; {free:,} slots free. "
                           f"Full plan needs {s['new_mosaics']:,} new images + up to one collection. " +
                           ("The full plan does not fit while keeping all originals; plan staged verification/retirement separately." if extra > free else
                            "Count headroom is sufficient at this snapshot; storage bytes and other queued work still matter."))
        self.tree.delete(*self.tree.get_children())
        for g in self.plan["groups"]:
            self.tree.insert("", "end", iid=g["id"], values=(g.get("action", "create"), g["date"], g["cycle"], g["pass"], g["crs"], len(engine.contributors(g)), g["savings"], ", ".join(g["versions"]), g["unknown_lineage"]))

    def accounting(self) -> None:
        """Expose unchanged inputs and blocked groups even when nothing can export."""
        if not self.plan:
            return
        dialog = tk.Toplevel(self.parent)
        dialog.title("Consolidation preview accounting")
        box = tk.Text(dialog, wrap="word", width=110, height=28)
        box.pack(fill="both", expand=True)
        value = {k: self.plan[k] for k in ("summary", "blocked", "already_represented", "unchanged", "superseded_revisions", "excluded", "notes") if k in self.plan}
        box.insert("1.0", json.dumps(value, indent=2))
        box.configure(state="disabled")

    def current_plan(self) -> dict:
        """Reject stale UI settings and concurrent operations."""
        if self.busy or not self.plan or self.plan_settings != self.settings():
            raise ValueError("Wait for any operation and calculate an up-to-date preview first")
        return self.plan

    def plan_folder(self) -> Path:
        """Allocate a private local output folder, separate from project manifests."""
        if self.folder is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            self.folder = consolidation_root() / stamp
        self.status_folder = None
        return self.folder

    def save(self) -> None:
        """Save a durable plan, group CSV and lineage before processing."""
        try:
            plan, folder = self.current_plan(), self.plan_folder()
        except ValueError as exc:
            messagebox.showinfo("Save plan", str(exc), parent=self.parent)
            return
        self.work("Saving plan and lineage...", lambda: engine.save_plan(folder, plan), lambda path: self.status.set(f"Saved plan and groups.csv: {path}"))

    def details(self) -> None:
        """Show source history or exclusions in a copyable text window."""
        if not self.plan:
            return
        ids = set(self.tree.selection())
        data = [g for g in self.plan["groups"] if g["id"] in ids] if ids else {"excluded": self.plan["excluded"], "notes": self.plan["notes"]}
        window = tk.Toplevel(self.parent)
        window.title("Consolidation sources and processing history")
        text = tk.Text(window, wrap="word", width=110, height=32)
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(data, indent=2))
        text.configure(state="disabled")

    def export(self) -> None:
        """Confirm a concrete selected batch, then create only new output assets."""
        try:
            plan = self.current_plan()
            destination = engine.validate_destination(plan["collection"], self.destination.get())
            ids = list(self.tree.selection())
            if not 1 <= len(ids) <= engine.MAX_BATCH:
                raise ValueError(f"Select 1–{engine.MAX_BATCH} groups from the preview")
            project, folder = self.project.get().strip(), self.plan_folder()
        except ValueError as exc:
            messagebox.showinfo("Export selected", str(exc), parent=self.parent)
            return
        selected = [g for g in plan["groups"] if g["id"] in ids]
        count = sum(len(engine.contributors(g)) for g in selected)
        summary = "\n".join(f"{g['date']} C{g['cycle']:03} P{g['pass']:03} {g['crs']}: {len(engine.contributors(g))} raster inputs, {len(g['sources'])} original stored sources" for g in selected)
        if not messagebox.askyesno("Export selected consolidation groups",
                f"Create {len(ids)} new mosaics from {count} source assets in:\n{destination}\n\n{summary}\n\n"
                f"Grouping: {engine.MODES[plan['options']['mode']]}\nOverlap: {engine.RULES[plan['options']['overlap']]}\n"
                "Original assets remain. A missing destination collection will be created. Exports use additional quota and do not repair existing integrity/completeness issues.\n\n"
                "Outputs require comparison before any separate retirement decision. Submit this batch?", parent=self.parent):
            return
        self.work("Checking source fingerprints, quota and destination...",
                  lambda: engine.submit_groups(engine.connect(project), plan, ids, destination, folder, self.progress), self.task_status)

    def task_status(self, receipt: dict) -> None:
        """Report submission/completion separately from scientific verification."""
        counts = Counter(r["state"] for r in receipt["tasks"].values())
        self.status.set("Exports: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) +
                        f". Completion is not scientific verification. Records: {self.folder}")

    def open_task_folder(self) -> None:
        """Reconnect status tracking after a restart without re-submitting tasks."""
        folder = filedialog.askdirectory(parent=self.parent, title="Folder containing consolidation tasks.json")
        if folder:
            path = Path(folder)
            if not (path / "tasks.json").is_file():
                messagebox.showerror("Task folder", "No tasks.json in this folder", parent=self.parent)
                return
            # A tracked folder is separate from the current preview output folder.
            self.status_folder = path
            self.refresh()

    def refresh(self) -> None:
        """Read task status only; do not touch other application or audit jobs."""
        folder = getattr(self, "status_folder", None) or self.folder
        if not folder or not (folder / "tasks.json").is_file():
            messagebox.showinfo("Export status", "Submit a batch or open a saved task folder first.", parent=self.parent)
            return
        project = self.project.get().strip()
        self.work("Reading saved export task status...", lambda: engine.refresh_tasks(engine.connect(project), folder),
                  lambda receipt: self.status.set("Saved exports: " + ", ".join(f"{k}: {v}" for k, v in Counter(r["state"] for r in receipt["tasks"].values()).items()) + f". Records: {folder}"))
