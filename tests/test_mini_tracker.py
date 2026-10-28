"""Tests for the floating Mini Tracker window, selective click-through, state persistence,
and WidgetView integration."""
from __future__ import annotations

import os
import types
import tkinter as tk
from datetime import datetime, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display (xvfb-run)")

from app_core import AppController
from config import Config
from conftest import tick
from models import PriceTick
from notifier import Notifier, NullBackends
from providers.mock import MockProvider
from store import Store
from test_config_store import EXAMPLE, write
from ui.common import TkTimers
from ui import mini_tracker
from ui.mini_tracker import (
    ACCENT,
    KEY,
    MiniTrackerSettingsDialog,
    MiniTrackerWindow,
    _clamp_to_work_area,
    _fmt_row,
    _work_area_for_rect,
)
from ui.widget import WidgetView

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


class FakeLayer:
    def __init__(self):
        self.fits = []
        self.interactive_calls = []

    def fit(self, w, h):
        self.fits.append((w, h))

    def set_interactive(self, on):
        self.interactive_calls.append(on)


@pytest.fixture
def tk_app(tmp_path):
    root = None
    for _ in range(5):
        try:
            root = tk.Tk()
            break
        except tk.TclError:
            import time
            time.sleep(0.05)
    if root is None:
        root = tk.Tk()
    root.withdraw()
    st = Store(tmp_path)
    write(st.watchlist_path, EXAMPLE)
    notifier = Notifier(Config(), NullBackends(), TkTimers(root), lambda: T0)
    mp = MockProvider({"MSFT": [410]})
    ctrl = AppController(Config(), st, mp, notifier, now_fn=lambda: T0)
    ctrl.on_ticks([tick("207.50", "AAPL", T0, 200), tick("201.90", "TTWO", T0, 206)], T0)
    layer = FakeLayer()
    quits = []
    view = WidgetView(root, ctrl, layer, {}, now_fn=lambda: T0, request_quit=lambda: quits.append(1))
    view.render()
    try:
        yield root, ctrl, view, layer, st
    finally:
        if view.mini and view.mini.winfo_exists():
            view.mini.destroy()
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_quote_listener_registration_and_dispatch(tmp_path):
    st = Store(tmp_path)
    write(st.watchlist_path, EXAMPLE)
    notifier = Notifier(Config(), NullBackends(), None, lambda: T0)
    ctrl = AppController(Config(), st, MockProvider({}), notifier, now_fn=lambda: T0)

    calls = []
    fn = lambda: calls.append(1)
    ctrl.add_quote_listener(fn)

    t1 = tick("210.00", "AAPL", T0, 200)
    ctrl.on_ticks([t1], T0)
    assert len(calls) == 1

    ctrl.remove_quote_listener(fn)
    ctrl.on_ticks([t1], T0)
    assert len(calls) == 1


def test_mini_tracker_state_persistence(tmp_path):
    st = Store(tmp_path)
    write(st.watchlist_path, EXAMPLE)
    notifier = Notifier(Config(), NullBackends(), None, lambda: T0)
    ctrl = AppController(Config(), st, MockProvider({}), notifier, now_fn=lambda: T0)

    assert ctrl.get_mini_tracker_state() == {}
    ctrl.set_mini_tracker_state({"pinned": True, "category_filter": "SELL", "selected_symbols": ["AAPL"]})

    saved = ctrl.get_mini_tracker_state()
    assert saved.get("pinned") is True
    assert saved.get("category_filter") == "SELL"
    assert saved.get("selected_symbols") == ["AAPL"]

    # Verify reload from disk
    ctrl2 = AppController(Config(), st, MockProvider({}), notifier, now_fn=lambda: T0)
    saved2 = ctrl2.get_mini_tracker_state()
    assert saved2.get("pinned") is True
    assert saved2.get("category_filter") == "SELL"


def test_clamp_to_work_area():
    x, y = _clamp_to_work_area(-100, -50, 200, 150)
    assert x >= 0
    assert y >= 0


def test_fmt_row():
    arrow, col, pct = _fmt_row(105.0, 5.0)
    assert arrow == "▲"
    assert "+5.00%" in pct

    arrow, col, pct = _fmt_row(95.0, -5.0)
    assert arrow == "▼"
    assert "-5.00%" in pct

    arrow, col, pct = _fmt_row(100.0, 0.0)
    assert arrow == "―"
    assert "0.00%" in pct

    arrow, col, pct = _fmt_row(None, None)
    assert arrow == "―"
    assert pct == "  --.--%"


