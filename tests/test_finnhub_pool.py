"""Multi-key pool: 15s 4-key slot rotation, progressive 429 backoff (60s/120s/240s),
in-slot partial batch failover, 401/403 handling, status UI redesign & all 10 required scenarios."""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app_core import AppController, ProblemIconView
from config import Config
from market_clock import Session
from models import Alert, StockConfig, Watchlist
from providers.base import MalformedResponse, NetworkFailure, ProviderError, RateLimited
from providers.finnhub import (MIN_GAP_S, RATE_LIMIT_QUARANTINE_S, REJECTED_RETRY_S, FinnhubProvider,
                               normalize_keys)
from scheduler import CLOSED_POLL_S, Scheduler
from store import Store

GOOD = '{"c": 101.5, "pc": 100, "d": 1.5, "dp": 1.5, "h": 102, "l": 99, "t": 1700000000}'
ZERO = '{"c": 0, "pc": 0, "d": null, "dp": null, "h": 0, "l": 0, "t": 0}'


class Resp:
    def __init__(self, code=200, text=GOOD):
        self.status_code, self.text = code, text


class FakeSession:
    """Answers by the token currently in the headers; records (token, symbol) per call."""

    def __init__(self, by_key=None, default=None):
        self.headers = {}
        self.by_key = by_key or {}          # token -> list of Resp (popped in order) or a single Resp
        self.default = default or Resp()
        self.calls = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        token = self.headers.get("X-Finnhub-Token")
        self.calls.append((token, (params or {}).get("symbol")))
        v = self.by_key.get(token, self.default)
        if isinstance(v, list):
            item = v.pop(0) if len(v) > 1 else v[0]
            if isinstance(item, Exception):
                raise item
            return item
        if isinstance(v, Exception):
            raise v
        return v

    def close(self):
        self.closed = True


