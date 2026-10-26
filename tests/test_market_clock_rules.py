from datetime import date, datetime, time
try:
    from market_clock import (
        ET, MarketCalendar, Session, holidays_for_year as H, early_closes_for_year as E,
        HOLIDAYS_2026, EARLY_CLOSES_2026)
except ImportError:
    from stock_alert_widget.market_clock import (
        ET, MarketCalendar, Session, holidays_for_year as H, early_closes_for_year as E,
        HOLIDAYS_2026, EARLY_CLOSES_2026)

D = date
OLD_2026 = frozenset(D(2026, m, d) for m, d in [
    (1,1),(1,19),(2,16),(4,3),(5,25),(6,19),(7,3),(9,7),(11,26),(12,25)])

def test_matches_old_hardcoded_2026():
    assert HOLIDAYS_2026 == OLD_2026
    assert EARLY_CLOSES_2026 == {D(2026,11,27), D(2026,12,24)}

def test_2025():
    assert H(2025) == {D(2025,1,1),D(2025,1,9),D(2025,1,20),D(2025,2,17),D(2025,4,18),
        D(2025,5,26),D(2025,6,19),D(2025,7,4),D(2025,9,1),D(2025,11,27),D(2025,12,25)}
    assert E(2025) == {D(2025,7,3),D(2025,11,28),D(2025,12,24)}

def test_2027_observation():
    h = H(2027)
    assert D(2027,6,18) in h and D(2027,7,5) in h and D(2027,12,24) in h
    assert D(2027,3,26) in h
    assert D(2027,12,24) not in E(2027)        # observed Christmas, no early close

def test_2028_saturday_new_year_not_observed():
    assert D(2027,12,31) not in H(2027) and D(2028,1,1) not in H(2028)
    assert D(2028,4,14) in H(2028)
    assert E(2028) == {D(2028,7,3),D(2028,11,24)}   # Dec 24 is a Sunday

def test_sunday_new_year():
    assert D(2023,1,2) in H(2023)

def test_far_future_and_sessions():
    c = MarketCalendar()
    assert not c.is_trading_day(D(2031,12,25))
    assert c.is_trading_day(D(2031,12,26))
    assert c.covers(D(2040,1,1))
    # Christmas 2030 closed; Black Friday 2030 closes 13:00
    assert c.session_at(datetime(2030,12,25,11,0,tzinfo=ET)) is Session.CLOSED
    assert c.close_time(D(2030,11,29)).hour == 13
    assert c.session_at(datetime(2030,11,29,14,0,tzinfo=ET)) is Session.AFTER
    assert c.session_at(datetime(2030,11,29,17,0,tzinfo=ET)) is Session.CLOSED

def test_extra_overrides_and_next_close():
    c = MarketCalendar(holidays=frozenset({D(2026,10,28)}))
    assert not c.is_trading_day(D(2026,10,28))
    w = c.next_close(datetime(2026,10,27,17,0,tzinfo=ET))
    assert w.date() == D(2026,10,29)
