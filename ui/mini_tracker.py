"""ui/mini_tracker.py - floating always-on-top companion for the Stock Alert Widget.

Adapter contract (wire these in ui/widget.py, see integration_notes.md):
    get_state() -> dict            # the "mini_tracker" block of state.json
    set_state(dict) -> None        # merge + persist
    get_rows()  -> list[dict]      # {"symbol","price","change_pct","category","alert"}
                                   #   alert: None or badge text like "SELL"/"RISK"
    get_alerts() -> (count, level) # level: "warn" (amber) | "critical" (red)
    acknowledge() -> None          # ctrl.notifier.acknowledge_latest
    on_closed() -> None            # sync the main-widget [◱ Mini] toggle
"""
import ctypes
import sys
import tkinter as tk
from ctypes import wintypes

try:
    import desktop_layer
except ImportError:
    from .. import desktop_layer

MARGIN = 24
HEADER_H = 30
ROW_H = 24
WIDTH = 250
CATEGORIES = ["ALL", "SELL", "HOLD", "BUY", "WATCH"]
DEFAULTS = {"enabled": False, "pinned": False, "pos_x": None, "pos_y": None,
            "category_filter": "ALL", "selected_symbols": []}

BG, HDR_BG, MUTED = "#141414", "#1F1F1F", "#9CA3AF"
UP, DOWN, FLAT = "#34D399", "#F87171", "#9CA3AF"
AMBER, RED, ACCENT = "#F59E0B", "#EF4444", "#00FFCC"
FONT_UI = ("Segoe UI", 9, "bold")
FONT_ROW = ("Consolas", 10)
FONT_ICON = ("Segoe UI Emoji", 9)


# ---------------------------------------------------------------- Win32 utils
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


def _clamp_to_work_area(x, y, w, h):
    """Clamp rect into the work area of the nearest *currently attached* monitor."""
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
                    return max(wa.left, min(x, wa.right - w)), max(wa.top, min(y, wa.bottom - h))
        except Exception:
            pass
    l, t, r_, b = _primary_work_area()
    return max(l, min(x, r_ - w)), max(t, min(y, b - h))


def _fmt_row(price, pct):
    if pct is None:
        return "―", FLAT, "  --.--%"
    if pct > 0:
        return "▲", UP, f"{pct:+.2f}%"
    if pct < 0:
        return "▼", DOWN, f"{pct:+.2f}%"
    return "―", FLAT, f"{0:.2f}%"