class Clock:
    def __init__(self, t=1000.0, dt: datetime = None):
        self.t = t
        # Regular market open timestamp: Wednesday 2026-10-21 14:30 UTC (10:30 AM ET)
        self.dt = dt or datetime(2026, 10, 21, 14, 30, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def now(self) -> datetime:
        return self.dt

    def advance(self, seconds: float):
        self.t += seconds
        self.dt += timedelta(seconds=seconds)

    def sleep(self, s):
        self.t += s


class FakeNotifier:
    def __init__(self):
        self.muted = False
        self.sent = []

    def toggle_mute(self):
        self.muted = not self.muted

    def notify(self, note):
        if note:
            if isinstance(note, (list, tuple)):
                self.sent.extend(note)
            else:
                self.sent.append(note)


def make(keys, session=None, clock=None, internet_check_fn=None):
    clock = clock or Clock()
    session = session or FakeSession()
    p = FinnhubProvider(
        keys, session=session, now_fn=clock.now, sleep_fn=clock.sleep,
        clock_fn=clock, internet_check_fn=internet_check_fn
    )
    return p, session, clock


def make_controller(tmp_path, keys=("k1", "k2", "k3", "k4"), symbols=("AAPL", "MSFT"),
                    session=None, clock=None, session_override=Session.REGULAR, internet_check_fn=None):
    clock = clock or Clock()
    provider, http_sess, clock = make(list(keys), session=session, clock=clock, internet_check_fn=internet_check_fn)
    cfg = Config(poll_seconds=15, stale_after_seconds=180, api_keys=list(keys))
    store = Store(tmp_path)
    from models import PlannedEntry
    wl = Watchlist(symbols=[
        StockConfig(
            symbol=sym,
            category="BUY",
            planned_entry=PlannedEntry(price=Decimal("150")),
            alerts=[Alert(id=f"a-{sym}", type="level_above", label="BUY", priority="HIGH", price=Decimal("200"))]
        )
        for sym in symbols
    ])
    store.save_watchlist(wl, cfg)
    notifier = FakeNotifier()
    ctrl = AppController(
        cfg, store, provider, notifier,
        now_fn=clock.now, session_override=session_override
    )
    q = queue.Queue()
    sched = Scheduler(
        provider, ctrl.symbols, q, cfg,
        now_fn=clock.now, jitter_fn=lambda: 0.0, epoch_fn=clock
    )
    if session_override is not None:
        sched.provider_session = session_override
    return ctrl, sched, q, provider, http_sess, notifier, clock


def tokens(session):
    return [t for t, _ in session.calls]


# ---- normalisation & single-key compatibility ----------------------------------------------
def test_normalize_keys_variants():
    assert normalize_keys('  "abc" ') == ["abc"]
    assert normalize_keys("a, b ;c\n d") == ["a", "b", "c", "d"]
    assert normalize_keys(["a", ' "b" ', "", None, 7, "a"]) == ["a", "b"]
    assert normalize_keys("") == [] and normalize_keys(None) == [] and normalize_keys('" "') == []


def test_single_string_key_sets_header_once_like_before():
    p, s, _ = make('  "abc"  ')
    assert s.headers["X-Finnhub-Token"] == "abc" and s.headers["Accept"] == "application/json"
    assert p.key_count == 1 and not p._multi


def test_keyless_sends_no_token_and_401_is_provider_error():
    s = FakeSession(default=Resp(401, "{}"))
    p, s, _ = make("", session=s)
    assert "X-Finnhub-Token" not in s.headers
    with pytest.raises(ProviderError, match=r"API key rejected \(HTTP 401\)"):
        p.fetch_quotes(["AAPL"])


def test_single_key_429_raises_with_partial_ticks_and_is_not_gated_next_time():
    s = FakeSession(default=[Resp(200), Resp(429, "{}"), Resp(200)])
    p, s, _ = make("k1", session=s)
    with pytest.raises(RateLimited) as ei:
        p.fetch_quotes(["A", "B", "C"])
    assert [t.symbol for t in ei.value.partial_ticks] == ["A"]
    assert len(s.calls) == 2
    assert [t.symbol for t in p.fetch_quotes(["D"])] == ["D"]


def test_single_key_401_and_403_message_unchanged():
    for code in (401, 403):
        p, _, _ = make("k1", session=FakeSession(default=Resp(code, "{}")))
        with pytest.raises(ProviderError) as ei:
            p.fetch_quotes(["A"])
        assert str(ei.value) == f"API key rejected (HTTP {code})"
        assert not isinstance(ei.value, (RateLimited, NetworkFailure, MalformedResponse))


# ---- 15-Second Slot Rotation & Per-Key Progressive Backoff ---------------------------------
def test_slot_rotation_uses_one_key_per_slot_and_advances():
    p, s, _ = make(["k1", "k2", "k3", "k4"])
    # Slot 1 (0s): Key 1 fetches all stocks
    p.fetch_quotes(["A", "B", "C"])
    assert tokens(s) == ["k1", "k1", "k1"]
    # Slot 2 (+15s): Key 2 fetches all stocks
    p.fetch_quotes(["A", "B", "C"])
    assert tokens(s)[3:6] == ["k2", "k2", "k2"]
    # Slot 3 (+30s): Key 3 fetches all stocks
    p.fetch_quotes(["A", "B", "C"])
    assert tokens(s)[6:9] == ["k3", "k3", "k3"]
    # Slot 4 (+45s): Key 4 fetches all stocks
    p.fetch_quotes(["A", "B", "C"])
    assert tokens(s)[9:12] == ["k4", "k4", "k4"]
    # Slot 5 (+60s): Back to Key 1
    p.fetch_quotes(["A", "B", "C"])
    assert tokens(s)[12:15] == ["k1", "k1", "k1"]


def test_per_key_progressive_backoff_60_120_240():
    s = FakeSession(by_key={"k1": Resp(429, "{}")})
    p, s, clock = make(["k1", "k2"], session=s)

    # 1st 429 on k1 -> 60s cooldown
    p.fetch_quotes(["A"])
    assert p.quarantined_until["k1"] == pytest.approx(clock.t + 60.0, abs=1.0)

    # Advance past 60s so k1 is retried on its turn -> 2nd 429 -> 120s cooldown
    clock.advance(61.0)
    p.fetch_quotes(["A"])   # k1's turn after k2 succeeded in slot 1
    assert p.quarantined_until["k1"] == pytest.approx(clock.t + 120.0, abs=1.0)

    # Advance past 120s -> 3rd 429 -> 240s cooldown
    clock.advance(121.0)
    p.fetch_quotes(["A"])
    assert p.quarantined_until["k1"] == pytest.approx(clock.t + 240.0, abs=1.0)


def test_list_input_cleaned_and_deduped():
    p, s, _ = make([' "k1" ', "k2", "k1", "  ", "k3,k4"])
    assert p.key_count == 4
    for sym in ("A", "B", "C", "D"):
        p.fetch_quotes([sym])
    assert tokens(s) == ["k1", "k2", "k3", "k4"]


def test_market_status_also_rotates():
    s = FakeSession(default=Resp(200, '{"isOpen": true, "holiday": null, "session": "pre-market"}'))
    p, s, _ = make(["k1", "k2"], session=s)
    assert p.fetch_market_status()["isOpen"] is True
    assert p.fetch_market_status()["session"] == "pre-market"
    assert tokens(s) == ["k1", "k2"]


# ============================================================================================
# 10 REQUIRED VERIFICATION SCENARIOS (Section 5)
# ============================================================================================

# Scenario 1: All keys healthy, market open -> Sun icon, green pulsing dot, 0 problem icons
def test_scenario_1_all_keys_healthy_market_open(tmp_path):
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, session_override=Session.REGULAR
    )
    sched.poll_once()
    ctrl.drain(q)
    clock.advance(8)

    strip = ctrl.status_strip(clock.now())
    assert strip.session_icon == "☀️"
    assert strip.heartbeat_color == "green"
    assert strip.heartbeat_dim is False
    assert strip.problems == []
    assert strip.overflow_count == 0
    assert strip.heartbeat_tooltip == "Live, updated 8s ago"


