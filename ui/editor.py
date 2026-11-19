"""Edit-mode forms: StockEditor, TradingView-inspired AlertDialog, AddStockDialog, ApiKeysStatusDialog.
Alert creation/editing/toggling/deleting persist immediately via AppController (no 2-step Save trap).
"""
from __future__ import annotations

import uuid
import tkinter as tk
from datetime import datetime, timezone
from decimal import Decimal
from tkinter import messagebox
from typing import Callable, Optional

from engine import (
    compute_expires_at,
    default_alert_message,
    resolve_alert_targets,
)
from market_clock import IST
from models import (
    ALERT_OPERATORS,
    CATEGORIES,
    CHANNEL_OPERATORS,
    DEFAULT_LABEL_BY_CATEGORY,
    EXPIRATION_LABELS,
    EXPIRATION_PRESETS,
    LABELS,
    MOVING_OPERATORS,
    OPERATOR_LABELS,
    PCT_BASE_LABELS,
    PCT_BASES,
    PRIMARY_OPERATORS,
    PRIORITIES,
    SESSION_SCOPE_LABELS,
    SESSION_SCOPES,
    SOUND_DURATIONS,
    SOUND_TONES,
    TRIGGER_LABELS,
    TRIGGER_MODES,
    Alert,
    CustomNotification,
    StockConfig,
    TradeInfo,
    ValidationError,
    resolve_notification_spec,
)
from positions import D, manual_trade_summary, pct_from_price, price_from_pct, q2, resolve_pct_base
from ui.common import (
    ACCENT,
    AMBER,
    BG,
    BG2,
    BTN,
    FG,
    GREEN,
    MUTED,
    RED,
    button,
    check,
    entry,
    fmt_pct,
    label,
    menu,
    parse_dec,
)
from watchlist import parse_alert

try:
    from desktop_layer import work_area_for_point
except ImportError:
    work_area_for_point = None


_PRIO_CHOICES = {
    "LOW": "LOW (Toast + chime)",
    "MEDIUM": "MEDIUM (Popup + 5s sound)",
    "HIGH": "HIGH (Popup + 2m repeat alarm)",
    "CUSTOM": "CUSTOM...",
}
_PRIO_BY_TEXT = {v: k for k, v in _PRIO_CHOICES.items()}
_MODE_CHOICES = {"price": "Price ($)", "pct": "Percent (%)"}
_MODE_BY_TEXT = {v: k for k, v in _MODE_CHOICES.items()}
_DUR_LABELS = {0: "Once", 5: "5 seconds", 10: "10 seconds", 30: "30 seconds"}
_DUR_BY_TEXT = {v: k for k, v in _DUR_LABELS.items()}
_PLACEHOLDERS = ("{{ticker}}", "{{price}}", "{{target}}", "{{change_pct}}", "{{session}}", "{{time}}")


def new_alert_id(symbol: str) -> str:
    return f"{symbol.lower()}-{uuid.uuid4().hex[:6]}"


def alert_summary(a: Alert, stock: StockConfig, prev_close: Optional[Decimal] = None) -> str:
    op_lbl = OPERATOR_LABELS.get(a.operator, a.operator)
    _, _, tgt_txt, base_lbl = resolve_alert_targets(stock, a, prev_close)
    if a.operator in CHANNEL_OPERATORS:
        if a.condition_mode == "pct" and a.lower is not None and a.upper is not None:
            cond = f"{op_lbl} {a.lower}%..{a.upper}%" + (f" (${tgt_txt})" if tgt_txt else "")
        else:
            cond = f"{op_lbl} ${tgt_txt or '?'}"
    elif a.operator in MOVING_OPERATORS:
        cond = f"{op_lbl} {tgt_txt or a.value}"
    elif a.condition_mode == "pct" and a.value is not None:
        pstr = fmt_pct(q2(a.value))
        ref_note = f" vs {base_lbl}" if base_lbl else ""
        resolved = f" (${tgt_txt})" if tgt_txt else ""
        cond = f"{op_lbl} {pstr}{ref_note}{resolved}"
    else:
        cond = f"{op_lbl} ${tgt_txt or '?'}"

    trig = TRIGGER_LABELS.get(a.trigger, a.trigger)
    if a.trigger == "every_time":
        trig += f" ({a.cooldown_min}m)"
    scope = {"all": "All", "regular": "RH", "extended": "PH+AH"}.get(a.session_scope, "All")
    parts = [cond, a.priority, trig, scope]
    if a.name:
        parts.append(a.name)
    return " · ".join(parts)


def watchlist_full_message(count: int, cap: int) -> str:
    return (
        f"The watchlist already has {count} stocks (max_symbols = {cap}).\n\n"
        "To add more: raise max_symbols in config.json and keep the polling budget at or below "
        "48 calls per minute, where calls per minute = (stocks + 1) x 60 / poll_seconds "
        "(30 stocks at 60 s is 31 calls; 30 stocks at 30 s is 62 and is refused). Then restart the app."
    )


def place_near_widget(win: tk.Toplevel, anchor: tk.Misc) -> None:
    """Position `win` centered near `anchor` and clamped inside `anchor`'s monitor work area."""
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    try:
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    except Exception:
        sw, sh = 1920, 1080
    l, t, r, b = 0, 0, sw, sh
    ax = ay = aw = ah = 0
    anchor_ok = False
    if anchor is not None:
        try:
            ax, ay = int(anchor.winfo_rootx()), int(anchor.winfo_rooty())
            aw, ah = int(anchor.winfo_width()), int(anchor.winfo_height())
            anchor_ok = True
        except Exception:
            anchor_ok = False
    if work_area_for_point is not None:
        try:
            px, py = (ax + aw // 2, ay + ah // 2) if anchor_ok else (0, 0)
            wl, wt, wr, wb = work_area_for_point(px, py)
            if wr > wl and wb > wt:
                l, t, r, b = wl, wt, wr, wb
        except Exception:
            pass
    if anchor_ok and aw > 1 and ah > 1 and (ax, ay) != (0, 0):
        x = ax + (aw - w) // 2
        y = ay + (ah - h) // 2
    else:
        x = l + (r - l - w) // 2
        y = t + (b - t - h) // 2
    x = max(l, min(x, r - w))
    y = max(t, min(y, b - h))
    win.geometry(f"+{x}+{y}")


class ChoiceDialog(tk.Toplevel):
    """Prompt when a state condition is already satisfied: Fire now / Wait for next cross."""

    def __init__(self, master, text: str, on_choice: Callable[[Optional[str]], None]):
        super().__init__(master, bg=BG)
        self.title("Alert condition already met")
        self.attributes("-topmost", True)
        self._cb, self._done = on_choice, False
        label(self, text, wraplength=360, justify="left").pack(padx=16, pady=(14, 8))
        row = tk.Frame(self, bg=BG)
        row.pack(pady=(0, 12))
        button(row, "Fire now", lambda: self._finish("fire_now")).pack(side="left", padx=4)
        button(row, "Wait for next cross", lambda: self._finish("wait_cross"), bg=ACCENT).pack(side="left", padx=4)
        button(row, "Cancel", lambda: self._finish(None)).pack(side="left", padx=4)
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(None))
        self.bind("<Escape>", lambda e: self._finish(None))
        place_near_widget(self, master)
        self.grab_set()

    def _finish(self, choice):
        if not self.winfo_exists():
            return
        if not self._done:
            self._done = True
            self.destroy()
            self._cb(choice)


