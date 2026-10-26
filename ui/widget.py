"""The widget: normal view (click-through text on the desktop layer) and edit mode (a normal
interactive panel). Everything it shows comes from AppController."""
from __future__ import annotations

import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

from ui.common import (AMBER, BG, BG2, DIM, FG, GREEN, GREY, MUTED, RED,
                       TRANSPARENT_KEY, button, label)
from ui.editor import AddStockDialog, AlertDialog, ChoiceDialog, StockEditor
from market_clock import utcnow

# "monitor" is optional: None = stay on the monitor the widget is on, "primary", or a 0-based index
# (0 = primary, then left-to-right). It is ignored when pos_x/pos_y are set.
DEFAULT_LOOK = {"anchor": "top-right", "margin_x": 24, "margin_y": 24, "pos_x": None, "pos_y": None,
                "monitor": None,
                "font": "Segoe UI", "size": 11, "text_color": "#FFFFFF", "shadow_color": "#000000",
                "shadow_offset": 2, "background_mode": "transparent", "panel_color": "#202020",
                "opacity": 0.92, "padding": 12}
COLORS = {"up": GREEN, "down": RED, "neutral": GREY, "amber": AMBER, "dim": DIM}
FLASH_BG = "#4B5563"
IDLE_EDIT_S = 60


def merged_look(cfg_look: dict) -> dict:
    look = dict(DEFAULT_LOOK)
    look.update({k: v for k, v in (cfg_look or {}).items() if k in DEFAULT_LOOK})
    return look


class SummaryWindow(tk.Toplevel):
    def __init__(self, master, card: dict):
        super().__init__(master, bg=BG)
        self.title(f"Daily summary {card['date']}")
        self.attributes("-topmost", True)
        lines = [f"Daily summary for {card['date']}" + ("  (built after close)" if card.get("built_after_close") else ""),
                 "Estimates are before tax.", ""]
        for s in card["stocks"]:
            head = f"{s['symbol']} · {s['category']} · last {s['last_price'] or 'n/a'}"
            if "change_today" in s:
                head += f" · today {s['change_today']} ({s['change_today_pct']}%)"
            lines.append(head)
            if s["category"] in ("HOLD", "SELL"):
                lines.append(f"    zone: {s.get('zone') or '-'} · est. net P/L: {s.get('est_net_pl') or 'n/a'}")
            for a in s.get("alerts_fired", []):
                lines.append(f"    alert {a['alert_id']} [{a['priority']}] at {a['price']} ({a['time'][11:16]} UTC)")
        t = tk.Text(self, bg=BG2, fg=FG, font=("Consolas", 10), width=76, height=min(30, len(lines) + 1), relief="flat")
        t.insert("1.0", "\n".join(lines))
        t.config(state="disabled")
        t.pack(padx=10, pady=10)
        button(self, "Close", self.destroy).pack(pady=(0, 10))
        self.bind("<Escape>", lambda e: self.destroy())


