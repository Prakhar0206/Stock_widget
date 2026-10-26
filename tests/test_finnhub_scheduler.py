import json
import queue
from datetime import datetime, timezone
from decimal import Decimal as Dc

import pytest

from config import Config
from market_clock import Session
from providers.base import MalformedResponse, NetworkFailure, ProviderError, RateLimited
from providers.finnhub import FinnhubProvider
from providers.mock import MockProvider, NETWORK, RATE_LIMIT
from scheduler import Scheduler, next_delay

KEY = "SECRETKEY_123"
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


class Resp:
    def __init__(self, code=200, body=None, text=None):
        self.status_code = code
        self.text = text if text is not None else json.dumps(body)


class FakeSession:
    def __init__(self, responses):
        self.headers, self.responses, self.calls = {}, list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def provider(responses, **kw):
    s = FakeSession(responses)
    return FinnhubProvider(KEY, session=s, now_fn=lambda: NOW, sleep_fn=lambda x: None, **kw), s


QUOTE = {"c": 261.74, "d": 1.2, "dp": 0.46, "h": 263.31, "l": 260.68, "o": 261.07, "pc": 259.45, "t": 1_780_000_000}


def test_quote_parsing_and_key_in_header_only():
    p, s = provider([Resp(200, QUOTE)])
    t = p.fetch_quotes(["AAPL"])[0]
    assert (t.symbol, t.price, t.prev_close, t.change_pct, t.source) == ("AAPL", Dc("261.74"), Dc("259.45"), Dc("0.46"), "finnhub")
    assert t.provider_time.tzinfo is not None and t.fetched_at == NOW
    assert s.headers["X-Finnhub-Token"] == KEY
    assert all(KEY not in str(c[0]) and KEY not in str(c[1]) for c in s.calls)   # never in URL or params
    assert s.calls[0][0].endswith("/quote") and s.calls[0][1] == {"symbol": "AAPL"}


def test_zero_price_passes_through_for_the_filter_to_ignore():
    p, _ = provider([Resp(200, {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0})])
    t = p.fetch_quotes(["NOPE"])[0]
    assert t.price == 0 and t.provider_time is None and t.prev_close is None


@pytest.mark.parametrize("resp,exc", [
    (Resp(429), RateLimited), (Resp(503), RateLimited), (Resp(401), ProviderError),
    (Resp(200, text="<html>"), None), (Resp(404), None)])
def test_http_errors(resp, exc):
    p, _ = provider([resp])
    if exc is None:                         # malformed: skipped, counted, not raised
        assert p.fetch_quotes(["AAPL"]) == [] and p.bad_symbols == 1
    else:
        with pytest.raises(exc) as e:
            p.fetch_quotes(["AAPL"])
        assert KEY not in str(e.value)


def test_network_failure_hides_details():
    p, _ = provider([ConnectionError(f"boom https://x?token={KEY}")])
    with pytest.raises(NetworkFailure) as e:
        p.fetch_quotes(["AAPL"])
    assert KEY not in str(e.value)


def test_requests_are_spaced_100ms():
    sleeps, clock = [], [0.0]
    p = FinnhubProvider(KEY, session=FakeSession([Resp(200, QUOTE)] * 3), now_fn=lambda: NOW,
                        sleep_fn=lambda x: (sleeps.append(round(x, 3)), clock.__setitem__(0, clock[0] + x)),
                        clock_fn=lambda: clock[0])
    p.fetch_quotes(["A", "B", "C"])
    assert sleeps == [0.1, 0.1]


def test_market_status():
    p, s = provider([Resp(200, {"exchange": "US", "holiday": None, "isOpen": True, "session": "regular", "t": 1})])
    assert p.fetch_market_status() == {"isOpen": True, "holiday": None, "session": "regular"}
    assert s.calls[0][1] == {"exchange": "US"}
    p, _ = provider([Resp(200, {"x": 1})])
    with pytest.raises(MalformedResponse):
        p.fetch_market_status()


# ---- scheduler ---------------------------------------------------------------------------------
def make(paths, now=NOW, symbols=("AAPL",), cfg=None):
    q = queue.Queue()
    mp = MockProvider(paths, now_fn=lambda: now)
    sc = Scheduler(mp, lambda: list(symbols), q, cfg or Config(), now_fn=lambda: now,
                   jitter_fn=lambda: 1.0, epoch_fn=lambda: 1000.0 * 60 + 10)
    return sc, mp, q


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get())
    return out


def test_delay_alignment_and_closed_polling():
    assert next_delay(True, 600.0 + 10, 60, 1.5) == 51.5          # aligned to the next minute + jitter
    assert next_delay(True, 600.0 + 59.9, 60, 0.0) > 60            # too close to the boundary: skip one
    assert next_delay(False, 123.0, 60, 1.0) == 900


def test_poll_once_puts_ticks_and_returns_aligned_delay():
    sc, mp, q = make({"AAPL": [100]})
    d = sc.poll_once()
    msgs = drain(q)
    assert [m[0] for m in msgs] == ["ticks"] and msgs[0][1][0].price == 100
    assert d == 50 + 1.0


def test_429_backoff_then_reset():
    sc, mp, q = make({"AAPL": [RATE_LIMIT, RATE_LIMIT, RATE_LIMIT, 100]})
    delays = []
    for _ in range(3):
        delays.append(sc.poll_once()); mp.advance()
    assert delays == [2, 4, 8]
    assert [m[:2] for m in drain(q)] == [("error", "rate")] * 3
    sc.poll_once()
    assert sc.backoff.next_delay() == 2                            # success reset it


def test_network_failure_retries_quickly_and_reports_offline():
    sc, mp, q = make({"AAPL": [NETWORK, 100]})
    assert sc.poll_once() == 2
    assert drain(q)[0][1] == "network"


def test_outside_session_polls_every_15_minutes():
    closed = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)    # Saturday
    sc, mp, q = make({"AAPL": [100]}, now=closed)
    assert sc.poll_once() == 900 and drain(q)[0][0] == "ticks"


def test_outside_session_wakes_at_market_open():
    from market_clock import ET
    # Monday Oct 26 at 09:26 ET (4 minutes = 240 s before 09:30 ET regular open)
    pre_open = datetime(2026, 10, 26, 9, 26, tzinfo=ET)
    sc, mp, q = make({"AAPL": [100]}, now=pre_open)
    assert sc.poll_once() == 240.0 and drain(q)[0][0] == "ticks"


def test_budget_refusal_blocks_polling():
    from dataclasses import replace
    sc, mp, q = make({}, symbols=[f"S{i}" for i in range(30)])
    sc.cfg = replace(Config(), poll_seconds=30)                    # 30 symbols at 30 s = 62 calls/min
    sc.poll_once()
    assert drain(q)[0][:2] == ("error", "budget")


def test_no_symbols_no_calls():
    sc, mp, q = make({}, symbols=())
    sc.poll_once()
    assert q.empty()


def test_run_call_returns_result_and_exception_on_queue():
    sc, mp, q = make({"AAPL": [100]})
    sc.run_call(lambda: 42, "cb1")
    sc.run_call(lambda: 1 / 0, "cb2")
    got = {}
    for _ in range(2):
        m = q.get(timeout=2)
        got[m[1]] = m
    assert got["cb1"][2] == 42 and isinstance(got["cb2"][3], ZeroDivisionError)
