"""Tests for the floating Mini Tracker window, theme alignment, dragging, state persistence,
and WidgetView integration."""
from __future__ import annotations

import os
import time
import types
import tkinter as tk
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display (xvfb-run)")

from app_core import AppController
from config import Config
from conftest import tick
from models import StockConfig
from notifier import Notifier, NullBackends
from providers.mock import MockProvider
from store import Store
from test_config_store import EXAMPLE, write
from ui.common import ACCENT, BG, BG2, BTN, FG, MUTED, TkTimers
from ui import mini_tracker
from ui.mini_tracker import (
    ALPHA,
    BORDER,
    DEFAULTS,
    FLASH_BG,
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
    ctrl.set_mini_tracker_state({
        "enabled": True,
        "pos_x": 120,
        "pos_y": 240,
        "category_filter": "SELL",
        "selected_symbols": ["AAPL"],
    })

    saved = ctrl.get_mini_tracker_state()
    assert saved.get("enabled") is True
    assert saved.get("pos_x") == 120
    assert saved.get("pos_y") == 240
    assert saved.get("category_filter") == "SELL"
    assert saved.get("selected_symbols") == ["AAPL"]
    assert "pinned" not in saved

    # Verify reload from disk
    ctrl2 = AppController(Config(), st, MockProvider({}), notifier, now_fn=lambda: T0)
    saved2 = ctrl2.get_mini_tracker_state()
    assert saved2.get("enabled") is True
    assert saved2.get("pos_x") == 120
    assert saved2.get("pos_y") == 240
    assert saved2.get("category_filter") == "SELL"
    assert saved2.get("selected_symbols") == ["AAPL"]
    assert "pinned" not in saved2


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

        # M4: Unknown symbols in selected_symbols are pruned automatically
        state["category_filter"] = "ALL"
        state["selected_symbols"] = ["NOPE"]
        pruned_rows = win._filtered_rows()
        assert state["selected_symbols"] == []
        assert len(pruned_rows) == 3

        # Empty filter state (valid symbol in non-matching category) renders placeholder message in BG2 chip
        state["category_filter"] = "SELL"
        state["selected_symbols"] = ["TTWO"]
        win.refresh()
        chips = win.rows_frame.winfo_children()
        assert len(chips) == 1
        assert chips[0].cget("bg").upper() == BG2.upper()
        lbl = chips[0].winfo_children()[0]
        assert lbl.cget("text") == "No stocks matching filter"
        assert lbl.cget("fg").upper() == MUTED.upper()
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
        assert win.center.cget("bg").upper() == "#FBBF24"
        assert win.center.cget("fg").upper() == "#111111"

        # Click the alert pill
        win._center_click(None)
        assert len(ack_called) == 1
        assert win._alerting is False
        assert win.center.cget("text") == "Mini Tracker"
        assert win.center.cget("bg").upper() == BG2.upper()
        assert win.center.cget("fg").upper() == FG.upper()
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
        assert win.center.cget("bg").upper() == "#F87171"
        assert win.center.cget("fg").upper() == "#111111"
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
        assert "flashing" in r
        assert isinstance(r["flashing"], bool)
        if not r["flashing"]:
            assert r["alert"] is None


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

    # Add new stock
    new_s = StockConfig("GOOGL", "WATCH", "Alphabet", False, None, [])
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

    # Enter Edit Mode with Mini Tracker closed -> BTN
    view.enter_edit()
    assert view.editing is True
    assert view.panel.btn_mini.cget("bg").upper() == BTN.upper()

    # Open Mini Tracker from Edit Mode -> ACCENT
    view.toggle_mini()
    assert view.mini is not None and view.mini.winfo_exists()
    assert view.panel.btn_mini.cget("bg").upper() == ACCENT.upper()

    # Close Mini Tracker via X while in Edit Mode -> immediately syncs back to BTN
    view.mini._close()
    assert view.mini is None
    assert view.mini_open is False
    assert view.panel.btn_mini.cget("bg").upper() == BTN.upper()

    # Open again, exit Edit Mode, and verify Mini Tracker stays open
    view.toggle_mini()
    assert view.panel.btn_mini.cget("bg").upper() == ACCENT.upper()
    view.exit_edit()
    assert view.editing is False
    assert view.mini.winfo_exists()
    view.toggle_mini()


# ======================================================================================
# Rebuilt Mini Tracker: toggle switch, main widget theme, no-pin, drag, flash, unlimited
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
    assert fill.upper() == BG2.upper()             # main widget BG2 track when OFF
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
    assert fill.upper() == BG2.upper() and thumb_cx < track_cx


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
    assert _toggle_geometry(view)[0].upper() == BG2.upper()
    assert ctrl.get_mini_tracker_state().get("enabled") is False


# ---- 2. main widget theme (no glass / -transparentcolor) -----------------------------
def test_mini_tracker_matches_main_widget_theme(tk_app):
    root, *_ = tk_app
    assert mini_tracker.BG == BG == "#202020"
    assert mini_tracker.BG2 == BG2 == "#2D2D2D"
    assert mini_tracker.BORDER == BORDER == "#444444"
    assert mini_tracker.FLASH_BG == FLASH_BG == "#4B5563"
    assert not hasattr(mini_tracker, "KEY")
    assert not hasattr(mini_tracker, "GLASS")
    assert not hasattr(mini_tracker, "apply_glass")

    state, win = _mk_win(root, _fake_rows(3))
    try:
        assert ALPHA == pytest.approx(0.92, abs=0.01)
        if os.name == "nt":                         # X11 without a compositor reports 1.0
            assert float(win.attributes("-alpha")) == pytest.approx(ALPHA, abs=0.02)
            assert str(win.attributes("-transparentcolor")) == ""

        assert win.cget("bg").upper() == BG.upper()
        assert win.shell.cget("bg").upper() == BG.upper()
        assert int(win.shell.cget("highlightthickness")) >= 1
        assert str(win.shell.cget("highlightbackground")).upper() == BORDER.upper()

        assert win.header.cget("bg").upper() == BG2.upper()
        assert int(win.header.cget("highlightthickness")) >= 1
        assert str(win.header.cget("highlightbackground")).upper() == BORDER.upper()
        assert win.center.cget("bg").upper() == BG2.upper()
        assert win.center.cget("fg").upper() == FG.upper()

        assert win.body.cget("bg").upper() == BG.upper()
        assert win.rows_frame.cget("bg").upper() == BG.upper()

        for row in win.rows_frame.winfo_children():
            assert int(row.cget("highlightthickness")) >= 1
            assert str(row.cget("highlightbackground")).upper() == BORDER.upper()
            assert row.cget("bg").upper() == BG2.upper()
    finally:
        win.destroy()


def test_settings_dialog_matches_main_widget_theme(tk_app):
    root, *_ = tk_app
    state, win = _mk_win(root, _fake_rows(3))
    try:
        win.open_settings()
        dlg = win._settings
        if os.name == "nt":
            assert 0.80 <= float(dlg.attributes("-alpha")) <= 0.98
            assert str(dlg.attributes("-transparentcolor")) == ""

        assert dlg.cget("bg").upper() == BG.upper()
        assert dlg.card.cget("bg").upper() == BG.upper()
        assert int(dlg.card.cget("highlightthickness")) >= 1
        assert str(dlg.card.cget("highlightbackground")).upper() == BORDER.upper()

        assert dlg.list_canvas.cget("bg").upper() == BG2.upper()
        assert dlg.inner.cget("bg").upper() == BG2.upper()

        # Active category pill uses ACCENT; inactive uses BTN/BORDER/MUTED
        all_pill = dlg._pill_btns["ALL"]
        buy_pill = dlg._pill_btns["BUY"]
        assert all_pill.itemcget(all_pill.shape, "fill").upper() == ACCENT.upper()
        assert all_pill.itemcget(all_pill.shape, "outline").upper() == ACCENT.upper()
        assert all_pill.itemcget(all_pill.label, "fill").upper() == "#FFFFFF"
        assert buy_pill.itemcget(buy_pill.shape, "fill").upper() == BTN.upper()
        assert buy_pill.itemcget(buy_pill.shape, "outline").upper() == BORDER.upper()
        assert buy_pill.itemcget(buy_pill.label, "fill").upper() == MUTED.upper()

        dlg._set_cat("SELL")
        sell_pill = dlg._pill_btns["SELL"]
        assert state["category_filter"] == "SELL"
        assert sell_pill.itemcget(sell_pill.shape, "fill").upper() == ACCENT.upper()
        assert all_pill.itemcget(all_pill.shape, "fill").upper() == BTN.upper()

        for cb in dlg.inner.winfo_children():
            assert cb.cget("bg").upper() == BG2.upper()
            assert cb.cget("fg").upper() == FG.upper()
            assert cb.cget("selectcolor").upper() == BG.upper()
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


# ---- 4. no pin mechanism, always draggable, alert click, flashing rows ---------------
def test_no_pin_mechanism(tk_app):
    root, *_ = tk_app
    assert "pinned" not in DEFAULTS
    assert set(DEFAULTS.keys()) == {"enabled", "pos_x", "pos_y", "category_filter", "selected_symbols"}

    state, win = _mk_win(root, _fake_rows(3), {"pinned": True})
    try:
        assert not hasattr(win, "pin_btn")
        assert not hasattr(win, "set_pinned")
        assert not hasattr(win, "_pinned")
        assert not hasattr(win, "_ct")
    finally:
        win.destroy()


def test_mini_tracker_draggable_and_saves_position(tk_app):
    root, *_ = tk_app
    state, win = _mk_win(root, _fake_rows(3))
    try:
        win.update()
        x0, y0 = win.winfo_x(), win.winfo_y()
        w, h = win.winfo_width(), win.winfo_height()
        l, t, r, b = _work_area_for_rect(x0, y0, w, h)

        target_x = max(l + 10, min(l + 120, r - w - 10))
        target_y = max(t + 10, min(t + 120, b - h - 10))
        if target_x == x0 and target_y == y0:
            target_x = max(l, min(x0 - 40, r - w))
            target_y = max(t, min(y0 - 40, b - h))

        start_e = types.SimpleNamespace(widget=win.header, x_root=x0 + 20, y_root=y0 + 10)
        win._drag_start(start_e)
        assert win._drag is not None

        move_e = types.SimpleNamespace(
            widget=win.header,
            x_root=target_x + win._drag[0],
            y_root=target_y + win._drag[1],
        )
        win._drag_move(move_e)
        win.update_idletasks()
        win._drag_end(move_e)
        win.update_idletasks()

        assert win._drag is None
        assert state["pos_x"] == target_x
        assert state["pos_y"] == target_y
        assert win.winfo_x() == target_x
        assert win.winfo_y() == target_y

        # Dragging past screen edge clamps to the work area
        win._drag_start(types.SimpleNamespace(widget=win.shell, x_root=target_x + 10, y_root=target_y + 10))
        assert win._drag is not None
        win._drag_move(types.SimpleNamespace(widget=win.shell, x_root=-5000, y_root=-5000))
        win.update_idletasks()
        win._drag_end(None)
        win.update_idletasks()
        assert state["pos_x"] >= l
        assert state["pos_y"] >= t
        assert win.winfo_x() == state["pos_x"]
        assert win.winfo_y() == state["pos_y"]
    finally:
        win.destroy()


def test_alert_pill_click_does_not_start_drag(tk_app):
    root, *_ = tk_app
    state = {}
    ack_called = []
    alert_info = [(1, "warn")]

    win = MiniTrackerWindow(
        root,
        get_state=lambda: state,
        set_state=lambda patch: state.update(patch),
        get_rows=lambda: _fake_rows(2),
        get_alerts=lambda: alert_info[0],
        acknowledge=lambda: (ack_called.append(1), alert_info.clear(), alert_info.append((0, "warn")))[0],
    )
    try:
        win.update()
        assert win._alerting is True
        orig_x, orig_y = win.winfo_x(), win.winfo_y()

        press = types.SimpleNamespace(widget=win.center, x_root=orig_x + 50, y_root=orig_y + 10)
        win._drag_start(press)
        assert win._drag is None

        win._drag_move(types.SimpleNamespace(widget=win.center, x_root=orig_x + 150, y_root=orig_y + 110))
        win._drag_end(press)
        win.update_idletasks()
        assert win.winfo_x() == orig_x
        assert win.winfo_y() == orig_y
        assert "pos_x" not in state and "pos_y" not in state

        # Clicking the alert pill acknowledges it
        win._center_click(press)
        assert len(ack_called) == 1
        assert win._alerting is False
        assert win.center.cget("text") == "Mini Tracker"

        # Once no longer alerting, win.center can start a drag normally
        win._drag_start(types.SimpleNamespace(widget=win.center, x_root=orig_x + 50, y_root=orig_y + 10))
        assert win._drag is not None
        win._drag_end(None)
    finally:
        win.destroy()


def test_flashing_row_highlights_with_flash_bg(tk_app):
    root, ctrl, view, layer, st = tk_app

    # 1. Direct MiniTrackerWindow rendering of "flashing": True vs False
    rows = [
        {"symbol": "AAPL", "price": 207.50, "change_pct": 3.75, "category": "SELL", "alert": "ALERT", "flashing": True},
        {"symbol": "TTWO", "price": 201.90, "change_pct": -2.0, "category": "BUY", "alert": None, "flashing": False},
    ]
    _, win = _mk_win(root, rows)
    try:
        chips = win.rows_frame.winfo_children()
        assert len(chips) == 2
        assert chips[0].cget("bg").upper() == FLASH_BG.upper()
        assert chips[1].cget("bg").upper() == BG2.upper()
    finally:
        win.destroy()

    # 2. Live WidgetView.flash(...) propagation to _mini_rows() and open MiniTrackerWindow
    view.toggle_mini()
    assert view.mini is not None

    rows_before = {r["symbol"]: r for r in view._mini_rows()}
    assert rows_before["AAPL"]["flashing"] is False
    assert rows_before["TTWO"]["flashing"] is False

    view.flash("AAPL", ms=400)
    rows_during = {r["symbol"]: r for r in view._mini_rows()}
    assert rows_during["AAPL"]["flashing"] is True
    assert rows_during["AAPL"]["alert"] == "ALERT"
    assert rows_during["TTWO"]["flashing"] is False

    visible = view.mini._filtered_rows()
    idx_aapl = next(i for i, r in enumerate(visible) if r["symbol"] == "AAPL")
    idx_ttwo = next(i for i, r in enumerate(visible) if r["symbol"] == "TTWO")
    live_chips = view.mini.rows_frame.winfo_children()
    assert live_chips[idx_aapl].cget("bg").upper() == FLASH_BG.upper()
    assert live_chips[idx_ttwo].cget("bg").upper() == BG2.upper()

    # After flash timeout, WidgetView refreshes MiniTrackerWindow back to BG2
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        root.update()
        if view.mini.rows_frame.winfo_children()[idx_aapl].cget("bg").upper() == BG2.upper():
            break
        time.sleep(0.02)

    rows_after = {r["symbol"]: r for r in view._mini_rows()}
    assert rows_after["AAPL"]["flashing"] is False
    assert view.mini.rows_frame.winfo_children()[idx_aapl].cget("bg").upper() == BG2.upper()


def test_privacy_hide_withdraws_mini_tracker_and_restores_preserving_enabled_state(tk_app):
    root, ctrl, view, layer, st = tk_app
    layer_hidden = []
    layer.set_hidden = lambda on: layer_hidden.append(on)

    # Case 1: Mini Tracker closed before hide -> stays closed on unhide
    assert not view.mini_open
    view.toggle_hidden()
    assert view.hidden is True and not view.mini_open
    view.toggle_hidden()
    assert view.hidden is False and not view.mini_open

    # Case 2: Mini Tracker open (plus settings dialog open) -> hides both, keeps enabled=True, restores updated prices
    view.toggle_mini()
    assert view.mini_open is True
    assert ctrl.get_mini_tracker_state().get("enabled") is True
    view.mini.open_settings()
    settings_dlg = view.mini._settings
    assert settings_dlg is not None and settings_dlg.winfo_exists()

    view.toggle_hidden()
    root.update()
    assert view.hidden is True
    assert view.mini_open is True
    assert view.mini._privacy_hidden is True
    assert view.mini.state() == "withdrawn"
    assert not settings_dlg.winfo_exists()
    assert ctrl.get_mini_tracker_state().get("enabled") is True

    # Background price update while hidden does not show Mini Tracker, yet updates data upon restore
    ctrl.on_ticks([tick("218.40", "AAPL", T0, 200)], T0)
    view.refresh()
    assert view.mini._privacy_hidden is True
    assert view.mini.state() == "withdrawn"

    view.toggle_hidden()
    root.update()
    assert view.hidden is False
    assert view.mini_open is True
    assert view.mini._privacy_hidden is False
    assert view.mini.state() != "withdrawn"
    assert layer_hidden == [True, False, True, False]
    price_texts = [w["price_lbl"].cget("text").strip() for w in view.mini._row_widgets]
    assert "218.40" in price_texts
