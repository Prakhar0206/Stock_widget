"""Tests for the floating Mini Tracker window, selective click-through, state persistence,
and WidgetView integration."""
from __future__ import annotations

import os
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
from ui.mini_tracker import (
    MiniTrackerWindow,
    _clamp_to_work_area,
    _fmt_row,
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
