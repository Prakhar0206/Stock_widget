"""Scheduler -> queue -> controller, driven by the mock provider (no threads, no clock)."""
import queue
from datetime import datetime, timedelta, timezone

from app_core import AppController
from config import Config
from providers.mock import MockProvider, NETWORK, RATE_LIMIT, ZERO
from scheduler import Scheduler
from store import Store
from test_config_store import EXAMPLE, write
from test_app_core import Rec, T0


def build(tmp_path, paths, prev=None):
    st = Store(tmp_path)
    write(st.watchlist_path, EXAMPLE)
    clock = [T0]
    mp = MockProvider(paths, prev or {"AAPL": 200, "TTWO": 206}, now_fn=lambda: clock[0])
    rec = Rec(st)
    c = AppController(Config(), st, mp, rec, now_fn=lambda: clock[0])
    q = queue.Queue()
    sc = Scheduler(mp, c.symbols, q, Config(), now_fn=lambda: clock[0], jitter_fn=lambda: 0, epoch_fn=lambda: 0.0)
    woke = []
    c.poll_now = lambda: woke.append(1)

    def step(minutes=1):
        clock[0] += timedelta(minutes=minutes)
        mp.advance()
        sc.poll_once()
        c.drain(q)
        c.housekeeping(clock[0])
    return c, rec, sc, mp, q, clock, step, woke


def test_network_outage_ambers_rows_then_recovers_and_old_prices_never_fire(tmp_path):
    paths = {"AAPL": [200, NETWORK, NETWORK, 212], "TTWO": [205, NETWORK, NETWORK, 205]}
    c, rec, sc, mp, q, clock, step, woke = build(tmp_path, paths)
    sc.poll_once(); c.drain(q)
    assert c.header(clock[0]).feed_dot == "green"
    step(); step()
    h = c.header(clock[0])
    assert (h.feed_dot, h.feed_text) == ("red", "offline, retrying")
    assert [r.color for r in c.row_views(clock[0])] == ["amber", "amber"]
    assert c.row_views(clock[0])[0].price_text == "200.00"             # last prices are kept
    assert not rec.notes
    step()                                                              # reconnect: automatic
    assert c.header(clock[0]).feed_dot == "green"
    assert any(e.alert_id == "aapl-target" for n in rec.notes for e in n.events)   # the 212 reading is evaluated once fresh
    assert c.zone_tag("AAPL") == "High sell zone"


def test_rate_limit_backoff_then_degraded_header(tmp_path):
    paths = {"AAPL": [200, RATE_LIMIT, RATE_LIMIT, 201], "TTWO": [205, RATE_LIMIT, RATE_LIMIT, 205]}
    c, rec, sc, mp, q, clock, step, woke = build(tmp_path, paths)
    sc.poll_once(); c.drain(q)
    step(0); assert c.header(clock[0]).feed_text == "feed degraded"
    assert sc.backoff.next_delay() == 4                                  # 2 s used, 4 s next
    step(); step()
    assert c.header(clock[0]).feed_dot == "green"


def test_stale_alert_fires_after_three_minutes_of_failures_only_once(tmp_path):
    paths = {"AAPL": [200] + [NETWORK] * 10, "TTWO": [205] + [NETWORK] * 10}
    c, rec, sc, mp, q, clock, step, woke = build(tmp_path, paths)
    sc.poll_once(); c.drain(q)
    for _ in range(6):
        step()
    stale = [n for n in rec.notes if n.headline.kind == "feed_stale"]
    assert len(stale) == 1
    assert c.header(clock[0]).feed_dot == "red"


def test_zero_price_poll_changes_nothing(tmp_path):
    paths = {"AAPL": [200, ZERO, ZERO], "TTWO": [205, ZERO, ZERO]}
    c, rec, sc, mp, q, clock, step, woke = build(tmp_path, paths)
    sc.poll_once(); c.drain(q)
    step(); step()
    assert [r.price_text for r in c.row_views(clock[0])] == ["200.00", "205.00"] and not rec.notes


def test_wake_polls_immediately_and_does_not_raise_a_stale_alert(tmp_path):
    c, rec, sc, mp, q, clock, step, woke = build(tmp_path, {"AAPL": [200] * 5, "TTWO": [205] * 5})
    sc.poll_once(); c.drain(q)
    clock[0] += timedelta(minutes=45)                                    # PC slept for 45 minutes
    c.on_wake(clock[0])
    assert woke == [1]
    c.housekeeping(clock[0] + timedelta(seconds=5))
    assert not rec.notes                                                 # sleeping is not an outage
    c.housekeeping(clock[0] + timedelta(seconds=185))
    assert [n.headline.kind for n in rec.notes] == ["feed_stale"]        # but a real 3-minute gap after wake is
