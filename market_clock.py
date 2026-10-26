"""US session clock from America/New_York (blueprint section 8.1, 8.2). Needs `tzdata` on Windows.

NYSE holidays and early closes are computed from rules (stdlib only), so the
calendar works for any year without annual edits. Rules implemented:

  * New Year's Day (Sun -> Mon). A Saturday Jan 1 is NOT observed on the prior
    Friday: NYSE stays open on Dec 31 in that case.
  * MLK Day (3rd Mon Jan), Presidents' Day (3rd Mon Feb), Good Friday,
    Memorial Day (last Mon May), Labor Day (1st Mon Sep), Thanksgiving (4th Thu Nov)
  * Juneteenth (from 2022), Independence Day, Christmas (Sat -> Fri, Sun -> Mon)
  * Early close 13:00 ET: day after Thanksgiving, Christmas Eve, and July 3
    when July 4 falls Tue-Fri (all only when they are normal trading days)
  * A few historic one-off closures (see ADHOC_CLOSURES). Pass `holidays=` /
    `early_closes=` to MarketCalendar to add future ad-hoc closures
    (e.g. a national day of mourning) without waiting for a code change.

Rules are accurate for 2022 onward; earlier years are best effort.
All datetimes passed in should be timezone-aware (naive values are interpreted
as system-local time by `astimezone`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from functools import lru_cache
from typing import FrozenSet, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
OPEN_T, CLOSE_T, EARLY_CLOSE_T = time(9, 30), time(16, 0), time(13, 0)
PRE_START_T, AFTER_END_T, AFTER_END_EARLY_T = time(4, 0), time(20, 0), time(17, 0)

# How many calendar days next_close / last_close_before scan (longest real gap is ~4 days).
_SEARCH_DAYS = 10
JUNETEENTH_FIRST_YEAR = 2022


class Session(str, Enum):
    REGULAR = "OPEN"
    PRE = "PRE"
    AFTER = "AFTER"
    CLOSED = "CLOSED"


# One-off full-day closures that no rule can predict.
ADHOC_CLOSURES = frozenset({
    date(2012, 10, 29), date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),                        # National Day of Mourning (G.H.W. Bush)
    date(2025, 1, 9),                         # National Day of Mourning (J. Carter)
})


# --------------------------------------------------------------------------- rules
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) given weekday (Mon=0) of a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Saturday -> preceding Friday, Sunday -> following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous / Meeus algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=None)
def holidays_for_year(year: int) -> FrozenSet[date]:
    """Rule-based NYSE full-day closures for `year` (plus known ad-hoc closures)."""
    days = set()
    jan1 = date(year, 1, 1)
    if jan1.weekday() != 5:  # Saturday New Year is not observed on Dec 31
        days.add(_observed(jan1))
    days.add(_nth_weekday(year, 1, 0, 3))                 # MLK Day
    days.add(_nth_weekday(year, 2, 0, 3))                 # Presidents' Day
    days.add(_easter(year) - timedelta(days=2))           # Good Friday
    days.add(_last_weekday(year, 5, 0))                   # Memorial Day
    if year >= JUNETEENTH_FIRST_YEAR:
        days.add(_observed(date(year, 6, 19)))            # Juneteenth
    days.add(_observed(date(year, 7, 4)))                 # Independence Day
    days.add(_nth_weekday(year, 9, 0, 1))                 # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))                # Thanksgiving
    days.add(_observed(date(year, 12, 25)))               # Christmas
    days.update(d for d in ADHOC_CLOSURES if d.year == year)
    return frozenset(days)


@lru_cache(maxsize=None)
def early_closes_for_year(year: int) -> FrozenSet[date]:
    """Rule-based 13:00 ET early-close days for `year` (never a holiday/weekend)."""
    hol = holidays_for_year(year)

    def trading(d: date) -> bool:
        return d.weekday() < 5 and d not in hol

    out = set()
    black_friday = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    if trading(black_friday):
        out.add(black_friday)
    xmas_eve = date(year, 12, 24)
    if trading(xmas_eve):
        out.add(xmas_eve)
    jul3 = date(year, 7, 3)
    if trading(jul3) and date(year, 7, 4).weekday() < 5:
        out.add(jul3)
    return frozenset(out)


# Back-compat constants (now derived from the rules, not hand-typed).
HOLIDAYS_2026 = holidays_for_year(2026)
EARLY_CLOSES_2026 = early_closes_for_year(2026)


# --------------------------------------------------------------------------- calendar
@dataclass
class MarketCalendar:
    """NYSE calendar. `holidays` / `early_closes` are OPTIONAL extra dates added on
    top of the rule-based ones (e.g. a surprise closure). `covered_years`, if set,
    restricts what `covers()` reports; by default every year is covered."""
    holidays: frozenset = frozenset()
    early_closes: frozenset = frozenset()
    covered_years: Optional[tuple] = None

    def covers(self, d: date) -> bool:
        return self.covered_years is None or d.year in self.covered_years

    def is_holiday(self, d: date) -> bool:
        return d in self.holidays or d in holidays_for_year(d.year)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and not self.is_holiday(d)

    def is_early_close(self, d: date) -> bool:
        return self.is_trading_day(d) and (
            d in self.early_closes or d in early_closes_for_year(d.year))

    def close_time(self, d: date) -> time:
        return EARLY_CLOSE_T if self.is_early_close(d) else CLOSE_T

    def regular_window(self, d: date) -> Optional[Tuple[datetime, datetime]]:
        """(open, close) as aware ET datetimes, or None when the market is closed all day."""
        if not self.is_trading_day(d):
            return None
        return (datetime.combine(d, OPEN_T, ET), datetime.combine(d, self.close_time(d), ET))

    def regular_window_ist(self, d: date) -> Optional[Tuple[datetime, datetime]]:
        w = self.regular_window(d)
        return None if w is None else (w[0].astimezone(IST), w[1].astimezone(IST))

    def session_at(self, when: datetime) -> Session:
        et = when.astimezone(ET)
        d, t = et.date(), et.time()
        if not self.is_trading_day(d):
            return Session.CLOSED
        close = self.close_time(d)
        if OPEN_T <= t < close:
            return Session.REGULAR
        if PRE_START_T <= t < OPEN_T:
            return Session.PRE
        after_end = AFTER_END_EARLY_T if self.is_early_close(d) else AFTER_END_T
        if close <= t < after_end:
            return Session.AFTER
        return Session.CLOSED

    def next_close(self, when: datetime) -> Optional[datetime]:
        """The first regular close strictly after `when` (aware ET), or None."""
        et = when.astimezone(ET)
        for i in range(_SEARCH_DAYS):
            w = self.regular_window(et.date() + timedelta(days=i))
            if w and w[1] > et:
                return w[1]
        return None

    def last_close_before(self, when: datetime) -> Optional[datetime]:
        """The most recent regular close at or before `when` (aware ET), or None."""
        et = when.astimezone(ET)
        for i in range(_SEARCH_DAYS):
            w = self.regular_window(et.date() - timedelta(days=i))
            if w and w[1] <= et:
                return w[1]
        return None


def fmt_ist(when: datetime) -> str:
    return when.astimezone(IST).strftime("%H:%M")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
