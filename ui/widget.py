"""The widget: normal view (click-through text on the desktop layer) and edit mode (a normal
interactive panel). Everything it shows comes from AppController."""
from __future__ import annotations

import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

from ui.common import (ACCENT, AMBER, BG, BG2, BTN, DIM, FG, GREEN, GREY,
                       MUTED, RED, TRANSPARENT_KEY, button, label)
from ui.editor import AddStockDialog, AlertDialog, ApiKeysStatusDialog, ChoiceDialog, StockEditor
from ui.mini_tracker import MiniTrackerWindow, rounded_rect_points
from market_clock import utcnow

# "monitor" is optional: None = stay on the monitor the widget is on, "primary", or a 0-based index
# (0 = primary, then left-to-right). It is ignored when pos_x/pos_y are set.
DEFAULT_LOOK = {"anchor": "top-right", "margin_x": 24, "margin_y": 24, "pos_x": None, "pos_y": None,
                "monitor": None, "click_through": False,
                "font": "Segoe UI", "size": 11, "text_color": "#FFFFFF", "shadow_color": "#000000",
                "shadow_offset": 0, "background_mode": "panel", "panel_color": "#202020",
                "opacity": 0.92, "padding": 10}
COLORS = {"up": GREEN, "down": RED, "neutral": GREY, "amber": AMBER, "dim": DIM}
DIM_DOT_COLORS = {"green": "#1E5631", "amber": "#785116", "red": "#6E1D1D"}
PULSE_DOT_COLORS = {"green": "#86EFAC", "amber": "#FCD34D", "red": "#FCA5A5"}
FLASH_BG = "#4B5563"
TOGGLE_W, TOGGLE_H, TOGGLE_GAP = 32, 16, 12      # Mini Tracker switch (far right of the header)
TOGGLE_OFF = {"track": BG2, "outline": MUTED, "thumb": FG, "thumb_outline": ""}
TOGGLE_ON = {"track": ACCENT, "outline": ACCENT, "thumb": "#FFFFFF", "thumb_outline": ""}
IDLE_EDIT_S = 60
_EDIT_DIALOG_TYPES = (AddStockDialog, AlertDialog, ApiKeysStatusDialog, ChoiceDialog)


def merged_look(cfg_look: dict) -> dict:
    raw = cfg_look or {}
    look = dict(DEFAULT_LOOK)
    look.update({k: v for k, v in raw.items() if k in DEFAULT_LOOK})
    if "time_font" in raw or "time_format" in raw:
        for k in ("background_mode", "panel_color", "shadow_offset", "padding"):
            look[k] = DEFAULT_LOOK[k]
    return look