class AlertDialog(tk.Toplevel):
    """TradingView-inspired Create/Edit alert dialog with Price/%, 13 operators, Trigger,
    Expiration, Session Scope, < Edit message, and < Notifications (Priority + Custom)."""

    def __init__(
        self,
        master,
        ctrl,
        symbol: str,
        alert: Optional[Alert],
        on_done: Callable[[str, Alert, Optional[str]], None],
    ):
        super().__init__(master, bg=BG)
        self.ctrl = ctrl
        self.existing = alert
        self.on_done = on_done
        self._show_more = bool(alert and alert.operator not in PRIMARY_OPERATORS)
        self._msg_dirty = bool(alert and alert.message.strip())

        self.title(("Edit alert" if alert else "Create alert") + f" · {symbol}")
        self.attributes("-topmost", True)
        self.configure(bg=BG, highlightthickness=1, highlightbackground="#444444")

        symbols = ctrl.symbols() or [symbol]
        if symbol not in symbols:
            symbols.insert(0, symbol)
        self.v_symbol = tk.StringVar(value=symbol)

        a = alert
        stock = self._cur_stock()
        tick = self._cur_tick()
        live_price_str = f"{q2(tick.price):.2f}" if (tick and tick.price > 0) else ""

        init_mode = a.condition_mode if a else "price"
        self.v_cmode = tk.StringVar(value=_MODE_CHOICES.get(init_mode, "Price ($)"))
        init_op = a.operator if a else "crossing"
        self.v_op = tk.StringVar(value=OPERATOR_LABELS.get(init_op, "Crossing"))
        self.v_pct_base = tk.StringVar(value=PCT_BASE_LABELS.get(a.pct_base if a else "auto", PCT_BASE_LABELS["auto"]))
        self.v_ref_price = tk.StringVar(value="" if (not a or a.ref_price is None) else str(a.ref_price))

        if a is not None:
            v_init = "" if a.value is None else str(a.value)
            lo_init = "" if a.lower is None else str(a.lower)
            hi_init = "" if a.upper is None else str(a.upper)
        else:
            v_init = live_price_str if init_mode == "price" else "1.0"
            lo_init = live_price_str
            hi_init = live_price_str
        self.v_val = tk.StringVar(value=v_init)
        self.v_lower = tk.StringVar(value=lo_init)
        self.v_upper = tk.StringVar(value=hi_init)
        self.v_window = tk.StringVar(value=str(a.window_min if a else 5))

        init_trig = a.trigger if a else "once"
        self.v_trigger = tk.StringVar(value=TRIGGER_LABELS.get(init_trig, "Once only"))
        self.v_cd = tk.StringVar(value=str(a.cooldown_min if a else 1))

        init_exp = a.expiration if a else "open_ended"
        self.v_exp = tk.StringVar(value=EXPIRATION_LABELS.get(init_exp, "Open-ended"))
        custom_dt_init = ""
        if a and a.expiration == "custom" and a.expires_at:
            try:
                dt = datetime.fromisoformat(a.expires_at.replace("Z", "+00:00")).astimezone(IST)
                custom_dt_init = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                custom_dt_init = ""
        self.v_exp_custom = tk.StringVar(value=custom_dt_init)

        init_scope = a.session_scope if a else "all"
        self.v_scope = tk.StringVar(value=SESSION_SCOPE_LABELS.get(init_scope, SESSION_SCOPE_LABELS["all"]))

        init_prio = a.priority if a else ctrl.cfg.default_priority
        if init_prio not in PRIORITIES:
            init_prio = "MEDIUM"
        self.v_prio = tk.StringVar(value=_PRIO_CHOICES[init_prio])
        init_label = a.label if (a and a.label in LABELS) else DEFAULT_LABEL_BY_CATEGORY.get(stock.category, "WATCH")
        self.v_label = tk.StringVar(value=init_label)
        self.v_on = tk.BooleanVar(value=a.enabled if a else True)
        self.v_name = tk.StringVar(value=a.name if a else "")
        self.v_msg_text = a.message if (a and a.message) else ""

        spec = resolve_notification_spec(init_prio, a.custom_notification if a else None)
        self.v_notif_popup = tk.BooleanVar(value=spec.popup)
        self.v_notif_toast = tk.BooleanVar(value=spec.toast)
        self.v_notif_sound_on = tk.BooleanVar(value=(spec.sound_tone != "none"))
        self.v_notif_tone = tk.StringVar(value=spec.sound_tone if spec.sound_tone != "none" else "chime")
        self.v_notif_dur = tk.StringVar(value=_DUR_LABELS.get(spec.sound_seconds, "5 seconds"))
        self.v_notif_repeat = tk.BooleanVar(value=spec.repeat_until_ack)

        self._container = tk.Frame(self, bg=BG)
        self._container.pack(fill="both", expand=True, padx=16, pady=12)

        self.f_main = tk.Frame(self._container, bg=BG)
        self.f_msg = tk.Frame(self._container, bg=BG)
        self.f_notif = tk.Frame(self._container, bg=BG)

        self._build_main(symbols, is_edit=(alert is not None))
        self._build_msg_subview()
        self._build_notif_subview()

        self._show_view(self.f_main)
        self._sync_default_message()
        self._refresh_layout()
        self.bind("<Escape>", lambda _e: self.destroy())
        place_near_widget(self, master)

    # ---- context helpers -------------------------------------------------------------------
    def _cur_symbol(self) -> str:
        return self.v_symbol.get().strip().upper()

    def _cur_stock(self) -> StockConfig:
        sym = self._cur_symbol()
        return self.ctrl.stock(sym) or StockConfig(symbol=sym, category="WATCH")

    def _cur_tick(self):
        row = self.ctrl.rows.get(self._cur_symbol())
        return row.tick if row else None

    def _op_key(self) -> str:
        lbl = self.v_op.get()
        for k, v in OPERATOR_LABELS.items():
            if v == lbl:
                return k
        return "crossing"

    def _cmode_key(self) -> str:
        op = self._op_key()
        if op in ("moving_up_pct", "moving_down_pct"):
            return "pct"
        if op in ("moving_up", "moving_down"):
            return "price"
        return _MODE_BY_TEXT.get(self.v_cmode.get(), "price")

    def _pct_base_key(self) -> str:
        lbl = self.v_pct_base.get()
        for k, v in PCT_BASE_LABELS.items():
            if v == lbl:
                return k
        return "auto"

    def _trig_key(self) -> str:
        lbl = self.v_trigger.get()
        for k, v in TRIGGER_LABELS.items():
            if v == lbl:
                return k
        return "once"

    def _exp_key(self) -> str:
        lbl = self.v_exp.get()
        for k, v in EXPIRATION_LABELS.items():
            if v == lbl:
                return k
        return "open_ended"

    def _scope_key(self) -> str:
        lbl = self.v_scope.get()
        for k, v in SESSION_SCOPE_LABELS.items():
            if v == lbl:
                return k
        return "all"

    def _prio_key(self) -> str:
        return _PRIO_BY_TEXT.get(self.v_prio.get(), "MEDIUM")

    def _show_view(self, target: tk.Frame) -> None:
        for fr in (self.f_main, self.f_msg, self.f_notif):
            fr.pack_forget()
        target.pack(fill="both", expand=True)
        self.update_idletasks()

    # ---- main view -------------------------------------------------------------------------
    def _build_main(self, symbols: list[str], is_edit: bool) -> None:
        f = self.f_main
        hdr = tk.Frame(f, bg=BG)
        hdr.pack(fill="x", pady=(0, 8))
        label(hdr, "Edit alert on" if is_edit else "Create alert on", bold=True).pack(side="left")
        m_sym = menu(hdr, self.v_symbol, symbols, self._on_symbol_changed, width=8)
        if is_edit:
            m_sym.config(state="disabled")
        m_sym.pack(side="left", padx=(6, 0))
        check(hdr, "Enabled", self.v_on).pack(side="right")

        grid = tk.Frame(f, bg=BG)
        grid.pack(fill="x")

        r = 0
        # Row 0: Condition mode (Price / %) + % base ref
        label(grid, "Condition", fg=MUTED).grid(row=r, column=0, sticky="nw", pady=4, padx=(0, 10))
        cond_box = tk.Frame(grid, bg=BG)
        cond_box.grid(row=r, column=1, columnspan=3, sticky="w", pady=2)

        mode_row = tk.Frame(cond_box, bg=BG)
        mode_row.pack(fill="x", pady=(0, 4))
        self.m_cmode = menu(mode_row, self.v_cmode, list(_MODE_CHOICES.values()), self._on_cmode_changed, width=11)
        self.m_cmode.pack(side="left")
        self.f_pct_ref = tk.Frame(mode_row, bg=BG)
        label(self.f_pct_ref, "vs", fg=MUTED).pack(side="left", padx=(6, 4))
        self.m_pct_base = menu(self.f_pct_ref, self.v_pct_base, list(PCT_BASE_LABELS.values()), self._refresh_layout, width=23)
        self.m_pct_base.pack(side="left")
        self.e_ref = entry(self.f_pct_ref, self.v_ref_price, 8)
        self.e_ref.bind("<KeyRelease>", lambda _e: self._update_preview())

        # Operator selector + Show more/less button
        op_row = tk.Frame(cond_box, bg=BG)
        op_row.pack(fill="x", pady=(0, 4))
        self.m_op = menu(op_row, self.v_op, [OPERATOR_LABELS[k] for k in PRIMARY_OPERATORS], self._on_op_changed, width=19)
        self.m_op.pack(side="left")
        self.btn_more = button(op_row, "Show more ▾", self._toggle_more_ops, bg=BTN)
        self.btn_more.config(padx=8, pady=2)
        self.btn_more.pack(side="left", padx=(6, 0))
        self._rebuild_op_menu()

        # Value / Channel / Moving input row
        val_row = tk.Frame(cond_box, bg=BG)
        val_row.pack(fill="x", pady=(0, 2))

        self.f_single = tk.Frame(val_row, bg=BG)
        self.e_val = entry(self.f_single, self.v_val, 11)
        self.e_val.pack(side="left")
        self.e_val.bind("<KeyRelease>", lambda _e: self._on_value_edited())
        b_up = button(self.f_single, "▲", lambda: self._nudge(1), bg=BTN)
        b_up.config(padx=5, pady=1)
        b_up.pack(side="left", padx=(4, 2))
        b_dn = button(self.f_single, "▼", lambda: self._nudge(-1), bg=BTN)
        b_dn.config(padx=5, pady=1)
        b_dn.pack(side="left")

        self.f_channel = tk.Frame(val_row, bg=BG)
        label(self.f_channel, "Lower", fg=MUTED).pack(side="left", padx=(0, 4))
        self.e_lo = entry(self.f_channel, self.v_lower, 9)
        self.e_lo.pack(side="left", padx=(0, 10))
        self.e_lo.bind("<KeyRelease>", lambda _e: self._on_value_edited())
        label(self.f_channel, "Upper", fg=MUTED).pack(side="left", padx=(0, 4))
        self.e_hi = entry(self.f_channel, self.v_upper, 9)
        self.e_hi.pack(side="left")
        self.e_hi.bind("<KeyRelease>", lambda _e: self._on_value_edited())

        self.f_moving = tk.Frame(val_row, bg=BG)
        label(self.f_moving, "Amount", fg=MUTED).pack(side="left", padx=(0, 4))
        self.e_mov = entry(self.f_moving, self.v_val, 8)
        self.e_mov.pack(side="left", padx=(0, 10))
        self.e_mov.bind("<KeyRelease>", lambda _e: self._on_value_edited())
        label(self.f_moving, "Window (min)", fg=MUTED).pack(side="left", padx=(0, 4))
        self.e_win = entry(self.f_moving, self.v_window, 5)
        self.e_win.pack(side="left")
        self.e_win.bind("<KeyRelease>", lambda _e: self._on_value_edited())

        self.lbl_preview = label(cond_box, "", fg=MUTED, wraplength=390, justify="left")
        self.lbl_preview.pack(anchor="w", pady=(2, 4))

        # Row 1: Trigger
        r += 1
        label(grid, "Trigger", fg=MUTED).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
        trig_box = tk.Frame(grid, bg=BG)
        trig_box.grid(row=r, column=1, columnspan=3, sticky="w", pady=2)
        menu(trig_box, self.v_trigger, list(TRIGGER_LABELS.values()), self._refresh_layout, width=22).pack(side="left")
        self.f_cd = tk.Frame(trig_box, bg=BG)
        label(self.f_cd, "every", fg=MUTED).pack(side="left", padx=(6, 4))
        entry(self.f_cd, self.v_cd, 4).pack(side="left")
        label(self.f_cd, "min", fg=MUTED).pack(side="left", padx=(4, 0))

        # Row 2: Expiration
        r += 1
        label(grid, "Expiration", fg=MUTED).grid(row=r, column=0, sticky="nw", pady=4, padx=(0, 10))
        exp_box = tk.Frame(grid, bg=BG)
        exp_box.grid(row=r, column=1, columnspan=3, sticky="w", pady=2)
        exp_top = tk.Frame(exp_box, bg=BG)
        exp_top.pack(fill="x")
        menu(exp_top, self.v_exp, list(EXPIRATION_LABELS.values()), self._refresh_layout, width=24).pack(side="left")
        self.f_exp_custom = tk.Frame(exp_box, bg=BG)
        entry(self.f_exp_custom, self.v_exp_custom, 17).pack(side="left", pady=(3, 0))
        label(self.f_exp_custom, "YYYY-MM-DD HH:MM IST", fg=MUTED).pack(side="left", padx=(6, 0), pady=(3, 0))
        self.lbl_exp_hint = label(exp_box, "", fg=MUTED)
        self.lbl_exp_hint.pack(anchor="w")

        # Row 3: Sessions
        r += 1
        label(grid, "Sessions", fg=MUTED).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
        menu(grid, self.v_scope, list(SESSION_SCOPE_LABELS.values()), width=28).grid(
            row=r, column=1, columnspan=3, sticky="w", pady=2
        )

        # Row 4: Message summary link -> < Edit message
        r += 1
        label(grid, "Message", fg=MUTED).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
        msg_row = tk.Frame(grid, bg=BG)
        msg_row.grid(row=r, column=1, columnspan=3, sticky="ew", pady=2)
        self.lbl_msg_sum = label(msg_row, "", fg=FG, wraplength=300, justify="left")
        self.lbl_msg_sum.pack(side="left", fill="x", expand=True)
        b_msg = button(msg_row, "Edit ›", self._open_msg_subview, bg=BG2)
        b_msg.config(padx=8, pady=2)
        b_msg.pack(side="right")

        # Row 5: Notifications (Priority + Configure ›) & Label
        r += 1
        label(grid, "Notifications", fg=MUTED).grid(row=r, column=0, sticky="nw", pady=4, padx=(0, 10))
        notif_box = tk.Frame(grid, bg=BG)
        notif_box.grid(row=r, column=1, columnspan=3, sticky="ew", pady=2)
        prio_row = tk.Frame(notif_box, bg=BG)
        prio_row.pack(fill="x")
        menu(prio_row, self.v_prio, list(_PRIO_CHOICES.values()), self._on_prio_changed, width=26).pack(side="left")
        b_notif = button(prio_row, "Custom ›", self._open_notif_subview, bg=BG2)
        b_notif.config(padx=8, pady=2)
        b_notif.pack(side="left", padx=(6, 0))
        self.lbl_notif_sum = label(notif_box, "", fg=MUTED)
        self.lbl_notif_sum.pack(anchor="w", pady=(2, 0))

        r += 1
        label(grid, "Label", fg=MUTED).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
        menu(grid, self.v_label, LABELS, width=8).grid(row=r, column=1, sticky="w", pady=2)

        self.err = label(f, "", fg=RED, wraplength=430, justify="left")
        self.err.pack(fill="x", pady=(6, 2))

        foot = tk.Frame(f, bg=BG)
        foot.pack(fill="x", pady=(6, 0))
        button(foot, "Save" if is_edit else "Create", self._save, bg=ACCENT).pack(side="right", padx=(6, 0))
        button(foot, "Cancel", self.destroy).pack(side="right")

    # ---- sub-views -------------------------------------------------------------------------
    def _build_msg_subview(self) -> None:
        f = self.f_msg
        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", pady=(0, 10))
        b_back = button(bar, "< Edit message", lambda: self._show_view(self.f_main), bg=BG)
        b_back.config(font=("Segoe UI", 10, "bold"), padx=2)
        b_back.pack(side="left")

        label(f, "Alert name", fg=MUTED).pack(anchor="w", pady=(2, 2))
        entry(f, self.v_name, 42).pack(fill="x", pady=(0, 8))

        label(f, "Message", fg=MUTED).pack(anchor="w", pady=(2, 2))
        self.txt_msg = tk.Text(
            f,
            height=4,
            width=44,
            bg=BG2,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=("Segoe UI", 10),
            wrap="word",
        )
        self.txt_msg.pack(fill="x", pady=(0, 6))
        self.txt_msg.insert("1.0", self.v_msg_text)

        label(f, "Add placeholder:", fg=MUTED).pack(anchor="w", pady=(2, 3))
        ph_bar = tk.Frame(f, bg=BG)
        ph_bar.pack(fill="x", pady=(0, 10))
        for i, ph in enumerate(_PLACEHOLDERS):
            b = button(ph_bar, ph, lambda p=ph: self._insert_placeholder(p), bg=BG2)
            b.config(font=("Consolas", 8), padx=6, pady=2)
            b.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 4), pady=2)

        foot = tk.Frame(f, bg=BG)
        foot.pack(fill="x", pady=(4, 0))
        button(foot, "Apply", self._apply_msg_subview, bg=ACCENT).pack(side="right", padx=(6, 0))
        button(foot, "Cancel", lambda: self._show_view(self.f_main)).pack(side="right")

    def _build_notif_subview(self) -> None:
        f = self.f_notif
        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", pady=(0, 10))
        b_back = button(bar, "< Notifications", lambda: self._show_view(self.f_main), bg=BG)
        b_back.config(font=("Segoe UI", 10, "bold"), padx=2)
        b_back.pack(side="left")

        check(f, "Show desktop popup & flash row", self.v_notif_popup).pack(anchor="w", pady=3)
        check(f, "Show Windows toast notification", self.v_notif_toast).pack(anchor="w", pady=3)
        check(f, "Play sound", self.v_notif_sound_on, self._sync_notif_subview).pack(anchor="w", pady=(6, 2))

        srow = tk.Frame(f, bg=BG)
        srow.pack(anchor="w", padx=(22, 0), pady=(0, 6))
        tones = [t for t in SOUND_TONES if t != "none"]
        self.m_tone = menu(srow, self.v_notif_tone, tones, width=9)
        self.m_tone.pack(side="left", padx=(0, 8))
        self.m_dur = menu(srow, self.v_notif_dur, list(_DUR_LABELS.values()), width=11)
        self.m_dur.pack(side="left")

        check(f, "Repeat every 2m until acknowledged", self.v_notif_repeat).pack(anchor="w", pady=4)

        foot = tk.Frame(f, bg=BG)
        foot.pack(fill="x", pady=(10, 0))
        button(foot, "Apply", self._apply_notif_subview, bg=ACCENT).pack(side="right", padx=(6, 0))
        button(foot, "Cancel", lambda: self._show_view(self.f_main)).pack(side="right")

    # ---- interactive updates ---------------------------------------------------------------
    def _rebuild_op_menu(self) -> None:
        ops = ALERT_OPERATORS if self._show_more else PRIMARY_OPERATORS
        cur_k = self._op_key()
        if cur_k not in ops:
            cur_k = ops[0]
            self.v_op.set(OPERATOR_LABELS[cur_k])
        m = self.m_op["menu"]
        m.delete(0, "end")
        for k in ops:
            lbl = OPERATOR_LABELS[k]
            m.add_command(label=lbl, command=lambda v=lbl: (self.v_op.set(v), self._on_op_changed()))
        self.btn_more.config(text="Show less ▴" if self._show_more else "Show more ▾")

    def _toggle_more_ops(self) -> None:
        self._show_more = not self._show_more
        self._rebuild_op_menu()
        self._refresh_layout()

    def _on_symbol_changed(self, *_):
        if self.existing is None:
            sym = self._cur_symbol()
            self.title(f"Create alert · {sym}")
            tick = self._cur_tick()
            if self._cmode_key() == "price" and tick and tick.price > 0:
                pstr = f"{q2(tick.price):.2f}"
                self.v_val.set(pstr)
                self.v_lower.set(pstr)
                self.v_upper.set(pstr)
            self.v_label.set(DEFAULT_LABEL_BY_CATEGORY.get(self._cur_stock().category, "WATCH"))
        self._sync_default_message()
        self._refresh_layout()

    def _on_cmode_changed(self, *_):
        if self.existing is None:
            if self._cmode_key() == "pct":
                self.v_val.set("2.0")
                self.v_lower.set("-2.0")
                self.v_upper.set("2.0")
            else:
                tick = self._cur_tick()
                pstr = f"{q2(tick.price):.2f}" if (tick and tick.price > 0) else ""
                self.v_val.set(pstr)
                self.v_lower.set(pstr)
                self.v_upper.set(pstr)
        self._sync_default_message()
        self._refresh_layout()

    def _on_op_changed(self, *_):
        op = self._op_key()
        if op in ("moving_up_pct", "moving_down_pct"):
            self.v_cmode.set("Percent (%)")
            if self.existing is None:
                self.v_val.set("2.0")
        elif op in ("moving_up", "moving_down"):
            self.v_cmode.set("Price ($)")
            if self.existing is None:
                self.v_val.set("2.00")
        elif op in CHANNEL_OPERATORS and self.existing is None:
            tick = self._cur_tick()
            if self._cmode_key() == "price" and tick and tick.price > 0:
                p = tick.price
                self.v_lower.set(f"{q2(p * Decimal('0.98')):.2f}")
                self.v_upper.set(f"{q2(p * Decimal('1.02')):.2f}")
        self._sync_default_message()
        self._refresh_layout()

    def _on_value_edited(self) -> None:
        self._sync_default_message()
        self._update_preview()

    def _nudge(self, direction: int) -> None:
        try:
            cur = Decimal(self.v_val.get().strip())
        except Exception:
            cur = Decimal(0)
        if self._cmode_key() == "pct":
            step = Decimal("0.5")
        else:
            step = Decimal("0.10") if cur < 20 else Decimal("1.00")
        nxt = cur + step * direction
        if self._cmode_key() == "price" and nxt <= 0:
            nxt = Decimal("0.01")
        self.v_val.set(f"{nxt:.2f}" if self._cmode_key() == "price" else f"{nxt.normalize()}")
        self._on_value_edited()

    def _on_prio_changed(self, *_):
        k = self._prio_key()
        if k == "CUSTOM":
            self._open_notif_subview()
            return
        spec = resolve_notification_spec(k)
        self.v_notif_popup.set(spec.popup)
        self.v_notif_toast.set(spec.toast)
        self.v_notif_sound_on.set(spec.sound_tone != "none")
        self.v_notif_tone.set(spec.sound_tone if spec.sound_tone != "none" else "chime")
        self.v_notif_dur.set(_DUR_LABELS.get(spec.sound_seconds, "5 seconds"))
        self.v_notif_repeat.set(spec.repeat_until_ack)
        self._refresh_layout()

    def _open_msg_subview(self) -> None:
        self.txt_msg.delete("1.0", "end")
        self.txt_msg.insert("1.0", self.v_msg_text or self._preview_default_message())
        self._show_view(self.f_msg)
        self.txt_msg.focus_set()

    def _insert_placeholder(self, ph: str) -> None:
        self.txt_msg.insert("insert", ph)
        self.txt_msg.focus_set()

    def _apply_msg_subview(self) -> None:
        self.v_msg_text = self.txt_msg.get("1.0", "end-1c").strip()
        self._msg_dirty = bool(self.v_msg_text)
        self._refresh_layout()
        self._show_view(self.f_main)

    def _sync_notif_subview(self) -> None:
        st = "normal" if self.v_notif_sound_on.get() else "disabled"
        self.m_tone.config(state=st)
        self.m_dur.config(state=st)

    def _open_notif_subview(self) -> None:
        self._sync_notif_subview()
        self._show_view(self.f_notif)

    def _apply_notif_subview(self) -> None:
        self.v_prio.set(_PRIO_CHOICES["CUSTOM"])
        self._refresh_layout()
        self._show_view(self.f_main)

    def _current_spec(self) -> CustomNotification:
        k = self._prio_key()
        if k != "CUSTOM":
            return resolve_notification_spec(k)
        return CustomNotification(
            popup=bool(self.v_notif_popup.get()),
            toast=bool(self.v_notif_toast.get()),
            sound_tone=self.v_notif_tone.get() if self.v_notif_sound_on.get() else "none",
            sound_seconds=_DUR_BY_TEXT.get(self.v_notif_dur.get(), 5) if self.v_notif_sound_on.get() else 0,
            repeat_until_ack=bool(self.v_notif_repeat.get()),
        )

    def _refresh_layout(self, *_) -> None:
        op = self._op_key()
        cmode = self._cmode_key()
        is_chan = op in CHANNEL_OPERATORS
        is_mov = op in MOVING_OPERATORS

        self.m_cmode.config(state="disabled" if is_mov else "normal")
        if cmode == "pct" and not is_mov:
            self.f_pct_ref.pack(side="left")
            if self._pct_base_key() == "custom":
                self.e_ref.pack(side="left", padx=(6, 0))
            else:
                self.e_ref.pack_forget()
        else:
            self.f_pct_ref.pack_forget()

        self.f_single.pack_forget()
        self.f_channel.pack_forget()
        self.f_moving.pack_forget()
        if is_chan:
            self.f_channel.pack(fill="x")
        elif is_mov:
            self.f_moving.pack(fill="x")
        else:
            self.f_single.pack(fill="x")

        if self._trig_key() == "every_time":
            self.f_cd.pack(side="left")
        else:
            self.f_cd.pack_forget()

        exp_k = self._exp_key()
        if exp_k == "custom":
            self.f_exp_custom.pack(fill="x")
            self.lbl_exp_hint.pack_forget()
        else:
            self.f_exp_custom.pack_forget()
            self.lbl_exp_hint.pack(anchor="w")
            iso = compute_expires_at(exp_k, self.ctrl.now_fn(), self.ctrl.calendar)
            if iso:
                dt_ist = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(IST)
                self.lbl_exp_hint.config(text=f"Expires {dt_ist.strftime('%Y-%m-%d %H:%M IST')}")
            else:
                self.lbl_exp_hint.config(text="Won't expire")

        # Update summaries
        msg_preview = self.v_msg_text.strip() or self._preview_default_message()
        if len(msg_preview) > 44:
            msg_preview = msg_preview[:41] + "..."
        self.lbl_msg_sum.config(text=msg_preview)

        spec = self._current_spec()
        bits = []
        if spec.popup:
            bits.append("Popup")
        if spec.toast:
            bits.append("Toast")
        if spec.sound_tone != "none":
            dur_lbl = "1 chime" if spec.sound_seconds == 0 else f"{spec.sound_seconds}s sound"
            bits.append(dur_lbl)
        if spec.repeat_until_ack:
            bits.append("Repeat 2m")
        if not bits:
            bits.append("Visual flash only")
        self.lbl_notif_sum.config(text=" · ".join(bits))

        self._update_preview()

    def _preview_default_message(self) -> str:
        sym = self._cur_symbol()
        op = self._op_key()
        cmode = self._cmode_key()
        op_lbl = OPERATOR_LABELS.get(op, op)
        if op in CHANNEL_OPERATORS:
            s = "%" if cmode == "pct" else ""
            return f"{sym} {op_lbl} {self.v_lower.get().strip() or '?'}{s}–{self.v_upper.get().strip() or '?'}{s}"
        if op in MOVING_OPERATORS:
            s = "%" if cmode == "pct" else ""
            return f"{sym} {op_lbl} {self.v_val.get().strip() or '?'}{s} ({self.v_window.get().strip() or 5}m)"
        s = "%" if cmode == "pct" else ""
        return f"{sym} {op_lbl} {self.v_val.get().strip() or '?'}{s}"

    def _sync_default_message(self) -> None:
        if not self._msg_dirty:
            self.v_msg_text = ""

    def _update_preview(self) -> None:
        stock = self._cur_stock()
        tick = self._cur_tick()
        pc = tick.prev_close if tick else None
        live = tick.price if (tick and tick.price > 0) else None
        cmode = self._cmode_key()
        op = self._op_key()
        bits = []

        if op not in MOVING_OPERATORS:
            if cmode == "pct":
                custom_ref = None
                if self._pct_base_key() == "custom":
                    try:
                        custom_ref = parse_dec(self.v_ref_price.get(), "ref")
                    except Exception:
                        custom_ref = None
                base, base_lbl = resolve_pct_base(stock, self._pct_base_key(), prev_close=pc, ref_price=custom_ref)
                if base is not None and base > 0:
                    try:
                        if op in CHANNEL_OPERATORS:
                            lo = parse_dec(self.v_lower.get(), "Lower")
                            hi = parse_dec(self.v_upper.get(), "Upper")
                            lp = q2(price_from_pct(base, lo))
                            hp = q2(price_from_pct(base, hi))
                            bits.append(f"vs {base_lbl} (${q2(base):.2f}) = ${lp:.2f}–${hp:.2f}")
                        else:
                            pct_v = parse_dec(self.v_val.get(), "Percent")
                            tp = q2(price_from_pct(base, pct_v))
                            bits.append(f"{fmt_pct(q2(pct_v))} vs {base_lbl} (${q2(base):.2f}) = ${tp:.2f}")
                    except Exception:
                        bits.append(f"base: {base_lbl} (${q2(base):.2f})")
                else:
                    bits.append("vs prev close (resolves when market quote loads)")
            else:
                base, base_lbl = resolve_pct_base(stock, "auto", prev_close=pc)
                if base is not None and base > 0 and op not in CHANNEL_OPERATORS:
                    try:
                        p_val = parse_dec(self.v_val.get(), "Price")
                        pct = pct_from_price(base, p_val)
                        if pct is not None:
                            bits.append(f"= {fmt_pct(q2(pct))} vs {base_lbl}")
                    except Exception:
                        pass

        if live is not None:
            bits.append(f"current ${q2(live):.2f}")
        self.lbl_preview.config(text=" · ".join(bits))

    # ---- save & persist --------------------------------------------------------------------
    def _build_candidate_alert(self) -> tuple[str, Alert]:
        sym = self._cur_symbol()
        stock = self._cur_stock()
        op = self._op_key()
        cmode = self._cmode_key()
        exp_k = self._exp_key()

        if exp_k == "custom":
            raw = self.v_exp_custom.get().strip()
            try:
                dt_ist = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            except ValueError:
                raise ValidationError("Enter custom expiration as YYYY-MM-DD HH:MM (IST).")
            now_utc = self.ctrl.now_fn()
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            if dt_ist.astimezone(timezone.utc) <= now_utc:
                raise ValidationError("Custom expiration must be in the future.")
            exp_at = dt_ist.astimezone(timezone.utc).isoformat(timespec="seconds")
        else:
            exp_at = compute_expires_at(exp_k, self.ctrl.now_fn(), self.ctrl.calendar)

        d: dict = {
            "id": self.existing.id if self.existing else new_alert_id(sym),
            "operator": op,
            "condition_mode": cmode,
            "pct_base": self._pct_base_key(),
            "trigger": self._trig_key(),
            "expiration": exp_k,
            "expires_at": exp_at,
            "session_scope": self._scope_key(),
            "name": self.v_name.get().strip(),
            "label": self.v_label.get(),
            "priority": self._prio_key(),
            "enabled": bool(self.v_on.get()),
        }
        if op in CHANNEL_OPERATORS:
            d["lower"] = parse_dec(self.v_lower.get(), "Lower")
            d["upper"] = parse_dec(self.v_upper.get(), "Upper")
        elif op in MOVING_OPERATORS:
            d["value"] = parse_dec(self.v_val.get(), "Amount")
            d["window_min"] = int(parse_dec(self.v_window.get(), "Window (min)"))
        else:
            d["value"] = parse_dec(self.v_val.get(), "Target")

        if cmode == "pct" and op not in MOVING_OPERATORS and d["pct_base"] == "custom":
            d["ref_price"] = parse_dec(self.v_ref_price.get(), "Custom reference price")

        if d["trigger"] == "every_time":
            d["cooldown_min"] = int(parse_dec(self.v_cd.get(), "Cooldown (min)"))

        if d["priority"] == "CUSTOM":
            spec = self._current_spec()
            d["custom_notification"] = {
                "popup": spec.popup,
                "toast": spec.toast,
                "sound_tone": spec.sound_tone,
                "sound_seconds": spec.sound_seconds,
                "repeat_until_ack": spec.repeat_until_ack,
            }

        alert = parse_alert(d, stock, self.ctrl.cfg)
        tick = self._cur_tick()
        pc = tick.prev_close if tick else None
        _, _, tgt_txt, _ = resolve_alert_targets(stock, alert, pc)
        alert.message = self.v_msg_text.strip() or default_alert_message(sym, alert, tgt_txt)
        return sym, alert

    def _save(self) -> None:
        try:
            sym, alert = self._build_candidate_alert()
        except ValidationError as exc:
            self.err.config(text=str(exc))
            return
        except (ValueError, ArithmeticError):
            self.err.config(text="Check the numbers you entered.")
            return

        stock = self._cur_stock()
        tick = self._cur_tick()
        live = tick.price if (tick and tick.price > 0) else None
        pc = tick.prev_close if tick else None
        if alert.enabled and live is not None and self.ctrl.engine.would_fire_now(stock, alert, live, pc):
            ChoiceDialog(
                self,
                f"Current price is ${q2(live):.2f} and this alert is already met. What should happen?",
                lambda c: self._finish(sym, alert, c),
            )
        else:
            self._finish(sym, alert, None)

    def _finish(self, sym: str, alert: Alert, choice: Optional[str]) -> None:
        if not self.winfo_exists():
            return
        stock = self._cur_stock()
        tick = self._cur_tick()
        live = tick.price if (tick and tick.price > 0) else None
        pc = tick.prev_close if tick else None
        if (choice is None and alert.enabled and live is not None
                and self.ctrl.engine.would_fire_now(stock, alert, live, pc)):
            return
        self.destroy()
        self.on_done(sym, alert, choice)


