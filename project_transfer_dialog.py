"""Tkinter entry point for offline project relocation and validation."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from project_relocation import (
    project_root, relocate_project, restore_relocation, same_path,
    save_validation_report, validate_project,
)


class ProjectTransferDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, on_open: Callable[[Path], None], *, current_root: str = "", initial_path: str = "") -> None:
        super().__init__(parent)
        self.title("Relocate / Validate Project")
        self.geometry("860x640")
        self.minsize(650, 520)
        self.transient(parent)
        self.grab_set()
        self.on_open = on_open
        self.current_root = current_root
        self.busy = False
        self.validated_root: Path | None = None
        self.events: queue.Queue = queue.Queue()
        self.path_var = tk.StringVar(value=initial_path)
        self.stopped_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose the copied project.yaml at its final destination.")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        location = ttk.Frame(self, padding=12)
        location.grid(row=0, column=0, sticky="ew")
        location.columnconfigure(0, weight=1)
        ttk.Label(location, text="Project at destination").grid(row=0, column=0, sticky="w")
        ttk.Entry(location, textvariable=self.path_var).grid(row=1, column=0, sticky="ew", pady=5)
        self.browse_button = ttk.Button(location, text="Browse...", command=self.browse)
        self.browse_button.grid(row=1, column=1, padx=(8, 0))
        options = ttk.Frame(self, padding=(12, 0))
        options.grid(row=1, column=0, sticky="ew")
        options.columnconfigure(0, weight=1)
        ttk.Label(options, text="Additional data-folder mappings (optional; old folder | new folder)").grid(row=0, column=0, sticky="w")
        self.mappings_text = tk.Text(options, height=3, wrap="none")
        self.mappings_text.grid(row=1, column=0, sticky="ew", pady=5)
        ttk.Checkbutton(options, text="All project processing and upload processes are stopped", variable=self.stopped_var).grid(row=2, column=0, sticky="w")
        actions = ttk.Frame(self, padding=12)
        actions.grid(row=2, column=0, sticky="ew")
        self.action_buttons = []
        for index, (title, action) in enumerate((("Validate", "validate"), ("Relocate Copy", "relocate"), ("Restore Backup", "restore"))):
            button = ttk.Button(actions, text=title, command=lambda selected=action: self.start(selected))
            button.grid(row=0, column=index, padx=(0, 8))
            self.action_buttons.append(button)
        self.open_button = ttk.Button(actions, text="Open Project", command=self.open_validated, state=tk.DISABLED)
        self.open_button.grid(row=0, column=3)
        body = ttk.Frame(self, padding=(12, 0))
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.output = tk.Text(body, wrap="word", state=tk.DISABLED)
        self.output.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, command=self.output.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scrollbar.set)
        footer = ttk.Frame(self, padding=12, height=86)
        footer.grid(row=4, column=0, sticky="ew")
        footer.grid_propagate(False)
        footer.columnconfigure(0, weight=1)
        status = ttk.Label(footer, textvariable=self.status_var, width=60, wraplength=600)
        status.grid(row=0, column=0, sticky="ew")
        footer.bind("<Configure>", lambda event: status.configure(wraplength=max(250, event.width - 24)))
        self.progress = ttk.Progressbar(footer, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after_id = self.after(100, self.poll)

    def browse(self) -> None:
        selected = filedialog.askopenfilename(parent=self, title="Choose copied project.yaml", filetypes=[("SWOTFlow project", "project.yaml")])
        if selected:
            self.path_var.set(selected)
            self.open_button.configure(state=tk.DISABLED)

    def start(self, action: str) -> None:
        if self.busy:
            return
        if not self.stopped_var.get():
            messagebox.showwarning("Stop project work first", "Stop all project processing and upload processes, including separate terminals, before continuing.", parent=self)
            return
        try:
            if not self.path_var.get().strip():
                raise ValueError("Choose the destination project.yaml first.")
            root = project_root(self.path_var.get().strip())
            mappings = []
            for line in self.mappings_text.get("1.0", "end").splitlines():
                if line.strip():
                    old, new = line.split("|", 1)
                    mappings.append((old.strip(), new.strip()))
            if action != "validate" and self.current_root and same_path(root, self.current_root):
                raise ValueError("This project is currently open. Close SWOTFlow, launch with a different/no project, then relocate this copy.")
        except ValueError as exc:
            messagebox.showerror("Project setup", str(exc), parent=self)
            return
        if action in {"relocate", "restore"} and not messagebox.askyesno(
            "Confirm metadata operation", f"{action.title()} metadata only in:\n{root}\n\nRaster files and the original project will not be changed. Continue?", parent=self
        ):
            return
        self.busy = True
        self.validated_root = None
        self.open_button.configure(state=tk.DISABLED)
        for button in [*self.action_buttons, self.browse_button]:
            button.configure(state=tk.DISABLED)
        self.progress.start()
        self.status_var.set("Working...")
        threading.Thread(target=self.worker, args=(action, root, mappings), daemon=True).start()

    def worker(self, action: str, root: Path, mappings: list[tuple[str, str]]) -> None:
        try:
            progress = lambda message: self.events.put(("progress", message))
            if action == "restore":
                restore_relocation(root, progress)
                self.events.put(("done", "Backup restored. Relocate Copy again when ready."))
                return
            report = (relocate_project(root, mappings, confirmed_stopped=True, progress=progress)
                      if action == "relocate" else validate_project(root, progress))
            path = save_validation_report(report)
            self.events.put(("report", (root, report, path)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def poll(self) -> None:
        latest_progress = ""
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "progress":
                latest_progress = value
                continue
            self.busy = False
            self.progress.stop()
            for button in [*self.action_buttons, self.browse_button]:
                button.configure(state=tk.NORMAL)
            if kind == "report":
                root, report, path = value
                message = report.summary() + f"\n\nFull validation report: {path}\n\nOn the destination computer: configure GDAL, authenticate, run fresh Automation preflight, then Start Automation. Completed history is preserved."
                if report.valid:
                    self.validated_root = root
                    self.open_button.configure(state=tk.NORMAL)
            else:
                message = str(value)
            self.output.configure(state=tk.NORMAL)
            self.output.delete("1.0", "end")
            self.output.insert("1.0", message)
            self.output.configure(state=tk.DISABLED)
            self.status_var.set("Finished" if kind != "error" else "Failed; see details above")
            latest_progress = ""
        if latest_progress:
            self.status_var.set(latest_progress)
        self.after_id = self.after(100, self.poll)

    def open_validated(self) -> None:
        if self.validated_root is not None:
            root = self.validated_root
            self.close()
            self.on_open(root / "project.yaml")

    def close(self) -> None:
        if self.busy:
            messagebox.showwarning("Operation in progress", "Wait until relocation or validation finishes before closing.", parent=self)
            return
        self.after_cancel(self.after_id)
        self.grab_release()
        self.destroy()
