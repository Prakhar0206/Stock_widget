"""ui/mini_tracker.py - floating always-on-top companion for the Stock Alert Widget.

Adapter contract (wired in ui/widget.py):
    get_state() -> dict            # the "mini_tracker" block of state.json
    set_state(dict) -> None        # merge + persist
    get_rows()  -> list[dict]      # {"symbol","price","change_pct","category","alert"}
    get_alerts() -> (count, level) # level: "warn" (amber) | "critical" (red)
    acknowledge() -> None          # ctrl.notifier.acknowledge_latest
    on_closed() -> None            # sync the main-widget Mini toggle switch

Look: a glass HUD. The window colour-keys its background (Windows `-transparentcolor`) and uses a
window alpha, so only the outlined header and row "chips" float over other windows.
Pinned (📌): every row is click-through; only the header stays clickable.
"""
import ctypes
import sys
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes

try:
    import desktop_layer
except ImportError:                      # not Windows (or run as a package)
    try:
        from .. import desktop_layer
    except ImportError:
        desktop_layer = None

MARGIN = 24
HEADER_H = 30
ROW_H = 24
ROW_GAP = 3
PAD = 4
WIDTH = 250
ALPHA = 0.90
CATEGORIES = ["ALL", "SELL", "HOLD", "BUY", "WATCH"]
DEFAULTS = {"enabled": False, "pinned": False, "pos_x": None, "pos_y": None,
            "category_filter": "ALL", "selected_symbols": []}

KEY = "#010203"                          # colour-key: fully transparent (and click-through) pixels
GLASS, HDR_BG, BG = "#10151C", "#161C25", "#10151C"
EDGE, EDGE_HI = "#2A3340", "#3B4757"
MUTED, MUTED_DIM = "#9CA3AF", "#6B7280"
UP, DOWN, FLAT = "#34D399", "#F87171", "#9CA3AF"
AMBER, RED, ACCENT = "#F59E0B", "#EF4444", "#00FFCC"
PIN_ON_BG = "#0E4D45"
FONT_UI = ("Segoe UI", 9, "bold")
FONT_ROW = ("Consolas", 10)
FONT_ICON = ("Segoe UI Emoji", 9)
FONT_PILL = ("Segoe UI", 9, "bold")


# ---------------------------------------------------------------- Win32 / geometry utils
class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _primary_work_area():
    if sys.platform == "win32":
        try:
            r = wintypes.RECT()
            u = getattr(ctypes, "windll", None)
            if u and hasattr(u, "user32"):
                u.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    return 0, 0, 1920, 1040


def _work_area_for_rect(x, y, w, h):
    """(left, top, right, bottom) work area of the nearest *attached* monitor to the rect."""
    if sys.platform == "win32":
        try:
            u = getattr(ctypes, "windll", None)
            if u and hasattr(u, "user32"):
                rect = wintypes.RECT(x, y, x + w, y + h)
                u.user32.MonitorFromRect.restype = wintypes.HANDLE
                hmon = u.user32.MonitorFromRect(ctypes.byref(rect), 2)  # MONITOR_DEFAULTTONEAREST
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(mi)
                if hmon and u.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    wa = mi.rcWork
                    return wa.left, wa.top, wa.right, wa.bottom
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


def apply_glass(win, alpha=ALPHA):
    """Translucent window with a colour-keyed background. Both are Windows features; elsewhere
    the calls are skipped/ignored and the KEY colour just shows as near-black."""
    try:
        win.attributes("-alpha", alpha)
    except tk.TclError:
        pass
    try:
        win.attributes("-transparentcolor", KEY)
    except tk.TclError:
        pass


class PillButton(tk.Canvas):
    """Rounded pill button drawn on a canvas. `parent_bg` fills the corners."""

    def __init__(self, parent, text, command=None, parent_bg=GLASS, fill="#1B2330",
                 fg="#E5E7EB", outline=EDGE_HI, hover=EDGE, padx=12, pady=3, font=FONT_PILL):
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

    def restyle(self, fill, fg, outline=None):
        self._fill = fill
        self.itemconfig(self.shape, fill=fill, **({"outline": outline} if outline else {}))
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