# Scenario 2: Market closed -> Moon icon, dim dot pulsing every 15 min, tooltip shows time to next update
def test_scenario_2_market_closed(tmp_path):
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, session_override=Session.CLOSED
    )
    delay = sched.poll_once()
    ctrl.drain(q)
    assert delay == float(CLOSED_POLL_S)

    clock.advance(4 * 60)  # 4 minutes elapsed since last 15-min poll -> 11 min remaining
    strip = ctrl.status_strip(clock.now())
    assert strip.session_icon == "🌙"
    assert strip.heartbeat_color == "green"
    assert strip.heartbeat_dim is True
    assert strip.heartbeat_tooltip == "Market closed, next update in 11 min"


# Scenario 3: Internet cut -> Only No-WiFi icon appears; dot turns amber then red;
# NO keys marked failed; no alert sounds; rows turn amber after 3 min; exactly 1 notification sent
def test_scenario_3_internet_cut(tmp_path):
    online = {"ok": True}
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, session_override=Session.REGULAR, internet_check_fn=lambda: online["ok"]
    )
    # Initial healthy poll
    sched.poll_once()
    ctrl.drain(q)
    assert len(notifier.sent) == 0

    # Cut internet
    online["ok"] = False
    clock.advance(120)  # 2 min after last good fetch
    sched.poll_once()
    ctrl.drain(q)
    ctrl.housekeeping(clock.now())

    strip = ctrl.status_strip(clock.now())
    assert len(strip.problems) == 1
    assert strip.problems[0].kind == "offline"
    assert strip.problems[0].glyph == "🚫📶"
    assert strip.problems[0].tooltip == "No internet, last update 2 min ago"
    assert strip.heartbeat_color == "amber"
    # NO keys marked failed/quarantined
    assert all(k["state"] == "ok" for k in provider.pool_status())
    assert len(notifier.sent) == 0

    # Advance past 3 minutes (180s total outage)
    clock.advance(65)
    sched.poll_once()
    ctrl.drain(q)
    ctrl.housekeeping(clock.now())

    strip2 = ctrl.status_strip(clock.now())
    assert strip2.heartbeat_color == "red"
    assert len(strip2.problems) == 1 and strip2.problems[0].glyph == "🚫📶"
    # Rows turn amber after 3 min
    assert all(r.color == "amber" for r in ctrl.row_views(clock.now()))
    # Exactly 1 feed-stale notification sent, and no false stock price alerts
    assert len(notifier.sent) == 1
    assert notifier.sent[0].headline.kind == "feed_stale"

    # Further polls while still offline do NOT send duplicate notifications
    clock.advance(30)
    ctrl.housekeeping(clock.now())
    assert len(notifier.sent) == 1


