"""Tests for Item 6.1: Unacknowledged Alerts Inbox + Async Offline (Laptop-Closed) Catch-Up."""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dc

import pytest

from app_core import AppController
from config import Config
from market_clock import MarketCalendar, Session
from models import Alert, OHLCBar, StockConfig, TradeInfo, Watchlist
from notifier import Notifier, NullBackends
from providers.finnhub import FinnhubProvider
from providers.mock import MockProvider
from scheduler import Scheduler
from store import Store
from conftest import tick


T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)   # 11:00 ET (RH)
T_PRE = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)  # 08:00 ET (PH)


class FakeTimers:
    def __init__(self):
        self.items = []
        self._seq = 0

    def schedule(self, ms, fn):
        self._seq += 1
        self.items.append((self._seq, ms, fn, False))
        return self._seq

    def cancel(self, handle):
        for i, item in enumerate(self.items):
            if item[0] == handle:
                self.items[i] = (item[0], item[1], item[2], True)


class BarMockProvider(MockProvider):
    def __init__(self, quotes=None, bars=None, fail=False):
        super().__init__(quotes or {"NVDA": [149]})
        self._bars = bars or {}
        self.fail = fail
        self.fetch_bar_calls = []

    def fetch_intraday_bars(self, symbols: list, since: datetime, until: datetime) -> dict:
        self.fetch_bar_calls.append((list(symbols), since, until, threading.current_thread().name))
        if self.fail:
            raise RuntimeError("simulated network blip")
        return {s: list(self._bars.get(s, [])) for s in symbols if s in self._bars}


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.text = json.dumps(payload)


class _FakeSession:
    def __init__(self, by_symbol: dict):
        self.by_symbol = by_symbol
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        sym = url.rsplit("/", 1)[-1]
        resp = self.by_symbol.get(sym)
        if resp is None:
            return _FakeResponse(404, {})
        if isinstance(resp, Exception):
            raise resp
        return resp

    def close(self):
        pass


def _make_stock(
    symbol="NVDA",
    alerts=None,
    trade=None,
):
    return StockConfig(
        symbol=symbol,
        nickname="Nvidia",
        category="BUY",
        alerts=list(alerts or []),
        trade=trade,
    )


def _bar(ts: datetime, o, h, l, c, prev_close=None, symbol="NVDA") -> OHLCBar:
    return OHLCBar(
        symbol=symbol,
        time=ts,
        open=Dc(str(o)),
        high=Dc(str(h)),
        low=Dc(str(l)),
        close=Dc(str(c)),
        prev_close=Dc(str(prev_close)) if prev_close is not None else None,
        source="yahoo_1m",
    )


# --------------------------------------------------------------------------- Yahoo 1m provider
def test_yahoo_fetch_intraday_bars_parses_ohlc_filters_window_and_never_sends_finnhub_key():
    prov = FinnhubProvider(api_key="FINNHUB_SECRET_KEY_XYZ", now_fn=lambda: T0)
    t_before = int((T0 - timedelta(minutes=10)).timestamp())
    t_b1 = int((T0 - timedelta(minutes=3)).timestamp())
    t_b2 = int((T0 - timedelta(minutes=1)).timestamp())
    t_after = int((T0 + timedelta(minutes=1)).timestamp())

    fake_yahoo = _FakeSession({
        "NVDA": _FakeResponse(200, {
            "chart": {
                "result": [{
                    "meta": {"chartPreviousClose": 145.0},
                    "timestamp": [t_before, t_b1, t_b2, t_after],
                    "indicators": {
                        "quote": [{
                            "open": [146.0, 148.0, 150.0, 155.0],
                            "high": [147.0, 151.5, 152.0, 156.0],
                            "low": [145.5, 147.5, 149.0, 154.0],
                            "close": [146.5, 149.0, 150.5, 155.5],
                        }]
                    },
                }]
            }
        }),
        "FAIL": RuntimeError("timeout"),
    })
    prov._yahoo_s = fake_yahoo
    prov._uses_real_http = True

    bars = prov.fetch_intraday_bars(["NVDA", "FAIL"], T0 - timedelta(minutes=5), T0)
    assert list(bars.keys()) == ["NVDA"]
    assert len(bars["NVDA"]) == 2
    assert bars["NVDA"][0].high == Dc("151.5")
    assert bars["NVDA"][0].low == Dc("147.5")
    assert bars["NVDA"][0].prev_close == Dc("145")
    assert bars["NVDA"][1].close == Dc("150.5")
    for _url, _params, hdrs in fake_yahoo.calls:
        assert "X-Finnhub-Token" not in hdrs

    # All-fail transport raises NetworkFailure so controller preserves catch-up window
    from providers.base import NetworkFailure
    with pytest.raises(NetworkFailure):
        prov.fetch_intraday_bars(["FAIL"], T0 - timedelta(minutes=5), T0)