def test_mini_tracker_filtering_and_rows(tk_app):
    root, ctrl, view, layer, st = tk_app
    state = {"category_filter": "ALL", "selected_symbols": []}

    rows_data = [
        {"symbol": "AAPL", "price": 207.50, "change_pct": 3.75, "category": "SELL", "alert": None},
        {"symbol": "TTWO", "price": 201.90, "change_pct": -2.0, "category": "BUY", "alert": "BUY"},
        {"symbol": "MSFT", "price": 410.00, "change_pct": 0.5, "category": "WATCH", "alert": None},
    ]

    win = MiniTrackerWindow(
        root,
        get_state=lambda: state,
        set_state=lambda patch: state.update(patch),
        get_rows=lambda: rows_data,
        get_alerts=lambda: (0, "warn"),
        acknowledge=lambda: True,
    )
    try:
        filtered = win._filtered_rows()
        assert len(filtered) == 3

        state["category_filter"] = "SELL"
        filtered_sell = win._filtered_rows()
        assert len(filtered_sell) == 1
        assert filtered_sell[0]["symbol"] == "AAPL"

        state["category_filter"] = "ALL"
        state["selected_symbols"] = ["TTWO"]
        filtered_sel = win._filtered_rows()
        assert len(filtered_sel) == 1
        assert filtered_sel[0]["symbol"] == "TTWO"
    finally:
        win.destroy()


def test_mini_tracker_alert_pill_acknowledge(tk_app):
    root, ctrl, view, layer, st = tk_app
    state = {}
    ack_called = []
    alert_info = [(1, "warn")]

    win = MiniTrackerWindow(
        root,
        get_state=lambda: state,
        set_state=lambda patch: state.update(patch),
        get_rows=lambda: [],
        get_alerts=lambda: alert_info[0],
        acknowledge=lambda: (ack_called.append(1), alert_info.clear(), alert_info.append((0, "warn")))[0],
    )
    try:
        assert win._alerting is True
        assert "1 Alert" in win.center.cget("text")

        # Click the alert pill
        win._center_click(None)
        assert len(ack_called) == 1
        assert win._alerting is False
        assert win.center.cget("text") == "Mini Tracker"
    finally:
        win.destroy()


def test_mini_tracker_critical_alert_pill(tk_app):
    root, ctrl, view, layer, st = tk_app
    state = {}

    win = MiniTrackerWindow(
        root,
        get_state=lambda: state,
        set_state=lambda patch: state.update(patch),
        get_rows=lambda: [],
        get_alerts=lambda: (2, "critical"),
        acknowledge=lambda: True,
    )
    try:
        assert win._alerting is True
        assert "🚨 2 Alerts" in win.center.cget("text")
    finally:
        win.destroy()


def test_mini_tracker_pinning(tk_app):
    root, ctrl, view, layer, st = tk_app
    state = {}

    win = MiniTrackerWindow(
        root,
        get_state=lambda: state,
        set_state=lambda patch: state.update(patch),
        get_rows=lambda: [],
        get_alerts=lambda: (0, "warn"),
        acknowledge=lambda: True,
    )
    try:
        win.set_pinned(True)
        assert win._pinned is True
        assert state.get("pinned") is True

        win.set_pinned(False)
        assert win._pinned is False
        assert state.get("pinned") is False
    finally:
        win.destroy()


def test_widget_view_toggle_mini(tk_app):
    root, ctrl, view, layer, st = tk_app

    assert view.mini is None
    view.toggle_mini()
    assert view.mini is not None
    assert view.mini.winfo_exists()
    assert ctrl.get_mini_tracker_state().get("enabled") is True

    # Toggle again should close it
    view.toggle_mini()
    assert view.mini is None
    assert ctrl.get_mini_tracker_state().get("enabled") is False


def test_widget_view_mini_rows(tk_app):
    root, ctrl, view, layer, st = tk_app
    rows = view._mini_rows()
    assert len(rows) >= 2
    symbols = [r["symbol"] for r in rows]
    assert "AAPL" in symbols
    assert "TTWO" in symbols
    for r in rows:
        assert "price" in r
        assert "change_pct" in r
        assert "category" in r
        assert "alert" in r


def test_status_strip_mini_tracker_button(tk_app):
    root, ctrl, view, layer, st = tk_app
    # Verify canvas tag for mini tracker
    items = view.canvas.find_withtag("status_mini_tracker")
    assert len(items) > 0
    assert "status_mini_tracker" in view.tooltips

    # Clicking the tag toggles the mini tracker
    view.toggle_mini()
    assert view.mini is not None
    view.render()
    items_after = view.canvas.find_withtag("status_mini_tracker")
    assert len(items_after) > 0
    view.toggle_mini()
    assert view.mini is None


