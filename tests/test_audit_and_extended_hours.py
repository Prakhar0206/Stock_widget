"""Regression tests covering audit fixes C1, H1-H8, M1-M11, L1-L8 and Extended Hours (PH/RH/AH)."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk

import pytest

from app_core import AppController
from config import Config, parse_config
from data_checks import IGNORED, PriceFilter
from engine import AlertEngine, Notification
from market_clock import ET, MarketCalendar, Session
import main as main_mod
from messages import format_message
from models import (
    ARMED,
    Alert,
    AlertEvent,
    Position,
    PriceTick,
    StockConfig,
    ValidationError,
    Watchlist,
)
from notifier import Notifier, NullBackends
from positions import CENT, FeeRates, breakeven, net_profit
from providers.finnhub import FinnhubProvider
from providers.mock import MockProvider
from providers.base import NetworkFailure, ProviderError, RateLimited
from scheduler import Scheduler
from store import Store, atomic_write_text
from ui.common import TkTimers
from ui.editor import AddStockDialog, AlertDialog, ChoiceDialog, StockEditor
from ui.mini_tracker import MiniTrackerSettingsDialog, MiniTrackerWindow
from ui.widget import IDLE_EDIT_S, WidgetView
from watchlist import parse_alert, parse_stock, parse_watchlist
from zones import build_zone_defs, zone_price_table


T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # Wednesday 11:00 ET (REGULAR)


def _tick(sym: str, price: str, *, prev: str = "198", t: datetime = T0,
          pre: str | None = None, reg: str | None = None, ah: str | None = None,
          session_kind: str | None = None) -> PriceTick:
    p = Decimal(price)
    pc = Decimal(prev) if prev else None
    chg = (p - pc) if (pc is not None and p > 0) else None
    pct = (chg / pc * 100) if (chg is not None and pc) else None
    return PriceTick(
        sym, p, pc, chg, pct, None, None, t - timedelta(seconds=4), t, "mock",
        pre_price=Decimal(pre) if pre else None,
        reg_price=Decimal(reg) if reg else None,
        ah_price=Decimal(ah) if ah else None,
        session_kind=session_kind,
    )


class _MemTimers:
    def __init__(self):
        self.seq = 0
        self.jobs: dict[int, tuple[int, object]] = {}
        self.cancelled: list[int] = []

    def schedule(self, ms: int, fn):
        self.seq += 1
        self.jobs[self.seq] = (ms, fn)
        return self.seq

    def cancel(self, handle):
        self.cancelled.append(handle)
        self.jobs.pop(handle, None)


class _SpyBackends(NullBackends):
    def __init__(self, on_flash=None):
        self.on_flash = on_flash
        self.flashes: list[tuple[str, int]] = []
        self.popups: list[tuple] = []
        self.popup_closed = 0
        self.toasts: list[tuple] = []
        self.beeps: list[str] = []

    def flash(self, symbol: str, ms: int):
        self.flashes.append((symbol, ms))
        if self.on_flash:
            self.on_flash(symbol)

    def popup_show(self, text: str, priority: str, seconds: int, on_ack):
        self.popups.append((text, priority, seconds, on_ack))

    def popup_close(self):
        self.popup_closed += 1

    def toast(self, text: str, stay: bool, silent: bool):
        self.toasts.append((text, stay, silent))

    def beep(self, kind: str):
        self.beeps.append(kind)


class _FakeLayer:
    def __init__(self):
        self.interactive = False
        self.fitted: list[tuple[int, int]] = []

    def fit(self, w: int, h: int):
        self.fitted.append((w, h))

    def set_interactive(self, on: bool):
        self.interactive = on

    def work_area_size(self):
        return (1920, 1040)


@pytest.fixture(scope="module")
def _tk_mod_root():
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        for w in list(root.winfo_children()):
            try:
                w.destroy()
            except tk.TclError:
                pass
        try:
            root.destroy()
        except tk.TclError:
            pass


@pytest.fixture
def tk_root(_tk_mod_root):
    try:
        yield _tk_mod_root
    finally:
        for w in list(_tk_mod_root.winfo_children()):
            try:
                w.destroy()
            except tk.TclError:
                pass
        try:
            _tk_mod_root.update_idletasks()
        except tk.TclError:
            pass


def _make_stock(sym: str, cat: str = "SELL") -> StockConfig:
    pos = Position(Decimal("10"), Decimal("200")) if cat in ("HOLD", "SELL") else None
    from models import PlannedEntry
    ent = PlannedEntry(Decimal("190")) if cat == "BUY" else None
    return StockConfig(sym, cat, sym, position=pos, planned_entry=ent)


def _setup_controller(tmp_path: Path, stocks: list[StockConfig], *, now=T0, session_override=Session.REGULAR):
    st = Store(tmp_path)
    cfg = Config()
    st.save_watchlist(Watchlist(symbols=stocks), cfg)
    backends = _SpyBackends()
    notifier = Notifier(cfg, backends, _MemTimers(), lambda: now)
    ctrl = AppController(cfg, st, MockProvider({}), notifier, now_fn=lambda: now, session_override=session_override)
    return ctrl, st, cfg, backends


# ---- C1: Budget scaling with multi-key and --mock --------------------------------------------
def test_c1_30_symbols_15s_with_4_keys_config_and_mock(tmp_path):
    cfg4 = parse_config({
        "poll_seconds": 15,
        "max_symbols": 30,
        "api_keys": ["key_alpha_1", "key_alpha_2", "key_alpha_3", "key_alpha_4"],
    })
    assert cfg4.num_keys == 4
    wl_data = {
        "version": 1,
        "symbols": [{"symbol": f"S{i:02d}", "category": "WATCH"} for i in range(30)],
    }
    wl = parse_watchlist(wl_data, cfg4)
    assert len(wl.symbols) == 30

    cfg1 = Config(poll_seconds=15, max_symbols=30, num_keys=1)
    with pytest.raises(ValidationError):
        parse_watchlist(wl_data, cfg1)

    rt = main_mod.build_runtime(main_mod.parse_args(["--mock", "--data-dir", str(tmp_path)]), {})
    assert rt.demo is True
    assert rt.cfg.poll_seconds == 15
    assert rt.cfg.num_keys == 4
    assert len(parse_watchlist(wl_data, rt.cfg).symbols) == 30


# ---- H1: Scheduler never spins 0.0 outside active hours -------------------------------------
def test_h1_scheduler_never_returns_zero_on_errors_outside_active_hours():
    import queue as q_mod

    class _ErrProvider:
        def __init__(self, exc, key_count=1):
            self.exc = exc
            self.key_count = key_count
            self.server_down = isinstance(exc, RateLimited)

        def fetch_quotes(self, symbols):
            raise self.exc

        def pop_invalid_key_events(self):
            return []

    cal = MarketCalendar()
    # Monday 03:59:59.950 ET -> 0.05s before 04:00 ET pre-market open
    near_pre = datetime(2026, 10, 26, 3, 59, 59, 950000, tzinfo=ET)
    # Monday 09:29:59.950 ET -> 0.05s before 09:30 ET regular open (used with extended_hours=False)
    near_reg = datetime(2026, 10, 26, 9, 29, 59, 950000, tzinfo=ET)

    for exc in (RateLimited("HTTP 503"), ProviderError("API key rejected (HTTP 401)")):
        for nk in (1, 4):
            q = q_mod.Queue()
            sc = Scheduler(_ErrProvider(exc, nk), lambda: ["AAPL"], q, Config(),
                           calendar=cal, now_fn=lambda: near_pre, jitter_fn=lambda: 0.0, epoch_fn=lambda: 0.0)
            delay_ext = sc.poll_once()
            assert delay_ext >= 1.0

            q2 = q_mod.Queue()
            sc2 = Scheduler(_ErrProvider(exc, nk), lambda: ["AAPL"], q2, Config(extended_hours=False),
                            calendar=cal, now_fn=lambda: near_reg, jitter_fn=lambda: 0.0, epoch_fn=lambda: 0.0)
            delay_reg = sc2.poll_once()
            assert delay_reg >= 1.0


# ---- H2: Inactive session updates display without mutating zone state -----------------------
def test_h2_inactive_sessions_preserve_zones_until_active_open():
    stock = _make_stock("AAPL", "SELL")
    engine = AlertEngine(Config())  # extended_hours=True
    t_reg1 = T0
    # First reading in REGULAR puts AAPL into 'sell' zone (~+5% vs 200.81 BE -> ~210)
    assert engine.process_tick(stock, _tick("AAPL", "210", t=t_reg1), t_reg1, Session.REGULAR) == []
    assert engine.sym_state("AAPL")["zone"] == "sell"

    # CLOSED session pushes 194 (which would be 'loss' zone): updates last_price, NOT zone
    t_closed = t_reg1 + timedelta(minutes=1)
    assert engine.process_tick(stock, _tick("AAPL", "194", t=t_closed), t_closed, Session.CLOSED) == []
    assert engine.sym_state("AAPL")["last_price"] == "194"
    assert engine.sym_state("AAPL")["zone"] == "sell"

    # Next ACTIVE reading sees sell -> loss transition and fires zone:loss
    t_reg2 = t_closed + timedelta(minutes=1)
    evs = engine.process_tick(stock, _tick("AAPL", "194", t=t_reg2), t_reg2, Session.REGULAR)
    assert any(e.alert_id == "zone:loss" for e in evs)

    # Same with extended_hours=False during PRE/AFTER
    eng_reg = AlertEngine(Config(extended_hours=False))
    eng_reg.process_tick(stock, _tick("AAPL", "210", t=t_reg1), t_reg1, Session.REGULAR)
    assert eng_reg.sym_state("AAPL")["zone"] == "sell"
    assert eng_reg.process_tick(stock, _tick("AAPL", "194", t=t_closed), t_closed, Session.PRE) == []
    assert eng_reg.process_tick(stock, _tick("AAPL", "194", t=t_closed), t_closed, Session.AFTER) == []
    assert eng_reg.sym_state("AAPL")["zone"] == "sell"
    assert eng_reg.sym_state("AAPL")["ah_price"] == "194"
    evs2 = eng_reg.process_tick(stock, _tick("AAPL", "194", t=t_reg2), t_reg2, Session.REGULAR)
    assert any(e.alert_id == "zone:loss" for e in evs2)


# ---- H3: server_down resets on ticks ---------------------------------------------------------
def test_h3_on_ticks_resets_server_down(tmp_path):
    ctrl, _, _, _ = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL")])
    ctrl.on_error("server", "HTTP 502", T0)
    assert ctrl.server_down is True
    assert any(p.kind == "server_down" for p in ctrl.status_strip(T0).problems)

    t1 = T0 + timedelta(seconds=15)
    ctrl.on_ticks([_tick("AAPL", "205", t=t1)], t1)
    assert ctrl.server_down is False
    assert not any(p.kind == "server_down" for p in ctrl.status_strip(t1).problems)


# ---- H4 & M1: Notifier _latest before flash, listeners, HIGH preserved across MEDIUM --------
def test_h4_m1_notifier_latest_before_flash_listeners_and_high_preservation():
    seen_inside_flash = []
    notifier_ref = []

    def _on_flash(sym):
        n = notifier_ref[0]
        seen_inside_flash.append((sym, dict(n._latest or {})))

    timers = _MemTimers()
    backends = _SpyBackends(on_flash=_on_flash)
    notifier = Notifier(Config(pop_seconds=10, escalation_seconds=120), backends, timers, lambda: T0)
    notifier_ref.append(notifier)

    listener_calls = []
    notifier.add_listener(lambda: listener_calls.append("notified"))

    ev_high = AlertEvent("AAPL", "a1", "level", "SELL", "HIGH", Decimal("210"), T0, "mock", condition="at 210")
    notifier.notify(Notification([ev_high]), T0)
    assert seen_inside_flash and seen_inside_flash[0][1]["count"] == 1
    assert len(listener_calls) == 1
    assert notifier._latest["priority"] == "HIGH"
    esc_handle = notifier._esc_h
    assert esc_handle is not None

    # Incoming MEDIUM alert while HIGH is unacked preserves HIGH priority and accumulates count
    ev_med = AlertEvent("MSFT", "a2", "level", "WATCH", "MEDIUM", Decimal("400"), T0, "mock", condition="at 400")
    notifier.notify(Notification([ev_med]), T0)
    assert notifier._latest["priority"] == "HIGH"
    assert notifier._latest["count"] == 2
    assert len(listener_calls) == 2

    # Ack marks all acked, cancels escalation timer, notifies listeners, and blocks late _escalate
    pending = notifier._pending_high
    assert notifier.acknowledge_latest() is True
    assert len(listener_calls) == 3
    assert esc_handle in timers.cancelled
    popups_before = len(backends.popups)
    notifier._escalate(pending)
    assert len(backends.popups) == popups_before


# ---- H5: Single dialog guards and cleanup on destroy ----------------------------------------
def test_h5_single_open_dialog_guards_and_cleanup(tmp_path, tk_root):
    ctrl, _, _, _ = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL")])
    ctrl.on_ticks([_tick("AAPL", "205")], T0)
    layer = _FakeLayer()
    view = WidgetView(tk_root, ctrl, layer, {})
    try:
        view.enter_edit()
        tk_root.update()
        panel = view.panel

        # EditPanel._add single dialog guard
        panel._add()
        dlg1 = panel._add_dlg
        assert dlg1 is not None and dlg1.winfo_exists()
        panel._add()
        assert panel._add_dlg is dlg1
        assert len([w for w in tk_root.winfo_children() if isinstance(w, AddStockDialog)]) == 1

        # StockEditor._open_alert single dialog guard + destroy cleanup
        panel.toggle("AAPL")
        tk_root.update()
        ed = panel.editor
        ed._open_alert(None)
        adlg1 = ed._alert_dlg
        assert adlg1 is not None and adlg1.winfo_exists()
        ed._open_alert(None)
        assert ed._alert_dlg is adlg1
        ed.destroy()
        assert not adlg1.winfo_exists()

        # Destroy EditPanel closes AddStockDialog
        panel.destroy()
        assert not dlg1.winfo_exists()

        # ChoiceDialog/AlertDialog destroyed _finish guard should be a clean no-op
        cd = ChoiceDialog(tk_root, "already true", lambda c: None)
        cd.destroy()
        cd._finish("fire_now")
    finally:
        if view.editing:
            view.exit_edit()


# ---- H6: breakeven precision + sell_rate guard ----------------------------------------------
def test_h6_breakeven_tiny_shares_and_sell_rate_guard():
    t0 = time.monotonic()
    p = breakeven(Decimal("0.00001"), Decimal("10"))
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5
    assert p is not None
    assert net_profit(Decimal("0.00001"), Decimal("10"), p) >= 0
    if p > CENT:
        assert net_profit(Decimal("0.00001"), Decimal("10"), p - CENT) < 0

    with pytest.raises(ValueError, match="sell_rate"):
        breakeven(Decimal("10"), Decimal("100"), FeeRates(sell_rate=Decimal("1")))


# ---- H7 & H8: update_stock session + quote_listener dispatch --------------------------------
def test_h7_h8_update_stock_respects_closed_session_and_notifies_listeners(tmp_path):
    ctrl, _, _, backends = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL")], session_override=Session.CLOSED)
    listener_calls = []
    ctrl.add_quote_listener(lambda: listener_calls.append("fired"))

    # Add stock -> notifies
    ctrl.add_stock(_make_stock("MSFT", "WATCH"))
    assert len(listener_calls) == 1

    # Give MSFT a live tick while CLOSED
    ctrl.on_ticks([_tick("MSFT", "400")], T0)
    assert len(listener_calls) == 2

    # Add immediately-true alert via update_stock during Session.CLOSED with choice='fire_now'
    msft = ctrl.stock("MSFT")
    msft.alerts.append(Alert("m1", "level_above", "WATCH", "HIGH", mode="once", price=Decimal("390")))
    ctrl.update_stock(msft, choices={"m1": "fire_now"})
    assert len(listener_calls) == 3
    assert backends.popups == []
    assert ctrl.engine.alert_state("MSFT", "m1")["state"] == ARMED

    # When REGULAR opens, it fires on the first regular tick
    ctrl.session_override = Session.REGULAR
    t1 = T0 + timedelta(seconds=15)
    ctrl.on_ticks([_tick("MSFT", "400", t=t1)], t1)
    assert len(backends.popups) == 1

    # Remove stock -> notifies
    ctrl.remove_stock("MSFT")
    assert len(listener_calls) == 5  # +1 on_ticks, +1 remove_stock


# ---- M2: FinnhubProvider offline reset, 3-error abort, Yahoo enrichment ----------------------
def test_m2_finnhub_offline_reset_and_3_net_failures():
    import requests

    class _FakeResp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.text = json.dumps(payload or {})

    responses = []

    class _FakeHttp:
        def __init__(self):
            self.headers = {}

        def get(self, url, params=None, headers=None, timeout=None):
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        def close(self):
            pass

    http = _FakeHttp()
    fp = FinnhubProvider(["k1", "k2"], session=http, sleep_fn=lambda _s: None)

    # 429, 401, and 500 should each clear _offline=False
    for code in (429, 401, 500):
        with fp._pool_lock:
            fp._offline = True
        responses.clear()
        responses.append(_FakeResp(code))
        try:
            fp._request_with_key(0, "/quote", {"symbol": "AAPL"})
        except Exception:
            pass
        assert fp.is_offline is False

    # Batch with 1 OK then 3 consecutive NetworkFailure aborts before 5th symbol and attaches partial_ticks
    responses[:] = [
        _FakeResp(200, {"c": 200, "pc": 198, "d": 2, "dp": 1.0, "h": 201, "l": 197, "t": 1700000000}),
        requests.ConnectionError("net1"),
        requests.ConnectionError("net2"),
        requests.ConnectionError("net3"),
        _FakeResp(200, {"c": 100}),  # GOOG should NEVER be reached
    ]
    with pytest.raises(NetworkFailure) as exc_info:
        fp.fetch_quotes(["AAPL", "MSFT", "NVDA", "TSLA", "GOOG"])
    assert fp.is_offline is True
    assert len(getattr(exc_info.value, "partial_ticks", [])) == 1
    assert exc_info.value.partial_ticks[0].symbol == "AAPL"
    assert len(responses) == 1  # GOOG was never popped


def test_m2_yahoo_extended_hours_offline_enrichment(monkeypatch):
    class _FakeResp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.text = json.dumps(payload)

    fp = FinnhubProvider("secret_finnhub_key_1", sleep_fn=lambda _s: None)
    try:
        assert "X-Finnhub-Token" not in fp._yahoo_s.headers
        assert fp._uses_real_http is True

        fh_payload = {"c": 200.0, "pc": 198.0, "d": 2.0, "dp": 1.01, "h": 202.0, "l": 197.0, "t": 1760454000}
        yahoo_payload = {
            "chart": {
                "result": [{
                    "meta": {
                        "chartPreviousClose": 198.0,
                        "preMarketPrice": 199.5,
                        "preMarketTime": 1760430000,
                        "regularMarketPrice": 200.0,
                        "regularMarketTime": 1760454000,
                        "postMarketPrice": 203.25,
                        "postMarketTime": 1760475000,
                        "currentTradingPeriod": {
                            "pre": {"start": 1760428800, "end": 1760448600},
                            "regular": {"start": 1760448600, "end": 1760472000},
                            "post": {"start": 1760472000, "end": 1760486400},
                        },
                    },
                    "timestamp": [],
                    "indicators": {"quote": [{"close": []}]},
                }]
            }
        }
        monkeypatch.setattr(fp._s, "get", lambda *a, **kw: _FakeResp(200, fh_payload))
        monkeypatch.setattr(fp._yahoo_s, "get", lambda *a, **kw: _FakeResp(200, yahoo_payload))

        fp.current_session = "AFTER"
        ticks = fp.fetch_quotes(["AAPL"])
        assert len(ticks) == 1
        t = ticks[0]
        assert t.price == Decimal("203.25")
        assert t.pre_price == Decimal("199.5")
        assert t.reg_price == Decimal("200.0")
        assert t.ah_price == Decimal("203.25")
        assert t.session_kind == "AFTER"
    finally:
        fp.close()


# ---- M3: key_invalid notification formatting -------------------------------------------------
def test_m3_key_invalid_message_clean(tmp_path):
    ctrl, _, _, backends = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL")])
    ctrl.on_invalid_key(1, 401, T0)
    assert len(backends.toasts) == 1
    msg = backends.toasts[0][0]
    assert "invalid" in msg.lower()
    assert "0.00" not in msg
    assert "no position entered" not in msg

    ev = AlertEvent("KEY", "key-invalid-1", "key_invalid", "RISK", "HIGH", Decimal(0), T0, "finnhub",
                    condition="Finnhub key #1 is invalid (HTTP 401)")
    fm = format_message(ev, T0)
    assert "0.00" not in fm and "no position entered" not in fm


# ---- M4 & M5: Mini Tracker sync_rows, drag defer, WM_DELETE_WINDOW, no false SELL badge -----
def test_m4_m5_mini_tracker_sync_drag_wm_delete_and_no_false_badge(tmp_path, tk_root):
    ctrl, _, _, _ = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL"), _make_stock("MSFT", "WATCH")])
    ctrl.on_ticks([_tick("AAPL", "205"), _tick("MSFT", "400")], T0)
    layer = _FakeLayer()
    view = WidgetView(tk_root, ctrl, layer, {})
    try:
        # M5: non-flashing SELL stock does NOT have alert="SELL" badge
        mini_rows = {r["symbol"]: r for r in view._mini_rows()}
        assert mini_rows["AAPL"]["alert"] is None
        view.flash("AAPL", 1000)
        assert {r["symbol"]: r for r in view._mini_rows()}["AAPL"]["alert"] == "ALERT"

        ctrl.set_mini_tracker_state({"enabled": True, "selected_symbols": ["AAPL", "MSFT"]})
        view.toggle_mini()
        tk_root.update()
        win = view.mini
        assert isinstance(win, MiniTrackerWindow)

        # Open Settings dialog and remove AAPL -> sync_rows + prune selected_symbols
        win.open_settings()
        tk_root.update()
        assert isinstance(win._settings, MiniTrackerSettingsDialog)
        assert "AAPL" in win._settings.vars

        ctrl.remove_stock("AAPL")
        win.refresh()
        tk_root.update()
        assert "AAPL" not in win._settings.vars
        assert ctrl.get_mini_tracker_state()["selected_symbols"] == ["MSFT"]

        # Deferred rebuild during _dragging
        child_ids_before = [str(c) for c in win.rows_frame.winfo_children()]
        win._dragging = True
        win.refresh()
        assert win._pending_refresh is True
        assert [str(c) for c in win.rows_frame.winfo_children()] == child_ids_before
        win._drag_end(None)
        assert win._dragging is False
        assert win._pending_refresh is False

        # WM_DELETE_WINDOW calls _close -> syncs toggle + state['enabled'] = False
        wm_cmd = win.protocol("WM_DELETE_WINDOW")
        assert wm_cmd
        win.tk.eval(wm_cmd)
        tk_root.update()
        assert view.mini_open is False
        assert ctrl.get_mini_tracker_state()["enabled"] is False
    finally:
        if view.mini is not None:
            view.mini.destroy()


# ---- M6, M7, M8: COOLDOWN + noisy, NaN -> IGNORED, exact-cent zone boundaries ----------------
def test_m6_m7_m8_cooldown_noisy_nan_ignored_and_exact_cent_zones():
    # M6: COOLDOWN expiry with noisy=True still needs 2 readings
    stock = StockConfig("AAPL", "WATCH", noisy=True,
                        alerts=[Alert("rep", "level_above", "WATCH", "MEDIUM", mode="repeat",
                                      price=Decimal("200"), cooldown_min=5)])
    eng = AlertEngine(Config())
    assert [e.alert_id for e in eng.process_tick(stock, _tick("AAPL", "205", t=T0), T0, Session.REGULAR)] == []
    assert [e.alert_id for e in eng.process_tick(stock, _tick("AAPL", "205", t=T0), T0, Session.REGULAR)] == ["rep"]

    t_after = T0 + timedelta(minutes=6)
    assert eng.process_tick(stock, _tick("AAPL", "205", t=t_after), t_after, Session.REGULAR) == []
    assert [e.alert_id for e in eng.process_tick(stock, _tick("AAPL", "205", t=t_after), t_after, Session.REGULAR)] == ["rep"]

    # M7: NaN / Infinity / invalid type -> IGNORED
    pf = PriceFilter()
    assert pf.submit(Decimal("100")) == "accepted"
    assert pf.submit(Decimal("NaN")) == IGNORED
    assert pf.submit(Decimal("Infinity")) == IGNORED
    assert pf.submit(Decimal("-Infinity")) == IGNORED
    assert pf.submit("100") == IGNORED  # type: ignore[arg-type]

    # M8: zone_price_table non-overlapping cent boundaries at base=100.00
    s_sell = _make_stock("AAPL", "SELL")
    defs = build_zone_defs(s_sell, Config().zone_defaults)
    table = {r["name"]: r for r in zone_price_table(defs, Decimal("100.00"))}
    assert table["loss"]["hi_price"] == Decimal("97.00")
    assert table["below_be"]["lo_price"] == Decimal("97.01")
    assert table["below_be"]["hi_price"] == Decimal("99.49")
    assert table["breakeven"]["lo_price"] == Decimal("99.50")
    assert table["breakeven"]["hi_price"] == Decimal("100.50")
    assert table["above_be"]["lo_price"] == Decimal("100.51")
    assert table["above_be"]["hi_price"] == Decimal("102.99")
    assert table["sell"]["lo_price"] == Decimal("103.00")
    assert table["sell"]["hi_price"] == Decimal("104.49")
    assert table["high_sell"]["lo_price"] == Decimal("104.50")


# ---- M9: Validation guards in parse_config, parse_alert, parse_stock ------------------------
def test_m9_validation_guards():
    with pytest.raises(ValueError):
        parse_config({"zone_band": {"standard": -0.1}})
    with pytest.raises(ValueError):
        parse_config({"zone_band": {"noisy": -1}})
    with pytest.raises(ValueError):
        parse_config({"level_rearm_band_pct": 100})
    with pytest.raises(ValueError):
        parse_config({"level_rearm_band_pct": -0.5})
    with pytest.raises(ValueError):
        parse_config({"zone_defaults": {"sell": 10, "high_sell": 5}})

    cfg = Config()
    st_sell = _make_stock("AAPL", "SELL")
    st_watch = _make_stock("MSFT", "WATCH")

    with pytest.raises(ValidationError):
        parse_alert({"id": "a1", "type": "level_above", "price": float("nan")}, st_sell, cfg)
    with pytest.raises(ValidationError):
        parse_alert({"id": "a1", "type": "pct_move_day", "price": 100, "pct": 5}, st_sell, cfg)
    for bad_cd in (True, 5.5, 0, 10081):
        with pytest.raises(ValidationError):
            parse_alert({"id": "a1", "type": "level_above", "price": 100, "mode": "repeat",
                         "cooldown_min": bad_cd}, st_sell, cfg)
    with pytest.raises(ValidationError):
        parse_alert({"id": "z1", "type": "zone_exit", "zone": "near_entry"}, st_sell, cfg)
    with pytest.raises(ValidationError):
        parse_alert({"id": "z2", "type": "zone_exit", "zone": "sell"}, st_watch, cfg)

    with pytest.raises(ValidationError):
        parse_stock({"symbol": "AAPL", "category": "SELL", "position": "bad"}, cfg)
    with pytest.raises(ValidationError):
        parse_stock({"symbol": "AAPL", "category": "BUY", "planned_entry": [1]}, cfg)
    with pytest.raises(ValidationError):
        parse_stock({"symbol": "AAPL", "category": "WATCH", "zones": "bad"}, cfg)
    with pytest.raises(ValidationError):
        parse_stock({"symbol": "AAPL", "category": "WATCH", "zones": {"sell": "bad"}}, cfg)
    with pytest.raises(ValidationError):
        parse_stock({"symbol": "AAPL", "category": "WATCH", "alerts": "not_list"}, cfg)


# ---- M10: Tooltip clamping and idle pause while child dialog is open ------------------------
def test_m10_tooltip_clamp_and_idle_pause_on_child_dialog(tmp_path, tk_root):
    ctrl, _, _, _ = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL")])
    ctrl.on_ticks([_tick("AAPL", "205")], T0)
    view = WidgetView(tk_root, ctrl, _FakeLayer(), {})
    try:
        view.render()
        tk_root.update()
        view._primary_work_area = lambda: (0, 0, 800, 600)
        view.show_tooltip("status_sun_moon", SimpleNamespace(x_root=795, y_root=595))
        tk_root.update_idletasks()
        assert view._tip_win is not None
        x = view._tip_win.winfo_x()
        y = view._tip_win.winfo_y()
        w = view._tip_win.winfo_reqwidth()
        h = view._tip_win.winfo_reqheight()
        assert 0 <= x <= 800 - w
        assert 0 <= y <= 600 - h

        # Open edit mode + AlertDialog; simulate elapsed > IDLE_EDIT_S and verify idle does not exit
        view.enter_edit()
        view.panel.toggle("AAPL")
        tk_root.update()
        view.panel.editor._open_alert(None)
        tk_root.update()
        assert view._has_open_edit_dialog() is True
        view._last_activity = time.monotonic() - (IDLE_EDIT_S + 10)
        # Re-run the idle check directly via scheduled callback
        assert view._idle_job is not None
        # Cancel existing idle job and invoke a fresh _schedule_idle check after monkeypatching after
        tk_root.after_cancel(view._idle_job)
        scheduled_cbs = []
        orig_after = tk_root.after
        tk_root.after = lambda ms, cb: scheduled_cbs.append(cb) or "fake_job"
        try:
            view._schedule_idle()
        finally:
            tk_root.after = orig_after
        assert scheduled_cbs
        view._last_activity = time.monotonic() - (IDLE_EDIT_S + 10)
        # Execute one check cycle without looping
        tk_root.after = lambda ms, cb: "fake_job"
        try:
            scheduled_cbs[0]()
        finally:
            tk_root.after = orig_after
        assert view.editing is True
    finally:
        if view._tip_win is not None:
            view._tip_win.destroy()
        if view.editing:
            view.exit_edit()


# ---- M11: peek_starts & no SelectiveClickThrough / hit_testing.py ----------------------------
def test_m11_peek_starts_and_no_selective_click_through():
    root_dir = Path(__file__).resolve().parent.parent
    assert not (root_dir / "hit_testing.py").exists()
    if sys.platform == "win32":
        import desktop_layer as dl
        assert not hasattr(dl, "SelectiveClickThrough")
        assert dl.peek_starts(ord("W"), down=True, alt=True, ctrl=False, shift=False) is True
        assert dl.peek_starts(ord("W"), down=True, alt=False, ctrl=False, shift=False) is False
        assert dl.peek_starts(ord("W"), down=True, alt=True, ctrl=False, shift=True) is False
        assert dl.peek_starts(ord("W"), down=True, alt=True, ctrl=True, shift=False) is False
        assert dl.peek_starts(dl.VK_MENU, down=True, alt=True, ctrl=False, shift=False) is False


# ---- L1-L8: atomic fsync, invalid key recovery, stale AddStockDialog check, q2, session poll -
def test_l1_to_l8_regressions(tmp_path, monkeypatch, tk_root):
    import os

    # L1: atomic_write_text invokes os.fsync
    fsync_calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (fsync_calls.append(fd), real_fsync(fd))[1])
    target = tmp_path / "sample.txt"
    atomic_write_text(target, "hello\n")
    assert fsync_calls and target.read_text(encoding="utf-8") == "hello\n"

    # L3: recovered 401 key clears _notified_invalid_keys so later 401 re-notifies
    class _SeqResp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self.text = json.dumps(payload or {})

    seq = [_SeqResp(401), _SeqResp(200, {"c": 100}), _SeqResp(401)]

    class _SeqHttp:
        headers = {}

        def get(self, *a, **kw):
            return seq.pop(0)

        def close(self):
            pass

    fp = FinnhubProvider("k1", session=_SeqHttp(), sleep_fn=lambda _s: None)
    st = Store(tmp_path)
    st.save_watchlist(Watchlist(symbols=[_make_stock("AAPL", "WATCH")]), Config())
    backends = _SpyBackends()
    notifier = Notifier(Config(), backends, _MemTimers(), lambda: T0)
    ctrl = AppController(Config(), st, fp, notifier, now_fn=lambda: T0, session_override=Session.REGULAR)

    with pytest.raises(ProviderError):
        fp.fetch_quotes(["AAPL"])
    ctrl._check_provider_invalid_keys(T0)
    assert len(backends.toasts) == 1

    # Key recovers on 200 -> _notified_invalid_keys cleared
    fp.rejected_until.clear()
    ticks = fp.fetch_quotes(["AAPL"])
    assert len(ticks) == 1 and ticks[0].price == Decimal("100")
    ctrl._check_provider_invalid_keys(T0)
    assert 1 not in ctrl._notified_invalid_keys

    # Second 401 after recovery re-notifies
    with pytest.raises(ProviderError):
        fp.fetch_quotes(["AAPL"])
    ctrl._check_provider_invalid_keys(T0)
    assert len(backends.toasts) == 2

    # L4/L5: AddStockDialog._checked_cb ignores stale symbol if user edited v_sym in flight
    dlg = AddStockDialog(tk_root, ctrl, lambda s: None)
    try:
        dlg.v_sym.set("NVDA")
        dlg._checked_cb("AAPL", _tick("AAPL", "200"), None)
        assert dlg._checked is None
        assert str(dlg.btn_add.cget("state")) == "disabled"
    finally:
        dlg.destroy()

    # L6: condition rounds level via q2(lvl)
    eng = AlertEngine(Config())
    stk = StockConfig("AAPL", "WATCH", alerts=[Alert("l1", "level_above", "WATCH", "HIGH",
                                                      mode="once", price=Decimal("100.126"))])
    assert eng.process_tick(stk, _tick("AAPL", "99"), T0, Session.REGULAR) == []
    evs = eng.process_tick(stk, _tick("AAPL", "101"), T0, Session.REGULAR)
    assert evs and evs[0].condition == "at or above 100.13"

    # L8: housekeeping triggers poll_now() on session transition
    polled = []
    clock_now = [datetime(2026, 10, 26, 3, 55, tzinfo=ET)]  # CLOSED
    ctrl_hb = AppController(Config(), st, MockProvider({}), notifier,
                            now_fn=lambda: clock_now[0], poll_now=lambda: polled.append(clock_now[0]))
    ctrl_hb.housekeeping(clock_now[0])
    assert polled == []
    clock_now[0] = datetime(2026, 10, 26, 4, 5, tzinfo=ET)  # PRE transition
    ctrl_hb.housekeeping(clock_now[0])
    assert len(polled) == 1


# ---- Extended Hours (PH/RH/AH) UI + RowView + ET rollover + StatusStripView icons ------------
def test_extended_hours_ph_rh_ah_columns_rollover_and_icons(tmp_path, tk_root):
    st = Store(tmp_path)
    cfg = Config()
    st.save_watchlist(Watchlist(symbols=[_make_stock("AAPL", "SELL")]), cfg)
    notifier = Notifier(cfg, _SpyBackends(), _MemTimers(), lambda: T0)

    # Dynamic session controller (no override)
    clock_now = [datetime(2026, 10, 26, 8, 30, tzinfo=ET)]  # PRE on Mon Oct 26
    ctrl = AppController(cfg, st, MockProvider({}), notifier, now_fn=lambda: clock_now[0])

    # Status strip icons across PRE / REGULAR / AFTER / CLOSED
    assert ctrl.status_strip(datetime(2026, 10, 26, 8, 30, tzinfo=ET)).session_icon == "🌅"
    assert ctrl.status_strip(datetime(2026, 10, 26, 11, 0, tzinfo=ET)).session_icon == "☀️"
    assert ctrl.status_strip(datetime(2026, 10, 26, 17, 30, tzinfo=ET)).session_icon == "🌇"
    assert ctrl.status_strip(datetime(2026, 10, 26, 22, 0, tzinfo=ET)).session_icon == "🌙"

    # PRE tick populates ph_price_text
    t_pre = datetime(2026, 10, 26, 8, 35, tzinfo=ET)
    ctrl.on_ticks([_tick("AAPL", "201.50", t=t_pre)], t_pre)
    rv = ctrl.row_views(t_pre)[0]
    assert rv.ph_price_text == "201.50"
    assert rv.rh_price_text == "—"
    assert rv.ah_price_text == "—"

    # REGULAR tick populates rh_price_text while preserving ph_price_text
    t_reg = datetime(2026, 10, 26, 11, 0, tzinfo=ET)
    ctrl.on_ticks([_tick("AAPL", "204.00", t=t_reg)], t_reg)
    rv = ctrl.row_views(t_reg)[0]
    assert (rv.ph_price_text, rv.rh_price_text, rv.ah_price_text) == ("201.50", "204.00", "—")

    # AFTER tick populates ah_price_text while preserving ph + rh
    t_ah = datetime(2026, 10, 26, 17, 30, tzinfo=ET)
    ctrl.on_ticks([_tick("AAPL", "206.25", t=t_ah)], t_ah)
    rv = ctrl.row_views(t_ah)[0]
    assert (rv.ph_price_text, rv.rh_price_text, rv.ah_price_text) == ("201.50", "204.00", "206.25")

    # CLOSED preserves last PH/RH/AH values
    t_closed = datetime(2026, 10, 26, 22, 0, tzinfo=ET)
    rv = ctrl.row_views(t_closed)[0]
    assert (rv.ph_price_text, rv.rh_price_text, rv.ah_price_text) == ("201.50", "204.00", "206.25")

    # ET date rollover into next day's PRE updates PH while retaining last trading day's RH and AH
    t_next_pre = datetime(2026, 10, 27, 8, 0, tzinfo=ET)
    ctrl.on_ticks([_tick("AAPL", "207.10", t=t_next_pre)], t_next_pre)
    rv = ctrl.row_views(t_next_pre)[0]
    assert rv.ph_price_text == "207.10"
    assert rv.rh_price_text == "204.00"
    assert rv.ah_price_text == "206.25"

    # WidgetView canvas renders PH / RH / AH columns; MiniTrackerWindow shows compact single price
    view = WidgetView(tk_root, ctrl, _FakeLayer(), {}, now_fn=lambda: t_next_pre)
    try:
        view.render()
        canvas_texts = {view.canvas.itemcget(i, "text")
                        for i in view.canvas.find_all()
                        if view.canvas.type(i) == "text"}
        assert {"PH", "RH", "AH"}.issubset(canvas_texts)

        view.toggle_mini()
        tk_root.update()
        mini_texts = [lbl.cget("text")
                      for chip in view.mini.rows_frame.winfo_children()
                      for lbl in chip.winfo_children()
                      if isinstance(lbl, tk.Label)]
        assert all("PH" not in t and "AH" not in t for t in mini_texts)
        assert any("207.10" in t for t in mini_texts)
    finally:
        if view.mini is not None:
            view.mini.destroy()


def test_finnhub_yahoo_extended_all_sessions_and_weekend_fallback():
    fri = datetime(2026, 11, 13, 0, 0, tzinfo=ET)
    ts_pre = int(fri.replace(hour=9, minute=15).timestamp())
    ts_reg = int(fri.replace(hour=15, minute=59).timestamp())
    ts_post = int(fri.replace(hour=19, minute=45).timestamp())
    mon = datetime(2026, 11, 16, 0, 0, tzinfo=ET)
    mon_pre_s = int(mon.replace(hour=4, minute=0).timestamp())
    mon_pre_e = int(mon.replace(hour=9, minute=30).timestamp())
    mon_reg_s = mon_pre_e
    mon_reg_e = int(mon.replace(hour=16, minute=0).timestamp())
    mon_post_s = mon_reg_e
    mon_post_e = int(mon.replace(hour=20, minute=0).timestamp())

    payload = {
        "chart": {
            "result": [{
                "meta": {
                    "chartPreviousClose": 195.0,
                    "currentTradingPeriod": {
                        "pre": {"start": mon_pre_s, "end": mon_pre_e},
                        "regular": {"start": mon_reg_s, "end": mon_reg_e},
                        "post": {"start": mon_post_s, "end": mon_post_e},
                    },
                },
                "timestamp": [ts_pre, ts_reg, ts_post],
                "indicators": {"quote": [{"close": [196.25, 200.50, 201.75]}]},
            }]
        }
    }

    class _Resp:
        status_code = 200
        text = json.dumps(payload)

    calls = []

    class _YahooStub:
        def get(self, url, params=None, timeout=None):
            calls.append(url)
            return _Resp()

    fp = FinnhubProvider("k1", sleep_fn=lambda _s: None)
    fp._uses_real_http = True
    fp._yahoo_s = _YahooStub()

    # Weekend / CLOSED falls back to latest ET date bar windows when currentTradingPeriod mismatches
    fp.current_session = "CLOSED"
    base_tick = _tick("AAPL", "200.50", prev="195.00")
    t_closed = fp._apply_yahoo_extended("AAPL", base_tick)
    assert len(calls) == 1
    assert t_closed.pre_price == Decimal("196.25")
    assert t_closed.reg_price == Decimal("200.50")
    assert t_closed.ah_price == Decimal("201.75")
    assert t_closed.price == Decimal("201.75")

    # REGULAR uses Finnhub live tick.price for RH/price while caching Yahoo ext across 15s polls
    fp.current_session = "REGULAR"
    t_reg1 = fp._apply_yahoo_extended("AAPL", _tick("AAPL", "203.10", prev="195.00"))
    t_reg2 = fp._apply_yahoo_extended("AAPL", _tick("AAPL", "203.40", prev="195.00"))
    assert len(calls) == 2
    assert t_reg1.price == Decimal("203.10") and t_reg1.reg_price == Decimal("203.10")
    assert t_reg1.pre_price == Decimal("196.25") and t_reg1.ah_price == Decimal("201.75")
    assert t_reg2.price == Decimal("203.40") and t_reg2.pre_price == Decimal("196.25")


def test_h5_m10_editor_alert_dlg_dirty_idle_and_wheel_cleanup(tmp_path, tk_root):
    ctrl, _, _, _ = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL")])
    ctrl.on_ticks([_tick("AAPL", "205")], T0)

    # Global 'all' wheel binding must survive EditPanel.destroy()
    all_bind_id = tk_root.bind_all("<MouseWheel>", lambda _e: None)
    assert all_bind_id
    view = WidgetView(tk_root, ctrl, _FakeLayer(), {})
    try:
        view.enter_edit()
        view.panel.toggle("AAPL")
        tk_root.update()
        ed = view.panel.editor
        assert ed.is_dirty() is False

        # H5: Category change and Revert both close an open AlertDialog
        ed._open_alert(None)
        dlg1 = ed._alert_dlg
        assert dlg1 is not None and dlg1.winfo_exists()
        ed.v_cat.set("WATCH")
        ed._on_category()
        assert dlg1.winfo_exists() == 0
        assert ed._alert_dlg is None
        assert ed.is_dirty() is True
        assert view._has_unsaved_editor_changes() is True

        ed._open_alert(None)
        dlg2 = ed._alert_dlg
        assert dlg2 is not None and dlg2.winfo_exists()
        ed._load(ctrl.stock("AAPL"))
        assert dlg2.winfo_exists() == 0
        assert ed.is_dirty() is False

        # Inline nick edit marks dirty and pauses idle auto-exit
        ed.v_nick.set("Apple Edited")
        assert ed.is_dirty() is True
        view._last_activity = time.monotonic() - (IDLE_EDIT_S + 10)
        tk_root.after_cancel(view._idle_job)
        cbs = []
        orig_after = tk_root.after
        tk_root.after = lambda _ms, cb: cbs.append(cb) or "j1"
        try:
            view._schedule_idle()
            tk_root.after = lambda _ms, _cb: "j2"
            cbs[0]()
        finally:
            tk_root.after = orig_after
        assert view.editing is True
    finally:
        if view.editing:
            view.exit_edit()
        assert tk_root.bind_all("<MouseWheel>")
        try:
            tk_root.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass


def test_l7_startup_order_single_instance_and_logging_before_runtime(tmp_path, monkeypatch):
    import desktop_layer as dl
    import store as store_mod

    # 1. Duplicate instance exits before setup_logging or build_runtime
    called = []
    monkeypatch.setattr(dl, "set_dpi_aware", lambda: called.append("dpi"))
    monkeypatch.setattr(dl, "single_instance", lambda name: (called.append(("single_instance", name)), None)[1])
    monkeypatch.setattr(store_mod, "setup_logging", lambda folder: called.append("setup_logging"))
    monkeypatch.setattr(main_mod, "build_runtime", lambda *_a, **_kw: called.append("build_runtime"))
    main_mod.run(["--data-dir", str(tmp_path)])
    assert called == ["dpi", ("single_instance", "Local\\StockAlertWidget_SingleInstance")]

    # 2. Primary instance calls single_instance -> setup_logging -> build_runtime in order
    class _DummyMutex:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _StopTest(RuntimeError):
        pass

    order = []
    dummy = _DummyMutex()
    monkeypatch.setattr(dl, "set_dpi_aware", lambda: order.append("dpi"))
    monkeypatch.setattr(dl, "single_instance", lambda name: (order.append("single_instance"), dummy)[1])
    monkeypatch.setattr(store_mod, "setup_logging", lambda folder: order.append("setup_logging"))

    def _fake_build_runtime(*_a, **_kw):
        order.append("build_runtime")
        raise _StopTest("stop after build_runtime")

    monkeypatch.setattr(main_mod, "build_runtime", _fake_build_runtime)
    with pytest.raises(_StopTest):
        main_mod.run(["--data-dir", str(tmp_path)])
    assert order == ["dpi", "single_instance", "setup_logging", "build_runtime"]
    assert dummy.closed is True


# ---- Post-implementation audit & layout regression tests -------------------------------------
import copy
import queue
import ui.editor as editor_mod
import ui.mini_tracker as mini_mod
import ui.popup as popup_mod
from ui.common import ACCENT, BG2, DIM, GREY, MUTED
from ui.editor import ApiKeysStatusDialog
from ui.popup import AlertPopup
from ui.widget import DEFAULT_LOOK, FLASH_BG


def test_finnhub_yahoo_extended_session_open_caches_900s_and_keeps_live_price():
    fri = datetime(2026, 11, 13, 0, 0, tzinfo=ET)
    ts_pre = int(fri.replace(hour=9, minute=15).timestamp())
    ts_reg = int(fri.replace(hour=15, minute=59).timestamp())
    ts_post = int(fri.replace(hour=19, minute=45).timestamp())
    mon = datetime(2026, 11, 16, 0, 0, tzinfo=ET)
    payload = {
        "chart": {
            "result": [{
                "meta": {
                    "chartPreviousClose": 195.0,
                    "currentTradingPeriod": {
                        "pre": {"start": int(mon.replace(hour=4, minute=0).timestamp()),
                                "end": int(mon.replace(hour=9, minute=30).timestamp())},
                        "regular": {"start": int(mon.replace(hour=9, minute=30).timestamp()),
                                    "end": int(mon.replace(hour=16, minute=0).timestamp())},
                        "post": {"start": int(mon.replace(hour=16, minute=0).timestamp()),
                                 "end": int(mon.replace(hour=20, minute=0).timestamp())},
                    },
                },
                "timestamp": [ts_pre, ts_reg, ts_post],
                "indicators": {"quote": [{"close": [196.25, 200.50, 201.75]}]},
            }]
        }
    }

    class _Resp:
        status_code = 200
        text = json.dumps(payload)

    calls = []

    class _YahooStub:
        def get(self, url, params=None, timeout=None):
            calls.append(url)
            return _Resp()

        def close(self):
            pass

    clock = [100.0]
    fp = FinnhubProvider("k1", sleep_fn=lambda _s: None, clock_fn=lambda: clock[0])
    try:
        fp._uses_real_http = True
        fp._yahoo_s = _YahooStub()
        fp.current_session = Session.REGULAR.value
        assert fp.current_session == "OPEN"

        t1 = fp._apply_yahoo_extended("AAPL", _tick("AAPL", "203.10", prev="195.00"))
        assert len(calls) == 1
        assert "AAPL" in fp._ext_regular_cache
        assert t1.price == Decimal("203.10")
        assert t1.reg_price == Decimal("203.10")
        assert t1.pre_price == Decimal("196.25")
        assert t1.ah_price == Decimal("201.75")
        assert t1.session_kind == "OPEN"

        # 15s later (still inside 900s cache TTL): Yahoo not called again, live tick.price kept
        clock[0] = 115.0
        t2 = fp._apply_yahoo_extended("AAPL", _tick("AAPL", "203.40", prev="195.00"))
        assert len(calls) == 1
        assert t2.price == Decimal("203.40")
        assert t2.reg_price == Decimal("203.40")
        assert t2.pre_price == Decimal("196.25")
        assert t2.ah_price == Decimal("201.75")

        # Past 900s TTL: refreshes cache once and still preserves live Finnhub tick.price
        clock[0] = 1001.0
        t3 = fp._apply_yahoo_extended("AAPL", _tick("AAPL", "204.00", prev="195.00"))
        assert len(calls) == 2
        assert t3.price == Decimal("204.00")
        assert t3.reg_price == Decimal("204.00")
    finally:
        fp.close()


def test_scheduler_poll_once_computes_next_delay_after_fetch():
    epoch = [0.0]

    class _SlowProvider:
        key_count = 1

        def fetch_quotes(self, symbols):
            epoch[0] = 8.0
            return [_tick(s, "200.00", t=T0) for s in symbols]

        def fetch_market_status(self):
            return None

    cfg = Config(poll_seconds=15, num_keys=1)
    sched = Scheduler(
        _SlowProvider(),
        lambda: ["AAPL"],
        queue.Queue(),
        cfg,
        now_fn=lambda: T0,
        jitter_fn=lambda: 0.0,
        epoch_fn=lambda: epoch[0],
    )
    delay = sched.poll_once()
    assert delay == 7.0


def test_main_widget_dark_card_theme_headers_dividers_and_edit_buttons(tmp_path, tk_root):
    assert DEFAULT_LOOK["background_mode"] == "panel"
    assert DEFAULT_LOOK["panel_color"] == "#202020"
    assert DEFAULT_LOOK["shadow_offset"] == 0

    ctrl, _, _, _ = _setup_controller(
        tmp_path,
        [_make_stock("AAPL", "SELL"), _make_stock("MSFT", "WATCH")],
        session_override=Session.REGULAR,
    )
    t_reg = datetime(2026, 10, 26, 11, 0, tzinfo=ET)
    ctrl.on_ticks(
        [
            _tick("AAPL", "207.50", prev="200", pre="201.00", reg="207.50", ah="208.00", t=t_reg),
            _tick("MSFT", "410.00", prev="405", pre="406.00", reg="410.00", ah="411.00", t=t_reg),
        ],
        t_reg,
    )

    layer = _FakeLayer()
    view = WidgetView(tk_root, ctrl, layer, {}, now_fn=lambda: t_reg)
    try:
        assert view.transparent is False
        assert view.canvas.cget("bg").upper() == "#202020"
        assert int(view.canvas.cget("highlightthickness")) >= 1
        assert str(view.canvas.cget("highlightbackground")).upper() == "#444444"

        for sess, active_hdr in ((Session.PRE, "PH"), (Session.REGULAR, "RH"), (Session.AFTER, "AH")):
            ctrl.session_override = sess
            view.render()
            c = view.canvas

            hdr_items = {
                c.itemcget(i, "text"): i
                for i in c.find_all()
                if c.type(i) == "text" and c.itemcget(i, "text") in ("PH", "RH", "AH")
            }
            assert set(hdr_items) == {"PH", "RH", "AH"}
            for txt, item_id in hdr_items.items():
                expected_col = ACCENT if txt == active_hdr else GREY
                assert c.itemcget(item_id, "fill").upper() == expected_col.upper()

            active_bbox = c.bbox(hdr_items[active_hdr])
            assert active_bbox is not None
            underlines = [
                i for i in c.find_all()
                if c.type(i) == "line"
                and c.itemcget(i, "fill").upper() == ACCENT.upper()
                and float(c.itemcget(i, "width")) == 2.0
            ]
            assert len(underlines) == 1
            ux1, uy1, ux2, uy2 = c.coords(underlines[0])
            assert uy1 == uy2
            assert abs(ux1 - active_bbox[0]) <= 1 and abs(ux2 - active_bbox[2]) <= 1

            dividers = [
                i for i in c.find_all()
                if c.type(i) == "line" and c.itemcget(i, "fill").upper() in ("#333333", "#2A2A2A")
            ]
            assert len(dividers) == 2
            for d_id in dividers:
                dx1, dy1, dx2, dy2 = c.coords(d_id)
                assert dy1 == dy2 and dx2 > dx1

            rects = [i for i in c.find_all() if c.type(i) == "rectangle"]
            assert len(rects) == 0

        # Active session price keeps row color while inactive session prices are MUTED
        ctrl.session_override = Session.REGULAR
        view.render()
        price_fills = {
            view.canvas.itemcget(i, "text"): view.canvas.itemcget(i, "fill").upper()
            for i in view.canvas.find_all()
            if view.canvas.type(i) == "text" and view.canvas.itemcget(i, "text") in ("201.00", "207.50", "208.00")
        }
        assert price_fills["201.00"] == MUTED.upper()
        assert price_fills["208.00"] == MUTED.upper()
        assert price_fills["207.50"] != MUTED.upper()

        # Exactly 1 rectangle when 1 row flashes
        view.flash("AAPL", 500)
        rects = [i for i in view.canvas.find_all() if view.canvas.type(i) == "rectangle"]
        assert len(rects) == 1
        assert view.canvas.itemcget(rects[0], "fill").upper() == FLASH_BG.upper()

        # EditPanel top bar buttons & "Exit edit mode" ordering
        view.enter_edit()
        top = view.panel.winfo_children()[0]
        top_children = top.winfo_children()
        btn_texts = [w.cget("text") for w in top_children]
        assert "API Keys" in btn_texts
        assert "Test Alert" in btn_texts
        assert top_children[-1].cget("text") == "Exit edit mode"
    finally:
        if view.editing:
            view.exit_edit()


def test_mini_tracker_overflow_indicators_and_icon_button_bounds(tk_root, monkeypatch):
    monkeypatch.setattr(mini_mod, "work_area", lambda: (0, 0, 1280, 260))
    monkeypatch.setattr(mini_mod, "work_area_for_point", lambda _x, _y: (0, 0, 1280, 260))

    rows = [
        {
            "symbol": f"SYM{i:02d}",
            "price": 100.0 + i,
            "change_pct": 1.0,
            "category": "SELL" if i % 2 == 0 else "BUY",
            "alert": None,
            "flashing": False,
        }
        for i in range(20)
    ]
    state = {}
    win = MiniTrackerWindow(
        tk_root,
        get_state=lambda: state,
        set_state=lambda patch: state.update(patch),
        get_rows=lambda: rows,
        get_alerts=lambda: (0, "warn"),
        acknowledge=lambda: False,
    )
    try:
        win.update()
        assert win._overflow is True
        # rows_frame must hold ONLY row chips (overflow indicators live on shell)
        assert len(win.rows_frame.winfo_children()) == 20
        assert all(isinstance(ch, tk.Frame) for ch in win.rows_frame.winfo_children())
        assert win.overflow_up_lbl not in win.rows_frame.winfo_children()
        assert win.overflow_down_lbl not in win.rows_frame.winfo_children()
        assert win.overflow_up_lbl.cget("text") == "▲"
        assert win.overflow_down_lbl.cget("text") == "▼"

        # Top of list: down visible, up hidden
        assert win.overflow_up_lbl.winfo_manager() == ""
        assert win.overflow_down_lbl.winfo_manager() == "place"
        assert win.overflow_up_lbl.winfo_ismapped() == 0
        assert win.overflow_down_lbl.winfo_ismapped() == 1

        # Clicking ▼ scrolls down and reveals ▲
        win.overflow_down_lbl.event_generate("<Button-1>", x=4, y=4)
        win.update()
        assert win.body.yview()[0] > 0.0
        assert win.overflow_up_lbl.winfo_manager() == "place"
        assert win.overflow_down_lbl.winfo_manager() == "place"

        # Bottom of list: up visible, down hidden
        win.body.yview_moveto(1.0)
        win._update_overflow_indicators()
        win.update()
        assert win.overflow_up_lbl.winfo_manager() == "place"
        assert win.overflow_down_lbl.winfo_manager() == ""

        # Clicking ▲ scrolls back up and reveals ▼
        win.overflow_up_lbl.event_generate("<Button-1>", x=4, y=4)
        win.update()
        assert win.overflow_down_lbl.winfo_manager() == "place"

        # Icon button bounds (Audit 4.3): press or outside release does NOT open settings
        win.update()
        gw = max(2, win.gear_btn.winfo_width())
        gh = max(2, win.gear_btn.winfo_height())
        win.gear_btn.event_generate("<ButtonPress-1>", x=gw // 2, y=gh // 2)
        win.update()
        assert win._settings is None

        for ox, oy in ((-5, gh // 2), (gw + 10, gh // 2), (gw // 2, -5), (gw // 2, gh + 10)):
            win.gear_btn.event_generate("<ButtonRelease-1>", x=ox, y=oy)
            win.update()
            assert win._settings is None

        # Inside release opens settings
        win.gear_btn.event_generate("<ButtonRelease-1>", x=gw // 2, y=gh // 2)
        win.update()
        dlg = win._settings
        assert dlg is not None and dlg.winfo_exists()

        # MiniTrackerSettingsDialog overflow indicators (Audit 4.2) & inner children purity
        assert all(isinstance(ch, tk.Checkbutton) for ch in dlg.inner.winfo_children())
        assert len(dlg.inner.winfo_children()) == 20
        assert dlg.overflow_up_lbl.winfo_manager() == ""
        assert dlg.overflow_down_lbl.winfo_manager() == "place"

        dlg._scroll_step(1)
        dlg.update()
        assert dlg.list_canvas.yview()[0] > 0.0
        assert dlg.overflow_up_lbl.winfo_manager() == "place"
        assert dlg.overflow_down_lbl.winfo_manager() == "place"

        dlg.list_canvas.yview_moveto(1.0)
        dlg._update_overflow_indicators()
        dlg.update()
        assert dlg.overflow_up_lbl.winfo_manager() == "place"
        assert dlg.overflow_down_lbl.winfo_manager() == ""

        # Settings close button also requires release inside bounds (Audit 4.3)
        cw = max(2, dlg.close_btn.winfo_width())
        ch = max(2, dlg.close_btn.winfo_height())
        dlg.close_btn.event_generate("<ButtonPress-1>", x=cw // 2, y=ch // 2)
        dlg.update()
        assert dlg.winfo_exists() == 1
        dlg.close_btn.event_generate("<ButtonRelease-1>", x=cw + 12, y=ch // 2)
        dlg.update()
        assert dlg.winfo_exists() == 1
        dlg.close_btn.event_generate("<ButtonRelease-1>", x=cw // 2, y=ch // 2)
        dlg.update()
        assert dlg.winfo_exists() == 0
    finally:
        win.destroy()


def test_mini_tracker_in_place_refresh_tooltip_and_double_click(tmp_path, tk_root):
    ctrl, _, _, _ = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL"), _make_stock("MSFT", "WATCH")])
    ctrl.on_ticks([_tick("AAPL", "207.50", prev="200"), _tick("MSFT", "400.00", prev="405")], T0)
    view = WidgetView(tk_root, ctrl, _FakeLayer(), {}, now_fn=lambda: T0)
    try:
        view.toggle_mini()
        tk_root.update()
        win = view.mini
        assert win is not None

        # Audit 5.3: in-place refresh preserves exact widget instances when symbols & alert presence stay identical
        before_chips = list(win.rows_frame.winfo_children())
        before_ids = [
            (id(w["frame"]), id(w["sym_lbl"]), id(w["price_lbl"]), id(w["pct_lbl"]))
            for w in win._row_widgets
        ]
        ctrl.on_ticks([_tick("AAPL", "208.00", prev="210"), _tick("MSFT", "415.00", prev="405")], T0)
        win.refresh()
        tk_root.update()
        after_chips = list(win.rows_frame.winfo_children())
        after_ids = [
            (id(w["frame"]), id(w["sym_lbl"]), id(w["price_lbl"]), id(w["pct_lbl"]))
            for w in win._row_widgets
        ]
        assert after_chips == before_chips
        assert after_ids == before_ids
        assert "208.00" in win._row_widgets[0]["price_lbl"].cget("text")
        assert "-0.95%" in win._row_widgets[0]["pct_lbl"].cget("text")
        assert win._row_widgets[0]["pct_lbl"].cget("fg").upper() == mini_mod.DOWN.upper()
        assert "415.00" in win._row_widgets[1]["price_lbl"].cget("text")
        assert "+2.47%" in win._row_widgets[1]["pct_lbl"].cget("text")
        assert win._row_widgets[1]["pct_lbl"].cget("fg").upper() == mini_mod.UP.upper()

        # Audit 5.5: _mini_alert_tooltip() and alert pill hover <Enter>/<Leave>
        assert view._mini_alert_tooltip() == ""
        win.center.event_generate("<Enter>")
        win.update()
        assert win._tip_win is None

        ctrl.send_test_alert(T0)
        win.refresh()
        win.update()
        expected_tip = view._mini_alert_tooltip()
        assert expected_tip and "TEST" in expected_tip
        assert win._alerting is True

        win.center.event_generate("<Enter>")
        win.update()
        assert win._tip_win is not None and win._tip_win.winfo_exists()
        assert win._tip_lbl is not None and win._tip_lbl.cget("text") == expected_tip

        win.center.event_generate("<Leave>")
        win.update()
        assert win._tip_win is None

        # Clicking alert pill clears tooltip & alert state
        win.center.event_generate("<Enter>")
        win.update()
        assert win._tip_win is not None
        win._center_click(SimpleNamespace())
        assert win._tip_win is None
        assert win._alerting is False
        assert view._mini_alert_tooltip() == ""

        # Item 6.3: Double-clicking a Mini Tracker row opens Edit Mode expanded to that StockEditor
        def _dbl_click(widget):
            for seq in ("<ButtonPress-1>", "<ButtonRelease-1>", "<ButtonPress-1>", "<ButtonRelease-1>"):
                widget.event_generate(seq, x=4, y=4)

        assert view.editing is False
        win._drag = (5, 5)
        win._dragging = True
        msft_entry = next(w for w in win._row_widgets if w["symbol"] == "MSFT")
        _dbl_click(msft_entry["sym_lbl"])
        tk_root.update()
        assert win._drag is None and win._dragging is False
        assert view.editing is True
        assert view.panel is not None
        assert view.panel.open_symbol == "MSFT"
        assert isinstance(view.panel.editor, StockEditor)
        assert view.panel.editor.symbol == "MSFT"

        # Double-clicking another row frame switches the expanded StockEditor
        aapl_entry = next(w for w in win._row_widgets if w["symbol"] == "AAPL")
        _dbl_click(aapl_entry["frame"])
        tk_root.update()
        assert view.panel.open_symbol == "AAPL"
        assert view.panel.editor.symbol == "AAPL"
    finally:
        ctrl.notifier.acknowledge_latest()
        if view.mini is not None:
            view.mini.destroy()
        if view.editing:
            view.exit_edit()


def test_mini_tracker_in_place_badge_update(tk_root):
    rows = [{"symbol": "AAPL", "price": 200.0, "change_pct": 1.0, "category": "SELL",
             "alert": "ALERT", "flashing": False}]
    state = {}
    win = MiniTrackerWindow(
        tk_root,
        get_state=lambda: state,
        set_state=lambda patch: state.update(patch),
        get_rows=lambda: rows,
        get_alerts=lambda: (0, "warn"),
        acknowledge=lambda: False,
    )
    try:
        win.update()
        w0 = win._row_widgets[0]
        chip_id = id(w0["frame"])
        badge_id = id(w0["badge_lbl"])
        assert w0["frame"].cget("bg").upper() == BG2.upper()
        assert w0["badge_lbl"].cget("text") == "[ALERT]"

        rows[0] = {"symbol": "AAPL", "price": 202.0, "change_pct": 2.0, "category": "SELL",
                   "alert": "CRIT", "flashing": True}
        win.refresh()
        win.update()
        assert id(win._row_widgets[0]["frame"]) == chip_id
        assert id(win._row_widgets[0]["badge_lbl"]) == badge_id
        assert win._row_widgets[0]["frame"].cget("bg").upper() == mini_mod.FLASH_BG.upper()
        assert win._row_widgets[0]["badge_lbl"].cget("text") == "[CRIT]"
    finally:
        win.destroy()


def test_audit_4_4_popup_wraplength_and_multimonitor_dialog_placement(tmp_path, tk_root, monkeypatch):
    sec_area = (1920, 0, 3840, 1040)
    popup_points = []
    editor_points = []
    monkeypatch.setattr(popup_mod, "work_area_for_point", lambda x, y: (popup_points.append((x, y)), sec_area)[1])
    monkeypatch.setattr(editor_mod, "work_area_for_point", lambda x, y: (editor_points.append((x, y)), sec_area)[1])

    anchor = tk.Toplevel(tk_root)
    anchor.geometry("200x100+2200+120")
    anchor.update()

    # AlertPopup: wraplength=460 on headline and centered inside mocked multi-monitor work area
    popup = AlertPopup(anchor)
    try:
        long_headline = "AAPL " + ("LONGHEADLINE " * 12)
        popup.show(f"{long_headline}\nDetail line", "HIGH", 10, lambda: None)
        popup.win.update()
        labels = [w for w in popup.win.winfo_children() if isinstance(w, tk.Label)]
        assert int(labels[0].cget("wraplength")) == 460
        px, py = popup.win.winfo_x(), popup.win.winfo_y()
        pw, ph = popup.win.winfo_reqwidth(), popup.win.winfo_reqheight()
        assert popup_points
        assert 1920 <= px <= 3840 - pw
        assert 0 <= py <= 1040 - ph
        assert abs(px - (1920 + (1920 - pw) // 2)) <= 2
        assert py == 60
    finally:
        popup.close()

    # place_near_widget clamps ChoiceDialog, AlertDialog, and AddStockDialog inside monitor work area
    ctrl, _, _, _ = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL")])
    ctrl.on_ticks([_tick("AAPL", "205")], T0)
    ed = StockEditor(tk_root, ctrl, "AAPL", lambda: T0, lambda: None, lambda: None)
    ctx = ed._context()

    anchor.geometry("200x100+3800+1000")
    anchor.update()

    choice = ChoiceDialog(anchor, "Would fire now", lambda _c: None)
    try:
        choice.update()
        cw, ch = choice.winfo_reqwidth(), choice.winfo_reqheight()
        assert 1920 <= choice.winfo_x() <= 3840 - cw
        assert 0 <= choice.winfo_y() <= 1040 - ch
    finally:
        choice.destroy()

    alert_dlg = AlertDialog(anchor, ctx, None, lambda _a, _c: None)
    try:
        alert_dlg.update()
        aw, ah = alert_dlg.winfo_reqwidth(), alert_dlg.winfo_reqheight()
        assert 1920 <= alert_dlg.winfo_x() <= 3840 - aw
        assert 0 <= alert_dlg.winfo_y() <= 1040 - ah
    finally:
        alert_dlg.destroy()

    add_dlg = AddStockDialog(anchor, ctrl, lambda _s: None)
    try:
        add_dlg.update()
        dw, dh = add_dlg.winfo_reqwidth(), add_dlg.winfo_reqheight()
        assert 1920 <= add_dlg.winfo_x() <= 3840 - dw
        assert 0 <= add_dlg.winfo_y() <= 1040 - dh
    finally:
        add_dlg.destroy()

    # Also verify clamping on top-left overflow edge of secondary monitor
    anchor.geometry("200x100+1860+-40")
    anchor.update()
    choice2 = ChoiceDialog(anchor, "Top-left clamp check", lambda _c: None)
    try:
        choice2.update()
        assert choice2.winfo_x() >= 1920
        assert choice2.winfo_y() >= 0
    finally:
        choice2.destroy()
    assert editor_points


def test_audit_4_5_stock_editor_hysteresis_label_and_noisy_toggle(tmp_path, tk_root):
    st = Store(tmp_path)
    cfg = Config(zone_band_standard=Decimal("0.5"), zone_band_noisy=Decimal("1.0"))
    st.save_watchlist(Watchlist(symbols=[_make_stock("AAPL", "SELL")]), cfg)
    notifier = Notifier(cfg, _SpyBackends(), _MemTimers(), lambda: T0)
    ctrl = AppController(cfg, st, MockProvider({}), notifier, now_fn=lambda: T0, session_override=Session.REGULAR)

    ed = StockEditor(tk_root, ctrl, "AAPL", lambda: T0, lambda: None, lambda: None)
    assert ed.lbl_hysteresis is not None and ed.lbl_hysteresis.winfo_exists()
    txt0 = ed.lbl_hysteresis.cget("text")
    assert "±0.5% active" in txt0 and "standard ±0.5%" in txt0 and "noisy ±1.0%" in txt0

    # Find the "Noisy stock..." Checkbutton and invoke it to test live command wiring
    noisy_cb = [
        w for w in ed.winfo_children()[0].winfo_children()
        if isinstance(w, tk.Checkbutton) and "Noisy stock" in w.cget("text")
    ][0]
    noisy_cb.invoke()
    assert ed.v_noisy.get() is True
    assert "±1.0% active" in ed.lbl_hysteresis.cget("text")

    noisy_cb.invoke()
    assert ed.v_noisy.get() is False
    assert "±0.5% active" in ed.lbl_hysteresis.cget("text")

    # WATCH hides hysteresis label; BUY restores it
    ed.v_cat.set("WATCH")
    ed._on_category()
    assert ed.lbl_hysteresis is None

    ed.v_cat.set("BUY")
    ed._on_category()
    assert ed.lbl_hysteresis is not None and ed.lbl_hysteresis.winfo_exists()


def test_audit_5_4_build_runtime_key_pool_info_and_status_dialog(tmp_path, tk_root):
    # 1. main.build_runtime sets key_source across --mock, environment, config.json, and none
    d_mock = tmp_path / "mock"
    rt_mock = main_mod.build_runtime(main_mod.parse_args(["--data-dir", str(d_mock), "--mock"]), environ={})
    assert rt_mock.provider.key_source == "demo"

    d_env = tmp_path / "env"
    rt_env = main_mod.build_runtime(
        main_mod.parse_args(["--data-dir", str(d_env)]),
        environ={"FINNHUB_API_KEY": "e_key_1, e_key_2"},
    )
    try:
        assert rt_env.provider.key_source == "environment"
    finally:
        rt_env.provider.close()

    d_cfg = tmp_path / "cfg"
    d_cfg.mkdir()
    (d_cfg / "config.json").write_text(json.dumps({"api_keys": ["cfg_key_1"]}), encoding="utf-8")
    rt_cfg = main_mod.build_runtime(main_mod.parse_args(["--data-dir", str(d_cfg)]), environ={})
    try:
        assert rt_cfg.provider.key_source == "config.json"
    finally:
        rt_cfg.provider.close()

    d_none = tmp_path / "none"
    rt_none = main_mod.build_runtime(main_mod.parse_args(["--data-dir", str(d_none)]), environ={})
    assert rt_none.provider.key_source == "none"

    # 2. AppController.key_pool_info() & ApiKeysStatusDialog with ok / rate_limited / rejected keys
    class _Resp:
        def __init__(self, code, body=None):
            self.status_code = code
            self.text = json.dumps(body or {})

    class _PoolHttp:
        def __init__(self):
            self.headers = {}
            self.seq = [_Resp(429), _Resp(403), _Resp(200, {"c": 205.0, "pc": 200.0})]

        def get(self, *a, **kw):
            return self.seq.pop(0)

        def close(self):
            pass

    fp = FinnhubProvider(["k1", "k2", "k3"], session=_PoolHttp(), sleep_fn=lambda _s: None, clock_fn=lambda: 100.0)
    fp.key_source = "environment"
    ticks = fp.fetch_quotes(["AAPL"])
    assert len(ticks) == 1

    st = Store(tmp_path / "app")
    cfg = Config()
    st.save_watchlist(Watchlist(symbols=[_make_stock("AAPL", "SELL")]), cfg)
    notifier = Notifier(cfg, _SpyBackends(), _MemTimers(), lambda: T0)
    ctrl = AppController(cfg, st, fp, notifier, now_fn=lambda: T0, session_override=Session.REGULAR)

    info = ctrl.key_pool_info()
    assert info["source"] == "environment"
    assert info["total"] == 3
    assert info["ok"] == 1
    assert info["rate_limited"] == 1
    assert info["rejected"] == 1
    assert info["reject_code"] == 403
    assert [k["state"] for k in info["keys"]] == ["rate_limited", "rejected", "ok"]

    def _walk_widgets(root_w):
        out = [root_w]
        for ch in root_w.winfo_children():
            out.extend(_walk_widgets(ch))
        return out

    dlg = ApiKeysStatusDialog(tk_root, ctrl)
    try:
        dlg.update()
        all_widgets = _walk_widgets(dlg)
        assert not any(isinstance(w, (tk.Entry, tk.Text)) for w in all_widgets)
        label_texts = [w.cget("text") for w in all_widgets if isinstance(w, tk.Label)]
        assert "Configured keys: 3" in label_texts
        assert "Source: Environment variables" in label_texts
        assert "1 working, 1 rate-limited, 1 rejected" in label_texts
        assert "Key #1" in label_texts and "Rate-limited (retry in 60s)" in label_texts
        assert "Key #2" in label_texts and "Rejected (HTTP 403)" in label_texts
        assert "Key #3" in label_texts and "Healthy / working" in label_texts
        assert "Calls: 1" in label_texts
    finally:
        dlg.destroy()

    # Demo mode & single-dialog guard via EditPanel "API Keys" button
    ctrl_demo = AppController(cfg, Store(tmp_path / "demo_ctrl"), rt_mock.provider, notifier, now_fn=lambda: T0)
    assert ctrl_demo.key_pool_info()["source"] == "demo"
    dlg_demo = ApiKeysStatusDialog(tk_root, ctrl_demo)
    try:
        texts_demo = [w.cget("text") for w in _walk_widgets(dlg_demo) if isinstance(w, tk.Label)]
        assert "Configured keys: 0" in texts_demo
        assert "Source: Demo mode" in texts_demo
        assert "No Finnhub API keys configured (Demo mode)" in texts_demo
    finally:
        dlg_demo.destroy()

    view = WidgetView(tk_root, ctrl, _FakeLayer(), {}, now_fn=lambda: T0)
    try:
        view.enter_edit()
        api_btn = [w for w in view.panel.winfo_children()[0].winfo_children() if w.cget("text") == "API Keys"][0]
        api_btn.invoke()
        first_dlg = view.panel._keys_dlg
        assert first_dlg is not None and first_dlg.winfo_exists()
        assert view._has_open_edit_dialog() is True
        api_btn.invoke()
        assert view.panel._keys_dlg is first_dlg
    finally:
        if view.editing:
            view.exit_edit()


def test_item_6_2_send_test_alert_and_edit_panel_button(tmp_path, tk_root):
    ctrl, st, _, backends = _setup_controller(tmp_path, [_make_stock("AAPL", "SELL")])
    ctrl.on_ticks([_tick("AAPL", "205.00")], T0)
    before_state = copy.deepcopy(ctrl.engine.state)

    # Direct AppController.send_test_alert()
    ctrl.send_test_alert(T0)
    assert len(backends.popups) == 1
    pop_text, pop_prio, _, _ = backends.popups[0]
    assert pop_prio == "HIGH"
    assert "TEST" in pop_text and "AAPL" in pop_text and "205.00" in pop_text
    assert len(backends.toasts) == 1
    assert "TEST" in backends.toasts[0][0] and backends.toasts[0][1] is True
    assert backends.beeps == ["high"]
    assert backends.flashes == [("AAPL", 2000)]
    assert ctrl.engine.state == before_state
    assert not st.alert_log_path.exists()
    assert st.read_alert_log() == []

    # EditPanel "Test Alert" button invokes EditPanel._test_alert() -> send_test_alert()
    ctrl.notifier.acknowledge_latest()
    backends.popups.clear()
    backends.toasts.clear()
    backends.beeps.clear()
    backends.flashes.clear()

    view = WidgetView(tk_root, ctrl, _FakeLayer(), {}, now_fn=lambda: T0)
    try:
        view.enter_edit()
        test_btn = [w for w in view.panel.winfo_children()[0].winfo_children() if w.cget("text") == "Test Alert"][0]
        test_btn.invoke()
        assert len(backends.popups) == 1
        assert backends.popups[0][1] == "HIGH" and "TEST" in backends.popups[0][0]
        assert len(backends.toasts) == 1 and "TEST" in backends.toasts[0][0]
        assert backends.beeps == ["high"]
        assert backends.flashes == [("AAPL", 2000)]
        assert ctrl.engine.state == before_state
        assert st.read_alert_log() == []
    finally:
        ctrl.notifier.acknowledge_latest()
        if view.editing:
            view.exit_edit()


def test_main_widget_header_gear_row_chips_closed_colors_and_legacy_look(tmp_path, tk_root):
    from ui.common import FG, GREEN, RED
    from ui.widget import merged_look

    # 1. Legacy clock look dict migrates cleanly to dark card panel defaults
    legacy = {
        "time_font": ["Segoe UI Light", 48],
        "time_format": "%H:%M:%S",
        "background_mode": "transparent",
        "panel_color": "#202020",
        "shadow_offset": 2,
        "padding": 12,
    }
    migrated = merged_look(legacy)
    assert "time_font" not in migrated and "time_format" not in migrated
    assert migrated["background_mode"] == "panel"
    assert migrated["panel_color"] == "#202020"
    assert migrated["shadow_offset"] == 0
    assert migrated["padding"] == 10

    # Explicit non-legacy transparent choice still works
    custom_trans = merged_look({"background_mode": "transparent", "shadow_offset": 2})
    assert custom_trans["background_mode"] == "transparent"
    assert custom_trans["shadow_offset"] == 2

    # 2. AppController.get_mini_tracker_state() strips stale "pinned"
    ctrl, st, _, _ = _setup_controller(
        tmp_path,
        [_make_stock("AAPL", "SELL"), _make_stock("NVDA", "WATCH")],
        session_override=Session.CLOSED,
    )
    ctrl.engine.state["mini_tracker"] = {"enabled": False, "pinned": True, "pos_x": 100}
    mt_state = ctrl.get_mini_tracker_state()
    assert "pinned" not in mt_state and mt_state["pos_x"] == 100
    assert "pinned" not in ctrl.engine.state["mini_tracker"]

    t_closed = datetime(2026, 10, 24, 15, 0, tzinfo=ET)  # Saturday (CLOSED)
    ctrl.on_ticks(
        [
            _tick("AAPL", "207.50", prev="200", pre="201.00", reg="207.00", ah="207.50", t=t_closed),
            _tick("NVDA", "161.01", prev="166.19", pre="167.21", reg="162.85", ah="161.01", t=t_closed),
        ],
        t_closed,
    )

    layer = _FakeLayer()
    view = WidgetView(tk_root, ctrl, layer, migrated, now_fn=lambda: t_closed)
    try:
        view.render()
        c = view.canvas

        # Settings gear button placed beside Mini toggle & preserves <= 40px constraint
        gear_items = c.find_withtag("status_settings_gear")
        assert len(gear_items) >= 2
        assert "status_settings_gear" in view.tooltips
        assert view.canvas.tag_bind("status_settings_gear", "<ButtonRelease-1>")
        gear_bbox = c.bbox("status_settings_gear")
        mini_bbox = c.bbox("status_mini_tracker")
        assert gear_bbox[0] > mini_bbox[2]
        assert int(float(c.cget("width"))) - mini_bbox[2] <= 40

        # Framed #2D2D2D header + row chips with #444444 outline, and 0 rectangles when not flashing
        assert len([i for i in c.find_all() if c.type(i) == "rectangle"]) == 0
        card_polys = [
            i for i in c.find_all()
            if c.type(i) == "polygon"
            and c.itemcget(i, "fill").upper() == BG2.upper()
            and c.itemcget(i, "outline").upper() == "#444444"
            and "mini_toggle_track" not in c.gettags(i)
            and "settings_gear_bg" not in c.gettags(i)
        ]
        assert len(card_polys) == 3  # 1 header card + 2 row chips

        # Even when CLOSED (r.color == "dim"), tickers stay FG and ▲/▼ changes stay GREEN/RED
        fills_by_text: dict[str, list[str]] = {}
        for i in c.find_all():
            if c.type(i) == "text":
                fills_by_text.setdefault(c.itemcget(i, "text"), []).append(c.itemcget(i, "fill").upper())
        assert fills_by_text["AAPL"] == [FG.upper(), MUTED.upper()]
        assert fills_by_text["NVDA"] == [FG.upper(), MUTED.upper()]
        assert fills_by_text["207.00"] == [FG.upper()]
        assert fills_by_text["207.50"] == [FG.upper()]
        assert fills_by_text["201.00"] == [MUTED.upper()]
        up_chg = next(v[0] for k, v in fills_by_text.items() if k.startswith("▲ "))
        dn_chg = next(v[0] for k, v in fills_by_text.items() if k.startswith("▼ "))
        assert up_chg == GREEN.upper()
        assert dn_chg == RED.upper()

        # Clicking settings gear opens Edit Mode
        assert view.editing is False
        view.on_settings_gear_click()
        assert view.editing is True
    finally:
        if view.editing:
            view.exit_edit()
