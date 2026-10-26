"""Multi-key pool: round robin, 429 quarantine + failover, 401/403 handling, single-key compatibility."""
import threading
from decimal import Decimal

import pytest

from providers.base import MalformedResponse, NetworkFailure, ProviderError, RateLimited
from providers.finnhub import (MIN_GAP_S, RATE_LIMIT_QUARANTINE_S, REJECTED_RETRY_S, FinnhubProvider,
                               normalize_keys)

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
            return v.pop(0) if len(v) > 1 else v[0]
        if isinstance(v, Exception):
            raise v
        return v

    def close(self):
        self.closed = True


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(keys, session=None, clock=None):
    clock = clock or Clock()
    session = session or FakeSession()
    p = FinnhubProvider(keys, session=session, sleep_fn=clock.sleep, clock_fn=clock)
    return p, session, clock


def tokens(session):
    return [t for t, _ in session.calls]


# ---- normalisation -------------------------------------------------------------------------
def test_normalize_keys_variants():
    assert normalize_keys('  "abc" ') == ["abc"]
    assert normalize_keys("a, b ;c\n d") == ["a", "b", "c", "d"]
    assert normalize_keys(["a", ' "b" ', "", None, 7, "a"]) == ["a", "b"]
    assert normalize_keys("") == [] and normalize_keys(None) == [] and normalize_keys('" "') == []


# ---- single key == old behaviour -----------------------------------------------------------
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
    assert len(s.calls) == 2                       # aborted after the 429, exactly as before
    # no pool gate with one key: the next poll still reaches the network
    assert [t.symbol for t in p.fetch_quotes(["D"])] == ["D"]


def test_single_key_401_and_403_message_unchanged():
    for code in (401, 403):
        p, _, _ = make("k1", session=FakeSession(default=Resp(code, "{}")))
        with pytest.raises(ProviderError) as ei:
            p.fetch_quotes(["A"])
        assert str(ei.value) == f"API key rejected (HTTP {code})"
        assert not isinstance(ei.value, (RateLimited, NetworkFailure, MalformedResponse))


# ---- round robin ---------------------------------------------------------------------------
def test_round_robin_cycles_every_request():
    p, s, _ = make(["k1", "k2", "k3", "k4"])
    p.fetch_quotes(["A", "B", "C", "D", "E", "F"])
    assert tokens(s) == ["k1", "k2", "k3", "k4", "k1", "k2"]
    assert [x["calls"] for x in p.pool_status()] == [2, 2, 1, 1]


def test_list_input_cleaned_and_deduped():
    p, s, _ = make([' "k1" ', "k2", "k1", "  ", "k3,k4"])
    assert p.key_count == 4
    p.fetch_quotes(["A", "B", "C", "D"])
    assert tokens(s) == ["k1", "k2", "k3", "k4"]


def test_market_status_also_rotates():
    s = FakeSession(default=Resp(200, '{"isOpen": true, "holiday": null, "session": "pre-market"}'))
    p, s, _ = make(["k1", "k2"], session=s)
    assert p.fetch_market_status()["isOpen"] is True
    assert p.fetch_market_status()["session"] == "pre-market"
    assert tokens(s) == ["k1", "k2"]


# ---- 429 failover --------------------------------------------------------------------------
def test_429_quarantines_key_and_retries_same_symbol_on_next_key():
    s = FakeSession(by_key={"k1": Resp(429, "{}")})
    p, s, clock = make(["k1", "k2", "k3"], session=s)
    ticks = p.fetch_quotes(["A", "B", "C"])
    assert [t.symbol for t in ticks] == ["A", "B", "C"]          # no tick lost
    assert s.calls[0] == ("k1", "A") and s.calls[1] == ("k2", "A")   # A retried at once on k2
    assert p.quarantined_until["k1"] == pytest.approx(clock.t + RATE_LIMIT_QUARANTINE_S, abs=1)
    assert "k1" not in tokens(s)[2:]                              # k1 skipped while quarantined
    status = {x["label"]: x["state"] for x in p.pool_status()}
    assert status == {"key #1": "rate_limited", "key #2": "ok", "key #3": "ok"}
    assert p.healthy_key_count() == 2


def test_quarantined_key_returns_after_60s():
    s = FakeSession(by_key={"k1": [Resp(429, "{}"), Resp(200)]})
    p, s, clock = make(["k1", "k2"], session=s)
    p.fetch_quotes(["A"])
    clock.t += RATE_LIMIT_QUARANTINE_S + 1
    p.fetch_quotes(["B", "C"])
    assert "k1" in tokens(s)[2:]
    assert all(x["state"] == "ok" for x in p.pool_status())
    assert "k1" not in p.quarantined_until