# --------------------------------------------------------------------------- Engine offline bars
def test_engine_offline_bars_catches_intrabar_high_and_deduplicates_with_peak_price():
    cfg = Config()
    cal = MarketCalendar()
    from engine import AlertEngine

    a_cross = Alert(id="a1", operator="crossing_up", condition_mode="price", value=Dc("150"),
                    trigger="once", priority="HIGH", session_scope="all")
    a_gt = Alert(id="a2", operator="greater_than", condition_mode="price", value=Dc("150"),
                 trigger="every_time", cooldown_min=1, priority="MEDIUM", session_scope="all")
    stock = _make_stock(alerts=[a_cross, a_gt])
    eng = AlertEngine(cfg)
    eng.sym_state("NVDA")["last_price"] = "147.00"

    bars = [
        _bar(T0 - timedelta(minutes=4), 148, 151.20, 147.80, 149.10),  # intra-min spike above 150
        _bar(T0 - timedelta(minutes=2), 149, 154.60, 148.90, 153.00),  # higher offline peak
        _bar(T0 - timedelta(minutes=1), 153, 153.50, 148.50, 148.80),  # drifted back below 150
    ]
    evs = eng.evaluate_offline_bars(stock, bars, cal, T0)
    assert len(evs) == 2
    by_id = {e.alert_id: e for e in evs}
    assert by_id["a1"].offline is True
    assert by_id["a1"].peak_price == Dc("154.60")
    assert by_id["a2"].peak_price == Dc("154.60")
    assert eng.alert_state("NVDA", "a1")["state"] == "DONE"
    assert by_id["a1"].time == T0 - timedelta(minutes=4)


def test_engine_offline_bars_respects_session_scope_and_downside_peak():
    cfg = Config()
    cal = MarketCalendar()
    from engine import AlertEngine

    a_reg = Alert(id="r1", operator="less_than", condition_mode="price", value=Dc("145"),
                  trigger="once", priority="HIGH", session_scope="regular")
    a_all = Alert(id="r2", operator="less_than", condition_mode="price", value=Dc("145"),
                  trigger="once", priority="MEDIUM", session_scope="all")
    stock = _make_stock(alerts=[a_reg, a_all])
    eng = AlertEngine(cfg)

    bars = [
        _bar(T_PRE - timedelta(minutes=2), 146, 146.5, 142.10, 144.0),  # PH dip below 145
        _bar(T_PRE - timedelta(minutes=1), 144, 144.2, 141.30, 143.0),
    ]
    evs = eng.evaluate_offline_bars(stock, bars, cal, T_PRE)
    assert [e.alert_id for e in evs] == ["r2"]
    assert evs[0].peak_price == Dc("141.30")
    assert eng.alert_state("NVDA", "r1")["state"] == "ARMED"
    assert eng.alert_state("NVDA", "r2")["state"] == "DONE"