def test_end_to_end_alert_flow_in_mini_tracker(tk_app):
    root, ctrl, view, layer, st = tk_app
    view.toggle_mini()
    assert view.mini is not None

    # Simulate an incoming unacknowledged HIGH alert
    ctrl.notifier._latest = {
        "text": "AAPL reached $215.00",
        "priority": "HIGH",
        "acked": False,
        "escalated": False,
    }
    view._refresh_mini()
    assert view.mini._alerting is True
    assert "🚨 1 Alert" in view.mini.center.cget("text")

    # Acknowledge via clicking the center pill
    view.mini._center_click(None)
    assert ctrl.notifier._latest["acked"] is True
    assert view.mini._alerting is False
    assert view.mini.center.cget("text") == "Mini Tracker"


def test_watchlist_update_propagates_to_mini_tracker(tk_app):
    root, ctrl, view, layer, st = tk_app
    view.toggle_mini()
    assert view.mini is not None

    from models import StockConfig
    # Add new stock
    new_s = StockConfig("GOOGL", "WATCH", "Alphabet", False, None, None, {}, [])
    ctrl.add_stock(new_s)

    # Deliver tick for GOOGL
    t_goog = tick("180.00", "GOOGL", T0, 175)
    ctrl.on_ticks([t_goog], T0)
    root.update_idletasks()

    rows = view._mini_rows()
    symbols = [r["symbol"] for r in rows]
    assert "GOOGL" in symbols

    # Remove stock
    ctrl.remove_stock("GOOGL")
    rows2 = view._mini_rows()
    symbols2 = [r["symbol"] for r in rows2]
    assert "GOOGL" not in symbols2


def test_edit_mode_and_mini_tracker_coexistence(tk_app):
    root, ctrl, view, layer, st = tk_app
    view.toggle_mini()
    assert view.mini is not None
    assert view.mini.winfo_exists()

    # Enter Edit Mode
    view.enter_edit()
    assert view.editing is True
    assert view.mini.winfo_exists()

    # Exit Edit Mode
    view.exit_edit()
    assert view.editing is False
    assert view.mini.winfo_exists()
    view.toggle_mini()



# ======================================================================================
# Redesign: toggle switch, glass look, unlimited selection, pin / click-through
# ======================================================================================
def _mk_win(root, rows=None, state=None, **kw):
    state = {} if state is None else state
    rows = [] if rows is None else rows
    return state, MiniTrackerWindow(
        root,
        get_state=lambda: state,
        set_state=lambda patch: state.update(patch),
        get_rows=lambda: rows,
        get_alerts=lambda: (0, "warn"),
        acknowledge=lambda: True,
        **kw,
    )


def _fake_rows(n, cat="WATCH"):
    return [{"symbol": f"S{i:03d}", "price": 10.0 + i, "change_pct": 0.5, "category": cat, "alert": None}
            for i in range(n)]


def _toggle_geometry(view):
    track = view.canvas.find_withtag("mini_toggle_track")[0]
    thumb = view.canvas.find_withtag("mini_toggle_thumb")[0]
    xs = view.canvas.coords(track)[0::2]
    tx1, _, tx2, _ = view.canvas.coords(thumb)
    return view.canvas.itemcget(track, "fill"), (min(xs) + max(xs)) / 2, (tx1 + tx2) / 2


# ---- 1. toggle switch ----------------------------------------------------------------
def test_toggle_switch_off_state_rendering(tk_app):
    root, ctrl, view, layer, st = tk_app
    fill, track_cx, thumb_cx = _toggle_geometry(view)
    assert fill.upper() != ACCENT.upper()          # outlined / dark pill
    assert thumb_cx < track_cx                      # thumb on the left
    assert view.tooltips["status_mini_tracker"] == "Enable Mini Tracker"
    assert view.show_tooltip("status_mini_tracker") == "Enable Mini Tracker"
    view.hide_tooltip()


def test_toggle_switch_on_state_and_clicks(tk_app):
    root, ctrl, view, layer, st = tk_app
    assert view.canvas.tag_bind("status_mini_tracker", "<Button-1>")   # clickable
    assert view.canvas.tag_bind("status_mini_tracker", "<Enter>")      # hover (cursor + tooltip)

    view.on_mini_toggle_click()                    # what a click on the pill calls
    assert view.mini_open and view.mini is not None
    fill, track_cx, thumb_cx = _toggle_geometry(view)
    assert fill.upper() == ACCENT.upper()          # filled accent pill
    assert thumb_cx > track_cx                      # thumb slid right
    assert view.tooltips["status_mini_tracker"] == "Disable Mini Tracker"

    view.on_mini_toggle_click()
    assert view.mini is None
    fill, track_cx, thumb_cx = _toggle_geometry(view)
    assert fill.upper() != ACCENT.upper() and thumb_cx < track_cx