class EditPanel(tk.Frame):
    """Header buttons, the stock list (one row open at a time) and the expanded section."""

    def __init__(self, view: "WidgetView"):
        super().__init__(view.root, bg=BG)
        self.view, self.ctrl = view, view.ctrl
        self.open_symbol = None
        self.editor = None
        self._rows: dict = {}
        self._resize_job = None
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=10, pady=(10, 4))
        button(top, "+ Add stock", self._add, bg="#3B82F6").pack(side="left")
        self.btn_summary = button(top, "Summary", self._summary)
        self.btn_summary.pack(side="left", padx=6)
        self.btn_mute = button(top, "", self._mute)
        self.btn_mute.pack(side="left")
        button(top, "Quit", view.request_quit, bg="#7F1D1D").pack(side="right")
        button(top, "Exit edit mode", view.exit_edit).pack(side="right", padx=6)
        self.lbl_head = label(self, "", fg=MUTED)
        self.lbl_head.pack(fill="x", padx=12)
        self.lbl_warn = label(self, "", fg=AMBER, wraplength=640, justify="left")
        self.lbl_warn.pack(fill="x", padx=12)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, width=660, height=100)
        self.sb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.bind_all("<MouseWheel>", self._wheel)
        self.rebuild()

    def destroy(self):
        try:
            self.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        super().destroy()

    def _wheel(self, e):
        try:
            if not e.delta or e.widget.winfo_toplevel() is not self.view.root:
                return                             # pop-up dialogs handle their own scrolling
            steps = int(-e.delta / 120) or (-1 if e.delta > 0 else 1)   # touchpads send small deltas
            self.canvas.yview_scroll(steps, "units")
        except (tk.TclError, AttributeError, KeyError):
            pass

    # ---- structure -------------------------------------------------------------------------------
    def rebuild(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self._rows, self.editor = {}, None
        for symbol in self.ctrl.symbols():
            box = tk.Frame(self.inner, bg=BG)
            box.pack(fill="x", pady=1)
            head = tk.Label(box, text="", bg=BG2, anchor="w", font=("Consolas", 10), padx=8, pady=4, cursor="hand2")
            head.pack(fill="x")
            head.bind("<Button-1>", lambda e, s=symbol: self.toggle(s))
            self._rows[symbol] = (box, head)
        if not self._rows:
            label(self.inner, "No stocks yet. Use + Add stock.", fg=MUTED).pack(padx=10, pady=10)
        if self.open_symbol in self._rows:
            self._open(self.open_symbol)
        else:
            self.open_symbol = None
        self.refresh()
        self.schedule_resize()

    def toggle(self, symbol):
        if self.open_symbol == symbol:
            self._close()
        else:
            self._close()
            self._open(symbol)
        self.refresh()
        self.schedule_resize()

    def _open(self, symbol):
        self.open_symbol = symbol
        box, _ = self._rows[symbol]
        self.editor = StockEditor(box, self.ctrl, symbol, self.view.now_fn, self.schedule_resize, self._removed)
        self.editor.pack(fill="x", pady=(1, 4))

    def _close(self):
        if self.editor is not None:
            self.editor.destroy()
        self.editor, self.open_symbol = None, None

    def _removed(self):
        self.open_symbol = None
        self.rebuild()

    # ---- header actions --------------------------------------------------------------------------
    def _add(self):
        AddStockDialog(self.view.root, self.ctrl, self._added)

    def _added(self, symbol):
        self.open_symbol = symbol                  # open it so alerts can be added (blueprint 9.6 step 4)
        self.rebuild()

    def _summary(self):
        card = self.ctrl.open_summary()
        if card:
            SummaryWindow(self.view.root, card)
        self.refresh()

    def _mute(self):
        self.ctrl.notifier.toggle_mute()
        self.refresh()

    # ---- live refresh ------------------------------------------------------------------------------
    def refresh(self):
        now = self.view.now_fn()
        hv = self.ctrl.header(now)
        self.lbl_head.config(text=f"{hv.market} · {hv.ist} · {hv.ny} · feed: {hv.feed_text}"
                                  + ("  · MUTED" if hv.muted else ""))
        self.lbl_warn.config(text="\n".join(self.ctrl.all_warnings()))
        self.btn_summary.config(text="Summary ●" if hv.summary_dot else "Summary")
        self.btn_mute.config(text="Unmute" if hv.muted else "Mute")
        flashing = self.view.flashing()
        for rv in self.ctrl.row_views(now):
            if rv.symbol not in self._rows:
                continue
            _, head = self._rows[rv.symbol]
            sel = "▾" if rv.symbol == self.open_symbol else "▸"
            text = (f"{sel} {rv.symbol:<6} {rv.nickname[:12]:<12} {rv.price_text:>9}  {rv.arrow} {rv.change_text:<17} "
                    f"{rv.category:<5} {rv.zone_tag[:18]:<18} {rv.alerts} alert{'s' if rv.alerts != 1 else ''}")
            head.config(text=text, fg=COLORS.get(rv.color, FG), bg=FLASH_BG if rv.symbol in flashing else BG2)
        if self.editor is not None and self.editor.winfo_exists():
            self.editor.refresh_price_line()

    def schedule_resize(self):
        if self._resize_job is None:
            self._resize_job = self.after_idle(self._resize)

    def _resize(self):
        self._resize_job = None
        if not self.winfo_exists():
            return
        self.update_idletasks()
        try:                                       # height of the monitor the widget is actually on
            max_h = int(self.view.layer.work_area_size()[1] * 0.85)
        except Exception:
            max_h = int(self.winfo_screenheight() * 0.85)
        chrome = self.winfo_reqheight() - self.canvas.winfo_reqheight()
        inner_h = self.inner.winfo_reqheight()
        self.canvas.config(height=max(60, min(inner_h, max_h - chrome)), width=max(660, self.inner.winfo_reqwidth()))
        self.update_idletasks()
        self.view.layer.fit(self.winfo_reqwidth() + 8, self.winfo_reqheight() + 8)


class WidgetView:
    def __init__(self, root: tk.Tk, ctrl, layer, look: dict, now_fn: Callable = utcnow,
                 request_quit: Callable = lambda: None):
        self.root, self.ctrl, self.layer, self.now_fn, self.request_quit = root, ctrl, layer, now_fn, request_quit
        self.look = merged_look(look)
        lk = self.look
        self.transparent = lk["background_mode"] == "transparent"
        self.bg = TRANSPARENT_KEY if self.transparent else lk["panel_color"]
        root.configure(bg=self.bg)
        if self.transparent:
            try:
                root.attributes("-transparentcolor", TRANSPARENT_KEY)     # Windows only
            except tk.TclError:
                pass
        self.f = tkfont.Font(root, family=lk["font"], size=lk["size"])
        self.fb = tkfont.Font(root, family=lk["font"], size=lk["size"], weight="bold")
        self.fs = tkfont.Font(root, family=lk["font"], size=max(8, lk["size"] - 2))
        self.canvas = tk.Canvas(root, bg=self.bg, highlightthickness=0, bd=0, width=10, height=10)
        self.canvas.pack()
        self.panel = None
        self._flash: dict = {}
        self._last_activity = time.monotonic()
        self._idle_job = None
        self._clock_job = None
        root.bind_all("<Button>", self._touch, add="+")
        root.bind_all("<Key>", self._touch, add="+")
        root.bind_all("<Escape>", self._escape, add="+")

    # ---- normal view -------------------------------------------------------------------------------
    @property
    def editing(self) -> bool:
        return self.panel is not None

    def flashing(self) -> set:
        now = time.monotonic()
        self._flash = {k: v for k, v in self._flash.items() if v > now}
        return set(self._flash)

    def flash(self, symbol: str, ms: int = 2000):
        self._flash[symbol] = time.monotonic() + ms / 1000
        self.refresh()
        self.root.after(ms + 60, self.refresh)

    def refresh(self):
        if self.editing:
            self.panel.refresh()
        else:
            self.render()

    def _t(self, x, y, text, fill, font, anchor="nw", width=None):
        c, lk = self.canvas, self.look
        kw = {"width": width} if width else {}
        if lk["shadow_color"]:
            o = lk["shadow_offset"]
            c.create_text(x + o, y + o, text=text, fill=lk["shadow_color"], font=font, anchor=anchor, **kw)
        return c.create_text(x, y, text=text, fill=fill, font=font, anchor=anchor, **kw)

    def render(self) -> tuple:
        """Draw the rows and fit the window to them. Returns (width, height)."""
        if self.editing:
            return (0, 0)
        now = self.now_fn()
        c, lk, f, fb = self.canvas, self.look, self.f, self.fb
        hv, rows, warns = self.ctrl.header(now), self.ctrl.row_views(now), self.ctrl.all_warnings()
        c.delete("all")
        pad, gap = lk["padding"], 14
        lh = f.metrics("linespace") + 4
        flashing = self.flashing()
        text = lk["text_color"]

        def wmax(font, vals):
            return max([font.measure(v) for v in vals] + [0])
        cw = [wmax(fb, [r.symbol for r in rows]), wmax(f, [r.nickname for r in rows]),
              wmax(f, [r.price_text for r in rows]), wmax(f, [f"{r.arrow} {r.change_text}" for r in rows]),
              wmax(f, [r.category for r in rows]), wmax(f, [r.zone_tag or r.warning for r in rows]),
              wmax(f, [f"{r.alerts} alert{'s' if r.alerts != 1 else ''}" for r in rows])]
        xs, x = [], pad
        for w_ in cw:
            xs.append(x)
            x += w_ + gap
        table_w = max(x - gap, pad) - pad
        y = pad
        # header: market, IST + NY time, feed dot, summary dot, mute
        hx = pad
        mcol = {"OPEN": GREEN, "PRE": AMBER, "AFTER": AMBER, "CLOSED": GREY}.get(hv.market, GREY)
        for s, col, font in ((hv.market, mcol, fb), (f"   {hv.ist}  ·  {hv.ny}", text, f)):
            item = self._t(hx, y, s, col, font)
            hx = c.bbox(item)[2]
        r_ = lh // 4
        dot = {"green": GREEN, "amber": AMBER, "red": RED}.get(hv.feed_dot, GREY)
        mid = y + lh // 2
        c.create_oval(hx + 14, mid - r_, hx + 14 + 2 * r_, mid + r_, fill=dot, outline="")
        item = self._t(hx + 14 + 2 * r_ + 6, y, hv.feed_text, dot, self.fs)
        hx = c.bbox(item)[2]
        if hv.summary_dot:
            c.create_oval(hx + 12, mid - r_, hx + 12 + 2 * r_, mid + r_, fill="#60A5FA", outline="")
            hx += 12 + 2 * r_
        if hv.muted:
            item = self._t(hx + 12, y, "MUTED", AMBER, self.fs)
            hx = c.bbox(item)[2]
        y += lh + 4
        for r in rows:
            if r.symbol in flashing:
                c.create_rectangle(pad - 6, y - 2, pad + table_w + 6, y + lh - 2, fill=FLASH_BG, outline="")
            col = COLORS.get(r.color, text)
            self._t(xs[0], y, r.symbol, col, fb)
            self._t(xs[1], y, r.nickname, text if r.color != "dim" else DIM, f)
            self._t(xs[2] + cw[2], y, r.price_text, col, f, anchor="ne")
            self._t(xs[3], y, f"{r.arrow} {r.change_text}".strip(), col, f)
            self._t(xs[4], y, r.category, DIM if r.color == "dim" else GREY, f)
            self._t(xs[5], y, r.zone_tag or r.warning, AMBER if (r.warning and not r.zone_tag) else col, f)
            self._t(xs[6], y, f"{r.alerts} alert{'s' if r.alerts != 1 else ''}", DIM if r.color == "dim" else GREY, f)
            y += lh
        if not rows:
            item = self._t(pad, y, "No stocks yet. Alt+Shift+W, then + Add stock.", GREY, f)
            table_w = max(table_w, c.bbox(item)[2] - pad)
            y += lh
        wrap = max(360, table_w, hx - pad)
        for msg in warns:
            item = self._t(pad, y + 4, msg, AMBER, self.fs, width=wrap)
            y = c.bbox(item)[3] + 2
        total_w = max(wrap, table_w, hx - pad) + 2 * pad + (lk["shadow_offset"] if lk["shadow_color"] else 0) + 8
        total_h = y + pad + (lk["shadow_offset"] if lk["shadow_color"] else 0)
        c.config(width=total_w, height=total_h)
        self.layer.fit(total_w, total_h)
        return total_w, total_h

    # ---- clock tick: the header shows minutes ----------------------------------------------------
    def start_clock(self):
        def tick():
            try:
                self.refresh()
            finally:                               # one failed refresh must not stop the clock
                now = time.time()
                self._clock_job = self.root.after(int((60 - now % 60) * 1000) + 50, tick)
        tick()

    # ---- edit mode ---------------------------------------------------------------------------------
    def toggle_edit(self):
        (self.exit_edit if self.editing else self.enter_edit)()

    def enter_edit(self):
        if self.editing:
            return
        self.layer.set_interactive(True)
        self.canvas.pack_forget()
        self.root.configure(bg=BG)
        try:
            self.panel = EditPanel(self)
        except Exception:                          # don't leave a blank, stuck window behind
            self.panel = None
            for w in list(self.root.winfo_children()):
                if isinstance(w, EditPanel):
                    w.destroy()
            self.root.configure(bg=self.bg)
            self.canvas.pack()
            self.layer.set_interactive(False)
            raise
        self.panel.pack(fill="both", expand=True)
        self._touch()
        self._schedule_idle()

    def exit_edit(self):
        if not self.editing:
            return
        for w in list(self.root.winfo_children()):
            if isinstance(w, (AlertDialog, ChoiceDialog, AddStockDialog, SummaryWindow)):
                w.destroy()
        self.panel.destroy()
        self.panel = None
        if self._idle_job is not None:
            self.root.after_cancel(self._idle_job)
            self._idle_job = None
        self.root.configure(bg=self.bg)
        self.canvas.pack()
        self.layer.set_interactive(False)          # back on the desktop layer
        self.render()

    def _touch(self, event=None):
        self._last_activity = time.monotonic()

    def _escape(self, event):
        try:
            on_root = event.widget.winfo_toplevel() is self.root
        except (tk.TclError, AttributeError, KeyError):
            return
        if self.editing and on_root:
            self.exit_edit()                        # Esc leaves immediately

    def _schedule_idle(self):
        def check():
            self._idle_job = None
            if not self.editing:
                return
            if time.monotonic() - self._last_activity >= IDLE_EDIT_S:
                self.exit_edit()
            else:
                self._idle_job = self.root.after(1000, check)
        self._idle_job = self.root.after(1000, check)
