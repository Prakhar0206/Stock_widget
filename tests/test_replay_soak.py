import gc
import json
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import Config
from engine import AlertEngine
from market_clock import Session
from providers.mock import MockProvider
from conftest import aapl, al, ttwo

DAY = json.loads((Path(__file__).parent / "data" / "sample_day.json").read_text())
START = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def stocks():
    a = aapl(
        alerts=[
            al("a-target", "greater_than", "210", trigger="once", prio="HIGH", label="SELL"),
            al("a-cross", "crossing_up", "205", trigger="rearm", prio="MEDIUM", label="SELL"),
        ]
    )
    t = ttwo([al("t-danger", "less_than", "195", trigger="rearm", prio="HIGH", label="RISK")])
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
    assert f1 == f2 and f1
    assert sum(1 for x in f1 if x[2] == "a-target") <= 1
    assert not any(x[0] == 250 for x in f1 if x[1] == "AAPL")
    assert not any(x[0] == 300 for x in f1 if x[1] == "AAPL")
    assert e1.is_paused("AAPL") is False
    json.dumps(e1.state)


def test_soak_simulated_8_hours_memory_flat():
    """Simulated 8 h at one poll/minute on the mock feed; internal containers and memory stay flat."""
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

    tracemalloc.start()
    try:
        for _ in range(4):
            one_pass(480)
        gc.collect()
        base = tracemalloc.take_snapshot()
        sizes = (
            len(engine.state["symbols"]),
            sum(len(v["alerts"]) for v in engine.state["symbols"].values()),
            len(engine._rt),
        )
        for _ in range(5):
            one_pass(480)
        gc.collect()
        growth = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(base, "filename"))
    finally:
        tracemalloc.stop()
    assert sizes == (
        len(engine.state["symbols"]),
        sum(len(v["alerts"]) for v in engine.state["symbols"].values()),
        len(engine._rt),
    )
    assert growth < 200_000, f"memory grew by {growth} bytes"