def test_toggle_switch_is_far_right_of_header(tk_app):
    root, ctrl, view, layer, st = tk_app
    right = view.canvas.bbox("status_mini_tracker")[2]
    for tag in ("status_sun_moon", "status_heartbeat", "status_clocks"):
        assert view.canvas.bbox(tag)[2] < view.canvas.bbox("status_mini_tracker")[0]
    assert int(float(view.canvas.cget("width"))) - right <= 40


def test_toggle_switch_syncs_when_mini_closed_with_x(tk_app):
    root, ctrl, view, layer, st = tk_app
    view.toggle_mini()
    assert _toggle_geometry(view)[0].upper() == ACCENT.upper()
    view.mini._close()                              # the X on the Mini Tracker
    assert view.mini is None
    assert _toggle_geometry(view)[0].upper() != ACCENT.upper()
    assert ctrl.get_mini_tracker_state().get("enabled") is False


# ---- 2. transparency -----------------------------------------------------------------
def test_mini_tracker_translucent_window(tk_app):
    root, *_ = tk_app
    state, win = _mk_win(root, _fake_rows(3))
    try:
        assert 0.80 <= mini_tracker.ALPHA <= 0.95
        if os.name == "nt":                         # X11 without a compositor reports 1.0
            assert 0.80 <= float(win.attributes("-alpha")) <= 0.95
        assert win.cget("bg") == KEY                # colour-keyed background, not a black box
        if os.name == "nt":
            assert str(win.attributes("-transparentcolor")) == KEY
        assert str(win.header.cget("highlightbackground")) not in ("", KEY)   # outlined header
        for row in win.rows_frame.winfo_children():
            assert int(row.cget("highlightthickness")) >= 1                   # outlined rows
            assert row.cget("bg") != KEY
    finally:
        win.destroy()


def test_settings_dialog_translucent(tk_app):
    root, *_ = tk_app
    state, win = _mk_win(root, _fake_rows(3))
    try:
        win.open_settings()
        dlg = win._settings
        if os.name == "nt":
            assert 0.80 <= float(dlg.attributes("-alpha")) <= 0.98
        assert dlg.cget("bg") == KEY
        assert int(dlg.card.cget("highlightthickness")) >= 1
    finally:
        win.destroy()


# ---- 3. unlimited selection ----------------------------------------------------------
def test_no_five_stock_cap_in_window(tk_app):
    root, *_ = tk_app
    rows = _fake_rows(12)
    state, win = _mk_win(root, rows, {"selected_symbols": [r["symbol"] for r in rows]})
    try:
        assert len(win._filtered_rows()) == 12
        assert len(win.rows_frame.winfo_children()) == 12
        assert not win._overflow                    # 12 rows fit comfortably
    finally:
        win.destroy()


def test_settings_dialog_select_all_clear_and_no_cap(tk_app):
    root, *_ = tk_app
    rows = _fake_rows(30)
    state, win = _mk_win(root, rows)
    try:
        assert not hasattr(MiniTrackerSettingsDialog, "MAX_SELECTED")
        win.open_settings()
        dlg = win._settings
        for sym in list(dlg.vars)[:8]:              # tick 8 by hand, past the old cap of 5
            dlg.vars[sym].set(True)
            dlg._toggle()
        assert len(state["selected_symbols"]) == 8
        assert len(win.rows_frame.winfo_children()) == 8

        dlg._select_all()
        assert len(state["selected_symbols"]) == 30
        assert len(win.rows_frame.winfo_children()) == 30
        dlg._clear()
        assert state["selected_symbols"] == []
    finally:
        win.destroy()


def test_settings_list_scrolls_instead_of_overflowing(tk_app):
    root, *_ = tk_app
    state, win = _mk_win(root, _fake_rows(300))
    try:
        win.open_settings()
        dlg = win._settings
        dlg.update_idletasks()
        wa = _work_area_for_rect(dlg.winfo_x(), dlg.winfo_y(), dlg.winfo_width(), dlg.winfo_height())
        assert dlg.winfo_height() <= wa[3] - wa[1]
        assert dlg._content_h > dlg._list_h          # more rows than visible -> scrollable
        dlg._on_wheel(types.SimpleNamespace(delta=-120, num=None))
        assert dlg.list_canvas.yview()[0] > 0
    finally:
        win.destroy()