# ------------------------------------------------------------- Mini Tracker
class MiniTrackerWindow(tk.Toplevel):
    def __init__(self, master, get_state, set_state, get_rows, get_alerts,
                 acknowledge, on_closed=None):
        super().__init__(master, bg=BG)
        self._get_state, self._set_state = get_state, set_state
        self._get_rows, self._get_alerts = get_rows, get_alerts
        self._acknowledge, self._on_closed = acknowledge, on_closed
        self._drag = None
        self._pinned = False
        self._settings = None
        self._flash_on = False
        self._flash_job = None

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.88)
        except tk.TclError:
            pass
        self.configure(highlightthickness=1, highlightbackground="#38BDF8")

        self._build_header()
        self.rows_frame = tk.Frame(self, bg=BG)
        self.rows_frame.pack(fill="both", expand=True)

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
        self.header = tk.Frame(self, bg=HDR_BG, height=HEADER_H)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.pin_btn = tk.Label(self.header, text="📍", font=FONT_ICON, bg=HDR_BG,
                                fg=MUTED, cursor="hand2", padx=8)
        self.pin_btn.pack(side="left")
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

    def _icon(self, text, cmd):
        b = tk.Label(self.header, text=text, font=FONT_ICON, bg=HDR_BG, fg=MUTED,
                     cursor="hand2", padx=7)
        b.pack(side="right")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.config(fg="#FFFFFF"))
        b.bind("<Leave>", lambda e: b.config(fg=MUTED))
        return b

    # -- dragging (header stays draggable)
    def _drag_start(self, e):
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, _e):
        if self._drag:
            self._drag = None
            self._save(pos_x=self.winfo_x(), pos_y=self.winfo_y())

    # -- positioning
    def _get_hwnd(self):
        if sys.platform == "win32":
            try:
                if hasattr(desktop_layer, "GetParent"):
                    return desktop_layer.GetParent(self.winfo_id()) or self.winfo_id()
                u = getattr(ctypes, "windll", None)
                if u and hasattr(u, "user32"):
                    return u.user32.GetParent(self.winfo_id()) or self.winfo_id()
            except Exception:
                pass
        return self.winfo_id()

    def _place_initial(self, st):
        self.update_idletasks()
        w = WIDTH
        h = HEADER_H + max(1, len(self._visible)) * ROW_H + 6
        x, y = st.get("pos_x"), st.get("pos_y")
        if x is None or y is None:
            l, t, r, b = _primary_work_area()
            x, y = r - w - MARGIN, b - h - MARGIN
        x, y = _clamp_to_work_area(int(x), int(y), w, h)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _autosize(self):
        """Resize height to fit rows while keeping the top-left anchored."""
        self.update_idletasks()
        w, h = WIDTH, HEADER_H + max(1, len(self._visible)) * ROW_H + 6
        x, y = self.winfo_x(), self.winfo_y()
        if self.winfo_viewable():
            x, y = _clamp_to_work_area(x, y, w, h)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # -- pinning
    def set_pinned(self, pinned: bool):
        self._pinned = pinned
        self.pin_btn.config(fg=ACCENT if pinned else MUTED,
                            text="📌" if pinned else "📍")
        self.update_idletasks()
        hwnd = self._get_hwnd()
        if pinned:
            if hasattr(desktop_layer, "setup_selective_clickthrough") and hwnd:
                desktop_layer.setup_selective_clickthrough(
                    hwnd, lambda: self.header.winfo_height() or HEADER_H, tk_root=self)
        else:
            if hasattr(desktop_layer, "teardown_selective_clickthrough") and hwnd:
                desktop_layer.teardown_selective_clickthrough(hwnd)
        self._save(pinned=pinned)

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
        return rows

    def refresh(self):
        """Call on every quote update (15s cadence)."""
        self._visible = self._filtered_rows()
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self._badges = []
        if not self._visible:
            tk.Label(self.rows_frame, text="No stocks matching filter", font=("Segoe UI", 8),
                     bg=BG, fg=MUTED, height=2).pack(fill="both", expand=True)
        for r in self._visible:
            arrow, color, pct = _fmt_row(r.get("price"), r.get("change_pct"))
            row = tk.Frame(self.rows_frame, bg=BG, height=ROW_H)
            row.pack(fill="x")
            row.pack_propagate(False)
            price = f"{r['price']:.2f}" if r.get("price") is not None else "--"
            tk.Label(row, text=f"{r['symbol']:<6}", font=FONT_ROW, bg=BG,
                     fg="#E5E7EB", width=6, anchor="w").pack(side="left", padx=(10, 0))
            tk.Label(row, text=f"{price:>8}", font=FONT_ROW, bg=BG,
                     fg="#E5E7EB", anchor="e").pack(side="left")
            tk.Label(row, text=f" {arrow} {pct}", font=FONT_ROW, bg=BG,
                     fg=color).pack(side="left")
            if r.get("alert"):
                b = tk.Label(row, text=f"[{r['alert']}]", font=("Segoe UI", 8, "bold"),
                             bg=BG, fg=AMBER)
                b.pack(side="right", padx=8)
                self._badges.append(b)
        self._update_alert_pill()
        self._autosize()
        if self._pinned:  # re-assert layered/topmost after rebuild
            self.attributes("-topmost", True)

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
        hwnd = self._get_hwnd()
        if hasattr(desktop_layer, "teardown_selective_clickthrough") and hwnd:
            try:
                desktop_layer.teardown_selective_clickthrough(hwnd)
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
    def __init__(self, parent, state, get_rows, on_apply):
        super().__init__(parent, bg="#1A1A1A")
        self._on_apply = on_apply
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        self.configure(highlightthickness=1, highlightbackground="#38BDF8")

        self.cat = tk.StringVar(value=state.get("category_filter", "ALL"))
        sel = set(state.get("selected_symbols") or [])
        rows = get_rows() or []

        tk.Label(self, text="Mini Tracker Filter", font=FONT_UI, bg="#1A1A1A",
                 fg="#E5E7EB").pack(anchor="w", padx=12, pady=(10, 4))

        pills = tk.Frame(self, bg="#1A1A1A")
        pills.pack(anchor="w", padx=10)
        self._pill_btns = {}
        for c in CATEGORIES:
            b = tk.Label(pills, text=c, font=("Segoe UI", 8, "bold"), padx=8, pady=2,
                         cursor="hand2")
            b.pack(side="left", padx=2)
            b.bind("<Button-1>", lambda e, c=c: self._set_cat(c))
            self._pill_btns[c] = b
        self._set_cat(self.cat.get(), apply=False)

        tk.Label(self, text="Select stocks to track", font=("Segoe UI", 8),
                 bg="#1A1A1A", fg=MUTED).pack(anchor="w", padx=12, pady=(8, 0))

        self.vars = {}
        box_wrap = tk.Frame(self, bg="#1A1A1A")
        box_wrap.pack(fill="both", expand=True, padx=10, pady=4)
        if len(rows) > 7:
            canvas = tk.Canvas(box_wrap, bg="#1A1A1A", highlightthickness=0, height=180, width=220)
            sb = tk.Scrollbar(box_wrap, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=sb.set)
            box = tk.Frame(canvas, bg="#1A1A1A")
            canvas.create_window((0, 0), window=box, anchor="nw")
            canvas.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            box.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        else:
            box = box_wrap

        for r in rows:
            v = tk.BooleanVar(value=r["symbol"] in sel)
            self.vars[r["symbol"]] = v
            tk.Checkbutton(box, text=f"{r['symbol']}  ({r.get('category') or '-'})",
                           variable=v, command=self._push,
                           bg="#1A1A1A", fg="#E5E7EB", selectcolor="#2A2A2A",
                           activebackground="#1A1A1A", activeforeground="#FFFFFF",
                           font=("Segoe UI", 9), anchor="w").pack(fill="x")

        btns = tk.Frame(self, bg="#1A1A1A")
        btns.pack(fill="x", padx=10, pady=(4, 10))
        for t, cmd in (("Done", self.destroy), ("Clear", self._clear)):
            b = tk.Label(btns, text=t, font=("Segoe UI", 9, "bold"), bg="#2A2A2A",
                         fg="#E5E7EB", padx=12, pady=3, cursor="hand2")
            b.pack(side="right", padx=3)
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.config(bg="#3A3A3A"))
            b.bind("<Leave>", lambda e, w=b: w.config(bg="#2A2A2A"))

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_x() - w - 8
        if x < 0:
            x = parent.winfo_x() + parent.winfo_width() + 8
        y = max(0, parent.winfo_y() + parent.winfo_height() - h)
        x, y = _clamp_to_work_area(x, y, w, h)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_force()

    def _set_cat(self, c, apply=True):
        self.cat.set(c)
        for name, b in self._pill_btns.items():
            on = name == c
            b.config(bg=ACCENT if on else "#2A2A2A", fg="#111111" if on else MUTED)
        if apply:
            self._push()

    def _toggle(self, sym):
        self._push()

    def _clear(self):
        for v in self.vars.values():
            v.set(False)
        self._push()

    def _push(self):
        self._on_apply(category_filter=self.cat.get(),
                       selected_symbols=[s for s, v in self.vars.items() if v.get()])