def test_engine_offline_peak_respects_session_scope():
    cfg = Config()
    cal = MarketCalendar()
    from engine import AlertEngine

    # Regular-only alert triggers during RH at 151, must NOT update peak from later AH spike (158)
    a_reg = Alert(id="reg_pk", operator="crossing_up", condition_mode="price", value=Dc("150"),
                  trigger="once", priority="HIGH", session_scope="regular")
    stock = _make_stock(alerts=[a_reg])
    eng = AlertEngine(cfg)
    eng.sym_state("NVDA")["last_price"] = "148.00"
    t_ah = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)   # 17:00 ET (AH)
    bars = [
        _bar(T0, 149, 152.00, 148.50, 151.00),                   # RH: peak 152.00
        _bar(t_ah, 151, 158.00, 150.50, 157.00),                 # AH: outside regular scope
    ]
    evs = eng.evaluate_offline_bars(stock, bars, cal, t_ah + timedelta(minutes=1))
    assert len(evs) == 1
    assert evs[0].peak_price == Dc("152.00")


# --------------------------------------------------------------------------- Controller + Notifier
def test_live_ack_clears_inbox_while_timeout_keeps_afk_alert(tmp_path):
    st = Store(tmp_path)
    cfg = Config()
    a1 = Alert(id="a1", operator="greater_than", condition_mode="price", value=Dc("150"),
               trigger="once", priority="MEDIUM")
    st.save_watchlist(Watchlist(symbols=[_make_stock(alerts=[a1])]), cfg)

    clock = [T0]
    timers = FakeTimers()
    notifier = Notifier(cfg, NullBackends(), timers, lambda: clock[0])
    ctrl = AppController(cfg, st, MockProvider({"NVDA": [151]}), notifier, now_fn=lambda: clock[0])

    # Fire live alert -> enters inbox & active notification
    ctrl.on_ticks([tick("151.00", "NVDA", clock[0], 148)], clock[0])
    assert ctrl.unacked_summary() == {"total": 1, "offline": 0, "live": 1}

    # Simulate Alt+A / Ack -> clears live card from Inbox
    assert notifier.acknowledge_latest() is True
    assert ctrl.unacked_summary() == {"total": 0, "offline": 0, "live": 0}

    # Add a second alert and let popup time out without Ack -> remains in Inbox (AFK case)
    clock[0] += timedelta(minutes=1)
    a2 = Alert(id="a2", operator="less_than", condition_mode="price", value=Dc("149"),
               trigger="once", priority="MEDIUM")
    ctrl.save_alert("NVDA", a2, choice="wait_cross")
    ctrl.on_ticks([tick("148.00", "NVDA", clock[0], 150)], clock[0])
    assert ctrl.unacked_summary() == {"total": 1, "offline": 0, "live": 1}
    notifier._end_popup()
    assert ctrl.unacked_summary() == {"total": 1, "offline": 0, "live": 1}


