from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from market_clock import ET, IST, MarketCalendar, Session

CAL = MarketCalendar()


def hhmm(dt):
    return dt.strftime("%H:%M")


def test_10_ist_session_times_around_dst_change():
    o, c = CAL.regular_window_ist(date(2026, 10, 30))        # Friday, still US daylight time
    assert (hhmm(o), hhmm(c), c.date()) == ("19:00", "01:30", date(2026, 10, 31))
    o, c = CAL.regular_window_ist(date(2026, 11, 2))         # first session after Nov 1 change
    assert (hhmm(o), hhmm(c), c.date()) == ("20:00", "02:30", date(2026, 11, 3))


def test_10_holidays_and_early_closes():
    for d in [(1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3), (9, 7), (11, 26), (12, 25)]:
        assert not CAL.is_trading_day(date(2026, *d)), d
    assert CAL.is_trading_day(date(2026, 11, 25))
    assert not CAL.is_trading_day(date(2026, 10, 31))        # Saturday
    for d in [(11, 27), (12, 24)]:
        o, c = CAL.regular_window(date(2026, *d))
        assert hhmm(c) == "13:00"
    assert CAL.session_at(datetime(2026, 11, 27, 13, 5, tzinfo=ET)) == Session.AFTER
    assert CAL.session_at(datetime(2026, 11, 27, 12, 59, tzinfo=ET)) == Session.REGULAR


def test_sessions():
    at = lambda h, m, d=(2026, 10, 14): datetime(*d, h, m, tzinfo=ET)
    assert CAL.session_at(at(9, 29)) == Session.PRE
    assert CAL.session_at(at(9, 30)) == Session.REGULAR
    assert CAL.session_at(at(15, 59)) == Session.REGULAR
    assert CAL.session_at(at(16, 0)) == Session.AFTER
    assert CAL.session_at(at(20, 0)) == Session.CLOSED
    assert CAL.session_at(at(3, 0)) == Session.CLOSED
    assert CAL.session_at(at(11, 0, (2026, 11, 26))) == Session.CLOSED     # Thanksgiving
    assert CAL.session_at(datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)) == Session.REGULAR


def test_session_in_utc_across_dst_change():
    # 14:30 UTC is 09:30 EST after the change, but only 10:30 EDT before it
    assert CAL.session_at(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)) == Session.REGULAR
    assert CAL.session_at(datetime(2026, 11, 2, 14, 29, tzinfo=timezone.utc)) == Session.PRE
    assert CAL.session_at(datetime(2026, 10, 30, 13, 29, tzinfo=timezone.utc)) == Session.PRE


def test_next_close_and_last_close():
    nxt = CAL.next_close(datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc))   # Wed after close
    assert nxt.date() == date(2026, 11, 27) and hhmm(nxt) == "13:00"           # skips Thanksgiving
    last = CAL.last_close_before(datetime(2026, 10, 12, 12, 0, tzinfo=ET))      # Monday morning
    assert last.date() == date(2026, 10, 9) and hhmm(last) == "16:00"
