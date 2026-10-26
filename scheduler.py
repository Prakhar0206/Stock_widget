"""Polling (blueprint 8.3, 8.6). All network calls run here, on a worker thread. The main thread
only drains the queue. `poll_once()` is synchronous so tests can drive it."""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from datetime import datetime
from typing import Callable

from config import BudgetError, Config, check_budget
from data_checks import Backoff
from market_clock import MarketCalendar, Session, utcnow
from providers.base import MalformedResponse, NetworkFailure, ProviderError, QuoteProvider, RateLimited

log = logging.getLogger("saw.scheduler")
CLOSED_POLL_S = 900            # outside the regular session: every 15 minutes, display only


def next_delay(regular: bool, now_epoch: float, poll_seconds: int, jitter: float) -> float:
    """Regular session: aligned to the poll boundary (15 s slots or 60 s) plus jitter."""
    if not regular:
        return float(CLOSED_POLL_S)
    d = poll_seconds - (now_epoch % poll_seconds)
    if d < min(5, poll_seconds / 3):
        d += poll_seconds
    return d + jitter


class Scheduler(threading.Thread):
    """Puts messages on `out`:
       ("ticks", [PriceTick], now)
       ("status", dict, now)
       ("error", kind, text, now)          kind: rate | network | malformed | auth | budget | server
       ("invalid_key", key_num, code, now) emitted once per newly rejected key (HTTP 401/403)
       ("call", callback, result, exception)   from run_call()"""

    def __init__(self, provider: QuoteProvider, get_symbols: Callable[[], list], out: queue.Queue,
                 cfg: Config, calendar: MarketCalendar = None, now_fn=utcnow,
                 jitter_fn=lambda: random.uniform(0, 2.0), epoch_fn=time.time):
        super().__init__(daemon=True, name="poller")
        self.provider, self.get_symbols, self.out, self.cfg = provider, get_symbols, out, cfg
        self.calendar = calendar or MarketCalendar()
        self.now_fn, self.jitter_fn, self.epoch_fn = now_fn, jitter_fn, epoch_fn
        self.backoff = Backoff()
        self._wake = threading.Event()
        self._stop_evt = threading.Event()   # NOT `_stop`: that name is Thread's own internal method
        self.provider_session = None            # set by the controller when the provider says otherwise
        self._status_poll_counter = 0

    def request_now(self):
        """Wake, unlock, stock added: poll immediately."""
        self._wake.set()

    def stop(self):
        self._stop_evt.set()
        self._wake.set()

    def close(self):
        """Stop polling and release the provider's HTTP session. Idempotent; never raises."""
        self.stop()
        closer = getattr(self.provider, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as exc:
                log.info("provider close failed: %s", type(exc).__name__)

    def run(self):
        while not self._stop_evt.is_set():
            try:
                delay = self.poll_once()
            except Exception:                   # e.g. get_symbols() or the calendar failed: keep polling
                log.exception("poll cycle failed")
                delay = float(self.cfg.poll_seconds)
            self._wake.wait(timeout=delay)
            self._wake.clear()

    def run_call(self, fn, callback):
        """Run any network call off the main thread (e.g. checking a ticker); result comes back
        on the queue as ("call", callback, result, exception)."""
        def work():
            try:
                self.out.put(("call", callback, fn(), None))
            except Exception as exc:
                self.out.put(("call", callback, None, exc))
        threading.Thread(target=work, daemon=True, name="call").start()

    def _emit_invalid_keys(self, now: datetime) -> None:
        pop_fn = getattr(self.provider, "pop_invalid_key_events", None)
        if callable(pop_fn):
            for key_num, code in pop_fn():
                self.out.put(("invalid_key", key_num, code, now))

    def _emit_partial(self, exc: Exception, now: datetime) -> None:
        partial = getattr(exc, "partial_ticks", None)
        if partial:
            self.out.put(("ticks", partial, now))

    def poll_once(self) -> float:
        now = self.now_fn()
        session = self.provider_session or self.calendar.session_at(now)
        regular = session == Session.REGULAR
        nominal = next_delay(regular, self.epoch_fn(), self.cfg.poll_seconds, self.jitter_fn())
        symbols = list(self.get_symbols())
        if not symbols:
            return nominal
        num_keys = max(1, getattr(self.provider, "key_count", 1) or 1)
        try:
            check_budget(len(symbols), self.cfg.poll_seconds if regular else CLOSED_POLL_S, num_keys=num_keys)
        except BudgetError as exc:
            self.out.put(("error", "budget", str(exc), now))
            return nominal
        try:
            ticks = self.provider.fetch_quotes(symbols)
            self._emit_invalid_keys(now)
            self.out.put(("ticks", ticks, self.now_fn()))
            self.backoff.reset()
        except RateLimited as exc:
            self._emit_invalid_keys(now)
            self._emit_partial(exc, self.now_fn())
            text = str(exc)
            kind = "server" if ("HTTP 5" in text or getattr(self.provider, "server_down", False)) else "rate"
            self.out.put(("error", kind, text, now))
            if not regular:
                # Closed market: if any healthy key remains, retry immediately; else wait
                healthy_fn = getattr(self.provider, "healthy_key_count", None)
                if callable(healthy_fn) and healthy_fn() > 0:
                    return 0.0
                return nominal
            # Multi-key slot rotation pauses until the next 15s slot when all keys fail;
            # single-key retains exponential backoff compatibility.
            if num_keys > 1:
                return nominal
            return self.backoff.next_delay()
        except NetworkFailure as exc:
            self._emit_partial(exc, self.now_fn())
            d = min(self.backoff.next_delay(), nominal)
            self.out.put(("error", "network", str(exc), now))
            return d
        except MalformedResponse as exc:
            self._emit_invalid_keys(now)
            self.out.put(("error", "malformed", str(exc), now))
            return nominal
        except ProviderError as exc:
            self._emit_invalid_keys(now)
            self._emit_partial(exc, self.now_fn())
            self.out.put(("error", "auth", str(exc), now))
            if not regular:
                healthy_fn = getattr(self.provider, "healthy_key_count", None)
                if callable(healthy_fn) and healthy_fn() > 0:
                    return 0.0
            if num_keys > 1:
                return nominal
            return max(nominal, 300.0)          # do not hammer with a rejected key
        except Exception as exc:                # never let the worker die
            log.exception("unexpected poll failure")
            self.out.put(("error", "malformed", type(exc).__name__, now))
            return nominal
        if self.provider_session is None:
            try:                                    # market status rides along with polls
                st = self.provider.fetch_market_status()
                if st is not None:
                    self.out.put(("status", st, self.now_fn()))
            except Exception as exc:
                log.info("market status unavailable: %s", type(exc).__name__)
        return nominal