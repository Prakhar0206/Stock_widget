"""HIGH-alert pop-up: a separate topmost window (outside the desktop layer) with a Got it button.
The Notifier decides when it closes (10 s) and when it repeats."""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from ui.common import FG, FONT, PRIO_COLORS, button

try:
    from desktop_layer import work_area_for_point
except ImportError:
    work_area_for_point = None


class AlertPopup:
    def __init__(self, root, make_window: Optional[Callable] = None):
        """make_window(toplevel): optional Windows hook that makes it no-activate / topmost."""
        self.root, self.make_window = root, make_window
        self.win: Optional[tk.Toplevel] = None

    def show(self, text: str, priority: str, seconds: int, on_ack: Callable) -> None:
        self.close()
        colour = PRIO_COLORS.get(priority, "#7F1D1D")
        w = self.win = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=colour, highlightthickness=2, highlightbackground="#F87171" if priority == "HIGH" else "#FBBF24")
        lines = text.split("\n")
        tk.Label(w, text=lines[0], bg=colour, fg=FG, font=("Segoe UI", 12, "bold"), anchor="w",
                 justify="left", wraplength=460).pack(fill="x", padx=14, pady=(12, 2))
        tk.Label(w, text="\n".join(lines[1:]), bg=colour, fg=FG, font=FONT, anchor="w", justify="left",
                 wraplength=460).pack(fill="x", padx=14)
        button(w, "Got it", lambda: on_ack(), bg="#111111").pack(anchor="e", padx=14, pady=10)
        w.update_idletasks()
        req_w, req_h = w.winfo_reqwidth(), w.winfo_reqheight()
        l, t, r, b = 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if work_area_for_point is not None:
            try:
                wl, wt, wr, wb = work_area_for_point(self.root.winfo_rootx(), self.root.winfo_rooty())
                if wr > wl and wb > wt:
                    l, t, r, b = wl, wt, wr, wb
            except Exception:
                pass
        x = max(l, min(l + (r - l - req_w) // 2, r - req_w))
        y = max(t, min(t + 60, b - req_h))
        w.geometry(f"+{x}+{y}")
        if self.make_window:
            try:
                self.make_window(w)
            except Exception:
                pass
        w.lift()

    def close(self) -> None:
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None
