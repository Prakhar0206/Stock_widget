"""Unacknowledged Alerts Inbox UI (Item 6.1): clean dark-card dialog for missed live & offline alerts."""
from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from market_clock import IST
from positions import q2
from ui.common import (
    ACCENT,
    AMBER,
    BG,
    BG2,
    BTN,
    DIM,
    FG,
    FONT,
    GREEN,
    MUTED,
    PRIO_COLORS,
    button,
    label,
)
from ui.editor import place_near_widget


def _fmt_price(raw) -> str:
    if raw is None or raw == "":
        return "—"
    try:
        d = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except Exception:
        return "—"
    if not d.is_finite() or d <= 0:
        return "—"
    return f"${q2(d):.2f}"


def _fmt_trigger_time(iso_raw: str) -> str:
    if not iso_raw:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_raw).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%d %b %H:%M IST")
    except ValueError:
        return str(iso_raw)


class AlertInboxDialog(tk.Toplevel):
    """Compact, scrollable dark-themed Inbox showing unacknowledged live & offline alerts."""

    def __init__(
        self,
        parent,
        ctrl,
        on_edit_rule: Optional[Callable[[str, str], None]] = None,
        on_change: Optional[Callable[[], None]] = None,
        place_anchor=None,
    ):
        super().__init__(parent, bg=BG)
        self.ctrl = ctrl
        self._on_edit_rule = on_edit_rule
        self._on_change = on_change
        self.cards: list[dict] = []

        self.title("Alerts Inbox — Unacknowledged")
        self.configure(bg=BG)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=12, pady=(12, 8))
        label(top, "🔔 Alerts Inbox", fg=FG, bold=True, bg=BG).pack(side="left")
        self.lbl_summary = label(top, "", fg=MUTED, bg=BG)
        self.lbl_summary.pack(side="left", padx=(10, 0))
        button(top, "Close", self._close, bg=BTN).pack(side="right")
        self.btn_ack_all = button(top, "✓ Acknowledge All", self._ack_all, bg="#166534")
        self.btn_ack_all.pack(side="right", padx=(0, 8))

        self.wrap = tk.Frame(self, bg=BG)
        self.wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.canvas = tk.Canvas(self.wrap, bg=BG, highlightthickness=0, bd=0, width=540, height=300)
        self.sb = tk.Scrollbar(self.wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner.bind("<Configure>", lambda _e: self._sync_scrollregion())
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.bind("<MouseWheel>", self._on_wheel)

        self.refresh()
        place_near_widget(self, place_anchor or parent)

    def _on_canvas_configure(self, e) -> None:
        self.canvas.itemconfigure(self._win_id, width=max(320, e.width))
        self._sync_scrollregion()

    def _sync_scrollregion(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self.inner.update_idletasks()
            cw = max(320, self.canvas.winfo_width() or 540)
            ch = max(1, self.canvas.winfo_height() or 300)
            req_h = max(0, self.inner.winfo_reqheight())
            self.canvas.configure(scrollregion=(0, 0, cw, max(req_h, ch)))
            if req_h <= ch:
                if self.sb.winfo_manager():
                    self.sb.pack_forget()
                self.canvas.yview_moveto(0)
            else:
                if not self.sb.winfo_manager():
                    self.sb.pack(side="right", fill="y")
        except tk.TclError:
            pass

    def _on_wheel(self, e) -> None:
        if not e.delta:
            return
        try:
            if self.inner.winfo_reqheight() <= max(1, self.canvas.winfo_height()):
                return
            steps = int(-e.delta / 120) or (-1 if e.delta > 0 else 1)
            self.canvas.yview_scroll(steps, "units")
        except tk.TclError:
            pass

    def _close(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _notify_parent(self) -> None:
        if callable(self._on_change):
            try:
                self._on_change()
            except Exception:
                pass

    def _ack_one(self, event_id: str) -> None:
        if self.ctrl.acknowledge_alert(event_id):
            self.refresh()
            self._notify_parent()

    def _ack_all(self) -> None:
        if self.ctrl.acknowledge_all_alerts("all"):
            self.refresh()
            self._notify_parent()

    def _rearm(self, symbol: str, alert_id: str) -> None:
        if self.ctrl.rearm_alert(symbol, alert_id):
            self.refresh()
            self._notify_parent()

    def _edit_rule(self, symbol: str, alert_id: str) -> None:
        if callable(self._on_edit_rule):
            self._on_edit_rule(symbol, alert_id)

    def refresh(self) -> None:
        if not self.winfo_exists():
            return
        records = self.ctrl.unacked_alerts("all")
        total = len(records)

        self.lbl_summary.config(
            text=f"{total} unacknowledged" if total > 0 else "All caught up",
            fg=AMBER if total > 0 else GREEN,
        )
        if total > 0:
            self.btn_ack_all.config(state="normal", bg="#166534", fg=FG)
        else:
            self.btn_ack_all.config(state="disabled", bg=BG2, fg=DIM)

        for child in list(self.inner.winfo_children()):
            child.destroy()
        self.cards.clear()

        if not records:
            empty = tk.Frame(self.inner, bg=BG2, highlightbackground="#383838", highlightthickness=1)
            empty.pack(fill="x", padx=2, pady=8)
            label(
                empty,
                "✓ All caught up — no unacknowledged alerts.",
                fg=GREEN,
                bold=True,
                bg=BG2,
            ).pack(anchor="w", padx=14, pady=(12, 4))
            label(
                empty,
                "Missed live pop-ups and offline laptop-closed alerts appear here.",
                fg=MUTED,
                bg=BG2,
                wraplength=490,
                justify="left",
            ).pack(anchor="w", padx=14, pady=(0, 12))
            self._sync_scrollregion()
            return

        for rec in records:
            self._build_card(rec)
        self._sync_scrollregion()

    def _build_card(self, rec: dict) -> None:
        prio = rec.get("priority", "MEDIUM")
        is_off = bool(rec.get("offline"))
        sym = rec.get("symbol", "")
        aid = rec.get("alert_id", "")
        eid = rec.get("event_id", "")

        card = tk.Frame(
            self.inner,
            bg=BG2,
            highlightbackground="#444444",
            highlightthickness=1,
        )
        card.pack(fill="x", padx=2, pady=4)

        head = tk.Frame(card, bg=BG2)
        head.pack(fill="x", padx=10, pady=(8, 4))

        prio_bg = PRIO_COLORS.get(prio, "#374151")
        tk.Label(
            head,
            text=f" {prio} ",
            bg=prio_bg,
            fg=FG,
            font=("Segoe UI", 8, "bold"),
            padx=4,
            pady=1,
        ).pack(side="left")

        title = sym if not rec.get("name") else f"{sym} · {rec['name']}"
        label(head, title, fg=FG, bold=True, bg=BG2).pack(side="left", padx=(8, 6))

        if is_off:
            tk.Label(
                head,
                text=" Offline ",
                bg="#1E3A8A",
                fg="#93C5FD",
                font=("Segoe UI", 8, "bold"),
                padx=4,
                pady=1,
            ).pack(side="left")

        sess = f" · {rec['session']}" if rec.get("session") else ""
        t_lbl = _fmt_trigger_time(rec.get("trigger_time", "")) + sess
        tk.Label(head, text=t_lbl, bg=BG2, fg=MUTED, font=FONT).pack(side="right")

        cond = rec.get("condition") or rec.get("operator") or ""
        status_txt = rec.get("alert_status", "")
        cond_line = f"{cond}   [{status_txt}]" if status_txt and status_txt != "Active" else cond
        label(card, cond_line, fg=FG, bg=BG2, wraplength=500, justify="left").pack(
            fill="x", padx=10, pady=(0, 6)
        )

        foot = tk.Frame(card, bg=BG2)
        foot.pack(fill="x", padx=10, pady=(0, 8))

        trig_p = _fmt_price(rec.get("trigger_price"))
        peak_p = _fmt_price(rec.get("peak_price"))
        live_p = rec.get("live_price_text", "—")
        parts = [f"Triggered {trig_p}"]
        if is_off and peak_p != "—" and peak_p != trig_p:
            parts.append(f"Peak {peak_p}")
        parts.append(f"Now {live_p}")
        label(foot, " · ".join(parts), fg=MUTED, bg=BG2).pack(side="left")

        btns: dict[str, tk.Button] = {}
        b_ack = button(foot, "✓ Ack", lambda e=eid: self._ack_one(e), bg=BTN)
        b_ack.pack(side="right")
        btns["ack"] = b_ack

        if rec.get("rule_exists") and callable(self._on_edit_rule):
            b_edit = button(foot, "Edit", lambda s=sym, a=aid: self._edit_rule(s, a), bg=BTN)
            b_edit.pack(side="right", padx=(0, 6))
            btns["edit"] = b_edit

        if rec.get("can_rearm"):
            b_rearm = button(foot, "↻ Re-arm", lambda s=sym, a=aid: self._rearm(s, a), bg=ACCENT)
            b_rearm.pack(side="right", padx=(0, 6))
            btns["rearm"] = b_rearm

        self.cards.append({"event_id": eid, "frame": card, "buttons": btns, "rec": rec})