# Scenario 4: Key 2 returns 429 -> Key 3 covers remaining stocks in the same slot;
# ⏳ 2 appears; Key 2 skipped during cooldown; icon clears ONLY when Key 2's next attempt succeeds
def test_scenario_4_key_2_returns_429_and_clears_on_next_success(tmp_path):
    s = FakeSession()
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, symbols=("S1", "S2", "S3"), session=s
    )
    # Slot 1: Key 1 succeeds on all 3 stocks
    sched.poll_once()
    ctrl.drain(q)
    assert tokens(s) == ["k1", "k1", "k1"]

    # Slot 2: Key 2 succeeds on S1, then hits 429 on S2 -> Key 3 covers S2 and S3 in the same slot
    s.by_key["k2"] = [Resp(200), Resp(429, "{}"), Resp(200), Resp(200), Resp(200)]
    s.calls.clear()
    clock.advance(15)
    sched.poll_once()
    ctrl.drain(q)

    assert s.calls == [("k2", "S1"), ("k2", "S2"), ("k3", "S2"), ("k3", "S3")]
    strip = ctrl.status_strip(clock.now())
    assert len(strip.problems) == 1
    assert strip.problems[0].glyph == "⏳"
    assert strip.problems[0].badge == "2"
    assert "Key 2 rate limited, retrying in ~" in strip.problems[0].tooltip
    # Temporary 429 does NOT send any desktop notification
    assert notifier.sent == []

    # Slot 3 (+15s): Key 4 runs; Key 2 is still cooling
    s.calls.clear()
    clock.advance(15)
    sched.poll_once()
    ctrl.drain(q)
    assert tokens(s) == ["k4", "k4", "k4"]

    # Slot 4 (+15s): Key 1 runs; Key 2 is skipped during cooldown (only 30s elapsed of 60s)
    s.calls.clear()
    clock.advance(15)
    sched.poll_once()
    ctrl.drain(q)
    assert tokens(s) == ["k1", "k1", "k1"]
    assert "k2" not in tokens(s)

    # Expire Key 2's 60s timer BEFORE Key 2 is actually tried again:
    # Icon Lifetime rule (Spec 3.4): icon must STILL be present until Key 2's next attempt succeeds!
    clock.advance(35)
    strip_before_retry = ctrl.status_strip(clock.now())
    assert len(strip_before_retry.problems) == 1 and strip_before_retry.problems[0].badge == "2"

    # Next slot: Key 2's natural turn after Key 1 -> Key 2 succeeds -> icon clears!
    s.calls.clear()
    sched.poll_once()
    ctrl.drain(q)
    assert tokens(s) == ["k2", "k2", "k2"]
    strip_after_retry = ctrl.status_strip(clock.now())
    assert strip_after_retry.problems == []


# Scenario 5: Key 3 returns 401/403 -> Red 🔑❌ 3 appears; immediate single notification;
# key is excluded from normal rotation
def test_scenario_5_key_3_returns_401_403(tmp_path):
    s = FakeSession(by_key={"k3": Resp(401, "{}")})
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, symbols=("AAPL",), session=s
    )
    # Slot 1: Key 1, Slot 2: Key 2
    sched.poll_once()
    ctrl.drain(q)
    sched.poll_once()
    ctrl.drain(q)
    assert notifier.sent == []

    # Slot 3: Key 3 returns 401 -> fails over to Key 4 in same slot
    sched.poll_once()
    ctrl.drain(q)

    strip = ctrl.status_strip(clock.now())
    assert len(strip.problems) == 1
    p = strip.problems[0]
    assert p.glyph == "🔑❌"
    assert p.color == "red"
    assert p.badge == "3"
    assert p.tooltip == "Key 3 invalid, check it in settings"

    # Immediate single notification sent
    assert len(notifier.sent) == 1
    assert "Finnhub key #3 is invalid (HTTP 401)" in notifier.sent[0].headline.condition

    # Subsequent slots exclude Key 3 from normal rotation and do not re-notify
    s.calls.clear()
    for _ in range(4):
        clock.advance(15)
        sched.poll_once()
        ctrl.drain(q)
    assert "k3" not in tokens(s)
    assert len(notifier.sent) == 1


