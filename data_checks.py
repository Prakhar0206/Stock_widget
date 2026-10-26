"""Price sanity filter, feed health and retry backoff (blueprint sections 8.5, 8.6)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

ACCEPTED, IGNORED, PAUSED = "accepted", "ignored", "paused"
JUMP_LIMIT = Decimal("0.25")


class PriceFilter:
    """Zero/empty prices are ignored. A reading >25% from the last trusted price pauses the stock;
    the next reading must be within 25% of the suspicious one to be accepted."""

    def __init__(self):
        self.last_trusted: Optional[Decimal] = None
        self.suspicious: Optional[Decimal] = None

    @property
    def paused(self) -> bool:
        return self.suspicious is not None

    def submit(self, price: Optional[Decimal]) -> str:
        if price is None or price <= 0:
            return IGNORED
        if self.last_trusted is None:
            self.last_trusted = price
            return ACCEPTED
        if self.suspicious is None:
            if abs(price - self.last_trusted) / self.last_trusted > JUMP_LIMIT:
                self.suspicious = price
                return PAUSED
        else:
            if abs(price - self.suspicious) / self.suspicious <= JUMP_LIMIT:
                self.suspicious = None
            else:
                self.suspicious = price      # still wild: stay paused, show a warning
                return PAUSED
        self.last_trusted = price
        return ACCEPTED


class FeedHealth:
    """Stale = no successful fetch for `stale_after` seconds in the regular session.
    `check()` returns "stale" once per outage and "recovered" once when data returns."""

    def __init__(self, stale_after_seconds: int = 180):
        self.stale_after = timedelta(seconds=stale_after_seconds)
        self.last_success: Optional[datetime] = None
        self.regular_since: Optional[datetime] = None
        self.stale = False

    def note_session(self, regular: bool, now: datetime) -> None:
        if regular and self.regular_since is None:
            self.regular_since = now
        elif not regular:
            self.regular_since = None
            if self.stale:
                self.stale = False

    def reset_clock(self, now: datetime) -> None:
        """After wake / unlock: measure staleness from now, so sleeping never counts as an outage."""
        if self.regular_since is not None:
            self.regular_since = now
        self.stale = False

    def record_success(self, now: datetime) -> None:
        self.last_success = now

    def check(self, now: datetime) -> Optional[str]:
        if self.regular_since is None:
            return None
        ref = max(t for t in (self.last_success, self.regular_since) if t is not None)
        is_stale = now - ref >= self.stale_after
        if is_stale and not self.stale:
            self.stale = True
            return "stale"
        if not is_stale and self.stale:
            self.stale = False
            return "recovered"
        return None


class Backoff:
    """HTTP 429 / 5xx: 2 s, doubling, capped at 5 minutes. Reset on success."""

    def __init__(self, start: float = 2.0, cap: float = 300.0):
        self.start, self.cap, self._next = start, cap, start

    def next_delay(self) -> float:
        d = self._next
        self._next = min(self._next * 2, self.cap)
        return d

    def reset(self) -> None:
        self._next = self.start
