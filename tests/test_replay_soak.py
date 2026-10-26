import gc
import json
import tracemalloc
from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dc
from pathlib import Path

from config import Config
from engine import AlertEngine
from market_clock import Session
from models import Alert
from providers.mock import MockProvider
from conftest import aapl, ttwo, lvl

DAY = json.loads((Path(__file__).parent / "data" / "sample_day.json").read_text())
START = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)      # 09:30 ET


GOLDEN = [(21, 'AAPL', 'zone:below_be', 'LOW'), (32, 'AAPL', 'zone:breakeven', 'MEDIUM'),
          (40, 'TTWO', 'zone:near_entry', 'MEDIUM'), (136, 'TTWO', 'zone:above_entry', 'LOW'),
          (150, 'TTWO', 'zone:entry_hit', 'HIGH'), (177, 'AAPL', 'zone:below_be', 'LOW'),
          (200, 'AAPL', 'zone:sell', 'MEDIUM'), (224, 'AAPL', 'a-target', 'HIGH'),
          (313, 'TTWO', 'zone:near_entry', 'MEDIUM'), (341, 'TTWO', 'zone:entry_hit', 'HIGH')]
# Recorded with per-edge hysteresis (zones start at their exact threshold, see zones.py).


def stocks():
    a = aapl(alerts=[lvl("a-target", "level_above", price="210", mode="once", prio="HIGH", label="SELL"),
                     Alert("a-slip", "zone_exit", "RISK", "MEDIUM", zone="sell")])
    t = ttwo([lvl("t-danger", "level_below", price="195", mode="rearm", prio="HIGH", label="RISK")])
    return [a, t]


def run_day(engine=None):
    cfg = Config()
    engine = engine or AlertEngine(cfg)
    st = stocks()
    now = [START]
    mp = MockProvider({"AAPL": DAY["AAPL"], "TTWO": DAY["TTWO"]}, DAY["prev_close"], lambda: now[0])
    fired = []
    for i in range(390):
        now[0] = START + timedelta(minutes=i)
        ticks = {t.symbol: t for t in mp.fetch_quotes(["AAPL", "TTWO"])}
        for s in st:
            for ev in engine.process_tick(s, ticks[s.symbol], now[0], Session.REGULAR):
                fired.append((i, ev.symbol, ev.alert_id, ev.priority))
        mp.advance()
    return engine, fired


def test_replay_saved_day_is_deterministic_and_sane():
    e1, f1 = run_day()
    e2, f2 = run_day()
    assert f1 == GOLDEN
    assert f1 == f2 and f1                                        # same day, same result, something fired
    assert all(i > 0 or aid.startswith("a-") or aid.startswith("t-") for i, _, aid, _ in f1)   # no zone entry at reading 0
    assert not any(i == 0 and aid.startswith("zone:") for i, _, aid, _ in f1)
    assert sum(1 for x in f1 if x[2] == "a-target") <= 1           # once-mode fires at most once
    assert not any(x[0] == 250 for x in f1 if x[1] == "AAPL")      # zero tick: nothing fires
    assert not any(x[0] == 300 for x in f1 if x[1] == "AAPL")      # bad tick: paused, nothing fires
    assert e1.is_paused("AAPL") is False                          # bad tick cleared after one more reading
    json.dumps(e1.state)                                           # state stays serialisable


def test_soak_simulated_8_hours_memory_flat():
    """Simulated 8 h at one poll/minute on the mock feed; internal containers and memory stay flat.
    (The real 8-hour wall-clock soak with Windows handle counts is a manual step, see README.)"""
    cfg = Config()
    engine = AlertEngine(cfg)
    st = stocks()
    now = START
    mp = MockProvider({"AAPL": DAY["AAPL"], "TTWO": DAY["TTWO"]}, DAY["prev_close"], lambda: now)
    def one_pass(n):
        nonlocal now
        for i in range(n):
            now = START + timedelta(minutes=i)
            for t in mp.fetch_quotes(["AAPL", "TTWO"]):
                s = next(x for x in st if x.symbol == t.symbol)
                engine.process_tick(s, t, now, Session.REGULAR)
            mp.advance()
        mp.i = 0
    one_pass(480)                                                  # warm-up: 8 hours
    gc.collect(); tracemalloc.start()
    base = tracemalloc.take_snapshot()
    sizes = (len(engine.state["symbols"]), sum(len(v["alerts"]) for v in engine.state["symbols"].values()),
             len(engine._rt))
    for _ in range(5):                                             # 40 more simulated hours
        one_pass(480)
    gc.collect()
    growth = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(base, "filename"))
    tracemalloc.stop()
    assert sizes == (len(engine.state["symbols"]), sum(len(v["alerts"]) for v in engine.state["symbols"].values()),
                     len(engine._rt))
    assert growth < 200_000, f"memory grew by {growth} bytes"