def test_async_offline_catchup_via_scheduler_option_a_and_2a_rearm_and_privacy(tmp_path):
    st = Store(tmp_path)
    cfg = Config()
    a1 = Alert(id="off1", operator="crossing_up", condition_mode="price", value=Dc("150"),
               trigger="once", priority="HIGH", session_scope="all")
    stock = _make_stock(
        alerts=[a1],
        trade=TradeInfo(shares=Dc("25"), buy_price=Dc("123.45"), fees=Dc("4.99")),
    )
    st.save_watchlist(Watchlist(symbols=[stock]), cfg)
    # Persist prior state with last_active_at 10 minutes before T0
    last_seen = (T0 - timedelta(minutes=10)).isoformat()
    st.save_state({"version": 1, "symbols": {"NVDA": {"last_price": "147.00", "alerts": {}}},
                   "last_active_at": last_seen, "unacked_alerts": []})

    bars = [
        _bar(T0 - timedelta(minutes=6), 148, 151.00, 147.50, 149.20),
        _bar(T0 - timedelta(minutes=3), 149, 155.25, 148.80, 149.50),
    ]
    prov = BarMockProvider(bars={"NVDA": bars})
    timers = FakeTimers()
    notifier = Notifier(cfg, NullBackends(), timers, lambda: T0)
    q_in = queue.Queue()
    sched = Scheduler(prov, lambda: ["NVDA"], q_in, cfg, now_fn=lambda: T0)
    ctrl = AppController(cfg, st, prov, notifier, run_call=sched.run_call, now_fn=lambda: T0)

    started = ctrl.start_offline_catchup(T0)
    assert started is True

    # Wait briefly for worker thread to push ("call", cb, res, exc) onto queue, then drain on main thread
    deadline = threading.Event()
    for _ in range(40):
        if not q_in.empty():
            break
        deadline.wait(0.025)
    assert not q_in.empty()
    ctrl.drain(q_in)

    # Worker executed off MainThread
    assert prov.fetch_bar_calls
    assert prov.fetch_bar_calls[0][3] != threading.current_thread().name

    # Option A: ONE summary notification fired; dismissing it MUST NOT ack offline cards in 6.1
    assert notifier._latest is not None
    assert "offline" in notifier._latest["text"].lower()
    assert notifier.acknowledge_latest() is True
    summary = ctrl.unacked_summary()
    assert summary == {"total": 1, "offline": 1, "live": 0}

    recs = ctrl.unacked_alerts("offline")
    assert len(recs) == 1
    assert recs[0]["peak_price"] == "155.25"
    assert recs[0]["can_rearm"] is True
    assert recs[0]["alert_status"] == "Triggered"

    # Privacy: no trade cost basis / shares leaked into state unacked_alerts or alerts_log.jsonl
    raw_state = st.state_path.read_text(encoding="utf-8")
    raw_log = st.alert_log_path.read_text(encoding="utf-8")
    for text in (raw_state, raw_log):
        assert "123.45" not in text
        assert "4.99" not in text
        assert "buy_price" not in text
        assert "shares" not in text

    # Persistence across controller reload
    ctrl2 = AppController(cfg, Store(tmp_path), prov, Notifier(cfg, NullBackends(), FakeTimers(), lambda: T0), now_fn=lambda: T0)
    assert ctrl2.unacked_summary() == {"total": 1, "offline": 1, "live": 0}

    # Option 2A: 1-click Re-arm resets DONE -> ARMED/WAIT_CROSS cleanly
    assert ctrl2.rearm_alert("NVDA", "off1", now=T0) is True
    assert ctrl2.engine.alert_state("NVDA", "off1")["state"] in ("ARMED", "WAIT_CROSS")
    assert ctrl2.unacked_alerts("offline")[0]["can_rearm"] is False

    # Per-card Ack clears it cleanly
    assert ctrl2.acknowledge_alert(recs[0]["event_id"]) is True
    assert ctrl2.unacked_summary() == {"total": 0, "offline": 0, "live": 0}


def test_inbox_ack_silences_active_notifier_alarm_and_pill(tmp_path):
    st = Store(tmp_path)
    cfg = Config()
    a1 = Alert(id="h1", operator="greater_than", condition_mode="price", value=Dc("150"),
               trigger="once", priority="HIGH")
    st.save_watchlist(Watchlist(symbols=[_make_stock(alerts=[a1])]), cfg)

    timers = FakeTimers()
    notifier = Notifier(cfg, NullBackends(), timers, lambda: T0)
    ctrl = AppController(cfg, st, MockProvider({"NVDA": [151]}), notifier, now_fn=lambda: T0)
    ctrl.on_ticks([tick("151.00", "NVDA", T0, 148)], T0)
    assert notifier._latest is not None and not notifier._latest["acked"]
    rec = ctrl.unacked_alerts("live")[0]

    assert ctrl.acknowledge_alert(rec["event_id"]) is True
    assert notifier._latest["acked"] is True
    assert ctrl.unacked_summary() == {"total": 0, "offline": 0, "live": 0}


