"""Edit-mode forms (blueprint 9.5, 9.6, 6.4): the expanded stock section, the alert dialog and the
add-stock dialog. All saving goes through AppController, which validates and writes atomically."""
from __future__ import annotations

import copy
import tkinter as tk
import uuid
from decimal import Decimal
from tkinter import messagebox
from typing import Callable, Optional

from market_clock import IST
from models import Alert, CATEGORIES, LABELS, MODES, PRIORITIES, PlannedEntry, Position, StockConfig, ValidationError
from positions import position_summary, price_from_pct, pct_from_price, q2, stock_base
from ui.common import (ACCENT, BG, BG2, FG, GREEN, MUTED, RED, button, check, entry, fmt_pct, label, menu,
                       parse_dec)
from watchlist import parse_alert
from zones import ZONE_DEFAULTS, TAGS, editor_rows

LEVEL_CHOICES = {"Price at or above": "level_above", "Price at or below": "level_below", "Leaves a zone": "zone_exit"}


def _pct_text(p) -> str:
    return "" if p is None else fmt_pct(q2(p))


def range_text(lo, hi) -> str:
    f = lambda x: fmt_pct(Decimal(x))
    if lo is None:
        return f"P ≤ {f(hi)}"
    if hi is None:
        return f"P ≥ {f(lo)}"
    return f"{f(lo)} to {f(hi)}"


def price_text(lo, hi) -> str:
    if lo is None and hi is None:
        return ""
    if lo is None:
        return f"≤ {hi:.2f}"
    if hi is None:
        return f"≥ {lo:.2f}"
    return f"{lo:.2f} – {hi:.2f}"


def alert_summary(a: Alert, base) -> str:
    if a.type == "zone_exit":
        what = "Leaves the sell range" if a.zone == "sell" else f"Leaves {TAGS.get(a.zone) or a.zone}"
    elif a.type in ("level_above", "level_below", "cross_above", "cross_below"):
        word = {"level_above": "at or above", "level_below": "at or below", "cross_above": "crosses above",
                "cross_below": "crosses below"}[a.type]
        if a.price is not None:
            lvl, pct = a.price, (pct_from_price(base, a.price) if base else None)
        else:
            lvl, pct = (price_from_pct(base, a.pct) if base else None), a.pct
        what = f"Price {word} " + (f"{q2(lvl):.2f}" if lvl is not None else "?")
        if pct is not None:
            what += f" ({fmt_pct(q2(pct))})"
    else:
        what = f"Moves {a.pct}% from previous close"
    extra = f" · {a.priority} · {a.label}" + (f" · {a.mode}" if a.type != "zone_exit" else "")
    return what + extra + (f" · {a.note}" if a.note else "")


def watchlist_full_message(count: int, cap: int) -> str:
    return (f"The watchlist already has {count} stocks (max_symbols = {cap}).\n\n"
            "To add more: raise max_symbols in config.json and keep the polling budget at or below "
            "48 calls per minute, where calls per minute = (stocks + 1) x 60 / poll_seconds "
            "(30 stocks at 60 s is 31 calls; 30 stocks at 30 s is 62 and is refused). Then restart the app.")


def new_alert_id(symbol: str) -> str:
    return f"{symbol.lower()}-{uuid.uuid4().hex[:6]}"


class ChoiceDialog(tk.Toplevel):
    """'current 201.90, this would fire now' with Fire now / Wait for next cross."""

    def __init__(self, master, text: str, on_choice: Callable[[Optional[str]], None]):
        super().__init__(master, bg=BG)
        self.title("Alert would fire now")
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
        self.grab_set()

    def _finish(self, choice):
        if not self._done:
            self._done = True
            self.destroy()
            self._cb(choice)


