from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dc

from data_checks import ACCEPTED, IGNORED, PAUSED, Backoff, FeedHealth, PriceFilter
from providers.base import NetworkFailure, RateLimited
from providers.mock import MockProvider, NETWORK, RATE_LIMIT, ZERO

import pytest

T = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


def test_11_zero_price_ignored_last_good_kept():
    f = PriceFilter()
    assert f.submit(Dc(100)) == ACCEPTED
    assert f.submit(Dc(0)) == IGNORED and f.submit(None) == IGNORED
    assert f.last_trusted == Dc(100) and not f.paused


def test_11_jump_filter():
    f = PriceFilter()
    f.submit(Dc(100))
    assert f.submit(Dc(125)) == ACCEPTED                    # exactly 25% is fine
    assert f.submit(Dc(160)) == PAUSED and f.paused         # >25%
    assert f.submit(Dc(300)) == PAUSED and f.paused         # not within 25% of 160: stays paused
    assert f.submit(Dc(310)) == ACCEPTED and not f.paused   # within 25% of the suspicious 300
    assert f.last_trusted == Dc(310)


def test_11_bad_tick_then_recovery_to_old_level():
    f = PriceFilter()
    f.submit(Dc(100))
    assert f.submit(Dc(10)) == PAUSED
    assert f.submit(Dc(100)) == PAUSED                      # not within 25% of the bad tick: warning stays
    assert f.paused


def test_11_stale_feed_one_alert_per_outage():
    h = FeedHealth(180)
    h.note_session(True, T)
    h.record_success(T)
    assert h.check(T + timedelta(seconds=179)) is None
    assert h.check(T + timedelta(seconds=180)) == "stale"
    assert h.check(T + timedelta(seconds=300)) is None      # only one per outage
    h.record_success(T + timedelta(seconds=310))
    assert h.check(T + timedelta(seconds=311)) == "recovered"
    assert h.check(T + timedelta(seconds=600)) == "stale"   # a new outage fires again


def test_stale_clock_does_not_count_closed_time():
    h = FeedHealth(180)
    h.note_session(False, T)
    h.record_success(T)
    h.note_session(True, T + timedelta(hours=18))           # market opens; last fetch 18 h ago
    assert h.check(T + timedelta(hours=18, seconds=5)) is None


def test_11_429_backoff_timing():
    b = Backoff()
    assert [b.next_delay() for _ in range(10)] == [2, 4, 8, 16, 32, 64, 128, 256, 300, 300]
    b.reset()
    assert b.next_delay() == 2


def test_mock_provider_scripts():
    mp = MockProvider({"AAPL": [100, ZERO, RATE_LIMIT, NETWORK, 101]})
    assert mp.fetch_quotes(["AAPL"])[0].price == 100
    mp.advance(); assert mp.fetch_quotes(["AAPL"])[0].price == 0
    mp.advance()
    with pytest.raises(RateLimited):
        mp.fetch_quotes(["AAPL"])
    mp.advance()
    with pytest.raises(NetworkFailure):
        mp.fetch_quotes(["AAPL"])
    mp.advance(); assert mp.fetch_quotes(["AAPL"])[0].price == 101
    assert mp.fetch_quotes(["NOPE"])[0].price == 0           # unknown symbol: c = 0