def test_many_rows_fit_work_area_and_scroll(tk_app):
    root, *_ = tk_app
    state, win = _mk_win(root, _fake_rows(300))
    try:
        assert len(win._filtered_rows()) == 300
        wa = _work_area_for_rect(win.winfo_x(), win.winfo_y(), win.winfo_width(), win.winfo_height())
        assert win.winfo_height() <= wa[3] - wa[1]  # clamped to the monitor
        assert win._overflow and win._content_h > win._body_h
        win._on_wheel(types.SimpleNamespace(delta=-120, num=None))
        assert win.body.yview()[0] > 0
        win.refresh()                               # 15s refresh keeps the scroll position
        assert win.body.yview()[0] > 0
    finally:
        win.destroy()


# ---- 4. pin / click-through ----------------------------------------------------------
class _FakeClickThrough:
    instances = []

    def __init__(self, widget, solid_rects_fn, poll_ms=25):
        self.widget, self.solid_rects_fn = widget, solid_rects_fn
        self.active = False
        self.starts = self.stops = 0
        _FakeClickThrough.instances.append(self)

    def start(self):
        self.active = True
        self.starts += 1

    def stop(self):
        self.active = False
        self.stops += 1


def test_pin_toggles_selective_click_through_and_visuals(tk_app, monkeypatch):
    root, *_ = tk_app
    _FakeClickThrough.instances.clear()
    monkeypatch.setattr(mini_tracker, "desktop_layer", types.SimpleNamespace(SelectiveClickThrough=_FakeClickThrough))
    state, win = _mk_win(root, _fake_rows(3))
    try:
        assert win.pin_btn.cget("text") == "📍" and not win._pinned
        muted = win.pin_btn.cget("fg")

        win.set_pinned(True)
        ct = win._ct
        assert ct is _FakeClickThrough.instances[0] and ct.active
        assert win.pin_btn.cget("text") == "📌"
        assert win.pin_btn.cget("fg").upper() == ACCENT.upper()     # bright highlight
        assert state["pinned"] is True

        # only the header is a solid (clickable) rect - rows are NOT in the list
        rects = ct.solid_rects_fn()
        assert rects == [win.header_screen_rect()]
        hx1, hy1, hx2, hy2 = rects[0]
        first_row = win.rows_frame.winfo_children()[0]
        assert first_row.winfo_rooty() >= hy2

        win.set_pinned(False)
        assert ct.stops == 1 and win._ct is None
        assert win.pin_btn.cget("text") == "📍" and win.pin_btn.cget("fg") == muted
        assert state["pinned"] is False
    finally:
        win.destroy()


def test_destroy_stops_click_through(tk_app, monkeypatch):
    root, *_ = tk_app
    _FakeClickThrough.instances.clear()
    monkeypatch.setattr(mini_tracker, "desktop_layer", types.SimpleNamespace(SelectiveClickThrough=_FakeClickThrough))
    state, win = _mk_win(root, _fake_rows(2), {"pinned": True})
    ct = win._ct
    assert ct is not None and ct.active              # restored pinned state starts click-through
    win.destroy()
    assert not ct.active


@pytest.mark.skipif(os.name != "nt", reason="Win32 click-through")
def test_win32_pinned_rows_click_through_header_solid(tk_app):
    import ctypes
    dl = getattr(mini_tracker, "desktop_layer", None)
    if dl is None:
        import desktop_layer as dl

    root, *_ = tk_app
    state, win = _mk_win(root, _fake_rows(3))
    try:
        win.update()
        win.set_pinned(True)
        ct = win._ct
        assert isinstance(ct, dl.SelectiveClickThrough)
        user32 = ctypes.windll.user32

        hx1, hy1, hx2, hy2 = win.header_screen_rect()
        if not user32.SetCursorPos((hx1 + hx2) // 2, (hy1 + hy2) // 2):
            pytest.skip("cannot move the cursor in this session")
        ct.poll_once()
        hwnd = ct.hwnd
        assert hwnd == dl.toplevel_hwnd(win)
        ex = dl.GetWindowLongW(hwnd, dl.GWL_EXSTYLE)
        assert ex & dl.WS_EX_LAYERED
        assert not ex & dl.WS_EX_TRANSPARENT          # over the header: solid

        row = win.rows_frame.winfo_children()[0]
        user32.SetCursorPos(row.winfo_rootx() + 20, row.winfo_rooty() + 5)
        ct.poll_once()
        assert dl.GetWindowLongW(hwnd, dl.GWL_EXSTYLE) & dl.WS_EX_TRANSPARENT   # over rows: click-through

        win.set_pinned(False)
        assert not dl.GetWindowLongW(hwnd, dl.GWL_EXSTYLE) & dl.WS_EX_TRANSPARENT
    finally:
        win.destroy()