class StockEditor(tk.Frame):
    """Expanded stock editor: manual trade fields and immediate-persisting alerts."""

    def __init__(
        self,
        parent,
        ctrl,
        symbol: str,
        now_fn,
        on_resize: Callable,
        on_removed: Callable,
        on_alerts_changed: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent, bg=BG, highlightthickness=1, highlightbackground="#444444")
        self.ctrl, self.symbol, self.now_fn = ctrl, symbol, now_fn
        self.on_resize, self.on_removed = on_resize, on_removed
        self.on_alerts_changed = on_alerts_changed
        self.cfg = ctrl.cfg
        self._alert_dlg: Optional[AlertDialog] = None
        self._saved_job: Optional[str] = None
        self._load(ctrl.stock(symbol))

    def _cancel_saved_timer(self):
        if self._saved_job is not None:
            try:
                self.after_cancel(self._saved_job)
            except tk.TclError:
                pass
            self._saved_job = None

    def _clear_saved(self):
        self._saved_job = None
        if self.winfo_exists() and hasattr(self, "err") and self.err.winfo_exists():
            self.err.config(text="", fg=RED)

    def _close_alert_dlg(self):
        if self._alert_dlg is not None and self._alert_dlg.winfo_exists():
            try:
                self._alert_dlg.destroy()
            except tk.TclError:
                pass
        self._alert_dlg = None

    def destroy(self):
        self._cancel_saved_timer()
        self._close_alert_dlg()
        super().destroy()

    # ---- state <-> fields -------------------------------------------------------------------
    def _load(self, stock: StockConfig):
        self._cancel_saved_timer()
        self._close_alert_dlg()
        self.stock = StockConfig(
            symbol=stock.symbol,
            category=stock.category,
            nickname=stock.nickname,
            noisy=stock.noisy,
            trade=None if stock.trade is None else TradeInfo(
                buy_price=stock.trade.buy_price,
                shares=stock.trade.shares,
                sell_price=stock.trade.sell_price,
                fees=stock.trade.fees,
            ),
            alerts=list(stock.alerts),
        )
        t = self.stock.trade or TradeInfo()
        self.v_cat = tk.StringVar(value=self.stock.category)
        self.v_nick = tk.StringVar(value=self.stock.nickname)
        self.v_noisy = tk.BooleanVar(value=self.stock.noisy)
        self.v_buy = tk.StringVar(value="" if t.buy_price is None else str(t.buy_price))
        self.v_shares = tk.StringVar(value="" if t.shares is None else str(t.shares))
        self.v_sell = tk.StringVar(value="" if t.sell_price is None else str(t.sell_price))
        self.v_fees = tk.StringVar(value="" if t.fees is None else str(t.fees))
        self._build()
        self._clean_snapshot = self._snapshot()

    def _snapshot(self) -> tuple:
        return (
            self.v_cat.get(),
            self.v_nick.get().strip(),
            bool(self.v_noisy.get()),
            self.v_buy.get().strip(),
            self.v_shares.get().strip(),
            self.v_sell.get().strip(),
            self.v_fees.get().strip(),
        )

    def is_dirty(self) -> bool:
        if not self.winfo_exists() or not hasattr(self, "_clean_snapshot"):
            return False
        try:
            return self._snapshot() != self._clean_snapshot
        except tk.TclError:
            return False

    def _collect_trade(self) -> Optional[TradeInfo]:
        bp = parse_dec(self.v_buy.get(), "Buy Price", blank_ok=True)
        sh = parse_dec(self.v_shares.get(), "Shares", blank_ok=True)
        sp = parse_dec(self.v_sell.get(), "Sell Price", blank_ok=True)
        fe = parse_dec(self.v_fees.get(), "Fees", blank_ok=True)
        if bp is not None and bp <= 0:
            raise ValidationError("Buy Price must be above 0.")
        if sh is not None and sh <= 0:
            raise ValidationError("Shares must be above 0.")
        if sp is not None and sp <= 0:
            raise ValidationError("Sell Price must be above 0.")
        if fe is not None and fe < 0:
            raise ValidationError("Fees cannot be negative.")
        if bp is None and sh is None and sp is None and fe is None:
            return None
        return TradeInfo(buy_price=bp, shares=sh, sell_price=sp, fees=fe)

    def _collect(self) -> StockConfig:
        latest = self.ctrl.stock(self.symbol) or self.stock
        return StockConfig(
            symbol=self.symbol,
            category=self.v_cat.get(),
            nickname=self.v_nick.get().strip(),
            noisy=bool(self.v_noisy.get()),
            trade=self._collect_trade(),
            alerts=list(latest.alerts),
        )

    def _temp_stock(self) -> Optional[StockConfig]:
        try:
            return self._collect()
        except (ValidationError, ArithmeticError):
            return None

    # ---- layout ---------------------------------------------------------------------------------
    def _build(self):
        for w in self.winfo_children():
            w.destroy()
        pad = {"padx": 10}
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", pady=(8, 4), **pad)
        label(top, "Category").pack(side="left")
        menu(top, self.v_cat, CATEGORIES, self._on_category, width=7).pack(side="left", padx=(6, 12))
        label(top, "Nickname").pack(side="left")
        entry(top, self.v_nick, 14).pack(side="left", padx=(6, 12))
        check(top, "Noisy stock (2-reading confirm)", self.v_noisy, self._update_live).pack(side="left")

        tf = tk.Frame(self, bg=BG)
        tf.pack(fill="x", pady=2, **pad)
        for txt, var, w in (
            ("Buy Price", self.v_buy, 9),
            ("Shares", self.v_shares, 8),
            ("Sell Price", self.v_sell, 9),
            ("Fees", self.v_fees, 8),
        ):
            label(tf, txt, fg=MUTED).pack(side="left", padx=(0, 4))
            e = entry(tf, var, w)
            e.pack(side="left", padx=(0, 12))
            e.bind("<KeyRelease>", lambda _e: self._update_live())

        self.lbl_info = label(self, "", fg=MUTED, justify="left", wraplength=640)
        self.lbl_info.pack(fill="x", pady=2, **pad)

        label(self, "Alerts", bold=True).pack(fill="x", pady=(8, 2), **pad)
        self.f_alerts = tk.Frame(self, bg=BG)
        self.f_alerts.pack(fill="x", **pad)
        self._build_alerts()

        self.lbl_live = label(self, "", fg=MUTED)
        self.lbl_live.pack(fill="x", pady=(8, 2), **pad)
        self.err = label(self, "", fg=RED, wraplength=640, justify="left")
        self.err.pack(fill="x", **pad)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", pady=(4, 10), **pad)
        button(bar, "Save details", self._save, bg=ACCENT).pack(side="left")
        button(bar, "Revert", lambda: self._load(self.ctrl.stock(self.symbol))).pack(side="left", padx=6)
        button(bar, "Remove stock", self._remove, bg="#7F1D1D").pack(side="right")

        self._update_live()
        self.on_resize()

    def _build_alerts(self):
        f = self.f_alerts
        for w in f.winfo_children():
            w.destroy()
        stock = self.ctrl.stock(self.symbol) or self.stock
        tick = (self.ctrl.rows.get(self.symbol) and self.ctrl.rows[self.symbol].tick) or None
        pc = tick.prev_close if tick else None
        if not stock.alerts:
            label(f, "No alerts yet. Click + Create alert.", fg=MUTED).pack(anchor="w", pady=2)
        for a in stock.alerts:
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", pady=1)
            var = tk.BooleanVar(value=a.enabled)
            check(row, "", var, lambda aid=a.id, v=var: self._toggle_alert(aid, v.get())).pack(side="left")
            status = self.ctrl.alert_status_label(self.symbol, a)
            scol = GREEN if status == "Active" else (AMBER if status in ("Waiting", "Cooldown") else MUTED)
            label(row, f"[{status}]", fg=scol, bold=True).pack(side="left", padx=(0, 6))
            label(row, alert_summary(a, stock, pc), wraplength=430, justify="left").pack(side="left", fill="x", expand=True)
            button(row, "Edit", lambda al=a: self._open_alert(al)).pack(side="left", padx=2)
            button(row, "Delete", lambda aid=a.id: self._delete_alert(aid)).pack(side="left", padx=2)
        button(f, "+ Create alert", lambda: self._open_alert(None), bg=ACCENT).pack(anchor="w", pady=(6, 0))

    # ---- immediate-persisting alert actions ----------------------------------------------------
    def _after_alert_mutation(self):
        self.stock = self.ctrl.stock(self.symbol) or self.stock
        self._build_alerts()
        self.on_resize()
        if self.on_alerts_changed:
            self.on_alerts_changed()

    def _open_alert(self, alert: Optional[Alert]):
        if self._alert_dlg is not None and self._alert_dlg.winfo_exists():
            self._alert_dlg.lift()
            self._alert_dlg.focus_force()
            return

        def done(sym: str, saved_alert: Alert, choice: Optional[str]):
            self._alert_dlg = None
            try:
                self.ctrl.save_alert(sym, saved_alert, choice)
            except (ValidationError, OSError) as exc:
                if self.winfo_exists():
                    self.err.config(text=str(exc) or "Could not save alert.", fg=RED)
                return
            if self.winfo_exists():
                self._after_alert_mutation()

        self._alert_dlg = AlertDialog(self.winfo_toplevel(), self.ctrl, self.symbol, alert, done)

    def _toggle_alert(self, alert_id: str, enabled: bool):
        try:
            self.ctrl.toggle_alert(self.symbol, alert_id, enabled)
            self._after_alert_mutation()
        except (ValidationError, OSError) as exc:
            self.err.config(text=str(exc) or "Could not update alert.", fg=RED)

    def _delete_alert(self, alert_id: str):
        try:
            self.ctrl.delete_alert(self.symbol, alert_id)
            self._after_alert_mutation()
        except (ValidationError, OSError) as exc:
            self.err.config(text=str(exc) or "Could not delete alert.", fg=RED)

    # ---- stock metadata actions ---------------------------------------------------------------
    def _on_category(self, *_):
        self._close_alert_dlg()
        self._update_live()
        self._build_alerts()

    def _save(self):
        self._cancel_saved_timer()
        try:
            new = self._collect()
            self.ctrl.update_stock(new)
        except ValidationError as exc:
            self.err.config(text=str(exc), fg=RED)
            return
        except OSError:
            self.err.config(text="Could not save the file. Nothing was written.", fg=RED)
            return
        except ArithmeticError:
            self.err.config(text="Check the numbers you entered.", fg=RED)
            return
        self._load(self.ctrl.stock(self.symbol))
        self.err.config(text="Saved.", fg=GREEN)
        self._saved_job = self.after(1500, self._clear_saved)
        if self.on_alerts_changed:
            self.on_alerts_changed()

    def _remove(self):
        self._cancel_saved_timer()
        msg = f"Remove {self.symbol}?\n\nThis deletes the stock, its trade details, and its alerts from the watchlist."
        if messagebox.askyesno("Remove stock", msg, parent=self.winfo_toplevel(), icon="warning"):
            try:
                self.ctrl.remove_stock(self.symbol)
            except (ValidationError, OSError) as exc:
                self.err.config(text=str(exc) or "Could not save the file. Nothing was written.", fg=RED)
                return
            self.on_removed()

    def _update_live(self):
        st = self._temp_stock()
        tick = self.ctrl.rows.get(self.symbol) and self.ctrl.rows[self.symbol].tick
        info = ""
        if st and st.trade:
            s = manual_trade_summary(st.trade, tick.price if tick else None)
            parts = []
            if "cost_total" in s:
                parts.append(f"Cost ${s['cost_total']:.2f}")
            if "cost_per_share" in s:
                parts.append(f"Eff. Buy ${q2(s['cost_per_share']):.2f}")
            if "live_pnl" in s:
                n = s["live_pnl"]
                parts.append(f"Live P/L {'+' if n >= 0 else '-'}${abs(n):.2f}")
            if "target_pnl" in s:
                n = s["target_pnl"]
                parts.append(f"Target P/L {'+' if n >= 0 else '-'}${abs(n):.2f}")
            info = " · ".join(parts)
        self.lbl_info.config(text=info)
        self.refresh_price_line()

    def refresh_price_line(self):
        row = self.ctrl.rows.get(self.symbol)
        if row is None or row.tick is None:
            self.lbl_live.config(text="Waiting for first price reading…")
            return
        t = row.tick
        pt = "—" if t.provider_time is None else t.provider_time.astimezone(IST).strftime("%H:%M:%S IST")
        age = max(0, int((self.now_fn() - t.fetched_at).total_seconds()))
        ph = f"{q2(row.pre_price):.2f}" if row.pre_price else "—"
        rh = f"{q2(row.reg_price):.2f}" if row.reg_price else "—"
        ah = f"{q2(row.ah_price):.2f}" if row.ah_price else "—"
        self.lbl_live.config(
            text=(f"Current price {t.price:.2f} · PH {ph} · RH {rh} · AH {ah} · "
                  f"provider time {pt} · data age {age} s · source {t.source}")
        )


