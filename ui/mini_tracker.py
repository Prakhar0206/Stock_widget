"""ui/mini_tracker.py - floating always-on-top companion for the Stock Alert Widget.

Adapter contract (wired in ui/widget.py):
    get_state() -> dict            # the "mini_tracker" block of state.json
    set_state(dict) -> None        # merge + persist
    get_rows()  -> list[dict]      # {"symbol","price","change_pct","category","alert","flashing"}
    get_alerts() -> (count, level) # level: "warn" (amber) | "critical" (red)
    acknowledge() -> None          # ctrl.notifier.acknowledge_latest
    on_closed() -> None            # sync the main-widget Mini toggle switch

Look: a translucent dark panel matching the main widget palette (ui/common.py).
Always interactive and draggable; scrolls on long watchlists.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

try:
    from desktop_layer import work_area, work_area_for_point
except ImportError:
    work_area = None
    work_area_for_point = None

try:
    from ui.common import ACCENT, AMBER, BG, BG2, BTN, FG, GREEN, GREY, MUTED, RED
except ImportError:
    from .common import ACCENT, AMBER, BG, BG2, BTN, FG, GREEN, GREY, MUTED, RED

MARGIN = 24
HEADER_H = 30
ROW_H = 24
ROW_GAP = 2
PAD = 4
WIDTH = 260
ALPHA = 0.92
BORDER = "#444444"
FLASH_BG = "#4B5563"
UP, DOWN, FLAT = GREEN, RED, GREY
CATEGORIES = ["ALL", "SELL", "HOLD", "BUY", "WATCH"]
DEFAULTS = {"enabled": False, "pos_x": None, "pos_y": None,
            "category_filter": "ALL", "selected_symbols": []}

FONT_UI = ("Segoe UI", 9, "bold")
FONT_ROW = ("Consolas", 10)
FONT_ICON = ("Segoe UI", 9, "bold")
FONT_PILL = ("Segoe UI", 8, "bold")


# ---------------------------------------------------------------- Win32 / geometry utils
def _primary_work_area():
    if work_area is not None:
        try:
            return work_area()
        except Exception:
            pass
    return 0, 0, 1920, 1040


def _work_area_for_rect(x, y, w, h):
    """(left, top, right, bottom) work area of the nearest *attached* monitor to the rect."""
    cx, cy = int(x) + int(w) // 2, int(y) + int(h) // 2
    if work_area_for_point is not None:
        try:
            return work_area_for_point(cx, cy)
        except Exception:
            pass
    return _primary_work_area()


def _clamp_to_work_area(x, y, w, h):
    """Clamp rect into the work area of the nearest *currently attached* monitor."""
    l, t, r, b = _work_area_for_rect(x, y, w, h)
    return max(l, min(x, r - w)), max(t, min(y, b - h))


def _fmt_row(price, pct):
    if pct is None:
        return "―", FLAT, "  --.--%"
    if pct > 0:
        return "▲", UP, f"{pct:+.2f}%"
    if pct < 0:
        return "▼", DOWN, f"{pct:+.2f}%"
    return "―", FLAT, f"{0:.2f}%"


def rounded_rect_points(x1, y1, x2, y2, r):
    """Points for create_polygon(..., smooth=True) that draw a rounded rectangle / pill."""
    return [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
            x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]


def apply_panel_style(win, alpha=ALPHA):
    """Apply the dark main-widget panel background and window translucency."""
    win.configure(bg=BG)
    try:
        win.attributes("-alpha", alpha)
    except tk.TclError:
        pass


class PillButton(tk.Canvas):
    """Rounded pill button drawn on a canvas. `parent_bg` fills the corners."""

    def __init__(self, parent, text, command=None, parent_bg=BG, fill=BTN,
                 fg=FG, outline=BORDER, hover="#4B4B4B", padx=10, pady=3, font=FONT_PILL):
        f = tkfont.Font(font=font)
        w, h = f.measure(text) + 2 * padx, f.metrics("linespace") + 2 * pady
        super().__init__(parent, width=w, height=h, bg=parent_bg, highlightthickness=0, cursor="hand2")
        self._fill, self._hover, self._command = fill, hover, command
        self.shape = self.create_polygon(rounded_rect_points(1, 1, w - 1, h - 1, (h - 2) // 2),
                                         smooth=True, fill=fill, outline=outline)
        self.label = self.create_text(w // 2, h // 2, text=text, fill=fg, font=font)
        self.bind("<Button-1>", lambda e: self._command and self._command())
        self.bind("<Enter>", lambda e: self.itemconfig(self.shape, fill=self._hover))
        self.bind("<Leave>", lambda e: self.itemconfig(self.shape, fill=self._fill))

    def restyle(self, fill, fg, outline=None, hover=None):
        """Update pill fill, text color, and optional outline/hover colors."""
        self._fill = fill
        if hover is not None:
            self._hover = hover
        self.itemconfig(self.shape, fill=fill, **({"outline": outline} if outline is not None else {}))
        self.itemconfig(self.label, fill=fg)


def _bind_wheel(widget, handler):
    """Mouse-wheel (Windows/macOS + X11) on a widget and all its descendants."""
    widget.bind("<MouseWheel>", handler, add="+")
    widget.bind("<Button-4>", handler, add="+")
    widget.bind("<Button-5>", handler, add="+")
    for ch in widget.winfo_children():
        _bind_wheel(ch, handler)


def _wheel_steps(e):
    if getattr(e, "num", None) == 4:
        return -1
    if getattr(e, "num", None) == 5:
        return 1
    d = getattr(e, "delta", 0)
    return int(-d / 120) or (-1 if d > 0 else 1 if d < 0 else 0)


def _bind_drag(widget, on_start, on_move, on_end):
    """Attach left-button drag handlers to a widget."""
    widget.bind("<ButtonPress-1>", on_start, add="+")
    widget.bind("<B1-Motion>", on_move, add="+")
    widget.bind("<ButtonRelease-1>", on_end, add="+")


def _bind_icon_button(btn, cmd):
    """Attach hover highlight and release-inside click handling to an icon label."""
    def _on_release(e):
        if 0 <= e.x <= max(1, btn.winfo_width()) and 0 <= e.y <= max(1, btn.winfo_height()):
            cmd()

    btn.bind("<ButtonPress-1>", lambda _e: "break")
    btn.bind("<ButtonRelease-1>", _on_release)
    btn.bind("<Enter>", lambda _e: btn.config(fg="#FFFFFF", bg=BTN))
    btn.bind("<Leave>", lambda _e: btn.config(fg=MUTED, bg=BG2))


def _make_overflow_indicators(parent, on_scroll):
    """Create compact ▲ / ▼ scroll indicators attached to `parent`."""
    style = dict(bg=BG2, fg=ACCENT, font=("Segoe UI", 7, "bold"), padx=4, pady=0, cursor="hand2")
    up_lbl = tk.Label(parent, text="▲", **style)
    down_lbl = tk.Label(parent, text="▼", **style)
    up_lbl.bind("<Button-1>", lambda _e: (on_scroll(-1), "break")[1])
    down_lbl.bind("<Button-1>", lambda _e: (on_scroll(1), "break")[1])
    return up_lbl, down_lbl


def _sync_overflow_indicators(canvas, up_lbl, down_lbl, overflow):
    """Update ▲ / ▼ placement and visibility for a scrollable canvas."""
    if not overflow:
        up_lbl.place_forget()
        down_lbl.place_forget()
        return
    y0, y1 = canvas.yview()
    if y0 > 0.001:
        up_lbl.place(in_=canvas, relx=1.0, x=-2, y=1, anchor="ne")
        up_lbl.lift(canvas)
    else:
        up_lbl.place_forget()
    if y1 < 0.999 or y1 == 0.0:
        down_lbl.place(in_=canvas, relx=1.0, rely=1.0, x=-2, y=-1, anchor="se")
        down_lbl.lift(canvas)
    else:
        down_lbl.place_forget()


# ------------------------------------------------------------- Mini Tracker
class MiniTrackerWindow(tk.Toplevel):
    """Floating dark-themed mini watchlist window."""

    def __init__(self, master, get_state, set_state, get_rows, get_alerts,
                 acknowledge, on_closed=None, get_alert_tooltip=None,
                 on_row_double_click=None):
        super().__init__(master, bg=BG)
        self._get_state, self._set_state = get_state, set_state
        self._get_rows, self._get_alerts = get_rows, get_alerts
        self._acknowledge, self._on_closed = acknowledge, on_closed
        self._get_alert_tooltip = get_alert_tooltip
        self._on_row_double_click = on_row_double_click
        self._drag = None
        self._dragging = False
        self._pending_refresh = False
        self._settings = None
        self._flash_on = False
        self._flash_job = None
        self._visible, self._badges = [], []
        self._row_widgets: list[dict] = []
        self._rendered_sig = None
        self._tip_win = None
        self._tip_lbl = None
        self._alerting = False
        self._overflow = False
        self._privacy_hidden = False
        self._body_h = self._content_h = 0

        self.overrideredirect(True)
        self.protocol("WM_DELETE_WINDOW", self._close)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        apply_panel_style(self, ALPHA)

        self.shell = tk.Frame(self, bg=BG, highlightthickness=1,
                              highlightbackground=BORDER, highlightcolor=BORDER)
        self.shell.pack(fill="both", expand=True)
        self._build_header()
        self.body = tk.Canvas(self.shell, bg=BG, highlightthickness=0, bd=0,
                              yscrollincrement=ROW_H + ROW_GAP, width=WIDTH - 2 - 2 * PAD, height=ROW_H)
        self.body.pack(fill="x", padx=PAD, pady=(0, PAD))
        self.rows_frame = tk.Frame(self.body, bg=BG)
        self._rows_win = self.body.create_window((0, 0), window=self.rows_frame, anchor="nw",
                                                 width=WIDTH - 2 - 2 * PAD)
        self.overflow_up_lbl, self.overflow_down_lbl = _make_overflow_indicators(
            self.shell, self._scroll_step
        )

        for w in (self.shell, self.body, self.rows_frame):
            _bind_drag(w, self._drag_start, self._drag_move, self._drag_end)
        _bind_wheel(self.body, self._on_wheel)
        _bind_wheel(self.rows_frame, self._on_wheel)
        _bind_wheel(self.overflow_up_lbl, self._on_wheel)
        _bind_wheel(self.overflow_down_lbl, self._on_wheel)

        st = self._state()
        self.refresh()
        self._place_initial(st)
        self._flash_tick()

    # -- state helpers
    def _state(self):
        s = dict(DEFAULTS)
        s.update(self._get_state() or {})
        return s

    def _save(self, **kw):
        self._set_state(kw)

    # -- header
    def _build_header(self):
        self.header = tk.Frame(self.shell, bg=BG2, height=HEADER_H, highlightthickness=1,
                               highlightbackground=BORDER, highlightcolor=BORDER)
        self.header.pack(fill="x", padx=PAD, pady=(PAD, ROW_GAP))
        self.header.pack_propagate(False)

        self.close_btn = self._icon("✕", self._close)
        self.gear_btn = self._icon("⚙", self.open_settings)

        self.center = tk.Label(self.header, text="Mini Tracker", font=FONT_UI,
                               bg=BG2, fg=FG)
        self.center.pack(side="left", expand=True)

        # Bind drag before _center_click so an alerting click checks _alerting before refresh() clears it.
        for w in (self.header, self.center):
            _bind_drag(w, self._drag_start, self._drag_move, self._drag_end)
        self.center.bind("<Button-1>", self._center_click, add="+")
        self.center.bind("<Enter>", self._show_alert_tooltip, add="+")
        self.center.bind("<Leave>", self._hide_alert_tooltip, add="+")
        _bind_wheel(self.header, self._on_wheel)

    def _icon(self, text, cmd):
        b = tk.Label(self.header, text=text, font=FONT_ICON, bg=BG2, fg=MUTED,
                     cursor="hand2", padx=7)
        b.pack(side="right", fill="y")
        _bind_icon_button(b, cmd)
        return b

    # -- dragging
    def _drag_start(self, e):
        if getattr(e, "widget", None) is self.center and self._alerting:
            self._drag = None
            self._dragging = False
            return
        self._dragging = True
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, _e):
        if self._drag:
            self._drag = None
            x, y = _clamp_to_work_area(self.winfo_x(), self.winfo_y(), self.winfo_width(), self.winfo_height())
            self.geometry(f"+{x}+{y}")
            self._save(pos_x=x, pos_y=y)
        self._dragging = False
        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh()

    # -- sizing / positioning
    def _measure(self, x, y):
        """-> (body_h, content_h, total_h); body is capped to the monitor's work area."""
        n = len(self._visible)
        content_h = n * (ROW_H + ROW_GAP) - ROW_GAP if n else 2 * ROW_H
        chrome = 2 + PAD + HEADER_H + ROW_GAP + PAD
        l, t, r, b = _work_area_for_rect(x, y, WIDTH, chrome + min(content_h, 2 * ROW_H))
        max_body = max(2 * ROW_H, (b - t) - 8 - chrome)
        body_h = min(content_h, max_body)
        return body_h, content_h, chrome + body_h

    def _autosize(self, anchor=None):
        """Fit height to the rows (scrolling if more than the screen holds); keep top-left."""
        self.update_idletasks()
        x, y = anchor or (self.winfo_x(), self.winfo_y())
        body_h, content_h, h = self._measure(x, y)
        self._body_h, self._content_h = body_h, content_h
        self._overflow = content_h > body_h
        self.body.config(height=body_h)
        self.body.configure(scrollregion=(0, 0, WIDTH - 2 - 2 * PAD, content_h))
        if not self._overflow:
            self.body.yview_moveto(0)
        if anchor is not None or self.winfo_viewable():
            x, y = _clamp_to_work_area(x, y, WIDTH, h)
        self.geometry(f"{WIDTH}x{h}+{x}+{y}")
        self._update_overflow_indicators()

    def _place_initial(self, st):
        self.update_idletasks()
        x, y = st.get("pos_x"), st.get("pos_y")
        if x is None or y is None:
            _, _, h = self._measure(0, 0)
            l, t, r, b = _primary_work_area()
            x, y = r - WIDTH - MARGIN, b - h - MARGIN
        self._autosize(anchor=(int(x), int(y)))
        try:
            self.update()
        except Exception:
            pass
        self._update_overflow_indicators()

    def _on_wheel(self, e):
        if not self._overflow:
            return
        self.body.yview_scroll(_wheel_steps(e), "units")
        self._update_overflow_indicators()

    def _scroll_step(self, delta):
        if not self._overflow:
            return
        self.body.yview_scroll(delta, "units")
        self._update_overflow_indicators()

    def _update_overflow_indicators(self):
        _sync_overflow_indicators(
            self.body, self.overflow_up_lbl, self.overflow_down_lbl, self._overflow
        )

    # -- content
    def _filtered_rows(self, rows=None):
        st = self._state()
        if rows is None:
            rows = self._get_rows() or []
        sel = list(st.get("selected_symbols") or [])
        cat = st.get("category_filter", "ALL")
        if sel and rows:
            known = {r.get("symbol") for r in rows}
            pruned = [s for s in sel if s in known]
            if pruned != sel:
                self._set_state({"selected_symbols": pruned})
                sel = pruned
        if sel:
            sel_set = set(sel)
            rows = [r for r in rows if r["symbol"] in sel_set]
        if cat != "ALL":
            rows = [r for r in rows if (r.get("category") or "").upper() == cat]
        return rows                                   # no cap: show everything that matches

    def _chip(self, height=ROW_H, bg=BG2):
        f = tk.Frame(self.rows_frame, bg=bg, height=height, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=BORDER)
        f.pack_propagate(False)
        return f

    def refresh(self):
        """Call on every quote update (15s cadence)."""
        if self._privacy_hidden:
            return
        if self._dragging:
            self._pending_refresh = True
            return
        rows = self._get_rows() or []
        if self._settings and self._settings.winfo_exists():
            self._settings.sync_rows(rows)
        y0 = self.body.yview()[0]
        self._visible = self._filtered_rows(rows)
        sig = tuple((r.get("symbol"), bool(r.get("alert"))) for r in self._visible) if self._visible else None
        can_reuse = (
            bool(self._visible)
            and sig == self._rendered_sig
            and len(self._row_widgets) == len(self._visible)
            and all(w["frame"].winfo_exists() for w in self._row_widgets)
        )
        if can_reuse:
            self._badges = []
            for w, r in zip(self._row_widgets, self._visible):
                arrow, color, pct = _fmt_row(r.get("price"), r.get("change_pct"))
                row_bg = FLASH_BG if r.get("flashing") else BG2
                price = f"{r['price']:.2f}" if r.get("price") is not None else "--"
                w["symbol"] = r["symbol"]
                w["frame"].config(bg=row_bg)
                w["sym_lbl"].config(text=f"{r['symbol']:<6}", bg=row_bg, fg=FG)
                w["price_lbl"].config(text=f"{price:>8}", bg=row_bg, fg=FG)
                w["pct_lbl"].config(text=f" {arrow} {pct}", bg=row_bg, fg=color)
                if w["badge_lbl"] is not None:
                    w["badge_lbl"].config(text=f"[{r['alert']}]", bg=row_bg, fg=AMBER)
                    self._badges.append(w["badge_lbl"])
        else:
            for w in self.rows_frame.winfo_children():
                w.destroy()
            self._badges = []
            self._row_widgets = []
            if not self._visible:
                chip = self._chip(2 * ROW_H, bg=BG2)
                chip.pack(fill="x")
                tk.Label(chip, text="No stocks matching filter", font=("Segoe UI", 8),
                         bg=BG2, fg=MUTED).pack(fill="both", expand=True)
                for it in (chip, *chip.winfo_children()):
                    _bind_drag(it, self._drag_start, self._drag_move, self._drag_end)
            for i, r in enumerate(self._visible):
                arrow, color, pct = _fmt_row(r.get("price"), r.get("change_pct"))
                row_bg = FLASH_BG if r.get("flashing") else BG2
                row = self._chip(bg=row_bg)
                row.pack(fill="x", pady=(ROW_GAP if i else 0, 0))
                badge_lbl = None
                # Pack right badge first so left columns never push it out of view.
                if r.get("alert"):
                    badge_lbl = tk.Label(row, text=f"[{r['alert']}]", font=("Segoe UI", 8, "bold"),
                                         bg=row_bg, fg=AMBER)
                    badge_lbl.pack(side="right", padx=8)
                    self._badges.append(badge_lbl)
                price = f"{r['price']:.2f}" if r.get("price") is not None else "--"
                sym_lbl = tk.Label(row, text=f"{r['symbol']:<6}", font=FONT_ROW, bg=row_bg,
                                   fg=FG, width=6, anchor="w")
                sym_lbl.pack(side="left", padx=(8, 0))
                price_lbl = tk.Label(row, text=f"{price:>8}", font=FONT_ROW, bg=row_bg,
                                     fg=FG, anchor="e")
                price_lbl.pack(side="left")
                pct_lbl = tk.Label(row, text=f" {arrow} {pct}", font=FONT_ROW, bg=row_bg,
                                   fg=color)
                pct_lbl.pack(side="left")
                entry = {
                    "symbol": r["symbol"],
                    "frame": row,
                    "sym_lbl": sym_lbl,
                    "price_lbl": price_lbl,
                    "pct_lbl": pct_lbl,
                    "badge_lbl": badge_lbl,
                }
                self._row_widgets.append(entry)

                def _on_dbl(_e, ent=entry):
                    self._drag = None
                    self._dragging = False
                    if self._on_row_double_click:
                        self._on_row_double_click(ent["symbol"])

                for it in (row, *row.winfo_children()):
                    _bind_drag(it, self._drag_start, self._drag_move, self._drag_end)
                    it.bind("<Double-Button-1>", _on_dbl, add="+")
            for ch in self.rows_frame.winfo_children():
                _bind_wheel(ch, self._on_wheel)
            self._rendered_sig = sig if self._visible else None
        self._update_alert_pill()
        self._autosize()
        self.body.yview_moveto(y0 if self._overflow else 0)
        self._update_overflow_indicators()

    def _update_alert_pill(self):
        count, level = (self._get_alerts() or (0, "warn"))
        if count > 0:
            col = RED if level == "critical" else AMBER
            icon = "🚨" if level == "critical" else "🔔"
            label = f"{icon} {count} Alert{'s' if count != 1 else ''}"
            self.center.config(text=f" {label} ", bg=col, fg="#111111", cursor="hand2",
                               padx=8, pady=1)
            self._alerting = True
        else:
            self.center.config(text="Mini Tracker", bg=BG2, fg=FG, cursor="",
                               padx=0, pady=0)
            self._alerting = False
            self._hide_alert_tooltip()

    def _show_alert_tooltip(self, _e=None):
        if not self._alerting or not self._get_alert_tooltip:
            self._hide_alert_tooltip()
            return
        text = self._get_alert_tooltip() or ""
        if not text.strip():
            self._hide_alert_tooltip()
            return
        try:
            if self._tip_win is None or not self._tip_win.winfo_exists():
                self._tip_win = tk.Toplevel(self, bg=BORDER)
                self._tip_win.overrideredirect(True)
                try:
                    self._tip_win.attributes("-topmost", True)
                except tk.TclError:
                    pass
                apply_panel_style(self._tip_win, 0.96)
                self._tip_win.configure(bg=BORDER)
                self._tip_lbl = tk.Label(
                    self._tip_win, text=text, bg=BG2, fg=FG, font=("Segoe UI", 8),
                    padx=6, pady=3, justify="left", wraplength=250,
                )
                self._tip_lbl.pack(padx=1, pady=1)
            else:
                self._tip_lbl.config(text=text)
            self._tip_win.update_idletasks()
            tw = self._tip_win.winfo_reqwidth()
            th = self._tip_win.winfo_reqheight()
            cx = self.center.winfo_rootx()
            cy = self.center.winfo_rooty()
            cw = max(1, self.center.winfo_width())
            ch = max(1, self.center.winfo_height())
            tx = cx + (cw - tw) // 2
            ty = cy + ch + 4
            _, _, _, wb = _work_area_for_rect(cx, cy, cw, ch)
            if ty + th > wb:
                ty = cy - th - 4
            tx, ty = _clamp_to_work_area(tx, ty, tw, th)
            self._tip_win.geometry(f"+{tx}+{ty}")
            self._tip_win.deiconify()
        except tk.TclError:
            pass

    def _hide_alert_tooltip(self, _e=None):
        if self._tip_win is not None:
            try:
                if self._tip_win.winfo_exists():
                    self._tip_win.destroy()
            except tk.TclError:
                pass
            self._tip_win = None
            self._tip_lbl = None

    def _center_click(self, _e):
        self._hide_alert_tooltip()
        if getattr(self, "_alerting", False):
            self._acknowledge()
            self.refresh()

    def _flash_tick(self):
        self._flash_on = not self._flash_on
        if not self._privacy_hidden:
            for b in getattr(self, "_badges", []):
                try:
                    b.config(fg=AMBER if self._flash_on else "#7A5208")
                except tk.TclError:
                    pass
        self._flash_job = self.after(600, self._flash_tick)

    def set_privacy_hidden(self, hidden: bool) -> None:
        """Temporarily withdraw or restore Mini Tracker for privacy without changing saved enabled state."""
        hidden = bool(hidden)
        if hidden == self._privacy_hidden:
            return
        self._privacy_hidden = hidden
        if hidden:
            self._drag = None
            self._dragging = False
            self._hide_alert_tooltip()
            if self._settings and self._settings.winfo_exists():
                try:
                    self._settings.destroy()
                except tk.TclError:
                    pass
                self._settings = None
            try:
                self.withdraw()
            except tk.TclError:
                pass
        else:
            try:
                self.deiconify()
                try:
                    self.attributes("-topmost", True)
                except tk.TclError:
                    pass
                self.lift()
            except tk.TclError:
                pass
            self.refresh()

    # -- settings / close
    def open_settings(self):
        if self._settings and self._settings.winfo_exists():
            self._settings.lift()
            return
        self._settings = MiniTrackerSettingsDialog(
            self, self._state(), self._get_rows,
            on_apply=lambda **kw: (self._save(**kw), self.refresh()))

    def _close(self):
        self.hide()
        if self._on_closed:
            self._on_closed()

    def hide(self):
        self._hide_alert_tooltip()
        self._save(enabled=False)
        self.destroy()

    def destroy(self):
        self._hide_alert_tooltip()
        if self._flash_job:
            try:
                self.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None
        if self._settings and self._settings.winfo_exists():
            try:
                self._settings.destroy()
            except Exception:
                pass
            self._settings = None
        super().destroy()


