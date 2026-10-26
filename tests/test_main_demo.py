"""--mock / --data-dir, the demo provider and the demo watchlist (no Windows needed)."""
import json
import math
import queue
from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dc

import pytest

import main
from app_core import AppController
from config import Config, ZONE_DEFAULTS_PCT
from market_clock import Session
from providers.mock import DEMO_SPECS, DEMO_WATCHLIST, DemoProvider
from scheduler import Scheduler
from store import Store
from test_app_core import Rec


def env(tmp_path, **extra):
    return {"APPDATA": str(tmp_path / "appdata"), **extra}


def test_parse_args_defaults_and_flags():
    a = main.parse_args([])
    assert a.mock is False and a.data_dir is None
    a = main.parse_args(["--mock", "--data-dir", r"C:\temp\x"])
    assert a.mock is True and a.data_dir == r"C:\temp\x"
    with pytest.raises(SystemExit):
        main.parse_args(["--nonsense"])


def test_data_dir_resolution_keeps_demo_away_from_real_data(tmp_path):
    e = env(tmp_path)
    real = main.resolve_data_dir(main.parse_args([]), e)
    demo = main.resolve_data_dir(main.parse_args(["--mock"]), e)
    custom = main.resolve_data_dir(main.parse_args(["--data-dir", str(tmp_path / "mine")]), e)
    assert real.name == "StockAlertWidget" or real.name == ".StockAlertWidget"      # APPDATA is read by data_dir()
    assert demo == tmp_path / "appdata" / "StockAlertWidget_demo" and demo.is_dir()
    assert custom == tmp_path / "mine" and custom.is_dir()
    assert demo != real


def test_demo_runtime_seeds_a_valid_watchlist_and_never_needs_a_key(tmp_path):
    e = env(tmp_path)                                       # no FINNHUB_API_KEY at all
    rt = main.build_runtime(main.parse_args(["--mock"]), e)
    assert rt.demo and rt.polling and rt.session_override == Session.REGULAR
    assert type(rt.provider).__name__ == "DemoProvider" and rt.cfg.poll_seconds == main.DEMO_POLL_S
    assert rt.warnings[0] == main.DEMO_BANNER and "DEMO" in rt.warnings[0]
    assert rt.mutex_name.endswith("_DEMO")
    st = Store(rt.folder)
    wl, warns = st.load_watchlist(rt.cfg)
    assert not warns and [s.symbol for s in wl.symbols] == ["AAPL", "TTWO", "MSFT", "NVDA", "VST"]
    assert [a.id for a in wl.get("AAPL").alerts][0] == "aapl-slip"
    # the user's own demo edits are never overwritten on the next start
    data = json.loads(st.watchlist_path.read_text())
    data["symbols"] = data["symbols"][:2]
    st.watchlist_path.write_text(json.dumps(data))
    main.build_runtime(main.parse_args(["--mock"]), e)
    assert len(json.loads(st.watchlist_path.read_text())["symbols"]) == 2


def test_real_runtime_without_and_with_a_key(tmp_path):
    rt = main.build_runtime(main.parse_args(["--data-dir", str(tmp_path / "d")]), env(tmp_path))
    assert not rt.polling and not rt.demo and rt.session_override is None and rt.cfg.poll_seconds == 60
    assert type(rt.provider).__name__ == "MockProvider"      # placeholder only; polling stays off
    rt = main.build_runtime(main.parse_args(["--data-dir", str(tmp_path / "d")]), env(tmp_path, FINNHUB_API_KEY="k-123"))
    assert rt.polling and type(rt.provider).__name__ == "FinnhubProvider" and "k-123" not in repr(rt.warnings)
    assert not rt.mutex_name.endswith("_DEMO")


def test_pythonw_streams_are_repaired(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "stdout", None); monkeypatch.setattr(sys, "stderr", None)
    main._fix_pythonw_streams()
    assert sys.stdout is not None and sys.stderr is not None
    with pytest.raises(SystemExit):                          # argparse errors no longer crash on a None stderr
        main.parse_args(["--bad"])


# ---- DemoProvider ----------------------------------------------------------------------------
T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


def test_demo_prices_are_deterministic_positive_and_swing_as_specified():
    clock = [T0]
    dp = DemoProvider(now_fn=lambda: clock[0])
    again = DemoProvider(now_fn=lambda: clock[0])
    prices = {s: [] for s in DEMO_SPECS}
    for i in range(0, 600, 5):
        clock[0] = T0 + timedelta(seconds=i)
        a = {t.symbol: t for t in dp.fetch_quotes(list(DEMO_SPECS))}
        b = {t.symbol: t for t in again.fetch_quotes(list(DEMO_SPECS))}
        for s, t in a.items():
            assert t.price > 0 and t.price == b[s].price and t.source == "mock"
            assert t.prev_close == Dc(str(DEMO_SPECS[s][0]))
            prices[s].append(float(t.price))
    for s, (base, amp, period, _) in DEMO_SPECS.items():
        assert max(prices[s]) > base * (1 + amp / 100 * 0.9) and min(prices[s]) < base * (1 - amp / 100 * 0.9)


def test_demo_accepts_any_ticker_and_nope_is_unknown():
    dp = DemoProvider(now_fn=lambda: T0)
    (t,) = dp.fetch_quotes(["XYZW"])
    assert t.price > 0 and dp.fetch_quotes(["XYZW"])[0].price == t.price
    (z,) = dp.fetch_quotes(["nope"])
    assert z.price == 0 and z.prev_close is None


def test_demo_end_to_end_alerts_fire_in_a_few_minutes(tmp_path):
    """Seeded demo watchlist + DemoProvider + forced open session: zones and alerts all trigger."""
    rt = main.build_runtime(main.parse_args(["--mock"]), env(tmp_path))
    clock = [T0]
    rt.provider.now_fn = lambda: clock[0]
    rec = Rec(Store(rt.folder))
    c = AppController(rt.cfg, Store(rt.folder), rt.provider, rec, now_fn=lambda: clock[0],
                      warnings=rt.warnings, session_override=rt.session_override)
    q = queue.Queue()
    sc = Scheduler(rt.provider, c.symbols, q, rt.cfg, now_fn=lambda: clock[0], jitter_fn=lambda: 0, epoch_fn=lambda: 0.0)
    sc.provider_session = Session.REGULAR
    for i in range(60):                                      # 15 simulated minutes at one poll per 15 s
        clock[0] = T0 + timedelta(seconds=15 * i)
        sc.poll_once()
        c.drain(q)
    fired = {e.alert_id for n in rec.notes for e in n.events}
    prios = {e.priority for n in rec.notes for e in n.events}
    assert {"HIGH", "MEDIUM", "LOW"} <= prios
    assert any(a.startswith("zone:") for a in fired) and ("aapl-target" in fired or "ttwo-danger" in fired)
    assert c.header(clock[0]).market == "OPEN" and "DEMO" in c.all_warnings()[0]