# ------------------------------------------------------------- Mini Tracker
class MiniTrackerWindow(tk.Toplevel):
    def __init__(self, master, get_state, set_state, get_rows, get_alerts,
                 acknowledge, on_closed=None):
        super().__init__(master, bg=KEY)
        self._get_state, self._set_state = get_state, set_state
        self._get_rows, self._get_alerts = get_rows, get_alerts
        self._acknowledge, self._on_closed = acknowledge, on_closed
        self._drag = None
        self._pinned = False
        self._settings = None
        self._flash_on = False
        self._flash_job = None
        self._ct = None                      # desktop_layer.SelectiveClickThrough while pinned
        self._visible, self._badges = [], []
        self._alerting = False
        self._overflow = False
        self._body_h = self._content_h = 0

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        apply_glass(self)

        # shell = the window outline; its interior is colour-keyed so only the chips are visible
        self.shell = tk.Frame(self, bg=KEY, highlightthickness=1,
                              highlightbackground=EDGE, highlightcolor=EDGE)
        self.shell.pack(fill="both", expand=True)
        self._build_header()
        self.body = tk.Canvas(self.shell, bg=KEY, highlightthickness=0, bd=0,
                              yscrollincrement=ROW_H + ROW_GAP, width=WIDTH - 2 - 2 * PAD, height=ROW_H)
        self.body.pack(fill="x", padx=PAD, pady=(0, PAD))
        self.rows_frame = tk.Frame(self.body, bg=KEY)
        self._rows_win = self.body.create_window((0, 0), window=self.rows_frame, anchor="nw",
                                                 width=WIDTH - 2 - 2 * PAD)

        st = self._state()
        self.refresh()
        self._place_initial(st)
        self.set_pinned(bool(st.get("pinned")))
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
        self.header = tk.Frame(self.shell, bg=HDR_BG, height=HEADER_H, highlightthickness=1,
                               highlightbackground=EDGE_HI, highlightcolor=EDGE_HI)
        self.header.pack(fill="x", padx=PAD, pady=(PAD, ROW_GAP))
        self.header.pack_propagate(False)

        self.pin_btn = tk.Label(self.header, text="📍", font=FONT_ICON, bg=HDR_BG, fg=MUTED_DIM,
                                cursor="hand2", padx=8, highlightthickness=1,
                                highlightbackground=HDR_BG)
        self.pin_btn.pack(side="left", padx=(2, 0))
        self.pin_btn.bind("<Button-1>", lambda e: self.set_pinned(not self._pinned))

        self.close_btn = self._icon("✕", self._close)
        self.gear_btn = self._icon("⚙", self.open_settings)

        self.center = tk.Label(self.header, text="Mini Tracker", font=FONT_UI,
                               bg=HDR_BG, fg=MUTED)
        self.center.pack(side="left", expand=True)
        self.center.bind("<Button-1>", self._center_click)

        for w in (self.header, self.center):  # drag handles
            w.bind("<ButtonPress-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
            w.bind("<ButtonRelease-1>", self._drag_end, add="+")
        # wheel over the (always solid) header scrolls the list, even while pinned
        for w in (self.header, self.center):
            w.bind("<MouseWheel>", self._on_wheel, add="+")
            w.bind("<Button-4>", self._on_wheel, add="+")
            w.bind("<Button-5>", self._on_wheel, add="+")

    def _icon(self, text, cmd):
        b = tk.Label(self.header, text=text, font=FONT_ICON, bg=HDR_BG, fg=MUTED,
                     cursor="hand2", padx=7)
        b.pack(side="right")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.config(fg="#FFFFFF"))
        b.bind("<Leave>", lambda e: b.config(fg=MUTED))
        return b

    def header_screen_rect(self):
        """The header in screen pixels - the part that stays clickable when pinned."""
        self.header.update_idletasks()
        x, y = self.header.winfo_rootx(), self.header.winfo_rooty()
        return x, y, x + self.header.winfo_width(), y + self.header.winfo_height()

    # -- dragging (header stays draggable)
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
            self._save(pos_x=x, pos_y=y)

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

    def _place_initial(self, st):
        self.update_idletasks()
        x, y = st.get("pos_x"), st.get("pos_y")
        if x is None or y is None:
            _, _, h = self._measure(0, 0)
            l, t, r, b = _primary_work_area()
            x, y = r - WIDTH - MARGIN, b - h - MARGIN
        self._autosize(anchor=(int(x), int(y)))

    def _on_wheel(self, e):
        if not self._overflow:
            return
        self.body.yview_scroll(_wheel_steps(e), "units")

    # -- pinning: Option 1 - rows click-through, header stays solid
    def set_pinned(self, pinned: bool):
        self._pinned = bool(pinned)
        if self._pinned:
            self.pin_btn.config(text="📌", fg=ACCENT, bg=PIN_ON_BG, highlightbackground=ACCENT)
        else:
            self.pin_btn.config(text="📍", fg=MUTED_DIM, bg=HDR_BG, highlightbackground=HDR_BG)
        if self._pinned:
            self._start_clickthrough()
        else:
            self._stop_clickthrough()
        self._save(pinned=self._pinned)

    def _start_clickthrough(self):
        if desktop_layer is None or not hasattr(desktop_layer, "SelectiveClickThrough"):
            return
        if self._ct is None:
            self._ct = desktop_layer.SelectiveClickThrough(self, lambda: [self.header_screen_rect()])
        self._ct.start()

    def _stop_clickthrough(self):
        if self._ct is not None:
            try:
                self._ct.stop()
            finally:
                self._ct = None

    # -- content
    def _filtered_rows(self):
        st = self._state()
        rows = self._get_rows() or []
        sel = st.get("selected_symbols") or []
        cat = st.get("category_filter", "ALL")
        if sel:
            rows = [r for r in rows if r["symbol"] in sel]
        if cat != "ALL":
            rows = [r for r in rows if (r.get("category") or "").upper() == cat]
        return rows                                   # no cap: show everything that matches

    def _chip(self, height=ROW_H):
        f = tk.Frame(self.rows_frame, bg=GLASS, height=height, highlightthickness=1,
                     highlightbackground=EDGE, highlightcolor=EDGE)
        f.pack_propagate(False)
        return f

    def refresh(self):
        """Call on every quote update (15s cadence)."""
        y0 = self.body.yview()[0]
        self._visible = self._filtered_rows()
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self._badges = []
        if not self._visible:
            chip = self._chip(2 * ROW_H)
            chip.pack(fill="x")
            tk.Label(chip, text="No stocks matching filter", font=("Segoe UI", 8),
                     bg=GLASS, fg=MUTED).pack(fill="both", expand=True)
        for i, r in enumerate(self._visible):
            arrow, color, pct = _fmt_row(r.get("price"), r.get("change_pct"))
            row = self._chip()
            row.pack(fill="x", pady=(ROW_GAP if i else 0, 0))
            price = f"{r['price']:.2f}" if r.get("price") is not None else "--"
            tk.Label(row, text=f"{r['symbol']:<6}", font=FONT_ROW, bg=GLASS,
                     fg="#E5E7EB", width=6, anchor="w").pack(side="left", padx=(8, 0))
            tk.Label(row, text=f"{price:>8}", font=FONT_ROW, bg=GLASS,
                     fg="#E5E7EB", anchor="e").pack(side="left")
            tk.Label(row, text=f" {arrow} {pct}", font=FONT_ROW, bg=GLASS,
                     fg=color).pack(side="left")
            if r.get("alert"):
                b = tk.Label(row, text=f"[{r['alert']}]", font=("Segoe UI", 8, "bold"),
                             bg=GLASS, fg=AMBER)
                b.pack(side="right", padx=8)
                self._badges.append(b)
        _bind_wheel(self.rows_frame, self._on_wheel)
        _bind_wheel(self.body, self._on_wheel)
        self._update_alert_pill()
        self._autosize()
        self.body.yview_moveto(y0 if self._overflow else 0)

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
            self.center.config(text="Mini Tracker", bg=HDR_BG, fg=MUTED, cursor="",
                               padx=0, pady=0)
            self._alerting = False

    def _center_click(self, _e):
        if getattr(self, "_alerting", False):
            self._acknowledge()
            self.refresh()

    def _flash_tick(self):
        self._flash_on = not self._flash_on
        for b in getattr(self, "_badges", []):
            try:
                b.config(fg=AMBER if self._flash_on else "#7A5208")
            except tk.TclError:
                pass
        self._flash_job = self.after(600, self._flash_tick)

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
        self._save(enabled=False)
        self.destroy()

    def destroy(self):
        if self._flash_job:
            try:
                self.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None
        try:
            self._stop_clickthrough()
        except Exception:
            pass
        if self._settings and self._settings.winfo_exists():
            try:
                self._settings.destroy()
            except Exception:
                pass
            self._settings = None
        super().destroy()