def test_wake_catchup_network_failure_preserves_pending_and_retries_on_next_tick(tmp_path):
    st = Store(tmp_path)
    cfg = Config()
    a1 = Alert(id="w1", operator="greater_than", condition_mode="price", value=Dc("150"),
               trigger="once", priority="HIGH", session_scope="all")
    st.save_watchlist(Watchlist(symbols=[_make_stock(alerts=[a1])]), cfg)
    last_seen = (T0 - timedelta(minutes=15)).isoformat()
    st.save_state({"version": 1, "symbols": {"NVDA": {"last_price": "148.00", "alerts": {}}},
                   "last_active_at": last_seen, "unacked_alerts": []})

    prov = BarMockProvider(bars={"NVDA": [_bar(T0 - timedelta(minutes=3), 149, 154.0, 148.5, 149.2)]}, fail=True)
    timers = FakeTimers()
    notifier = Notifier(cfg, NullBackends(), timers, lambda: T0)
    polls = []
    q_in = queue.Queue()
    sched = Scheduler(prov, lambda: ["NVDA"], q_in, cfg, now_fn=lambda: T0)
    ctrl = AppController(cfg, st, prov, notifier, run_call=sched.run_call,
                         poll_now=lambda: polls.append(1), now_fn=lambda: T0)

    ctrl.on_wake(T0)
    assert polls == [1]
    assert ctrl._catchup_in_flight is True

    ev = threading.Event()
    for _ in range(40):
        if not q_in.empty():
            break
        ev.wait(0.025)
    assert not q_in.empty()
    ctrl.drain(q_in)
    assert ctrl._catchup_in_flight is False
    # Failed wake fetch preserves _pending_catchup_since and does not overwrite last_active_at
    assert ctrl._pending_catchup_since is not None
    assert ctrl.unacked_summary() == {"total": 0, "offline": 0, "live": 0}

    # Next successful live tick automatically retries offline catch-up once network recovers
    prov.fail = False
    t_next = T0 + timedelta(seconds=15)
    ctrl.on_ticks([tick("149.20", "NVDA", t_next, 148)], t_next)
    for _ in range(40):
        if not q_in.empty():
            break
        ev.wait(0.025)
    assert not q_in.empty()
    ctrl.drain(q_in)
    assert ctrl._pending_catchup_since is None
    assert ctrl.unacked_summary() == {"total": 1, "offline": 1, "live": 0}


def test_main_relaunch_self_builds_detached_popen_cmd(monkeypatch):
    import main as main_mod

    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return "PROC"

    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)
    res = main_mod.relaunch_self(argv=["--mock"], executable="C:/py/pythonw.exe")
    assert res == "PROC"
    assert captured["cmd"][0] == "C:/py/pythonw.exe"
    assert captured["cmd"][1].endswith("main.py")
    assert captured["cmd"][2:] == ["--mock"]
    assert captured["kw"]["close_fds"] is True