class EditPanel(tk.Frame):
    """Header buttons, the stock list (one row open at a time) and the expanded section."""

    def __init__(self, view: "WidgetView"):
        super().__init__(view.root, bg=BG)
        self.view, self.ctrl = view, view.ctrl
        self.open_symbol = None
        self.editor = None
        self._add_dlg = None
        self._keys_dlg = None
        self._rows: dict = {}
        self._resize_job = None
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=10, pady=(10, 4))
        button(top, "+ Add stock", self._add, bg="#3B82F6").pack(side="left")
        self.btn_mute = button(top, "", self._mute)
        self.btn_mute.pack(side="left", padx=6)
        self.btn_mini = button(top, "Mini Tracker", view.toggle_mini, bg=ACCENT if view.mini_open else BTN)
        self.btn_mini.pack(side="left", padx=6)
        button(top, "API Keys", self._open_keys_status).pack(side="left", padx=6)
        button(top, "Test Alert", self._test_alert).pack(side="left", padx=6)
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
        self._wheel_bind_id = self.view.root.bind("<MouseWheel>", self._wheel, add="+")
        self.rebuild()

    def destroy(self):
        if getattr(self, "_wheel_bind_id", None):
            try:
                self.view.root.unbind("<MouseWheel>", self._wheel_bind_id)
            except tk.TclError:
                pass
            self._wheel_bind_id = None
        for attr in ("_add_dlg", "_keys_dlg"):
            dlg = getattr(self, attr, None)
            if dlg is not None:
                try:
                    if dlg.winfo_exists():
                        dlg.destroy()
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        super().destroy()

    def _wheel(self, e):
        try:
            if not e.delta or e.widget.winfo_toplevel() is not self.view.root:
                return                             # pop-up dialogs handle their own scrolling
            self.view._touch()
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
        if self._add_dlg is not None and self._add_dlg.winfo_exists():
            self._add_dlg.lift()
            self._add_dlg.focus_force()
            return
        dlg = AddStockDialog(self.view.root, self.ctrl, self._added)
        if dlg.winfo_exists():
            self._add_dlg = dlg

    def _added(self, symbol):
        self._add_dlg = None
        self.open_symbol = symbol                  # open it so alerts can be added (blueprint 9.6 step 4)
        self.rebuild()

    def _mute(self):
        self.ctrl.notifier.toggle_mute()
        self.refresh()

    def _open_keys_status(self):
        if self._keys_dlg is not None:
            try:
                if self._keys_dlg.winfo_exists():
                    self._keys_dlg.lift()
                    self._keys_dlg.focus_force()
                    return
            except tk.TclError:
                pass
        dlg = ApiKeysStatusDialog(self.view.root, self.ctrl)
        if dlg.winfo_exists():
            self._keys_dlg = dlg

    def _test_alert(self):
        self.ctrl.send_test_alert(self.view.now_fn())
        self.view.refresh()

    # ---- live refresh ------------------------------------------------------------------------------
    def refresh(self):
        now = self.view.now_fn()
        hv = self.ctrl.header(now)
        strip = self.ctrl.status_strip(now)
        prob_txt = "  ".join(f"{p.glyph}{(' ' + p.badge) if p.badge else ''}" for p in strip.problems)
        if strip.overflow_count > 0:
            prob_txt += f" +{strip.overflow_count}"
        self.lbl_head.config(
            text=f"{strip.session_icon} {hv.market} · {hv.ist} · {hv.ny} · {strip.heartbeat_tooltip}"
                 + (f" · {prob_txt}" if prob_txt else "")
                 + ("  · MUTED" if hv.muted else "")
        )
        self.lbl_warn.config(text="\n".join(self.ctrl.all_warnings()))
        self.btn_mute.config(text="Unmute" if hv.muted else "Mute")
        self.btn_mini.config(bg=ACCENT if self.view.mini_open else BTN)
        flashing = self.view.flashing()
        for rv in self.ctrl.row_views(now):
            if rv.symbol not in self._rows:
                continue
            _, head = self._rows[rv.symbol]
            sel = "▾" if rv.symbol == self.open_symbol else "▸"
            ph = getattr(rv, "ph_price_text", "—")
            rh = getattr(rv, "rh_price_text", "—")
            ah = getattr(rv, "ah_price_text", "—")
            text = (f"{sel} {rv.symbol:<6} {rv.nickname[:12]:<12} "
                    f"PH {ph:>7}  RH {rh:>7}  AH {ah:>7}  {rv.arrow} {rv.change_text:<17} "
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
        self.fbadge = tkfont.Font(root, family=lk["font"], size=max(7, lk["size"] - 3), weight="bold")
        if self.transparent:
            self.canvas = tk.Canvas(root, bg=self.bg, highlightthickness=0, bd=0, width=10, height=10)
        else:
            self.canvas = tk.Canvas(
                root, bg=self.bg, highlightthickness=1,
                highlightbackground="#444444", highlightcolor="#444444", bd=0,
                width=10, height=10,
            )
        self.canvas.pack()
        self.panel = None
        self._flash: dict = {}
        self._last_activity = time.monotonic()
        self._idle_job = None
        self._clock_job = None
        self._pulse_job = None
        self._last_seen_heartbeat = None
        self._hb_core_id = None
        self._hb_ring_id = None
        self._hb_center = (0, 0)
        self._hb_base_r = 4.5
        self._hb_color = "green"
        self._hb_dim = False
        self.tooltips: dict[str, str] = {}         # canvas tag -> tooltip text
        self._active_tooltip_tag: str | None = None
        self._tip_win: tk.Toplevel | None = None
        self._tip_lbl: tk.Label | None = None
        self.mini: MiniTrackerWindow | None = None
        self.ctrl.add_quote_listener(lambda: self.root.after(0, self._refresh_mini))
        if callable(getattr(getattr(self.ctrl, "notifier", None), "add_listener", None)):
            self.ctrl.notifier.add_listener(lambda: self.root.after(0, self._refresh_mini))
        try:
            if self.ctrl.get_mini_tracker_state().get("enabled"):
                self.root.after(100, self.toggle_mini)
        except Exception:
            pass
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
        self._refresh_mini()
        self.root.after(ms + 60, lambda: (self.refresh(), self._refresh_mini()))

    def refresh(self):
        if self.editing:
            self.panel.refresh()
        else:
            self.render()
        self._refresh_mini()

    def _t(self, x, y, text, fill, font, anchor="nw", width=None, tags=()):
        c, lk = self.canvas, self.look
        kw = {"width": width} if width else {}
        if tags:
            kw["tags"] = tags
        if lk["shadow_color"] and lk["shadow_offset"]:
            o = lk["shadow_offset"]
            c.create_text(x + o, y + o, text=text, fill=lk["shadow_color"], font=font, anchor=anchor, **kw)
        return c.create_text(x, y, text=text, fill=fill, font=font, anchor=anchor, **kw)

    # ---- hover tooltips ----------------------------------------------------------------------------
    def _bind_tooltip(self, tag: str, text: str) -> None:
        if not text:
            return
        self.tooltips[tag] = text
        self.canvas.tag_bind(tag, "<Enter>", lambda e, t=tag: self.show_tooltip(t, e))
        self.canvas.tag_bind(tag, "<Leave>", lambda e, t=tag: self.hide_tooltip(t))

    def _primary_work_area(self) -> tuple[int, int, int, int]:
        try:
            from desktop_layer import work_area
            return work_area()
        except Exception:
            return 0, 0, max(1, self.root.winfo_screenwidth()), max(1, self.root.winfo_screenheight())

    def show_tooltip(self, tag: str, event=None) -> str:
        """Display a crisp single-line tooltip for `tag`. Returns the tooltip text."""
        text = self.tooltips.get(tag, "")
        if not text:
            return ""
        self._active_tooltip_tag = tag
        try:
            if self._tip_win is None or not self._tip_win.winfo_exists():
                self._tip_win = tk.Toplevel(self.root, bg="#111827", bd=1, relief="solid")
                self._tip_win.overrideredirect(True)
                try:
                    self._tip_win.attributes("-topmost", True)
                except tk.TclError:
                    pass
                self._tip_lbl = tk.Label(
                    self._tip_win, text=text, bg="#111827", fg="#F9FAFB",
                    font=self.fs, padx=6, pady=2, justify="left"
                )
                self._tip_lbl.pack()
            else:
                self._tip_lbl.config(text=text)
            if event is not None and hasattr(event, "x_root") and hasattr(event, "y_root"):
                x = event.x_root + 10
                y = event.y_root + 16
            else:
                bbox = self.canvas.bbox(tag) or (0, 0, 0, 0)
                x = self.canvas.winfo_rootx() + bbox[0]
                y = self.canvas.winfo_rooty() + bbox[3] + 4
            self._tip_win.update_idletasks()
            tw = self._tip_win.winfo_reqwidth()
            th = self._tip_win.winfo_reqheight()
            l, t, r, b = self._primary_work_area()
            x = max(l, min(x, r - tw))
            y = max(t, min(y, b - th))
            self._tip_win.geometry(f"+{x}+{y}")
            self._tip_win.deiconify()
        except tk.TclError:
            pass
        return text

    def hide_tooltip(self, tag: str | None = None) -> None:
        if tag is not None and self._active_tooltip_tag not in (None, tag):
            return
        self._active_tooltip_tag = None
        if self._tip_win is not None:
            try:
                self._tip_win.withdraw()
            except tk.TclError:
                pass

    def pulse_heartbeat(self):
        """Manually trigger the pulse animation (useful for testing or demo)."""
        self._trigger_heartbeat_pulse()

    def _trigger_heartbeat_pulse(self) -> None:
        """Trigger an expanding sonar ripple and organic core bloom on fetch."""
        if self._pulse_job is not None:
            try:
                self.root.after_cancel(self._pulse_job)
            except tk.TclError:
                pass
            self._pulse_job = None

        if self.editing or self._hb_core_id is None:
            return

        cx, cy = self._hb_center
        base_r = self._hb_base_r
        col = self._hb_color
        dim = self._hb_dim

        # Animation frames: (core_radius_boost, core_color, ring_radius, ring_outline_color, ring_width)
        if dim:
            frames = [
                (1.8, "#22C55E", base_r + 2.0, "#1E5631", 2),
                (1.4, "#1E5631", base_r + 4.5, "#166534", 1.5),
                (0.8, "#1E5631", base_r + 7.0, "#14532D", 1.0),
                (0.4, "#1E5631", base_r + 9.5, "#052E16", 1.0),
                (0.0, DIM_DOT_COLORS.get(col, DIM), 0.0, "", 0),
            ]
        elif col == "red":
            frames = [
                (3.2, "#FECACA", base_r + 2.0, "#F87171", 2.5),
                (2.6, "#F87171", base_r + 4.5, "#EF4444", 2.0),
                (2.0, "#EF4444", base_r + 7.5, "#DC2626", 2.0),
                (1.4, "#DC2626", base_r + 10.5, "#B91C1C", 1.5),
                (0.8, "#DC2626", base_r + 13.5, "#991B1B", 1.5),
                (0.4, "#DC2626", base_r + 16.0, "#7F1D1D", 1.0),
                (0.1, "#DC2626", base_r + 18.0, "#450A0A", 1.0),
                (0.0, RED, 0.0, "", 0),
            ]
        elif col == "amber":
            frames = [
                (3.2, "#FEF3C7", base_r + 2.0, "#FBBF24", 2.5),
                (2.6, "#FBBF24", base_r + 4.5, "#F59E0B", 2.0),
                (2.0, "#F59E0B", base_r + 7.5, "#D97706", 2.0),
                (1.4, "#D97706", base_r + 10.5, "#B45309", 1.5),
                (0.8, "#D97706", base_r + 13.5, "#92400E", 1.5),
                (0.4, "#D97706", base_r + 16.0, "#78350F", 1.0),
                (0.1, "#D97706", base_r + 18.0, "#451A03", 1.0),
                (0.0, AMBER, 0.0, "", 0),
            ]
        else:  # vivid emerald green live heartbeat
            frames = [
                (3.2, "#DCFCE7", base_r + 2.5, "#86EFAC", 2.5),
                (2.6, "#86EFAC", base_r + 5.0, "#4ADE80", 2.0),
                (2.0, "#4ADE80", base_r + 8.0, "#22C55E", 2.0),
                (1.4, "#22C55E", base_r + 11.0, "#16A34A", 1.5),
                (0.8, "#22C55E", base_r + 14.0, "#15803D", 1.5),
                (0.4, "#22C55E", base_r + 16.5, "#166534", 1.0),
                (0.1, "#22C55E", base_r + 18.5, "#14532D", 1.0),
                (0.0, GREEN, 0.0, "", 0),
            ]

        def step(idx: int):
            if idx >= len(frames) or self.editing:
                self._pulse_job = None
                return
            boost, c_col, r_rad, r_col, r_w = frames[idx]
            try:
                if self._hb_core_id and self.canvas.winfo_exists():
                    cr = base_r + boost
                    self.canvas.coords(self._hb_core_id, cx - cr, cy - cr, cx + cr, cy + cr)
                    self.canvas.itemconfig(self._hb_core_id, fill=c_col)
                    if r_rad > 0 and r_col:
                        self.canvas.coords(self._hb_ring_id, cx - r_rad, cy - r_rad, cx + r_rad, cy + r_rad)
                        self.canvas.itemconfig(self._hb_ring_id, outline=r_col, width=r_w)
                    else:
                        self.canvas.itemconfig(self._hb_ring_id, outline="", width=0)

                self._pulse_job = self.root.after(35, lambda: step(idx + 1))
            except tk.TclError:
                self._pulse_job = None

        step(0)

    def render(self) -> tuple:
        """Draw the corner status strip and stock rows, then fit the window. Returns (width, height)."""
        if self.editing:
            return (0, 0)
        now = self.now_fn()
        c, lk, f, fb = self.canvas, self.look, self.f, self.fb
        strip = self.ctrl.status_strip(now)
        hv = self.ctrl.header(now)
        rows = self.ctrl.row_views(now)
        warns = self.ctrl.all_warnings()

        # Cancel any in-flight pulse before clearing canvas
        if self._pulse_job is not None:
            try:
                self.root.after_cancel(self._pulse_job)
            except tk.TclError:
                pass
            self._pulse_job = None

        new_fetch = (strip.last_heartbeat_at is not None and strip.last_heartbeat_at != self._last_seen_heartbeat)
        if new_fetch:
            self._last_seen_heartbeat = strip.last_heartbeat_at

        c.delete("all")
        self.tooltips.clear()
        pad, gap = lk["padding"], 10
        text_h = f.metrics("linespace")
        lh = text_h + 2
        chip_h = text_h + 5
        row_gap = 2
        row_step = chip_h + row_gap
        card_pad = max(4, pad - 4)
        card_x1 = card_pad
        flashing = self.flashing()
        text = lk["text_color"]

        def wmax(font, vals):
            return max([font.measure(v) for v in vals] + [0])
        cw = [wmax(fb, [r.symbol for r in rows]), wmax(f, [r.nickname for r in rows]),
              wmax(fb, ["PH", *(getattr(r, "ph_price_text", "—") for r in rows)]),
              wmax(fb, ["RH", *(getattr(r, "rh_price_text", "—") for r in rows)]),
              wmax(fb, ["AH", *(getattr(r, "ah_price_text", "—") for r in rows)]),
              wmax(f, [f"{r.arrow} {r.change_text}" for r in rows]),
              wmax(f, [r.category for r in rows]), wmax(f, [r.zone_tag or r.warning for r in rows]),
              wmax(f, [f"{r.alerts} alert{'s' if r.alerts != 1 else ''}" for r in rows])]
        xs, x = [], pad
        for w_ in cw:
            xs.append(x)
            x += w_ + gap
        table_w = max(x - gap, pad) - pad
        y = pad
        hdr_y1 = card_pad
        hdr_y2 = pad + lh + 3
        header_card = None
        if not self.transparent:
            header_card = c.create_polygon(
                rounded_rect_points(card_x1, hdr_y1, card_x1 + 120, hdr_y2, 4),
                smooth=True, fill=BG2, outline="#444444", width=1, tags=("header_card",),
            )

        # ---- Corner Status Strip: [ ☀️ / 🌙 ] [ Heartbeat Dot ] [ Clocks ] [ Problems ] [ Mini ] [ ⚙ ]
        hx = pad
        mid = y + lh // 2

        # 1. Sun / Moon Icon
        sun_moon_tag = "status_sun_moon"
        sm_item = self._t(hx, y, strip.session_icon, text, fb, tags=(sun_moon_tag,))
        self._bind_tooltip(sun_moon_tag, strip.session_tooltip)
        hx = c.bbox(sm_item)[2] + 10

        # 2. Heartbeat Dot & Sonar Ripple Wave
        hb_tag = "status_heartbeat"
        base_r = max(4.5, min(6.0, lh / 3.8))
        self._hb_base_r = base_r
        self._hb_color = strip.heartbeat_color
        self._hb_dim = strip.heartbeat_dim

        # Dedicated arena for expanding sonar ripple
        cx = hx + int(base_r + 6)
        cy = mid
        self._hb_center = (cx, cy)

        if strip.heartbeat_dim:
            core_col = DIM_DOT_COLORS.get(strip.heartbeat_color, DIM)
        else:
            core_col = {"green": GREEN, "amber": AMBER, "red": RED}.get(strip.heartbeat_color, GREY)

        # Expanding Ripple Ring (under the core dot)
        self._hb_ring_id = c.create_oval(
            cx - base_r, cy - base_r, cx + base_r, cy + base_r,
            outline="", width=0, tags=(hb_tag, "status_heartbeat_ring")
        )

        # Solid Core Dot
        self._hb_core_id = c.create_oval(
            cx - base_r, cy - base_r, cx + base_r, cy + base_r,
            fill=core_col, outline="", tags=(hb_tag,)
        )
        self._bind_tooltip(hb_tag, strip.heartbeat_tooltip)
        hx = cx + int(base_r + 10)

        # 3. Direct Clocks (IST | NY) - crisp FG text visible at a glance
        clock_str = f"{hv.ist}  |  {hv.ny}"
        clk_item = self._t(hx, y, clock_str, FG, f, tags=("status_clocks",))
        self._bind_tooltip("status_clocks", f"{strip.session_tooltip} · {strip.heartbeat_tooltip}")
        hx = c.bbox(clk_item)[2] + 12

        # 4. Problem Icons Strip (ordered Red first, then Amber, with key badges & +N overflow)
        for idx, prob in enumerate(strip.problems):
            ptag = f"status_problem_{idx}"
            pcol = RED if prob.color == "red" else AMBER
            g_item = self._t(hx, y, prob.glyph, pcol, self.fs, tags=(ptag,))
            hx = c.bbox(g_item)[2]
            if prob.badge:
                b_item = self._t(hx + 2, y + 1, prob.badge, pcol, self.fbadge, tags=(ptag,))
                hx = c.bbox(b_item)[2]
            self._bind_tooltip(ptag, prob.tooltip)
            hx += 10

        if strip.overflow_count > 0:
            otag = "status_problem_overflow"
            o_item = self._t(hx, y + 1, f"+{strip.overflow_count}", AMBER, self.fbadge, tags=(otag,))
            self._bind_tooltip(otag, strip.overflow_tooltip or f"{strip.overflow_count} more problem(s)")
            hx = c.bbox(o_item)[2] + 10

        if hv.muted:
            mtag = "status_muted"
            m_item = self._t(hx, y, "🔇", AMBER, self.fs, tags=(mtag,))
            self._bind_tooltip(mtag, "Alerts muted")
            hx = c.bbox(m_item)[2] + 8

        # 5. Mini Tracker switch + Settings gear: reserve room now, draw right-aligned once width is known
        mini_open = self.mini_open
        gear_w, gear_gap = 18, 6
        mini_w = self.fs.measure("Mini") + 6 + TOGGLE_W
        header_mid = mid
        hx_end = hx + TOGGLE_GAP + mini_w + 2 + gear_gap + gear_w

        y = hdr_y2 + 3
        strip_div_y = y
        strip_div = c.create_line(card_x1, strip_div_y, card_x1 + 1, strip_div_y, fill="#333333", width=1)
        y += 4

        # ---- Stock rows ----------------------------------------------------------------------------
        raw_sess = getattr(strip, "session", "REGULAR")
        active_sess = getattr(raw_sess, "name", getattr(raw_sess, "value", raw_sess))
        if active_sess == "OPEN":
            active_sess = "REGULAR"
        hdr_div = None
        hdr_div_y = 0
        if rows:
            lh_small = self.fs.metrics("linespace") + 2
            for col_idx, hdr_txt, sess_key in ((2, "PH", "PRE"), (3, "RH", "REGULAR"), (4, "AH", "AFTER")):
                is_active_col = (active_sess == sess_key)
                hdr_col = ACCENT if is_active_col else GREY
                hdr_item = self._t(xs[col_idx] + cw[col_idx], y, hdr_txt, hdr_col, self.fs, anchor="ne")
                if is_active_col:
                    hb = c.bbox(hdr_item)
                    if hb:
                        uy = hb[3]
                        c.create_line(hb[0], uy, hb[2], uy, fill=ACCENT, width=2)
            y += lh_small + 2
            hdr_div_y = y - 1
            hdr_div = c.create_line(card_x1, hdr_div_y, card_x1 + 1, hdr_div_y, fill="#2A2A2A", width=1)
            y += 3
        row_chips = []
        flash_rects = []
        for r in rows:
            ry1, ry2 = y, y + chip_h
            if r.symbol in flashing:
                flash_rects.append((
                    c.create_rectangle(card_x1, ry1, card_x1 + max(table_w, 120), ry2,
                                       fill=FLASH_BG, outline=AMBER, width=1),
                    ry1, ry2,
                ))
            elif not self.transparent:
                row_chips.append((
                    c.create_polygon(
                        rounded_rect_points(card_x1, ry1, card_x1 + max(table_w, 120), ry2, 4),
                        smooth=True, fill=BG2, outline="#444444", width=1,
                    ),
                    ry1, ry2,
                ))
            ty = y + 2
            is_warn = r.color == "amber"
            sym_col = AMBER if is_warn else FG
            chg_col = AMBER if is_warn else (GREEN if r.arrow == "▲" else (RED if r.arrow == "▼" else MUTED))
            if is_warn:
                ph_col = rh_col = ah_col = AMBER
                ph_font = rh_font = ah_font = f
            elif active_sess == "PRE":
                ph_col, rh_col, ah_col = FG, MUTED, MUTED
                ph_font, rh_font, ah_font = fb, f, f
            elif active_sess == "REGULAR":
                ph_col, rh_col, ah_col = MUTED, FG, MUTED
                ph_font, rh_font, ah_font = f, fb, f
            elif active_sess == "AFTER":
                ph_col, rh_col, ah_col = MUTED, MUTED, FG
                ph_font, rh_font, ah_font = f, f, fb
            else:
                ph_col, rh_col, ah_col = MUTED, FG, FG
                ph_font, rh_font, ah_font = f, f, f
            tail_txt = r.zone_tag or r.warning
            if r.warning or is_warn:
                tail_col = AMBER
            elif "BUY" in tail_txt.upper():
                tail_col = GREEN
            elif "SELL" in tail_txt.upper():
                tail_col = RED
            else:
                tail_col = FG
            self._t(xs[0], ty, r.symbol, sym_col, fb)
            self._t(xs[1], ty, r.nickname, MUTED, f)
            self._t(xs[2] + cw[2], ty, getattr(r, "ph_price_text", "—"), ph_col, ph_font, anchor="ne")
            self._t(xs[3] + cw[3], ty, getattr(r, "rh_price_text", "—"), rh_col, rh_font, anchor="ne")
            self._t(xs[4] + cw[4], ty, getattr(r, "ah_price_text", "—"), ah_col, ah_font, anchor="ne")
            self._t(xs[5], ty, f"{r.arrow} {r.change_text}".strip(), chg_col, f)
            self._t(xs[6], ty, r.category, MUTED, f)
            self._t(xs[7], ty, tail_txt, tail_col, f)
            self._t(xs[8], ty, f"{r.alerts} alert{'s' if r.alerts != 1 else ''}", MUTED, f)
            y += row_step
        if not rows:
            item = self._t(pad, y, "No stocks yet. Alt+Shift+W, then + Add stock.", GREY, f)
            table_w = max(table_w, c.bbox(item)[2] - pad)
            y += lh
        wrap = max(360, table_w, hx_end - pad)
        for msg in warns:
            item = self._t(pad, y + 4, msg, AMBER, self.fs, width=wrap)
            y = c.bbox(item)[3] + 2
        shadow_pad = lk["shadow_offset"] if (lk["shadow_color"] and lk["shadow_offset"]) else 0
        total_w = max(wrap, table_w, hx_end - pad) + 2 * pad + shadow_pad + 4
        if rows and not warns:
            y -= row_gap
        total_h = y + card_pad + shadow_pad
        right_edge = total_w - pad - shadow_pad
        card_x2 = total_w - card_pad - shadow_pad
        if header_card is not None:
            c.coords(header_card, *rounded_rect_points(card_x1, hdr_y1, card_x2, hdr_y2, 4))
        for chip_id, ry1, ry2 in row_chips:
            c.coords(chip_id, *rounded_rect_points(card_x1, ry1, card_x2, ry2, 4))
        for rect_id, ry1, ry2 in flash_rects:
            c.coords(rect_id, card_x1, ry1, card_x2, ry2)
        c.coords(strip_div, card_x1, strip_div_y, card_x2, strip_div_y)
        if hdr_div is not None:
            c.coords(hdr_div, card_x1, hdr_div_y, card_x2, hdr_div_y)
        gear_x2 = right_edge
        mini_x1 = gear_x2 - gear_w - gear_gap - mini_w - 2
        self._draw_mini_toggle(mini_x1, header_mid, mini_open)
        self._draw_settings_gear(gear_x2, header_mid, gear_w)
        c.config(width=total_w, height=total_h)
        bw = 0 if self.transparent else 1
        self.layer.fit(total_w + 2 * bw, total_h + 2 * bw)
        if new_fetch:
            self._trigger_heartbeat_pulse()
        return total_w, total_h

    # ---- Mini Tracker switch & Settings gear -----------------------------------------------------
    @property
    def mini_open(self) -> bool:
        if self.mini is not None and not self.mini.winfo_exists():
            self.mini = None                       # closed behind our back: stay in sync
        return self.mini is not None

    def _draw_mini_toggle(self, x1: int, cy: int, on: bool) -> None:
        """Pill switch matching reference design:
        Label 'Mini' on left + pill toggle on right.
        OFF: hollow outline track with light thumb on left.
        ON: filled accent track with white thumb on right."""
        c, tag = self.canvas, "status_mini_tracker"
        st = TOGGLE_ON if on else TOGGLE_OFF

        lbl_col = ACCENT if on else GREY
        lh = self.fs.metrics("linespace")
        lbl_item = self._t(x1, cy - lh // 2, "Mini", lbl_col, self.fs, tags=(tag,))
        bbox = c.bbox(lbl_item)
        sw_x = (bbox[2] + 6) if bbox else (x1 + 30)

        y1, x2, y2 = cy - TOGGLE_H // 2, sw_x + TOGGLE_W, cy - TOGGLE_H // 2 + TOGGLE_H
        c.create_polygon(rounded_rect_points(sw_x, y1, x2, y2, TOGGLE_H // 2), smooth=True,
                         fill=st["track"], outline=st["outline"], width=2,
                         tags=(tag, "mini_toggle_track"))
        d, m = TOGGLE_H - 6, 3
        tx = x2 - m - d if on else sw_x + m
        c.create_oval(tx, y1 + m, tx + d, y1 + m + d, fill=st["thumb"], outline=st.get("thumb_outline", ""),
                      width=1, tags=(tag, "mini_toggle_thumb"))
        self._bind_tooltip(tag, "Disable Mini Tracker" if on else "Enable Mini Tracker")
        c.tag_bind(tag, "<Button-1>", self.on_mini_toggle_click)
        c.tag_bind(tag, "<Enter>", lambda e: c.config(cursor="hand2"), add="+")
        c.tag_bind(tag, "<Leave>", lambda e: c.config(cursor=""), add="+")

    def on_mini_toggle_click(self, event=None) -> None:
        self.hide_tooltip()
        self.toggle_mini()

    def _draw_settings_gear(self, x2: int, cy: int, w: int = 18) -> None:
        """Compact ⚙ icon button right beside the Mini toggle to enter Edit Mode."""
        c, tag = self.canvas, "status_settings_gear"
        gx1, gx2 = x2 - w, x2
        gy1, gy2 = cy - 9, cy + 9
        base_fill = "" if self.transparent else BG2
        outline_col = "" if self.transparent else "#444444"
        bg_poly = c.create_polygon(
            rounded_rect_points(gx1, gy1, gx2, gy2, 4), smooth=True,
            fill=base_fill, outline=outline_col, width=1,
            tags=(tag, "settings_gear_bg"),
        )
        glyph = c.create_text(
            (gx1 + gx2) // 2, cy, text="⚙", fill=MUTED,
            font=(self.look["font"], max(10, self.look["size"])),
            anchor="center", tags=(tag, "settings_gear_icon"),
        )
        self._bind_tooltip(tag, "Settings / Edit Mode (Alt+Shift+W)")

        def _on_enter(_e):
            c.config(cursor="hand2")
            c.itemconfig(glyph, fill="#FFFFFF")
            if not self.transparent:
                c.itemconfig(bg_poly, fill=BTN)

        def _on_leave(_e):
            c.config(cursor="")
            c.itemconfig(glyph, fill=MUTED)
            if not self.transparent:
                c.itemconfig(bg_poly, fill=BG2)

        def _on_release(e):
            bbox = c.bbox(tag)
            if bbox and bbox[0] <= e.x <= bbox[2] and bbox[1] <= e.y <= bbox[3]:
                self.on_settings_gear_click()

        c.tag_bind(tag, "<Enter>", _on_enter, add="+")
        c.tag_bind(tag, "<Leave>", _on_leave, add="+")
        c.tag_bind(tag, "<ButtonPress-1>", lambda _e: "break")
        c.tag_bind(tag, "<ButtonRelease-1>", _on_release)

    def on_settings_gear_click(self, event=None) -> None:
        self.hide_tooltip()
        self.toggle_edit()

    # ---- clock tick: refresh status & age tooltips -----------------------------------------------
    def start_clock(self):
        def tick():
            try:
                self.refresh()
            finally:                               # one failed refresh must not stop the clock
                now = time.time()
                self._clock_job = self.root.after(int((15 - now % 15) * 1000) + 50, tick)
        tick()

    # ---- edit mode ---------------------------------------------------------------------------------
    def toggle_edit(self):
        (self.exit_edit if self.editing else self.enter_edit)()

    def enter_edit(self):
        if self.editing:
            return
        self.hide_tooltip()
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
            if isinstance(w, _EDIT_DIALOG_TYPES):
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

    def _has_open_edit_dialog(self) -> bool:
        containers = [self.root]
        if self.panel is not None:
            containers.append(self.panel)
        for parent in containers:
            try:
                for w in list(parent.winfo_children()):
                    if isinstance(w, _EDIT_DIALOG_TYPES) and w.winfo_exists():
                        return True
            except tk.TclError:
                pass
        return False

    def _has_unsaved_editor_changes(self) -> bool:
        ed = self.panel.editor if self.panel is not None else None
        return bool(ed is not None and ed.winfo_exists() and ed.is_dirty())

    def _schedule_idle(self):
        def check():
            self._idle_job = None
            if not self.editing:
                return
            if self._has_open_edit_dialog() or self._has_unsaved_editor_changes():
                self._touch()
                self._idle_job = self.root.after(1000, check)
            elif time.monotonic() - self._last_activity >= IDLE_EDIT_S:
                self.exit_edit()
            else:
                self._idle_job = self.root.after(1000, check)
        self._idle_job = self.root.after(1000, check)

    # ---- mini tracker ------------------------------------------------------------------------------
    def open_stock_editor(self, symbol: str) -> None:
        if not self.editing:
            self.enter_edit()
        if self.panel is not None and symbol in self.panel._rows:
            if self.panel.open_symbol != symbol:
                self.panel.toggle(symbol)
            self.refresh()
            self._touch()

    def toggle_mini(self):
        if self.mini_open:
            self.mini.hide()
            self.mini = None
        else:
            self.ctrl.set_mini_tracker_state({"enabled": True})
            self.mini = MiniTrackerWindow(
                self.root,
                get_state=self.ctrl.get_mini_tracker_state,
                set_state=self.ctrl.set_mini_tracker_state,
                get_rows=self._mini_rows,
                get_alerts=self._mini_alerts,
                acknowledge=self.ctrl.notifier.acknowledge_latest,
                on_closed=self._on_mini_closed,
                get_alert_tooltip=self._mini_alert_tooltip,
                on_row_double_click=self.open_stock_editor,
            )
        self.refresh()

    def _on_mini_closed(self):
        self.mini = None
        self.refresh()

    def _refresh_mini(self):
        if self.mini is not None and self.mini.winfo_exists():
            self.mini.refresh()

    def _mini_rows(self) -> list[dict]:
        flashing = self.flashing()
        out = []
        for s in self.ctrl.wl.symbols:
            row = self.ctrl.rows.get(s.symbol)
            t = row.tick if row else None
            price = float(t.price) if (t and t.price > 0) else None
            pct = None
            if t:
                if t.change_pct is not None:
                    pct = float(t.change_pct)
                elif t.prev_close and t.prev_close > 0:
                    pct = float((t.price - t.prev_close) / t.prev_close * 100)
            alert_badge = "ALERT" if s.symbol in flashing else None
            out.append({
                "symbol": s.symbol,
                "price": price,
                "change_pct": pct,
                "category": s.category,
                "alert": alert_badge,
                "flashing": s.symbol in flashing,
            })
        return out

    def _mini_alerts(self) -> tuple[int, str]:
        latest = getattr(self.ctrl.notifier, "_latest", None)
        if latest and not latest.get("acked"):
            prio = latest.get("priority", "HIGH")
            level = "critical" if prio == "HIGH" else "warn"
            return int(latest.get("count", 1)), level
        return 0, "warn"

    def _mini_alert_tooltip(self) -> str:
        latest = getattr(self.ctrl.notifier, "_latest", None)
        if latest and not latest.get("acked"):
            return str(latest.get("text") or "")
        return ""