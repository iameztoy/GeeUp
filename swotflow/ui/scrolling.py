"""Vertical page scrolling without application-wide mouse-wheel bindings."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class VerticalScrolledFrame(ttk.Frame):
    """Keep a form reachable in a short viewport and preserve nested list scrolling."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0,
                                width=1, height=1, yscrollincrement=24)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._resize)
        self.canvas.bind("<Configure>", self._resize)
        self._wheel_tag = f"VerticalPageWheel:{id(self)}"
        self._wheel_bindings = {}
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._wheel_bindings[sequence] = self.bind_class(self._wheel_tag, sequence, self._wheel)
        self.bind("<Destroy>", self._destroy_bindings)

    def _resize(self, _event: tk.Event | None = None) -> None:
        """Fit the width; keep enough height for the entire form and results."""
        width = max(1, self.canvas.winfo_width())
        height = max(self.content.winfo_reqheight(), self.canvas.winfo_height())
        self.canvas.itemconfigure(self._window, width=width, height=height)
        self.canvas.configure(scrollregion=(0, 0, width, height))

    def bind_mousewheel(self) -> None:
        """Attach a local wheel tag after callers finish constructing the form."""
        pending = [self]
        while pending:
            widget = pending.pop()
            tags = widget.bindtags()
            if self._wheel_tag not in tags:
                widget.bindtags((tags[0], self._wheel_tag, *tags[1:]))
            pending.extend(widget.winfo_children())

    def _wheel(self, event: tk.Event) -> str | None:
        """Scroll settings/background; lists, tables and selectors handle themselves."""
        if event.widget.winfo_class() in {"Treeview", "Listbox", "Text", "TCombobox",
                                         "TSpinbox", "Spinbox", "Scrollbar", "TScrollbar"}:
            return None
        if event.state & 1:  # Preserve Shift-wheel behavior for nested horizontal views.
            return None
        if event.num in (4, 5):
            units = -3 if event.num == 4 else 3
        elif event.delta:
            divisor = 1 if self.tk.call("tk", "windowingsystem") == "aqua" else 120
            units = -max(1, abs(int(event.delta / divisor))) * (1 if event.delta > 0 else -1) * 3
        else:
            return None
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(units, "units")
        return "break"

    def _destroy_bindings(self, event: tk.Event) -> None:
        """Remove this page's bindings without changing other tabs' bindings."""
        if event.widget is self:
            for sequence, command in self._wheel_bindings.items():
                self.unbind_class(self._wheel_tag, sequence)
                self._root().deletecommand(command)
