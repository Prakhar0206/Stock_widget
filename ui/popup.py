"""HIGH-alert pop-up: a separate topmost window (outside the desktop layer) with a Got it button.
The Notifier decides when it closes (10 s) and when it repeats."""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from ui.common import BG, FG, FONT, FONT_B, PRIO_COLORS, button


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
                 justify="left").pack(fill="x", padx=14, pady=(12, 2))
        tk.Label(w, text="\n".join(lines[1:]), bg=colour, fg=FG, font=FONT, anchor="w", justify="left",
                 wraplength=460).pack(fill="x", padx=14)
        button(w, "Got it", lambda: on_ack(), bg="#111111").pack(anchor="e", padx=14, pady=10)
        w.update_idletasks()
        sw = self.root.winfo_screenwidth()
        w.geometry(f"+{max(0, (sw - w.winfo_reqwidth()) // 2)}+60")
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