class AddStockDialog(tk.Toplevel):
    """+ Add stock: ticker (checked live), nickname, category, and optional manual trade figures."""

    def __init__(self, master, ctrl, on_added: Callable[[str], None]):
        super().__init__(master, bg=BG)
        self.ctrl, self.on_added = ctrl, on_added
        self._checked: Optional[str] = None
        self._closed = False
        n, cap = len(ctrl.symbols()), ctrl.cfg.max_symbols
        if n >= cap:
            messagebox.showerror("Watchlist full", watchlist_full_message(n, cap), parent=master)
            self._closed = True
            self.destroy()
            return
        self.title("Add stock")
        self.attributes("-topmost", True)
        self.v_sym, self.v_nick = tk.StringVar(), tk.StringVar()
        self.v_cat = tk.StringVar(value="WATCH")
        self.v_buy, self.v_shares = tk.StringVar(), tk.StringVar()
        self.v_sell, self.v_fees = tk.StringVar(), tk.StringVar()

        g = tk.Frame(self, bg=BG)
        g.pack(padx=16, pady=12)
        label(g, "Ticker").grid(row=0, column=0, sticky="w", pady=2)
        e = entry(g, self.v_sym, 10)
        e.grid(row=0, column=1, sticky="w")
        e.bind("<Return>", lambda _ev: self._check())
        e.bind("<KeyRelease>", lambda _ev: self._invalidate())
        button(g, "Check price", self._check).grid(row=0, column=2, padx=6)
        self.lbl_status = label(g, "", fg=MUTED, wraplength=320, justify="left")
        self.lbl_status.grid(row=1, column=1, columnspan=3, sticky="w")
        label(g, "Nickname").grid(row=2, column=0, sticky="w", pady=2)
        entry(g, self.v_nick, 20).grid(row=2, column=1, columnspan=2, sticky="w")
        label(g, "Category").grid(row=3, column=0, sticky="w", pady=2)
        menu(g, self.v_cat, CATEGORIES, width=8).grid(row=3, column=1, sticky="w")

        tf = tk.Frame(g, bg=BG)
        tf.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 2))
        label(tf, "Optional trade figures:", fg=MUTED).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 2))
        for r, (t, v) in enumerate((("Buy Price", self.v_buy), ("Shares", self.v_shares),
                                    ("Sell Price", self.v_sell), ("Fees", self.v_fees)), start=1):
            label(tf, t).grid(row=r, column=0, sticky="w", pady=2)
            entry(tf, v, 10).grid(row=r, column=1, padx=6)

        self.err = label(self, "", fg=RED, wraplength=380, justify="left")
        self.err.pack(padx=16, anchor="w")
        bar = tk.Frame(self, bg=BG)
        bar.pack(pady=10)
        self.btn_add = button(bar, "Add", self._add, bg=ACCENT)
        self.btn_add.pack(side="left", padx=4)
        self.btn_add.config(state="disabled")
        button(bar, "Cancel", self.destroy).pack(side="left", padx=4)
        self.bind("<Escape>", lambda _ev: self.destroy())
        self.bind("<Destroy>", self._on_destroy)
        if not self._closed:
            place_near_widget(self, master)
        e.focus_set()

    def _on_destroy(self, ev):
        if ev.widget is self:
            self._closed = True

    def _invalidate(self):
        if self._checked and self.v_sym.get().strip().upper() != self._checked:
            self._checked = None
            self.btn_add.config(state="disabled")
            self.lbl_status.config(text="", fg=MUTED)

    def _check(self):
        sym = self.v_sym.get().strip().upper()
        self.v_sym.set(sym)
        if not sym:
            self.lbl_status.config(text="Enter a ticker.", fg=RED)
            return
        self.lbl_status.config(text="Checking…", fg=MUTED)
        self.ctrl.check_symbol(sym, lambda tick, exc, sym=sym: self._checked_cb(sym, tick, exc))

    def _checked_cb(self, sym, tick, exc):
        if self._closed or not self.winfo_exists():
            return
        if self.v_sym.get().strip().upper() != sym:
            return
        if exc is not None:
            msg = str(exc) if isinstance(exc, ValidationError) else "Could not check the ticker right now (feed problem)."
            self.lbl_status.config(text=msg, fg=RED)
            self._checked = None
            self.btn_add.config(state="disabled")
            return
        self._checked = sym
        self.lbl_status.config(text=f"{sym}: {tick.price:.2f} ({tick.source})", fg=GREEN)
        self.btn_add.config(state="normal")

    def _add(self):
        if self._closed or not self.winfo_exists():
            return
        sym = self.v_sym.get().strip().upper()
        if self._checked != sym:
            self.err.config(text="Check the ticker first.")
            return
        cat = self.v_cat.get()
        try:
            bp = parse_dec(self.v_buy.get(), "Buy Price", blank_ok=True)
            sh = parse_dec(self.v_shares.get(), "Shares", blank_ok=True)
            sp = parse_dec(self.v_sell.get(), "Sell Price", blank_ok=True)
            fe = parse_dec(self.v_fees.get(), "Fees", blank_ok=True)
            if bp is not None and bp <= 0:
                raise ValidationError("Buy Price must be above 0.")
            if sh is not None and sh <= 0:
                raise ValidationError("Shares must be above 0.")
            if sp is not None and sp <= 0:
                raise ValidationError("Sell Price must be above 0.")
            if fe is not None and fe < 0:
                raise ValidationError("Fees cannot be negative.")
            trade = TradeInfo(bp, sh, sp, fe) if any(x is not None for x in (bp, sh, sp, fe)) else None
            self.ctrl.add_stock(StockConfig(sym, cat, self.v_nick.get().strip(), False, trade))
        except ValidationError as exc:
            self.err.config(text=str(exc))
            return
        except OSError:
            self.err.config(text="Could not save the file. Nothing was written.")
            return
        except ArithmeticError:
            self.err.config(text="Check the numbers you entered.")
            return
        self._closed = True
        self.destroy()
        self.on_added(sym)