# --------------------------------------------------------- Settings dialog
class MiniTrackerSettingsDialog(tk.Toplevel):
    """Glass-styled filter dialog. No selection cap; the list scrolls on long watchlists."""

    def __init__(self, parent, state, get_rows, on_apply):
        super().__init__(parent, bg=KEY)
        self._on_apply = on_apply
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        apply_glass(self, 0.94)

        self.card = tk.Frame(self, bg=GLASS, highlightthickness=1,
                             highlightbackground=EDGE_HI, highlightcolor=EDGE_HI)
        self.card.pack(fill="both", expand=True)
        card = self.card

        self.cat = tk.StringVar(value=state.get("category_filter", "ALL"))
        sel = set(state.get("selected_symbols") or [])
        rows = get_rows() or []

        tk.Label(card, text="Mini Tracker Filter", font=FONT_UI, bg=GLASS,
                 fg="#E5E7EB").pack(anchor="w", padx=12, pady=(10, 4))

        pills = tk.Frame(card, bg=GLASS)
        pills.pack(anchor="w", padx=10)
        self._pill_btns = {}
        for c in CATEGORIES:
            b = PillButton(pills, c, lambda c=c: self._set_cat(c), parent_bg=GLASS,
                           padx=8, pady=2, font=("Segoe UI", 8, "bold"))
            b.pack(side="left", padx=2)
            self._pill_btns[c] = b
        self._set_cat(self.cat.get(), apply=False)

        self.count_lbl = tk.Label(card, text="", font=("Segoe UI", 8), bg=GLASS, fg=MUTED)
        self.count_lbl.pack(anchor="w", padx=12, pady=(8, 0))

        # scrollable checkbox list
        self.vars = {}
        wrap = tk.Frame(card, bg=GLASS)
        wrap.pack(fill="x", padx=10, pady=4)
        self.list_canvas = tk.Canvas(wrap, bg=GLASS, highlightthickness=0, bd=0, width=230, height=40)
        self.list_canvas.pack(fill="x")
        self.inner = tk.Frame(self.list_canvas, bg=GLASS)
        self.list_canvas.create_window((0, 0), window=self.inner, anchor="nw", width=230)
        for r in rows:
            v = tk.BooleanVar(value=r["symbol"] in sel)
            self.vars[r["symbol"]] = v
            tk.Checkbutton(self.inner, text=f"{r['symbol']}  ({r.get('category') or '-'})",
                           variable=v, command=self._toggle,
                           bg=GLASS, fg="#E5E7EB", selectcolor="#2A2A2A",
                           activebackground=GLASS, activeforeground="#FFFFFF",
                           highlightthickness=0, bd=0,
                           font=("Segoe UI", 9), anchor="w").pack(fill="x")
        self.inner.update_idletasks()
        self._content_h = max(1, self.inner.winfo_reqheight())
        self.list_canvas.configure(scrollregion=(0, 0, 230, self._content_h))
        _bind_wheel(self.inner, self._on_wheel)
        _bind_wheel(self.list_canvas, self._on_wheel)

        btns = tk.Frame(card, bg=GLASS)
        btns.pack(fill="x", padx=10, pady=(4, 10))
        for t, cmd in (("Done", self.destroy), ("Clear", self._clear), ("All", self._select_all)):
            PillButton(btns, t, cmd, parent_bg=GLASS).pack(side="right", padx=3)

        self._update_count()
        self.update_idletasks()
        w = self.winfo_reqwidth()
        x0 = parent.winfo_x() - w - 8
        if x0 < 0:
            x0 = parent.winfo_x() + parent.winfo_width() + 8
        # cap the list so the dialog fits the monitor's work area
        l, t, r, b = _work_area_for_rect(x0, parent.winfo_y(), w, 200)
        chrome = self.winfo_reqheight() - self.list_canvas.winfo_reqheight()
        self._list_h = max(40, min(self._content_h, int((b - t) * 0.9) - chrome))
        self.list_canvas.config(height=self._list_h)
        self.update_idletasks()
        h = self.winfo_reqheight()
        y0 = max(0, parent.winfo_y() + parent.winfo_height() - h)
        x0, y0 = _clamp_to_work_area(x0, y0, w, h)
        self.geometry(f"{w}x{h}+{x0}+{y0}")
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_force()

    def _on_wheel(self, e):
        if self._content_h > self.list_canvas.winfo_height():
            self.list_canvas.yview_scroll(_wheel_steps(e) * 2, "units")

    def _set_cat(self, c, apply=True):
        self.cat.set(c)
        for name, b in self._pill_btns.items():
            on = name == c
            b.restyle(ACCENT if on else "#1B2330", "#111111" if on else MUTED,
                      ACCENT if on else EDGE_HI)
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
