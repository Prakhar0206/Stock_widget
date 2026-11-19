"""Centered desktop overlay alert pop-up with clean visual hierarchy.
The Notifier decides when it closes (10 s) and when it repeats."""
from __future__ import annotations

import re
import tkinter as tk
from typing import Callable, Optional

from ui.common import FG

try:
    from desktop_layer import work_area_for_point
except ImportError:
    work_area_for_point = None

_STOCK_HEAD_RE = re.compile(
    r"^\[([A-Z]+)\]\s+([A-Z0-9.\-]+)(?:\s+·\s+(.+?))?\s+·\s+(\$[0-9,]+(?:\.[0-9]+)?)\s+·\s+(.+?)$"
)
_SESS_TAIL_RE = re.compile(r"^(.*?)\s*\((PH|RH|AH)\)$")

_POPUP_THEMES = {
    "HIGH": {
        "bg": "#1A1A1D",
        "footer": "#232328",
        "divider": "#2E2E36",
        "accent": "#EF4444",
        "border": "#EF4444",
        "pill_bg": "#3B1518",
        "pill_fg": "#FCA5A5",
        "cta_bg": "#EF4444",
        "cta_active": "#DC2626",
        "cta_fg": "#FFFFFF",
        "badge": "HIGH ALERT",
    },
    "MEDIUM": {
        "bg": "#1A1A1D",
        "footer": "#232328",
        "divider": "#2E2E36",
        "accent": "#F59E0B",
        "border": "#D97706",
        "pill_bg": "#3A2810",
        "pill_fg": "#FCD34D",
        "cta_bg": "#F59E0B",
        "cta_active": "#D97706",
        "cta_fg": "#111111",
        "badge": "ALERT",
    },
    "CUSTOM": {
        "bg": "#1A1A1D",
        "footer": "#232328",
        "divider": "#2E2E36",
        "accent": "#3B82F6",
        "border": "#3B82F6",
        "pill_bg": "#172554",
        "pill_fg": "#93C5FD",
        "cta_bg": "#3B82F6",
        "cta_active": "#2563EB",
        "cta_fg": "#FFFFFF",
        "badge": "CUSTOM ALERT",
    },
    "LOW": {
        "bg": "#1A1A1D",
        "footer": "#232328",
        "divider": "#2E2E36",
        "accent": "#64748B",
        "border": "#475569",
        "pill_bg": "#1E293B",
        "pill_fg": "#CBD5E1",
        "cta_bg": "#475569",
        "cta_active": "#334155",
        "cta_fg": "#FFFFFF",
        "badge": "INFO",
    },
}


def _parse_popup_text(text: str, priority: str) -> dict:
    """Extract structured display fields from formatted notification text."""
    raw_lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not raw_lines:
        raw_lines = [f"[{priority}] Alert"]

    head = raw_lines[0]
    m = _STOCK_HEAD_RE.match(head)
    is_stock = bool(m)
    symbol = name = price = time_text = headline = ""

    if m:
        _, symbol, opt_name, price, raw_time = m.groups()
        name = (opt_name or "").strip()
        sm = _SESS_TAIL_RE.match(raw_time.strip())
        if sm:
            time_text = f"{sm.group(1).strip()} · {sm.group(2)}"
        else:
            time_text = raw_time.strip()
    else:
        headline = head

    body_lines: list[str] = []
    extra_lines: list[str] = []
    source_line = ""

    for ln in raw_lines[1:]:
        if ln.startswith("Also ") or ln.startswith("... and "):
            extra_lines.append(ln)
        elif ln.startswith("Source "):
            clean = ln.rstrip(".")
            if clean.startswith("Source "):
                clean = clean[7:]
            source_line = clean.replace(", data age ", " · ")
        else:
            body_lines.append(ln)

    return {
        "is_stock": is_stock,
        "symbol": symbol,
        "name": name,
        "price": price,
        "time_text": time_text,
        "headline": headline,
        "body_lines": body_lines,
        "extra_lines": extra_lines,
        "source_line": source_line,
    }