# --------------------------------------------------------------------------- UI Integration
@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt", reason="needs a display")
def test_ui_inbox_dialog_widget_badge_and_mini_tracker(tmp_path):
    from test_ui_smoke import FakeLayer
    from ui.common import TkTimers
    from ui.inbox import AlertInboxDialog
    from ui.widget import WidgetView

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
    try:
        st = Store(tmp_path)
        cfg = Config()
        a_off = Alert(id="off1", operator="crossing_up", condition_mode="price", value=Dc("150"),
                      trigger="once", priority="HIGH", session_scope="all")
        a_live = Alert(id="live1", operator="less_than", condition_mode="price", value=Dc("147"),
                       trigger="once", priority="MEDIUM", session_scope="all")
        st.save_watchlist(Watchlist(symbols=[_make_stock(alerts=[a_off, a_live])]), cfg)
        last_seen = (T0 - timedelta(minutes=10)).isoformat()
        st.save_state({"version": 1, "symbols": {"NVDA": {"last_price": "147.00", "alerts": {}}},
                       "last_active_at": last_seen, "unacked_alerts": []})

        bars = [_bar(T0 - timedelta(minutes=4), 148, 153.40, 147.50, 149.10)]
        prov = BarMockProvider(quotes={"NVDA": [146.50]}, bars={"NVDA": bars})
        notifier = Notifier(cfg, NullBackends(), TkTimers(root), lambda: T0)
        ctrl = AppController(cfg, st, prov, notifier, now_fn=lambda: T0)
        ctrl.start_offline_catchup(T0)
        ctrl.on_ticks([tick("146.50", "NVDA", T0, 147)], T0)

        layer = FakeLayer()
        restarts = []
        view = WidgetView(root, ctrl, layer, {}, now_fn=lambda: T0, request_restart=lambda: restarts.append(1))
        view.render()

        # Header badge drawn to the LEFT of Mini without breaking card_polys or gear > mini
        c = view.canvas
        badge_bbox = c.bbox("status_inbox_badge")
        mini_bbox = c.bbox("status_mini_tracker")
        gear_bbox = c.bbox("status_settings_gear")
        assert badge_bbox is not None
        assert badge_bbox[2] <= mini_bbox[0]
        assert gear_bbox[0] > mini_bbox[2]
        assert any("🔔 2" in c.itemcget(i, "text") for i in c.find_withtag("status_inbox_badge")
                   if c.type(i) == "text")

        # Edit Mode Alerts (2) appears before Quit / Exit edit mode; Restart (↻) sits in bottom-right footer
        view.enter_edit()
        assert "Alerts (2)" in view.panel.btn_alerts.cget("text")
        top_frame = view.panel.btn_alerts.master
        assert top_frame.winfo_children()[-1].cget("text") == "Exit edit mode"
        assert view.panel.btn_restart.master is not top_frame
        assert view.panel.btn_restart.cget("text") == "↻"
        view.panel.btn_restart.invoke()
        assert restarts == [1]
        view.exit_edit()

        # Mini Tracker left inbox button shows 🔔 1 after live1 is acked while off1 persists
        notifier.acknowledge_latest()
        root.update()
        view.toggle_mini()
        root.update_idletasks()
        assert view.mini is not None
        assert view.mini.inbox_btn.cget("text") == "🔔 1"

        # Open simplified AlertInboxDialog (topmost, no filter bar) + 1-click Re-arm + scroll lock
        view.open_alert_inbox()
        root.update()
        dlg = view.inbox_dlg
        assert isinstance(dlg, AlertInboxDialog) and dlg.winfo_exists()
        assert not hasattr(dlg, "_filter_btns")
        assert len(dlg.cards) == 1
        assert dlg.yview if hasattr(dlg, "yview") else dlg.canvas.yview() == (0.0, 1.0)
        assert not dlg.sb.winfo_manager()

        # Wheel scroll while short/empty does not move banner/content
        class _WheelEv:
            delta = -120
        dlg._on_wheel(_WheelEv())
        assert dlg.canvas.yview() == (0.0, 1.0)

        # Clicking Re-arm resets DONE -> ARMED/WAIT_CROSS cleanly
        assert "rearm" in dlg.cards[0]["buttons"]
        dlg.cards[0]["buttons"]["rearm"].invoke()
        root.update_idletasks()
        assert ctrl.engine.alert_state("NVDA", "off1")["state"] in ("ARMED", "WAIT_CROSS")

        # Clicking Edit closes topmost Inbox and opens Edit Mode on stock
        dlg.cards[0]["buttons"]["edit"].invoke()
        root.update_idletasks()
        assert view.inbox_dlg is None
        assert view.editing and view.panel.open_symbol == "NVDA"
        view.exit_edit()

        # Reopen Inbox, Ack All clears cards and hides scrollbar cleanly
        view.open_alert_inbox()
        root.update()
        dlg = view.inbox_dlg
        dlg._ack_all()
        root.update_idletasks()
        assert len(dlg.cards) == 0
        assert not dlg.sb.winfo_manager()
        assert dlg.canvas.yview() == (0.0, 1.0)
        assert str(dlg.btn_ack_all.cget("state")) == "disabled"

        # Privacy hide closes Inbox cleanly
        view.set_hidden(True)
        assert view.inbox_dlg is None
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
