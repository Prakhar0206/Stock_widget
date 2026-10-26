"""Small Tk helpers shared by the widget, editor and pop-up (dark palette, no ttk theming)."""
from __future__ import annotations

import tkinter as tk
from decimal import Decimal
from typing import Callable, Optional

from models import ValidationError

BG, BG2, FG, MUTED = "#202020", "#2D2D2D", "#EEEEEE", "#A3A3A3"
BTN, ACCENT = "#3A3A3A", "#3B82F6"
GREEN, RED, AMBER, GREY, DIM = "#4ADE80", "#F87171", "#FBBF24", "#A3A3A3", "#6B7280"
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
PRIO_COLORS = {"HIGH": "#7F1D1D", "MEDIUM": "#78350F", "LOW": "#1F2937"}
TRANSPARENT_KEY = "#010203"    # colour made fully transparent on Windows (never used for text)


def label(parent, text="", fg=FG, bold=False, **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", BG), fg=fg, font=FONT_B if bold else FONT, anchor="w", **kw)


def button(parent, text, cmd: Callable, **kw):
    return tk.Button(parent, text=text, command=cmd, bg=kw.pop("bg", BTN), fg=FG, activebackground="#4B4B4B",
                     activeforeground=FG, relief="flat", bd=0, padx=10, pady=4, font=FONT, cursor="hand2", **kw)


def entry(parent, var: tk.StringVar, width=10, **kw):
    return tk.Entry(parent, textvariable=var, width=width, bg=BG2, fg=FG, insertbackground=FG,
                    relief="flat", font=FONT, **kw)


def check(parent, text, var: tk.BooleanVar, cmd: Optional[Callable] = None):
    return tk.Checkbutton(parent, text=text, variable=var, command=cmd, bg=BG, fg=FG, selectcolor=BG2,
                          activebackground=BG, activeforeground=FG, font=FONT, anchor="w")


def menu(parent, var: tk.StringVar, values, cmd: Optional[Callable] = None, width=9):
    m = tk.OptionMenu(parent, var, *values, command=(lambda _v: cmd()) if cmd else None)
    m.config(bg=BTN, fg=FG, activebackground="#4B4B4B", activeforeground=FG, relief="flat", bd=0,
             highlightthickness=0, font=FONT, width=width)
    m["menu"].config(bg=BG2, fg=FG, font=FONT)
    return m


def parse_dec(text: str, what: str, *, blank_ok=False) -> Optional[Decimal]:
    t = (text or "").strip().replace(",", "")
    if not t:
        if blank_ok:
            return None
        raise ValidationError(f"{what}: enter a number.")
    try:
        return Decimal(t)
    except Exception:
        raise ValidationError(f"{what}: '{text}' is not a number.")


def fmt_pct(p: Decimal) -> str:
    return f"{'+' if p >= 0 else '-'}{abs(p).normalize():f}%".replace("+0%", "0%")


class TkTimers:
    """Adapter for the notifier: schedule(ms, fn) / cancel(handle) on the Tk loop."""
    def __init__(self, root):
        self.root = root

    def schedule(self, ms, fn):
        return self.root.after(int(ms), fn)

    def cancel(self, handle):
        try:
            self.root.after_cancel(handle)
        except Exception:
            pass