class AlertPopup:
    def __init__(self, root, make_window: Optional[Callable] = None):
        """make_window(toplevel): optional Windows hook that makes it no-activate / topmost."""
        self.root, self.make_window = root, make_window
        self.win: Optional[tk.Toplevel] = None

    def show(self, text: str, priority: str, seconds: int, on_ack: Callable) -> None:
        self.close()
        theme = _POPUP_THEMES.get(priority, _POPUP_THEMES["HIGH"])
        bg = theme["bg"]
        footer_bg = theme["footer"]
        divider = theme["divider"]
        parsed = _parse_popup_text(text, priority)

        w = self.win = tk.Toplevel(self.root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=bg, highlightthickness=1, highlightbackground=theme["border"])

        shell = tk.Frame(w, bg=bg)
        shell.pack(fill="both", expand=True)

        # Left urgency stripe
        tk.Frame(shell, bg=theme["accent"], width=5).pack(side="left", fill="y")

        right_col = tk.Frame(shell, bg=bg)
        right_col.pack(side="left", fill="both", expand=True)

        content = tk.Frame(right_col, bg=bg, padx=18, pady=14)
        content.pack(fill="both", expand=True)

        # Guarantee clean balanced card width >= 440px
        tk.Frame(content, bg=bg, width=420, height=1).pack(fill="x")

        # Top row: urgency badge + alert name | time + close x
        top = tk.Frame(content, bg=bg)
        top.pack(fill="x", pady=(0, 8))

        tk.Label(
            top,
            text=theme["badge"],
            bg=theme["pill_bg"],
            fg=theme["pill_fg"],
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=2,
        ).pack(side="left")

        if parsed["name"]:
            tk.Label(
                top,
                text=parsed["name"],
                bg=bg,
                fg="#A1A1AA",
                font=("Segoe UI", 10),
            ).pack(side="left", padx=(8, 0))

        btn_x = tk.Button(
            top,
            text="✕",
            command=lambda: on_ack(),
            bg=bg,
            fg="#8E8E9A",
            activebackground=footer_bg,
            activeforeground=FG,
            relief="flat",
            bd=0,
            padx=6,
            pady=0,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        btn_x.pack(side="right", padx=(6, 0))

        if parsed["time_text"]:
            tk.Label(
                top,
                text=parsed["time_text"],
                bg=bg,
                fg="#94A3B8",
                font=("Segoe UI", 9),
            ).pack(side="right")

        # Hero row or fallback headline
        if parsed["is_stock"]:
            hero = tk.Frame(content, bg=bg)
            hero.pack(fill="x", pady=(0, 6))
            tk.Label(
                hero,
                text=parsed["symbol"],
                bg=bg,
                fg="#F8FAFC",
                font=("Segoe UI", 20, "bold"),
                anchor="w",
            ).pack(side="left")
            tk.Label(
                hero,
                text=parsed["price"],
                bg=bg,
                fg="#F8FAFC",
                font=("Segoe UI", 20, "bold"),
                anchor="e",
            ).pack(side="right")
        else:
            tk.Label(
                content,
                text=parsed["headline"],
                bg=bg,
                fg="#F8FAFC",
                font=("Segoe UI", 13, "bold"),
                anchor="w",
                justify="left",
                wraplength=460,
            ).pack(fill="x", pady=(0, 6))

        # Primary condition / message body
        body_text = "\n".join(parsed["body_lines"])
        if body_text:
            tk.Label(
                content,
                text=body_text,
                bg=bg,
                fg="#E2E8F0",
                font=("Segoe UI", 11),
                anchor="w",
                justify="left",
                wraplength=460,
            ).pack(fill="x", pady=(0, 4))

        # Extra merged events if present
        if parsed["extra_lines"]:
            tk.Frame(content, bg=divider, height=1).pack(fill="x", pady=(6, 6))
            tk.Label(
                content,
                text="\n".join(parsed["extra_lines"]),
                bg=bg,
                fg="#CBD5E1",
                font=("Segoe UI", 9),
                anchor="w",
                justify="left",
                wraplength=460,
            ).pack(fill="x")

        # Hairline divider + sleek footer bar
        tk.Frame(right_col, bg=divider, height=1).pack(fill="x")
        foot = tk.Frame(right_col, bg=footer_bg, padx=18, pady=10)
        foot.pack(fill="x")

        meta_parts = []
        if parsed["source_line"]:
            meta_parts.append(parsed["source_line"])
        meta_parts.append("Alt+A to dismiss")
        tk.Label(
            foot,
            text="  ·  ".join(meta_parts),
            bg=footer_bg,
            fg="#8E8E9A",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(side="left", pady=2)

        tk.Button(
            foot,
            text="Acknowledge",
            command=lambda: on_ack(),
            bg=theme["cta_bg"],
            fg=theme["cta_fg"],
            activebackground=theme["cta_active"],
            activeforeground=theme["cta_fg"],
            relief="flat",
            bd=0,
            padx=16,
            pady=5,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        ).pack(side="right")

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
        y = max(t, min(t + (b - t - req_h) // 2, b - req_h))

        # Apply tool/no-activate style before mapping, set explicit geometry, then re-enforce topmost after layout
        w.geometry(f"{req_w}x{req_h}+{x}+{y}")
        if self.make_window:
            try:
                self.make_window(w)
            except Exception:
                pass
        w.deiconify()
        w.geometry(f"{req_w}x{req_h}+{x}+{y}")
        w.update_idletasks()
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