# --------------------------------------------------------- Settings dialog
class MiniTrackerSettingsDialog(tk.Toplevel):
    """Dark panel filter dialog matching the main widget. Scrolls on long watchlists."""

    def __init__(self, parent, state, get_rows, on_apply):
        super().__init__(parent, bg=BG)
        self._on_apply = on_apply
        self._drag = None
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        apply_panel_style(self, 0.95)

        self.card = tk.Frame(self, bg=BG, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=BORDER)
        self.card.pack(fill="both", expand=True)
        card = self.card

        self.cat = tk.StringVar(value=state.get("category_filter", "ALL"))
        sel = set(state.get("selected_symbols") or [])
        rows = get_rows() or []

        hdr = tk.Frame(card, bg=BG2, height=HEADER_H, highlightthickness=1,
                       highlightbackground=BORDER, highlightcolor=BORDER)
        hdr.pack(fill="x", padx=PAD, pady=(PAD, 6))
        hdr.pack_propagate(False)
        title_lbl = tk.Label(hdr, text="Mini Tracker Filter", font=FONT_UI, bg=BG2, fg=FG)
        title_lbl.pack(side="left", padx=(8, 0))
        close_btn = tk.Label(hdr, text="✕", font=FONT_ICON, bg=BG2, fg=MUTED,
                             cursor="hand2", padx=7)
        close_btn.pack(side="right", fill="y")
        _bind_icon_button(close_btn, self.destroy)
        self.close_btn = close_btn
        for w in (hdr, title_lbl):
            _bind_drag(w, self._drag_start, self._drag_move, self._drag_end)

        pills = tk.Frame(card, bg=BG)
        pills.pack(anchor="w", padx=10, pady=(2, 0))
        self._pill_btns = {}
        for c in CATEGORIES:
            b = PillButton(pills, c, lambda c=c: self._set_cat(c), parent_bg=BG,
                           padx=8, pady=2, font=FONT_PILL)
            b.pack(side="left", padx=2)
            self._pill_btns[c] = b
        self._set_cat(self.cat.get(), apply=False)

        self.count_lbl = tk.Label(card, text="", font=("Segoe UI", 8), bg=BG, fg=MUTED)
        self.count_lbl.pack(anchor="w", padx=12, pady=(8, 0))

        # scrollable checkbox list
        self.vars = {}
        self._row_sig = None
        self._list_h = 40
        wrap = tk.Frame(card, bg=BG2, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        wrap.pack(fill="x", padx=10, pady=4)
        self.list_canvas = tk.Canvas(wrap, bg=BG2, highlightthickness=0, bd=0, width=230, height=40)
        self.list_canvas.pack(fill="x", padx=2, pady=2)
        self.inner = tk.Frame(self.list_canvas, bg=BG2)
        self._inner_win = self.list_canvas.create_window((0, 0), window=self.inner, anchor="nw", width=230)
        self.overflow_up_lbl, self.overflow_down_lbl = _make_overflow_indicators(
            wrap, self._scroll_step
        )
        _bind_wheel(self.overflow_up_lbl, self._on_wheel)
        _bind_wheel(self.overflow_down_lbl, self._on_wheel)
        self.list_canvas.bind(
            "<Configure>",
            lambda e: (
                self.list_canvas.itemconfig(self._inner_win, width=max(230, e.width)),
                self._update_overflow_indicators(),
            ),
        )
        self._populate_rows(rows, sel)

        btns = tk.Frame(card, bg=BG)
        btns.pack(fill="x", padx=10, pady=(6, 10))
        PillButton(btns, "Done", self.destroy, parent_bg=BG,
                   fill=ACCENT, fg="#FFFFFF", outline=ACCENT, hover="#2563EB").pack(side="right", padx=3)
        PillButton(btns, "Clear", self._clear, parent_bg=BG,
                   fill=BTN, fg=FG, outline=BORDER).pack(side="right", padx=3)
        PillButton(btns, "All", self._select_all, parent_bg=BG,
                   fill=BTN, fg=FG, outline=BORDER).pack(side="right", padx=3)

        self.update_idletasks()
        w = self.winfo_reqwidth()
        pl, pt, pr, pb = _work_area_for_rect(parent.winfo_x(), parent.winfo_y(),
                                             parent.winfo_width(), parent.winfo_height())
        x0 = parent.winfo_x() - w - 8
        if x0 < pl:
            x0 = parent.winfo_x() + parent.winfo_width() + 8
        # cap the list so the dialog fits the monitor's work area
        l, t, r, b = _work_area_for_rect(x0, parent.winfo_y(), w, 200)
        chrome = self.winfo_reqheight() - self.list_canvas.winfo_reqheight()
        self._list_h = max(40, min(self._content_h, int((b - t) * 0.9) - chrome))
        self.list_canvas.config(height=self._list_h)
        self.update_idletasks()
        h = self.winfo_reqheight()
        y0 = max(t, parent.winfo_y() + parent.winfo_height() - h)
        x0, y0 = _clamp_to_work_area(x0, y0, w, h)
        self.geometry(f"{w}x{h}+{x0}+{y0}")
        self._update_overflow_indicators()
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_force()

    def _list_viewport_h(self):
        h = self.list_canvas.winfo_height()
        return h if h > 1 else self._list_h

    def _update_overflow_indicators(self):
        _sync_overflow_indicators(
            self.list_canvas, self.overflow_up_lbl, self.overflow_down_lbl,
            self._content_h > self._list_viewport_h(),
        )

    def _scroll_step(self, delta):
        if self._content_h > self._list_viewport_h():
            self.list_canvas.yview_scroll(delta, "units")
            self._update_overflow_indicators()

    def _populate_rows(self, rows, sel):
        for w in self.inner.winfo_children():
            w.destroy()
        self.vars = {}
        self._row_sig = [(r.get("symbol"), r.get("category") or "-") for r in rows]
        if rows:
            for r in rows:
                v = tk.BooleanVar(value=r["symbol"] in sel)
                self.vars[r["symbol"]] = v
                tk.Checkbutton(self.inner, text=f"{r['symbol']}  ({r.get('category') or '-'})",
                               variable=v, command=self._toggle,
                               bg=BG2, fg=FG, selectcolor=BG,
                               activebackground=BG2, activeforeground="#FFFFFF",
                               highlightthickness=0, bd=0,
                               font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=4, pady=1)
        else:
            tk.Label(self.inner, text="No stocks in watchlist", font=("Segoe UI", 9),
                     bg=BG2, fg=MUTED, pady=8).pack(fill="x")
        self.inner.update_idletasks()
        self._content_h = max(1, self.inner.winfo_reqheight())
        self.list_canvas.configure(scrollregion=(0, 0, 230, self._content_h))
        _bind_wheel(self.inner, self._on_wheel)
        _bind_wheel(self.list_canvas, self._on_wheel)
        self._update_count()
        self._update_overflow_indicators()

    def sync_rows(self, rows):
        """Rebuild ticker checkboxes in place when watchlist symbols or categories change."""
        rows = rows or []
        sig = [(r.get("symbol"), r.get("category") or "-") for r in rows]
        if sig == self._row_sig:
            return
        sel = {s for s, v in self.vars.items() if v.get()}
        self._populate_rows(rows, sel)
        self.update_idletasks()
        x0, y0 = self.winfo_x(), self.winfo_y()
        w = max(self.winfo_width(), self.winfo_reqwidth())
        l, t, r, b = _work_area_for_rect(x0, y0, w, 200)
        chrome = self.winfo_reqheight() - self.list_canvas.winfo_reqheight()
        self._list_h = max(40, min(self._content_h, int((b - t) * 0.9) - chrome))
        self.list_canvas.config(height=self._list_h)
        self.update_idletasks()
        h = self.winfo_reqheight()
        x0, y0 = _clamp_to_work_area(x0, y0, w, h)
        self.geometry(f"{w}x{h}+{x0}+{y0}")
        self._update_overflow_indicators()

    def _drag_start(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, _e):
        if self._drag:
            self._drag = None
            x, y = _clamp_to_work_area(self.winfo_x(), self.winfo_y(), self.winfo_width(), self.winfo_height())
            self.geometry(f"+{x}+{y}")

    def _on_wheel(self, e):
        if self._content_h > self._list_viewport_h():
            self.list_canvas.yview_scroll(_wheel_steps(e) * 2, "units")
            self._update_overflow_indicators()

    def _set_cat(self, c, apply=True):
        self.cat.set(c)
        for name, b in self._pill_btns.items():
            on = name == c
            b.restyle(
                fill=ACCENT if on else BTN,
                fg="#FFFFFF" if on else MUTED,
                outline=ACCENT if on else BORDER,
                hover="#2563EB" if on else "#4B4B4B",
            )
        if apply:
            self._push()

    def _update_count(self):
        n = sum(1 for v in self.vars.values() if v.get())
        self.count_lbl.config(text=f"{n} of {len(self.vars)} selected" + (" (none = show all)" if n == 0 else ""))

    def _toggle(self, *_):
        self._push()

    def _select_all(self):
        for v in self.vars.values():
            v.set(True)
        self._push()

    def _clear(self):
        for v in self.vars.values():
            v.set(False)
        self._push()

    def _push(self):
        self._update_count()
        self._on_apply(category_filter=self.cat.get(),
                       selected_symbols=[s for s, v in self.vars.items() if v.get()])