# Scenario 6: Keys 1 and 3 rate limited -> A single hourglass icon shows "1, 3"
def test_scenario_6_keys_1_and_3_rate_limited(tmp_path):
    s = FakeSession(by_key={"k1": Resp(429, "{}"), "k3": Resp(429, "{}")})
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, symbols=("AAPL",), session=s
    )
    # Slot 1: k1 -> 429, k2 -> 200 OK
    sched.poll_once()
    ctrl.drain(q)
    # Slot 2: k3 -> 429, k4 -> 200 OK
    clock.advance(15)
    sched.poll_once()
    ctrl.drain(q)

    strip = ctrl.status_strip(clock.now())
    assert len(strip.problems) == 1
    assert strip.problems[0].glyph == "⏳"
    assert strip.problems[0].badge == "1, 3"


# Scenario 7: Throttle after stock #6 of 10 -> First 6 prices update on screen;
# remaining 4 stocks are dispatched to the next healthy key in the same slot
def test_scenario_7_throttle_after_stock_6_of_10(tmp_path):
    syms = [f"S{i}" for i in range(1, 11)]
    # Key 1 succeeds on 6 stocks, then returns 429 on the 7th stock
    s = FakeSession(by_key={
        "k1": [Resp(200, f'{{"c": {100 + i}, "pc": 100}}') for i in range(1, 7)] + [Resp(429, "{}")],
        "k2": [Resp(200, f'{{"c": {100 + i}, "pc": 100}}') for i in range(7, 11)],
    })
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, symbols=syms, session=s
    )
    sched.poll_once()
    ctrl.drain(q)

    # Verify first 6 were fetched by k1, 7th failed on k1 and immediately retried on k2 through 10th
    k1_syms = [sym for tok, sym in s.calls if tok == "k1"]
    k2_syms = [sym for tok, sym in s.calls if tok == "k2"]
    assert k1_syms == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    assert k2_syms == ["S7", "S8", "S9", "S10"]

    # All 10 prices are updated on screen in the same slot
    views = {rv.symbol: rv.price_text for rv in ctrl.row_views(clock.now())}
    for i in range(1, 11):
        assert views[f"S{i}"] == f"{100 + i:.2f}"


# Scenario 8: All 4 keys fail in one slot -> Each key tried once; appropriate icons shown;
# scheduler pauses until next slot without spinning
def test_scenario_8_all_4_keys_fail_in_one_slot(tmp_path):
    s = FakeSession(by_key={
        "k1": Resp(429, "{}"),
        "k2": Resp(429, "{}"),
        "k3": Resp(401, "{}"),
        "k4": Resp(503, "{}"),
    })
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, symbols=("AAPL", "MSFT"), session=s
    )
    delay = sched.poll_once()
    ctrl.drain(q)

    # Each key tried once in the slot (no infinite retry loop)
    assert tokens(s) == ["k1", "k2", "k3", "k4"]
    # Scheduler pauses until next 15s slot
    assert delay >= 5.0

    strip = ctrl.status_strip(clock.now())
    # Red 🔑❌ 3 first, then Amber ⏳ 1, 2, then Amber ☁️❌
    assert [p.glyph for p in strip.problems] == ["🔑❌", "⏳", "☁️❌"]
    assert strip.problems[0].badge == "3" and strip.problems[0].color == "red"
    assert strip.problems[1].badge == "1, 2" and strip.problems[1].color == "amber"
    assert strip.problems[2].badge == "" and strip.problems[2].color == "amber"


# Scenario 9: Heartbeat activity -> Heartbeat timestamp updates on each successful poll
def test_scenario_9_heartbeat_timestamp_updates_each_poll(tmp_path):
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(tmp_path)
    assert ctrl.last_heartbeat_at is None

    sched.poll_once()
    ctrl.drain(q)
    t1 = ctrl.status_strip(clock.now()).last_heartbeat_at
    assert t1 == clock.now()

    clock.advance(15)
    sched.poll_once()
    ctrl.drain(q)
    t2 = ctrl.status_strip(clock.now()).last_heartbeat_at
    assert t2 == clock.now() and t2 > t1