_KEY_SOURCE_LABELS = {
    "environment": "Environment variables",
    "config.json": "config.json",
    "demo": "Demo mode",
    "none": "Not configured",
}


class ApiKeysStatusDialog(tk.Toplevel):
    """Read-only dialog displaying Finnhub API key count, source, and per-key pool health."""

    def __init__(self, master, ctrl):
        super().__init__(master, bg=BG)
        self.overrideredirect(False)
        self.title("API Keys & Status")
        self.attributes("-topmost", True)
        self.ctrl = ctrl
        info = ctrl.key_pool_info() if hasattr(ctrl, "key_pool_info") else {}
        self.info = info = dict(info or {})

        keys = list(info.get("keys") or [])
        total = int(info.get("total", len(keys)))
        ok = int(info.get("ok", 0))
        rate_limited = int(info.get("rate_limited", 0))
        rejected = int(info.get("rejected", 0))
        reject_code = info.get("reject_code", 401)
        source_raw = str(info.get("source") or "none")
        if source_raw in _KEY_SOURCE_LABELS:
            source_text = _KEY_SOURCE_LABELS[source_raw]
        elif source_raw == "unknown":
            source_text = "Not configured" if total == 0 else "Unknown"
        else:
            source_text = source_raw

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        label(body, f"Configured keys: {total}", bold=True).pack(anchor="w")
        label(body, f"Source: {source_text}", fg=MUTED).pack(anchor="w", pady=(2, 8))

        if total == 0:
            summary_text = ("No Finnhub API keys configured (Demo mode)"
                            if source_raw == "demo" else "No Finnhub API keys configured")
            summary_fg = AMBER if source_raw == "demo" else RED
        elif ok == total and rate_limited == 0 and rejected == 0:
            summary_text = f"All {total} key(s) healthy & working"
            summary_fg = GREEN
        else:
            summary_text = f"{ok} working, {rate_limited} rate-limited, {rejected} rejected"
            summary_fg = RED if rejected > 0 else AMBER

        label(body, summary_text, fg=summary_fg, bold=True, wraplength=420, justify="left").pack(anchor="w", pady=(0, 8))

        if keys:
            box = tk.Frame(body, bg=BG2, padx=10, pady=8, highlightthickness=1, highlightbackground="#444444")
            box.pack(fill="x", pady=(0, 8))
            for idx, k in enumerate(keys):
                num = k.get("num", idx + 1)
                state = k.get("state", "ok")
                retry_s = float(k.get("retry_in_s", 0.0) or 0.0)
                calls = int(k.get("calls", 0) or 0)
                if state == "ok":
                    st_text, st_fg = "Healthy / working", GREEN
                elif state == "rate_limited":
                    st_text, st_fg = f"Rate-limited (retry in {retry_s:.0f}s)", AMBER
                elif state == "rejected":
                    st_text, st_fg = f"Rejected (HTTP {reject_code})", RED
                else:
                    st_text, st_fg = str(state), MUTED
                label(box, f"Key #{num}", bg=BG2, bold=True).grid(row=idx, column=0, sticky="w", padx=(0, 12), pady=2)
                label(box, st_text, bg=BG2, fg=st_fg).grid(row=idx, column=1, sticky="w", padx=(0, 12), pady=2)
                label(box, f"Calls: {calls}", bg=BG2, fg=MUTED).grid(row=idx, column=2, sticky="e", pady=2)
            box.grid_columnconfigure(1, weight=1)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(0, 12))
        button(bar, "Close", self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())
        place_near_widget(self, master)
        self.focus_set()