class AlertDialog(tk.Toplevel):
    """Create or edit one custom alert (level above/below, or zone exit)."""

    def __init__(self, master, ctx: dict, alert: Optional[Alert], on_done: Callable):
        """ctx: stock (temp StockConfig), cfg, base, price (live or None), zone_options [(key, text)]."""
        super().__init__(master, bg=BG)
        self.title("Alert")
        self.attributes("-topmost", True)
        self.ctx, self.on_done, self.existing = ctx, on_done, alert
        cfg, stock = ctx["cfg"], ctx["stock"]
        a = alert
        inv = {v: k for k, v in LEVEL_CHOICES.items()}
        self.v_type = tk.StringVar(value=inv.get(a.type, "Price at or below") if a else "Price at or above")
        self.v_by = tk.StringVar(value="percent" if (a and a.pct is not None) else "price")
        self.v_val = tk.StringVar(value="" if not a else str(a.price if a.price is not None else (a.pct or "")))
        self.v_zone = tk.StringVar(value="")
        self.v_label = tk.StringVar(value=a.label if a else {"BUY": "BUY", "SELL": "SELL"}.get(stock.category, "WATCH"))
        self.v_prio = tk.StringVar(value=a.priority if a else cfg.default_priority)
        self.v_mode = tk.StringVar(value=a.mode if a else cfg.level_alert_mode)
        self.v_cd = tk.StringVar(value=str(a.cooldown_min if a else cfg.cooldown_min))
        self.v_note = tk.StringVar(value=a.note if a else "")
        self.v_on = tk.BooleanVar(value=a.enabled if a else True)
        g = tk.Frame(self, bg=BG)
        g.pack(padx=16, pady=12)
        r = 0
        label(g, "When").grid(row=r, column=0, sticky="w", pady=2)
        menu(g, self.v_type, list(LEVEL_CHOICES), self._layout, width=18).grid(row=r, column=1, columnspan=3, sticky="w")
        r += 1
        self.lbl_val = label(g, "Value")
        self.lbl_val.grid(row=r, column=0, sticky="w", pady=2)
        self.f_val = tk.Frame(g, bg=BG)
        self.f_val.grid(row=r, column=1, columnspan=3, sticky="w")
        tk.Radiobutton(self.f_val, text="price $", variable=self.v_by, value="price", command=self._update_equiv, bg=BG, fg=FG,
                       selectcolor=BG2, activebackground=BG, activeforeground=FG).pack(side="left")
        self.rb_pct = tk.Radiobutton(self.f_val, text="percent %", variable=self.v_by, value="percent", command=self._update_equiv,
                                     bg=BG, fg=FG, selectcolor=BG2, activebackground=BG, activeforeground=FG)
        self.rb_pct.pack(side="left")
        if ctx["base"] is None:
            self.rb_pct.config(state="disabled")
            self.v_by.set("price")
        self.e_val = entry(self.f_val, self.v_val, 9)
        self.e_val.pack(side="left", padx=6)
        self.e_val.bind("<KeyRelease>", lambda e: self._update_equiv())
        r += 1
        self.lbl_eq = label(g, "", fg=MUTED)
        self.lbl_eq.grid(row=r, column=1, columnspan=3, sticky="w")
        r += 1
        self.lbl_zone = label(g, "Zone")
        self.lbl_zone.grid(row=r, column=0, sticky="w", pady=2)
        opts = [t for _, t in ctx["zone_options"]] or ["(no zones for this category)"]
        cur = next((t for k, t in ctx["zone_options"] if a and k == a.zone), opts[0])
        self.v_zone.set(cur)
        self.m_zone = menu(g, self.v_zone, opts, width=30)
        self.m_zone.grid(row=r, column=1, columnspan=3, sticky="w")
        r += 1
        for text, var, vals in (("Label", self.v_label, LABELS), ("Priority", self.v_prio, PRIORITIES)):
            label(g, text).grid(row=r, column=0, sticky="w", pady=2)
            menu(g, var, vals).grid(row=r, column=1, sticky="w")
            r += 1
        self.lbl_mode = label(g, "Mode")
        self.lbl_mode.grid(row=r, column=0, sticky="w", pady=2)
        self.f_mode = tk.Frame(g, bg=BG)
        self.f_mode.grid(row=r, column=1, columnspan=3, sticky="w")
        menu(self.f_mode, self.v_mode, MODES, self._layout).pack(side="left")
        self.lbl_cd = label(self.f_mode, "  cooldown min")
        self.e_cd = entry(self.f_mode, self.v_cd, 5)
        r += 1
        label(g, "Note").grid(row=r, column=0, sticky="w", pady=2)
        entry(g, self.v_note, 32).grid(row=r, column=1, columnspan=3, sticky="w")
        r += 1
        check(g, "Enabled", self.v_on).grid(row=r, column=1, sticky="w")
        self.err = label(self, "", fg=RED, wraplength=380, justify="left")
        self.err.pack(padx=16, anchor="w")
        row = tk.Frame(self, bg=BG)
        row.pack(pady=10)
        button(row, "Save", self._save, bg=ACCENT).pack(side="left", padx=4)
        button(row, "Cancel", self.destroy).pack(side="left", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())
        self._layout()

    def _is_zone(self):
        return LEVEL_CHOICES[self.v_type.get()] == "zone_exit"

    def _layout(self, *_):
        z = self._is_zone()
        for w in (self.lbl_val, self.f_val):
            (w.grid_remove if z else w.grid)()
        for w in (self.lbl_zone, self.m_zone):
            (w.grid if z else w.grid_remove)()
        for w in (self.lbl_mode, self.f_mode):
            (w.grid_remove if z else w.grid)()
        if self.v_mode.get() == "repeat" and not z:
            self.lbl_cd.pack(side="left")
            self.e_cd.pack(side="left")
        else:
            self.lbl_cd.pack_forget()
            self.e_cd.pack_forget()
        self._update_equiv()

    def _update_equiv(self):
        base, price = self.ctx["base"], self.ctx["price"]
        txt = ""
        try:
            v = parse_dec(self.v_val.get(), "value")
            name = "breakeven" if self.ctx["stock"].category in ("HOLD", "SELL") else "planned entry"
            if self.v_by.get() == "percent" and base is not None:
                txt = f"= ${q2(price_from_pct(base, v)):.2f} (moves when {name} changes)"
            elif base is not None:
                txt = f"= {fmt_pct(q2(pct_from_price(base, v)))} vs {name} (price stays fixed)"
        except Exception:
            pass
        if price is not None:
            txt = (txt + "   " if txt else "") + f"current {price:.2f}"
        self.lbl_eq.config(text=txt)

    def _save(self):
        try:
            typ = LEVEL_CHOICES[self.v_type.get()]
            d = {"id": self.existing.id if self.existing else new_alert_id(self.ctx["stock"].symbol),
                 "type": typ, "label": self.v_label.get(), "priority": self.v_prio.get(),
                 "enabled": self.v_on.get(), "note": self.v_note.get().strip()}
            if typ == "zone_exit":
                key = next((k for k, t in self.ctx["zone_options"] if t == self.v_zone.get()), None)
                if key is None:
                    raise ValidationError("This category has no zones to leave.")
                d["zone"] = key
            else:
                val = parse_dec(self.v_val.get(), "Value")
                d["price" if self.v_by.get() == "price" else "pct"] = val
                d["mode"] = self.v_mode.get()
                if d["mode"] == "repeat":
                    d["cooldown_min"] = int(parse_dec(self.v_cd.get(), "Cooldown"))
            alert = parse_alert(d, self.ctx["stock"], self.ctx["cfg"])
        except ValidationError as exc:
            self.err.config(text=str(exc))
            return
        except (ValueError, ArithmeticError):
            self.err.config(text="Check the numbers you entered.")
            return
        price = self.ctx["price"]
        probe = copy.deepcopy(self.ctx["stock"])
        probe.alerts = [alert]
        engine = self.ctx["engine"]
        if alert.enabled and price is not None and engine.would_fire_now(probe, alert, price):
            lvl = engine.level_price(alert, stock_base(probe, self.ctx["cfg"].rates))
            text = f"current {price:.2f}, this would fire now (level {q2(lvl):.2f}). What should happen?"
            ChoiceDialog(self, text, lambda c: self._finish(alert, c))
        else:
            self._finish(alert, None)

    def _finish(self, alert, choice):
        if alert is not None and choice is None and alert.enabled and self.ctx["price"] is not None \
                and self.ctx["engine"].would_fire_now(self._probe(alert), alert, self.ctx["price"]):
            return                                   # the choice dialog was cancelled: stay open
        self.destroy()
        self.on_done(alert, choice)

    def _probe(self, alert):
        p = copy.deepcopy(self.ctx["stock"])
        p.alerts = [alert]
        return p