# Scenario 10: >3 simultaneous problems -> Displays top 3 icons sorted by severity
# (Red before Amber) + "+N" badge
def test_scenario_10_more_than_3_problems_overflow_and_severity_order(tmp_path):
    s = FakeSession(by_key={
        "k1": Resp(429, "{}"),
        "k2": Resp(401, "{}"),
        "k3": Resp(503, "{}"),
    })
    ctrl, sched, q, provider, http_sess, notifier, clock = make_controller(
        tmp_path, symbols=("AAPL",), session=s
    )
    # Trigger k1 (429), k2 (401), k3 (503), then k4 succeeds
    sched.poll_once()
    ctrl.drain(q)

    # Add two extra problems (one red, one amber) so 5 simultaneous problems exist
    ctrl.server_down = True
    ctrl.extra_problems = [
        ProblemIconView("custom_amber", "⚠️", "amber", "", "Custom amber warning"),
        ProblemIconView("custom_red", "🛑", "red", "4", "Custom red error"),
    ]
    strip = ctrl.status_strip(clock.now())
    assert len(strip.problems) == 3
    assert strip.overflow_count == 2
    # Severity order: Red icons first (🔑❌, 🛑), then Amber (⏳), with remaining 2 in +2 overflow
    assert [p.color for p in strip.problems] == ["red", "red", "amber"]
    assert [p.glyph for p in strip.problems] == ["🔑❌", "🛑", "⏳"]


# ---- preservations & security --------------------------------------------------------------
def test_zero_price_is_kept_not_malformed():
    p, _, _ = make(["k1", "k2"], session=FakeSession(default=Resp(200, ZERO)))
    (t,) = p.fetch_quotes(["XXXX"])
    assert t.symbol == "XXXX" and t.price == Decimal("0") and t.day_high is None and t.prev_close is None
    assert p.bad_symbols == 0


def test_negative_price_still_malformed_and_counted():
    p, _, _ = make(["k1", "k2"], session=FakeSession(default=Resp(200, '{"c": -1}')))
    assert p.fetch_quotes(["A"]) == [] and p.bad_symbols == 1


def test_network_failure_skips_symbol_and_does_not_penalise_key():
    s = FakeSession(by_key={"k1": [ConnectionError("boom secret-text"), Resp(200)]})
    p, _, _ = make(["k1", "k2"], session=s)
    assert [t.symbol for t in p.fetch_quotes(["A", "B"])] == ["B"]
    assert p.network_errors == 1 and p.healthy_key_count() == 2


def test_all_network_failures_raise_network_failure_without_echoing_text():
    p, _, _ = make(["k1", "k2"], session=FakeSession(default=TimeoutError("secret")))
    with pytest.raises(NetworkFailure) as ei:
        p.fetch_quotes(["A", "B"])
    assert str(ei.value) == "TimeoutError"


def test_pacing_100ms_between_requests_across_keys():
    p, s, clock = make(["k1", "k2", "k3"])
    start = clock.t
    p.fetch_quotes(["A", "B", "C", "D"])
    assert clock.t - start == pytest.approx(3 * MIN_GAP_S)


def test_keys_never_leak_into_repr_logs_or_errors(caplog):
    caplog.set_level("DEBUG")
    s = FakeSession(by_key={"SECRET1": Resp(429, "{}"), "SECRET2": Resp(401, "{}"), "SECRET3": Resp(429, "{}")})
    p, _, _ = make(["SECRET1", "SECRET2", "SECRET3"], session=s)
    with pytest.raises(Exception) as ei:
        p.fetch_quotes(["A"])
    blob = repr(p) + str(ei.value) + caplog.text + " ".join(p.pop_notices()) + str(p.pool_status())
    assert "SECRET" not in blob


def test_close_is_idempotent():
    p, s, _ = make(["k1", "k2"])
    p.close()
    p.close()
    assert s.closed


def test_threads_share_the_pool_without_losing_ticks():
    s = FakeSession(by_key={"k1": [Resp(429, "{}"), Resp(200)]})
    p, s, _ = make(["k1", "k2", "k3", "k4"], session=s)
    out, errs = [], []

    def work(syms):
        try:
            out.extend(p.fetch_quotes(syms))
        except Exception as exc:           # pragma: no cover
            errs.append(exc)

    threads = [threading.Thread(target=work, args=([f"S{i}{j}" for j in range(5)],)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errs and len(out) == 20
    assert p.healthy_key_count() >= 3