def test_all_keys_429_raises_rate_limited_with_partial_ticks():
    s = FakeSession(by_key={"k1": Resp(200), "k2": Resp(429, "{}"), "k3": Resp(429, "{}")})
    p, s, _ = make(["k1", "k2", "k3"], session=s)
    # A is served by k1; for B, k2 and k3 answer 429 and k1 now answers 429 too -> every key cooling
    s.by_key["k1"] = [Resp(200), Resp(429, "{}")]
    with pytest.raises(RateLimited) as ei:
        p.fetch_quotes(["A", "B", "C"])
    assert [t.symbol for t in ei.value.partial_ticks] == ["A"]
    assert not isinstance(ei.value, NetworkFailure)
    assert p.healthy_key_count() == 0


def test_when_every_key_is_cooling_no_network_call_is_made():
    s = FakeSession(default=Resp(429, "{}"))
    p, s, clock = make(["k1", "k2"], session=s)
    with pytest.raises(RateLimited):
        p.fetch_quotes(["A"])
    n = len(s.calls)
    with pytest.raises(RateLimited, match="frees up"):
        p.fetch_quotes(["A", "B"])
    assert len(s.calls) == n                                       # nothing sent while all cool down
    clock.t += RATE_LIMIT_QUARANTINE_S + 1
    s.default = Resp(200)
    assert len(p.fetch_quotes(["A", "B"])) == 2


def test_5xx_is_rate_limited_but_does_not_quarantine_or_failover():
    s = FakeSession(by_key={"k1": Resp(503, "{}")})
    p, s, _ = make(["k1", "k2"], session=s)
    with pytest.raises(RateLimited, match="HTTP 503"):
        p.fetch_quotes(["A"])
    assert tokens(s) == ["k1"] and p.quarantined_until == {} and p.healthy_key_count() == 2


# ---- 401 / 403 -----------------------------------------------------------------------------
@pytest.mark.parametrize("code", [401, 403])
def test_rejected_key_is_skipped_and_request_retried(code):
    s = FakeSession(by_key={"k1": Resp(code, "{}")})
    p, s, _ = make(["k1", "k2", "k3"], session=s)
    assert [t.symbol for t in p.fetch_quotes(["A", "B", "C", "D"])] == ["A", "B", "C", "D"]
    assert tokens(s).count("k1") == 1                              # tried once, then skipped
    assert p.pool_status()[0]["state"] == "rejected"
    notes = p.pop_notices()
    assert len(notes) == 1 and "key #1" in notes[0] and "k1" not in notes[0].replace("key #1", "")
    assert p.pop_notices() == []                                   # each notice once


def test_all_keys_rejected_raises_provider_error_not_rate_limited():
    p, s, _ = make(["k1", "k2"], session=FakeSession(default=Resp(401, "{}")))
    with pytest.raises(ProviderError, match=r"API key rejected \(HTTP 401\)") as ei:
        p.fetch_quotes(["A"])
    assert not isinstance(ei.value, RateLimited)
    n = len(s.calls)
    with pytest.raises(ProviderError):                             # gated: no new network calls
        p.fetch_quotes(["A"])
    assert len(s.calls) == n


def test_mix_of_rejected_and_rate_limited_backs_off_instead_of_failing():
    s = FakeSession(by_key={"k1": Resp(401, "{}"), "k2": Resp(429, "{}")})
    p, _, _ = make(["k1", "k2"], session=s)
    with pytest.raises(RateLimited):
        p.fetch_quotes(["A"])


def test_rejected_key_is_probed_again_and_recovers():
    s = FakeSession(by_key={"k1": [Resp(403, "{}"), Resp(200)]})
    p, s, clock = make(["k1", "k2"], session=s)
    p.fetch_quotes(["A"])
    assert p.pool_status()[0]["state"] == "rejected"
    p.pop_notices()
    clock.t += REJECTED_RETRY_S + 1
    p.fetch_quotes(["B", "C"])
    assert p.pool_status()[0]["state"] == "ok"
    assert any("works again" in n for n in p.pop_notices())


def test_429_on_a_rejected_probe_proves_key_is_valid():
    s = FakeSession(by_key={"k1": [Resp(401, "{}"), Resp(429, "{}")]})
    p, s, clock = make(["k1", "k2"], session=s)
    p.fetch_quotes(["A"])
    clock.t += REJECTED_RETRY_S + 1
    p.fetch_quotes(["B", "C"])
    assert p.pool_status()[0]["state"] == "rate_limited"


# ---- preservations -------------------------------------------------------------------------
def test_zero_price_is_kept_not_malformed():
    p, _, _ = make(["k1", "k2"], session=FakeSession(default=Resp(200, ZERO)))
    (t,) = p.fetch_quotes(["XXXX"])
    assert t.symbol == "XXXX" and t.price == Decimal("0") and t.day_high is None and t.prev_close is None
    assert p.bad_symbols == 0


def test_negative_price_still_malformed_and_counted():
    p, _, _ = make(["k1", "k2"], session=FakeSession(default=Resp(200, '{"c": -1}')))
    assert p.fetch_quotes(["A"]) == [] and p.bad_symbols == 1


def test_network_failure_skips_symbol_and_does_not_penalise_key():
    s = FakeSession(by_key={"k1": ConnectionError("boom secret-text")})
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