class StockEditor(tk.Frame):
    """The expanded section for one stock (edit mode only)."""

    def __init__(self, parent, ctrl, symbol: str, now_fn, on_resize: Callable, on_removed: Callable):
        super().__init__(parent, bg=BG, highlightthickness=1, highlightbackground="#444444")
        self.ctrl, self.symbol, self.now_fn = ctrl, symbol, now_fn
        self.on_resize, self.on_removed = on_resize, on_removed
        self.cfg = ctrl.cfg
        self.choices: dict = {}
        self._zv: dict = {}
        self._zlabels: dict = {}
        self._load(ctrl.stock(symbol))

    # ---- state <-> fields -------------------------------------------------------------------
    def _load(self, stock: StockConfig):
        self.stock = copy.deepcopy(stock)
        s = self.stock
        self.v_cat = tk.StringVar(value=s.category)
        self.v_nick = tk.StringVar(value=s.nickname)
        self.v_noisy = tk.BooleanVar(value=s.noisy)
        self.v_shares = tk.StringVar(value="" if not s.position else str(s.position.shares))
        self.v_avg = tk.StringVar(value="" if not s.position else str(s.position.avg_price))
        fee = s.position.actual_buy_fee if s.position else None
        self.v_fee = tk.StringVar(value="" if fee is None else str(fee))
        self.v_ep = tk.StringVar(value="" if not s.planned_entry else str(s.planned_entry.price))
        self.v_es = tk.StringVar(value="" if not s.planned_entry or s.planned_entry.shares is None else str(s.planned_entry.shares))
        self._zv = {}
        self.choices = {}
        self._build()

    def _collect(self) -> StockConfig:
        cat = self.v_cat.get()
        old = self.stock
        pos, ent = old.position, old.planned_entry
        if cat in ("HOLD", "SELL"):
            pos = Position(parse_dec(self.v_shares.get(), "Shares"), parse_dec(self.v_avg.get(), "Average price"),
                           parse_dec(self.v_fee.get(), "Actual buy fee", blank_ok=True))
        else:                                        # hidden fields: keep numbers when they still parse
            try:
                pos = Position(parse_dec(self.v_shares.get(), "x"), parse_dec(self.v_avg.get(), "x"),
                               parse_dec(self.v_fee.get(), "x", blank_ok=True))
            except ValidationError:
                pass
        if cat == "BUY":
            ent = PlannedEntry(parse_dec(self.v_ep.get(), "Planned entry price"),
                               parse_dec(self.v_es.get(), "Planned shares", blank_ok=True))
        else:
            try:
                ent = PlannedEntry(parse_dec(self.v_ep.get(), "x"), parse_dec(self.v_es.get(), "x", blank_ok=True))
            except ValidationError:
                pass
        st = StockConfig(old.symbol, cat, self.v_nick.get().strip(), self.v_noisy.get(), pos, ent, {}, old.alerts)
        st.zones = self._collect_zones(st)
        return st

    def _collect_zones(self, st: StockConfig) -> dict:
        """Store only what differs from the defaults, as the user typed it."""
        out = {}
        for name, v in self._zv.items():
            ov = {}
            key = v["key"]
            if key and v["pct"].get().strip():
                pct = parse_dec(v["pct"].get(), f"{TAGS[name]} threshold")
                if pct != Decimal(str(self.cfg.zone_defaults[key])):
                    ov["pct"] = pct
            if v["prio"].get() != ZONE_DEFAULTS[name][0]:
                ov["priority"] = v["prio"].get()
            if v["on"].get() != ZONE_DEFAULTS[name][2]:
                ov["enabled"] = v["on"].get()
            if ov:
                out[name] = ov
        # keep overrides of zones not shown for this category (category changes keep numbers)
        for name, ov in self.stock.zones.items():
            if name not in self._zv and name not in out:
                out[name] = ov
        return out

    def _temp_stock(self) -> Optional[StockConfig]:
        try:
            return self._collect()
        except (ValidationError, ArithmeticError):
            return None

    # ---- layout ---------------------------------------------------------------------------------
    def _build(self):
        for w in self.winfo_children():
            w.destroy()
        cat = self.v_cat.get()
        pad = {"padx": 10}
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", pady=(8, 2), **pad)
        label(top, "Category").pack(side="left")
        menu(top, self.v_cat, CATEGORIES, self._on_category, width=7).pack(side="left", padx=(6, 12))
        label(top, "Nickname").pack(side="left")
        entry(top, self.v_nick, 14).pack(side="left", padx=(6, 12))
        check(top, "Noisy stock (wider bands, 2-reading confirm)", self.v_noisy).pack(side="left")

        if cat in ("HOLD", "SELL"):
            f = tk.Frame(self, bg=BG)
            f.pack(fill="x", pady=2, **pad)
            for text, var, w in (("Shares", self.v_shares, 8), ("Average price", self.v_avg, 10),
                                 ("Actual buy fee (blank = projected)", self.v_fee, 8)):
                label(f, text).pack(side="left", padx=(0, 4))
                e = entry(f, var, w)
                e.pack(side="left", padx=(0, 12))
                e.bind("<KeyRelease>", lambda ev: self._update_live())
        elif cat == "BUY":
            f = tk.Frame(self, bg=BG)
            f.pack(fill="x", pady=2, **pad)
            for text, var, w in (("Planned entry price", self.v_ep, 10), ("Shares (optional)", self.v_es, 8)):
                label(f, text).pack(side="left", padx=(0, 4))
                e = entry(f, var, w)
                e.pack(side="left", padx=(0, 12))
                e.bind("<KeyRelease>", lambda ev: self._update_live())
        self.lbl_info = label(self, "", fg=MUTED, justify="left", wraplength=640)
        self.lbl_info.pack(fill="x", pady=2, **pad)

        self._zlabels = {}
        if cat != "WATCH":
            label(self, "Zones", bold=True).pack(fill="x", pady=(8, 2), **pad)
            self._zone_grid = tk.Frame(self, bg=BG)
            self._zone_grid.pack(fill="x", **pad)
            self._build_zones()
        label(self, "Custom alerts", bold=True).pack(fill="x", pady=(8, 2), **pad)
        self.f_alerts = tk.Frame(self, bg=BG)
        self.f_alerts.pack(fill="x", **pad)
        self._build_alerts()

        self.lbl_live = label(self, "", fg=MUTED)
        self.lbl_live.pack(fill="x", pady=(8, 2), **pad)
        self.err = label(self, "", fg=RED, wraplength=640, justify="left")
        self.err.pack(fill="x", **pad)
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", pady=(4, 10), **pad)
        button(bar, "Save", self._save, bg=ACCENT).pack(side="left")
        button(bar, "Revert", lambda: self._load(self.ctrl.stock(self.symbol))).pack(side="left", padx=6)
        button(bar, "Remove stock", self._remove, bg="#7F1D1D").pack(side="right")
        self._update_live()
        self.on_resize()

    def _build_zones(self):
        g = self._zone_grid
        for w in g.winfo_children():
            w.destroy()
        st = self._temp_stock() or self.stock
        base = stock_base(st, self.cfg.rates)
        rows = editor_rows(st, self.cfg.zone_defaults, base)
        for c, h in enumerate(("Tag", "Range of P", "Price", "On", "Priority", "Threshold %")):
            label(g, h, fg=MUTED).grid(row=0, column=c, sticky="w", padx=4)
        for i, r in enumerate(rows, start=1):
            name = r["name"]
            v = self._zv.get(name)
            if v is None:
                key = r["threshold_key"]
                pct = r["threshold_pct"]
                v = self._zv[name] = {"key": key, "on": tk.BooleanVar(value=r["enabled"]),
                                      "prio": tk.StringVar(value=r["priority"]),
                                      "pct": tk.StringVar(value="" if pct is None else str(pct.normalize()))}
            label(g, r["tag"]).grid(row=i, column=0, sticky="w", padx=4)
            rng = label(g, range_text(r["lo_pct"], r["hi_pct"]), fg=MUTED)
            rng.grid(row=i, column=1, sticky="w", padx=4)
            prc = label(g, price_text(r["lo_price"], r["hi_price"]), fg=GREEN)
            prc.grid(row=i, column=2, sticky="w", padx=4)
            self._zlabels[name] = (rng, prc)
            check(g, "", v["on"]).grid(row=i, column=3)
            menu(g, v["prio"], PRIORITIES, width=7).grid(row=i, column=4, padx=4)
            if v["key"]:
                e = entry(g, v["pct"], 7)
                e.grid(row=i, column=5, padx=4)
                e.bind("<KeyRelease>", lambda ev: self._update_live())
            else:
                label(g, "fixed", fg=MUTED).grid(row=i, column=5, sticky="w", padx=4)

    def _build_alerts(self):
        f = self.f_alerts
        for w in f.winfo_children():
            w.destroy()
        base = stock_base(self._temp_stock() or self.stock, self.cfg.rates)
        for a in self.stock.alerts:
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", pady=1)
            var = tk.BooleanVar(value=a.enabled)
            check(row, "", var, lambda a=a, v=var: setattr(a, "enabled", v.get())).pack(side="left")
            label(row, alert_summary(a, base), wraplength=470, justify="left").pack(side="left", fill="x", expand=True)
            button(row, "Edit", lambda a=a: self._open_alert(a)).pack(side="left", padx=2)
            button(row, "Delete", lambda a=a: self._delete_alert(a)).pack(side="left", padx=2)
        button(f, "+ Add alert", lambda: self._open_alert(None)).pack(anchor="w", pady=(4, 0))

    # ---- actions ----------------------------------------------------------------------------------
    def _on_category(self, *_):
        self._build()

    def _context(self) -> dict:
        st = self._temp_stock() or self.stock
        st = copy.deepcopy(st)
        st.category = self.v_cat.get()
        tick = (self.ctrl.rows.get(self.symbol) and self.ctrl.rows[self.symbol].tick) or None
        zopts = []
        if st.category != "WATCH":
            if st.category == "SELL":
                zopts.append(("sell", "Sell range (sell zone and above)"))
            zopts += [(r["name"], r["tag"]) for r in editor_rows(st, self.cfg.zone_defaults, None)
                      if not (st.category == "SELL" and r["name"] == "sell")]
        return {"stock": st, "cfg": self.cfg, "base": stock_base(st, self.cfg.rates),
                "price": None if tick is None else tick.price, "zone_options": zopts, "engine": self.ctrl.engine}

    def _open_alert(self, alert: Optional[Alert]):
        def done(new: Alert, choice):
            if alert is not None:
                self.stock.alerts = [new if x.id == alert.id else x for x in self.stock.alerts]
            else:
                self.stock.alerts.append(new)
            if choice:
                self.choices[new.id] = choice
            self._build_alerts()
            self.on_resize()
        AlertDialog(self.winfo_toplevel(), self._context(), alert, done)

    def _delete_alert(self, alert: Alert):
        self.stock.alerts = [a for a in self.stock.alerts if a.id != alert.id]
        self._build_alerts()
        self.on_resize()

    def _save(self):
        try:
            new = self._collect()
            self.ctrl.update_stock(new, self.choices)
        except ValidationError as exc:
            self.err.config(text=str(exc))
            return
        except OSError:
            self.err.config(text="Could not save the file. Nothing was written.")
            return
        except ArithmeticError:
            self.err.config(text="Check the numbers you entered.")
            return
        self._load(self.ctrl.stock(self.symbol))
        self.err.config(text="Saved.", fg=GREEN)
        self.after(1500, lambda: self.err.winfo_exists() and self.err.config(text="", fg=RED))

    def _remove(self):
        msg = (f"Remove {self.symbol}?\n\nThis deletes its position and alerts from the watchlist. "
               "Realized profit is not saved in v1.")
        if messagebox.askyesno("Remove stock", msg, parent=self.winfo_toplevel(), icon="warning"):
            try:
                self.ctrl.remove_stock(self.symbol)
            except (ValidationError, OSError) as exc:
                self.err.config(text=str(exc) or "Could not save the file. Nothing was written.")
                return
            self.on_removed()

    # ---- live numbers ---------------------------------------------------------------------------
    def _update_live(self):
        st = self._temp_stock()
        tick = self.ctrl.rows.get(self.symbol) and self.ctrl.rows[self.symbol].tick
        info = ""
        if st is not None and st.category in ("HOLD", "SELL") and st.position:
            price = tick.price if tick else st.position.avg_price
            s = position_summary(st, price, self.cfg.rates)
            if s.get("breakeven") is not None:
                n = s["net_estimate"]
                info = (f"Breakeven {s['breakeven']:.2f} · cost basis {s['cost_basis']:.2f} (buy fee {s['buy_fee']:.2f}) · "
                        f"net at {price:.2f}: {'+' if n >= 0 else '-'}${abs(n):.2f} before tax")
        elif st is not None and st.category == "BUY" and st.planned_entry:
            info = f"Planned entry {st.planned_entry.price:.2f}"
        self.lbl_info.config(text=info)
        if st is not None and self._zlabels:
            rows = editor_rows(st, self.cfg.zone_defaults, stock_base(st, self.cfg.rates))
            for r in rows:
                if r["name"] in self._zlabels:
                    rng, prc = self._zlabels[r["name"]]
                    rng.config(text=range_text(r["lo_pct"], r["hi_pct"]))
                    prc.config(text=price_text(r["lo_price"], r["hi_price"]))
        self.refresh_price_line()

    def refresh_price_line(self):
        row = self.ctrl.rows.get(self.symbol)
        t = row.tick if row else None
        if t is None:
            self.lbl_live.config(text="No price yet.")
            return
        now = self.now_fn()
        pt = t.provider_time.astimezone(IST).strftime("%H:%M:%S IST") if t.provider_time else "n/a"
        age = int((now - (row.last_ok or t.fetched_at)).total_seconds())
        self.lbl_live.config(text=f"Current price {t.price:.2f} · provider time {pt} · data age {age} s · source {t.source}")


