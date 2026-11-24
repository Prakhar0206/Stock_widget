"""Unacknowledged Alerts Inbox UI (Item 6.1): dark-card review dialog for missed live & offline alerts."""
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
    FONT_B,
    GREEN,
    GREY,
    MUTED,
    PRIO_COLORS,
    RED,
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
    """Scrollable dark-themed Inbox showing unacknowledged live & offline-catchup alerts."""

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
        self._source_filter = "all"
        self._sym_var = tk.StringVar(value="ALL")
        self._filter_btns: dict[str, tk.Button] = {}
        self.cards: list[dict] = []

        self.title("Alerts Inbox — Unacknowledged")
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=12, pady=(12, 6))
        label(top, "🔔 Alerts Inbox", fg=FG, bold=True, bg=BG).pack(side="left")
        self.lbl_summary = label(top, "", fg=MUTED, bg=BG)
        self.lbl_summary.pack(side="left", padx=(10, 0))
        button(top, "Close", self._close, bg=BTN).pack(side="right")
        self.btn_ack_all = button(top, "✓ Acknowledge All", self._ack_all, bg="#166534")
        self.btn_ack_all.pack(side="right", padx=(0, 8))

        bar = tk.Frame(self, bg=BG2, highlightbackground="#383838", highlightthickness=1)
        bar.pack(fill="x", padx=12, pady=(0, 8))
        bar_inner = tk.Frame(bar, bg=BG2)
        bar_inner.pack(fill="x", padx=8, pady=6)
        for key, title in (("all", "All"), ("offline", "While Offline"), ("live", "Live")):
            b = tk.Button(
                bar_inner,
                text=title,
                command=lambda k=key: self._set_source(k),
                bg=ACCENT if key == "all" else BTN,
                fg=FG,
                activebackground="#4B4B4B",
                activeforeground=FG,
                relief="flat",
                bd=0,
                padx=10,
                pady=3,
                font=FONT,
                cursor="hand2",
            )
            b.pack(side="left", padx=(0, 6))
            self._filter_btns[key] = b

        label(bar_inner, "Ticker:", fg=MUTED, bg=BG2).pack(side="right", padx=(8, 4))
        self.sym_menu = tk.OptionMenu(bar_inner, self._sym_var, "ALL", command=lambda _v: self.refresh())
        self.sym_menu.config(
            bg=BTN,
            fg=FG,
            activebackground="#4B4B4B",
            activeforeground=FG,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=FONT,
            width=8,
        )
        self.sym_menu["menu"].config(bg=BG2, fg=FG, font=FONT)
        self.sym_menu.pack(side="right")

        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0, width=640, height=400)
        self.sb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.inner.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._win_id, width=max(320, e.width)),
        )
        self.bind("<MouseWheel>", self._on_wheel)

        self.refresh()
        place_near_widget(self, place_anchor or parent)

    def _on_wheel(self, e) -> None:
        if not e.delta:
            return
        steps = int(-e.delta / 120) or (-1 if e.delta > 0 else 1)
        try:
            self.canvas.yview_scroll(steps, "units")
        except tk.TclError:
            pass

    def _set_source(self, key: str) -> None:
        self._source_filter = key
        self.refresh()

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
        sym = None if self._sym_var.get() == "ALL" else self._sym_var.get()
        if self.ctrl.acknowledge_all_alerts(self._source_filter, sym):
            self.refresh()
            self._notify_parent()

    def _rearm(self, symbol: str, alert_id: str) -> None:
        if self.ctrl.rearm_alert(symbol, alert_id):
            self.refresh()
            self._notify_parent()

    def _edit_rule(self, symbol: str, alert_id: str) -> None:
        if callable(self._on_edit_rule):
            self._on_edit_rule(symbol, alert_id)

    def _sync_symbol_choices(self, all_records: list[dict]) -> None:
        syms = ["ALL"] + sorted({r.get("symbol", "") for r in all_records if r.get("symbol")})
        cur = self._sym_var.get()
        if cur not in syms:
            self._sym_var.set("ALL")
        m = self.sym_menu["menu"]
        m.delete(0, "end")
        for s in syms:
            m.add_command(
                label=s,
                command=lambda val=s: (self._sym_var.set(val), self.refresh()),
            )

    def refresh(self) -> None:
        if not self.winfo_exists():
            return
        summary = self.ctrl.unacked_summary()
        total = int(summary.get("total", 0))
        off_n = int(summary.get("offline", 0))
        live_n = int(summary.get("live", 0))

        self.lbl_summary.config(
            text=(
                f"{total} unacknowledged ({off_n} offline, {live_n} live)"
                if total > 0
                else "All caught up"
            ),
            fg=AMBER if total > 0 else GREEN,
        )
        labels = {
            "all": f"All ({total})",
            "offline": f"While Offline ({off_n})",
            "live": f"Live ({live_n})",
        }
        for key, btn in self._filter_btns.items():
            btn.config(
                text=labels[key],
                bg=ACCENT if self._source_filter == key else BTN,
            )

        all_recs = self.ctrl.unacked_alerts("all")
        self._sync_symbol_choices(all_recs)
        sym = None if self._sym_var.get() == "ALL" else self._sym_var.get()
        visible = self.ctrl.unacked_alerts(self._source_filter, sym)
        self.btn_ack_all.config(state="normal" if visible else "disabled")

        for child in list(self.inner.winfo_children()):
            child.destroy()
        self.cards.clear()

        if not visible:
            empty = tk.Frame(self.inner, bg=BG2, highlightbackground="#383838", highlightthickness=1)
            empty.pack(fill="x", padx=4, pady=12)
            label(
                empty,
                "✓ No unacknowledged alerts match this filter.",
                fg=GREEN,
                bold=True,
                bg=BG2,
            ).pack(anchor="w", padx=14, pady=(12, 4))
            label(
                empty,
                "Missed live popups and alerts triggered while your laptop was closed or asleep will appear here.",
                fg=MUTED,
                bg=BG2,
                wraplength=580,
                justify="left",
            ).pack(anchor="w", padx=14, pady=(0, 12))
            return

        for rec in visible:
            self._build_card(rec)

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
        card.pack(fill="x", padx=4, pady=5)

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

        src_text = "While Offline" if is_off else "Live"
        src_bg = "#1E3A8A" if is_off else "#14532D"
        src_fg = "#93C5FD" if is_off else "#86EFAC"
        tk.Label(
            head,
            text=f" {src_text} ",
            bg=src_bg,
            fg=src_fg,
            font=("Segoe UI", 8, "bold"),
            padx=4,
            pady=1,
        ).pack(side="left")

        sess = f" · {rec['session']}" if rec.get("session") else ""
        t_lbl = _fmt_trigger_time(rec.get("trigger_time", "")) + sess
        tk.Label(head, text=t_lbl, bg=BG2, fg=MUTED, font=FONT).pack(side="right")

        cond = rec.get("condition") or rec.get("operator") or ""
        status_txt = rec.get("alert_status", "")
        cond_line = f"{cond}   [{status_txt}]" if status_txt else cond
        label(card, cond_line, fg=FG, bg=BG2, wraplength=610, justify="left").pack(
            fill="x", padx=10, pady=(0, 3)
        )

        trig_p = _fmt_price(rec.get("trigger_price"))
        peak_p = _fmt_price(rec.get("peak_price"))
        live_p = rec.get("live_price_text", "—")
        parts = [f"Triggered: {trig_p}"]
        if is_off and peak_p != "—":
            parts.append(f"Offline Peak: {peak_p}")
        parts.append(f"Now: {live_p}")
        label(card, "   |   ".join(parts), fg=MUTED, bg=BG2).pack(fill="x", padx=10, pady=(0, 6))

        foot = tk.Frame(card, bg=BG2)
        foot.pack(fill="x", padx=10, pady=(0, 8))
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