class AddStockDialog(tk.Toplevel):
    """+ icon: ticker (checked with the provider on the worker thread), nickname, category, position."""

    def __init__(self, master, ctrl, on_added: Callable[[str], None]):
        super().__init__(master, bg=BG)
        self.ctrl, self.on_added = ctrl, on_added
        self._checked: Optional[str] = None
        self._price = None
        self._closed = False
        n, cap = len(ctrl.symbols()), ctrl.cfg.max_symbols
        if n >= cap:                                   # blueprint 9.6: going past the cap needs a config change
            messagebox.showerror("Watchlist full", watchlist_full_message(n, cap), parent=master)
            self._closed = True
            self.destroy()
            return
        self.title("Add stock")
        self.attributes("-topmost", True)
        self.v_sym, self.v_nick = tk.StringVar(), tk.StringVar()
        self.v_cat = tk.StringVar(value="WATCH")
        self.v_shares, self.v_avg, self.v_fee = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.v_ep, self.v_es = tk.StringVar(), tk.StringVar()
        g = tk.Frame(self, bg=BG)
        g.pack(padx=16, pady=12)
        label(g, "Ticker").grid(row=0, column=0, sticky="w", pady=2)
        e = entry(g, self.v_sym, 10)
        e.grid(row=0, column=1, sticky="w")
        e.bind("<Return>", lambda ev: self._check())
        e.bind("<KeyRelease>", lambda ev: self._invalidate())
        button(g, "Check price", self._check).grid(row=0, column=2, padx=6)
        self.lbl_status = label(g, "", fg=MUTED, wraplength=320, justify="left")
        self.lbl_status.grid(row=1, column=1, columnspan=3, sticky="w")
        label(g, "Nickname").grid(row=2, column=0, sticky="w", pady=2)
        entry(g, self.v_nick, 20).grid(row=2, column=1, columnspan=2, sticky="w")
        label(g, "Category").grid(row=3, column=0, sticky="w", pady=2)
        menu(g, self.v_cat, CATEGORIES, self._layout, width=8).grid(row=3, column=1, sticky="w")
        self.f_pos, self.f_buy = tk.Frame(g, bg=BG), tk.Frame(g, bg=BG)
        for r, (t, v) in enumerate((("Shares", self.v_shares), ("Average price", self.v_avg),
                                    ("Actual buy fee (optional)", self.v_fee))):
            label(self.f_pos, t).grid(row=r, column=0, sticky="w", pady=2)
            entry(self.f_pos, v, 10).grid(row=r, column=1, padx=6)
        for r, (t, v) in enumerate((("Planned entry price", self.v_ep), ("Shares (optional)", self.v_es))):
            label(self.f_buy, t).grid(row=r, column=0, sticky="w", pady=2)
            entry(self.f_buy, v, 10).grid(row=r, column=1, padx=6)
        self.f_pos.grid(row=4, column=0, columnspan=3, sticky="w")
        self.f_buy.grid(row=5, column=0, columnspan=3, sticky="w")
        self.err = label(self, "", fg=RED, wraplength=380, justify="left")
        self.err.pack(padx=16, anchor="w")
        bar = tk.Frame(self, bg=BG)
        bar.pack(pady=10)
        self.btn_add = button(bar, "Add", self._add, bg=ACCENT)
        self.btn_add.pack(side="left", padx=4)
        self.btn_add.config(state="disabled")
        button(bar, "Cancel", self.destroy).pack(side="left", padx=4)
        self.bind("<Escape>", lambda ev: self.destroy())
        self.bind("<Destroy>", self._on_destroy)
        self._layout()
        e.focus_set()

    def _on_destroy(self, ev):
        if ev.widget is self:
            self._closed = True

    def _layout(self, *_):
        c = self.v_cat.get()
        (self.f_pos.grid if c in ("HOLD", "SELL") else self.f_pos.grid_remove)()
        (self.f_buy.grid if c == "BUY" else self.f_buy.grid_remove)()

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
        if self._closed:
            return
        if exc is not None:
            msg = str(exc) if isinstance(exc, ValidationError) else "Could not check the ticker right now (feed problem)."
            self.lbl_status.config(text=msg, fg=RED)
            self._checked = None
            self.btn_add.config(state="disabled")
            return
        self._checked, self._price = sym, tick.price
        self.lbl_status.config(text=f"{sym}: {tick.price:.2f} ({tick.source})", fg=GREEN)
        self.btn_add.config(state="normal")
        if self.v_cat.get() == "BUY" and not self.v_ep.get():
            self.v_ep.set(f"{tick.price:.2f}")

    def _add(self):
        sym = self.v_sym.get().strip().upper()
        if self._checked != sym:
            self.err.config(text="Check the ticker first.")
            return
        cat = self.v_cat.get()
        try:
            pos = ent = None
            if cat in ("HOLD", "SELL"):
                pos = Position(parse_dec(self.v_shares.get(), "Shares"), parse_dec(self.v_avg.get(), "Average price"),
                               parse_dec(self.v_fee.get(), "Actual buy fee", blank_ok=True))
            if cat == "BUY":
                ent = PlannedEntry(parse_dec(self.v_ep.get(), "Planned entry price"),
                                   parse_dec(self.v_es.get(), "Shares", blank_ok=True))
            self.ctrl.add_stock(StockConfig(sym, cat, self.v_nick.get().strip(), False, pos, ent))
        except ValidationError as exc:
            self.err.config(text=str(exc))
            return
        except OSError:
            self.err.config(text="Could not save the file. Nothing was written.")
            return
        except ArithmeticError:
            self.err.config(text="Check the numbers you entered.")
            return
        self.destroy()
        self.on_added(sym)
