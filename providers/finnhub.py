"""Finnhub REST provider (blueprint 8.4) with an optional pool of API keys.

The key goes in the X-Finnhub-Token header only: never in a URL, a log line or an error message.
Keys are named "key #1", "key #2", ... everywhere they are logged or shown.

Key pool (one key behaves exactly like the old single-key provider):
  * Round robin: every request takes the next healthy key, so N keys share the load evenly and the
    call budget is N x 48 calls/min.
  * HTTP 429: that key is quarantined for 60 s and the SAME request is retried on the next healthy
    key at once. Only when every key is cooling down does RateLimited reach the scheduler.
  * HTTP 401/403: that key is marked rejected and skipped; the request is retried on the others.
    A rejected key is probed again after 15 min, so a one-off 403 (for example a symbol the free
    plan cannot read) never switches a key off until restart.
  * HTTP 5xx: not the key's fault, so no quarantine and no failover; RateLimited as before.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from models import PriceTick
from providers.base import (MalformedResponse, NetworkFailure, ProviderError, QuoteProvider,
                            RateLimited)

log = logging.getLogger("saw.finnhub")
BASE = "https://finnhub.io/api/v1"
MIN_GAP_S = 0.1                      # requests at least 100 ms apart
RATE_LIMIT_QUARANTINE_S = 60.0       # a 429 key sits out one full rate-limit window
REJECTED_RETRY_S = 900.0             # a 401/403 key is probed again after 15 minutes
TOKEN_HEADER = "X-Finnhub-Token"
_KEY_SPLIT = re.compile(r"[,;\s]+")


def _dec(v) -> Optional[Decimal]:
    return None if v is None else Decimal(str(v))


def normalize_keys(api_key) -> list:
    """A string (one key, or several separated by commas) or a list/tuple of strings -> clean,
    de-duplicated, non-empty keys in the original order. Stray quotes and whitespace are trimmed."""
    if isinstance(api_key, str):
        items = [api_key]
    elif isinstance(api_key, (list, tuple)):
        items = list(api_key)
    else:
        items = []
    keys = []
    for item in items:
        if not isinstance(item, str):
            continue
        for part in _KEY_SPLIT.split(item):
            key = part.strip().strip('"\'').strip()
            if key and key not in keys:
                keys.append(key)
    return keys


class FinnhubProvider(QuoteProvider):
    name = "finnhub"

    def __init__(self, api_key: str | list[str], session=None, timeout: float = 8.0,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep_fn: Callable[[float], None] = time.sleep,
                 clock_fn: Callable[[], float] = time.monotonic):
        if session is None:
            import requests
            session = requests.Session()
        # no usable key means "keyless": no token header is sent and Finnhub answers 401
        keys = normalize_keys(api_key)
        self._s = session
        headers = {"Accept": "application/json"}
        if keys:
            headers[TOKEN_HEADER] = keys[0]
        self._s.headers.update(headers)
        self._timeout = timeout
        self._now, self._sleep, self._clock = now_fn, sleep_fn, clock_fn
        self._last_call = None
        # Serialises pacing + the HTTP call: the poller thread and run_call() worker threads
        # share this provider, its requests.Session and _last_call. Non-reentrant on purpose;
        # nothing inside the critical section calls back into _get().
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()    # guards bad_symbols (kept apart from the network lock)
        self.bad_symbols = 0          # malformed answers seen (for the error counter)
        self.network_errors = 0       # per-symbol transient network failures skipped in a batch

        # ---- key pool. _pool_lock guards everything below and is never held during I/O --------
        self._keys = keys
        self._slots = keys or [None]            # keyless mode: one anonymous slot, no token header
        self._multi = len(keys) > 1             # failover and quarantine gating only exist with 2+ keys
        self._pool_lock = threading.Lock()
        self._next = 0                          # round-robin pointer
        self._calls = [0] * len(self._slots)    # attempts per slot (diagnostics)
        self.quarantined_until: dict = {}       # key -> monotonic deadline after a 429
        self.rejected_until: dict = {}          # key -> monotonic deadline after a 401/403
        self._last_reject_code = 401
        self._notices: deque = deque(maxlen=20)  # human-readable pool events for the widget

    def __repr__(self) -> str:                  # never include a key
        return f"<FinnhubProvider keys={len(self._keys)}>"

    # ---- key pool --------------------------------------------------------------------------
    @property
    def key_count(self) -> int:
        return len(self._keys)

    def _usable(self, key, now: float) -> bool:           # caller holds _pool_lock
        return (self.quarantined_until.get(key, 0.0) <= now
                and self.rejected_until.get(key, 0.0) <= now)

    def healthy_key_count(self) -> int:
        now = self._clock()
        with self._pool_lock:
            return sum(1 for k in self._keys if self._usable(k, now))

    def pool_status(self) -> list:
        """One dict per key for the UI: label, state (ok / rate_limited / rejected), retry_in_s, calls."""
        now = self._clock()
        out = []
        with self._pool_lock:
            for i, k in enumerate(self._keys):
                rejected = self.rejected_until.get(k, 0.0) - now
                cooling = self.quarantined_until.get(k, 0.0) - now
                if rejected > 0:
                    state, wait = "rejected", rejected
                elif cooling > 0:
                    state, wait = "rate_limited", cooling
                else:
                    state, wait = "ok", 0.0
                out.append({"label": f"key #{i + 1}", "state": state,
                            "retry_in_s": round(wait, 1), "calls": self._calls[i]})
        return out

    def pop_notices(self) -> list:
        """Pool events since the last call (key rejected, key back). Each is returned once."""
        with self._pool_lock:
            out = list(self._notices)
            self._notices.clear()
        return out

    def _acquire(self, tried: set) -> Optional[int]:
        """Index of the next healthy slot in round-robin order, skipping those already tried by this
        request. None when nothing is left. With a single key the gate is off, so that key is
        always used once per request (old behaviour: the scheduler's own backoff handles 429)."""
        now = self._clock()
        with self._pool_lock:
            n = len(self._slots)
            for step in range(n):
                i = (self._next + step) % n
                if i in tried:
                    continue
                if self._multi and not self._usable(self._slots[i], now):
                    continue
                self._next = (i + 1) % n
                self._calls[i] += 1
                return i
        return None

    def _on_ok(self, idx: int) -> None:
        key = self._slots[idx]
        with self._pool_lock:
            was_rejected = self.rejected_until.pop(key, None) is not None
            self.quarantined_until.pop(key, None)
            if was_rejected and self._multi:
                self._notices.append(f"Finnhub key #{idx + 1} works again and is back in rotation.")
        if was_rejected:
            log.info("key #%d accepted again; back in rotation", idx + 1)

    def _on_rate_limited(self, idx: int) -> RateLimited:
        key = self._slots[idx]
        now = self._clock()
        with self._pool_lock:
            self.quarantined_until[key] = now + RATE_LIMIT_QUARANTINE_S
            self.rejected_until.pop(key, None)      # a 429 proves the key itself is valid
            healthy = sum(1 for k in self._keys if self._usable(k, now))
        log.warning("key #%d of %d rate limited (HTTP 429); quarantined %.0f s; %d healthy key(s) left",
                    idx + 1, len(self._slots), RATE_LIMIT_QUARANTINE_S, healthy)
        return RateLimited("HTTP 429")

    def _on_rejected(self, idx: int, code: int) -> ProviderError:
        key = self._slots[idx]
        now = self._clock()
        with self._pool_lock:
            self.rejected_until[key] = now + REJECTED_RETRY_S
            self.quarantined_until.pop(key, None)
            self._last_reject_code = code
            if self._multi:
                self._notices.append(f"Finnhub key #{idx + 1} was rejected (HTTP {code}) and is skipped.")
        log.warning("key #%d of %d rejected (HTTP %d); skipped, re-probe in %.0f min",
                    idx + 1, len(self._slots), code, REJECTED_RETRY_S / 60)
        return ProviderError(f"API key rejected (HTTP {code})")

    def _exhausted(self, last: Optional[ProviderError]) -> ProviderError:
        """The exception to raise when no key is left for this request."""
        if not self._multi:
            return last if last is not None else RateLimited("no usable key")
        now = self._clock()
        n = len(self._slots)
        with self._pool_lock:
            waits = [self.quarantined_until[k] - now for k in self._keys
                     if self.quarantined_until.get(k, 0.0) > now and self.rejected_until.get(k, 0.0) <= now]
            any_rejected = any(self.rejected_until.get(k, 0.0) > now for k in self._keys)
            code = self._last_reject_code
        if waits:      # a cooling key comes back by itself: tell the scheduler to back off
            return RateLimited(f"HTTP 429 on all {n} keys; the next frees up in {min(waits):.0f} s")
        if any_rejected:
            return ProviderError(f"API key rejected (HTTP {code}); all {n} keys in the pool were refused")
        return RateLimited("HTTP 429")          # state cleared by another thread meanwhile

    # ---- HTTP ------------------------------------------------------------------------------
    def _get(self, path: str, params: dict):
        # Per attempt, only the pacing check and the network call hold the lock. `with` releases
        # it on every exit path, including the NetworkFailure raised below. Status handling and
        # JSON parsing work on the already-downloaded response, so they run outside the lock.
        # A 429 or 401/403 moves on to the next key; each key is tried at most once per request.
        tried: set = set()
        last: Optional[ProviderError] = None
        while True:
            idx = self._acquire(tried)
            if idx is None:
                raise self._exhausted(last)
            tried.add(idx)
            key = self._slots[idx]
            with self._lock:
                if self._last_call is not None:                   # space requests >= 100 ms apart
                    wait = MIN_GAP_S - (self._clock() - self._last_call)
                    if wait > 0:
                        self._sleep(wait)
                if self._multi:                                   # one session, one header per call
                    self._s.headers.update({TOKEN_HEADER: key})
                self._last_call = self._clock()
                try:
                    r = self._s.get(BASE + path, params=params, timeout=self._timeout)
                except Exception as exc:                          # requests errors: never echo the text
                    raise NetworkFailure(type(exc).__name__)
            code = r.status_code
            if code == 429:
                last = self._on_rate_limited(idx)
                continue
            if code in (401, 403):
                last = self._on_rejected(idx, code)
                continue
            if code >= 500:                                       # server side: not this key's fault
                raise RateLimited(f"HTTP {code}")
            if code != 200:
                raise MalformedResponse(f"HTTP {code}")
            self._on_ok(idx)
            try:
                return json.loads(r.text, parse_float=Decimal)
            except Exception:
                raise MalformedResponse("not JSON")

    # ---- interface -------------------------------------------------------------------------
    def fetch_quotes(self, symbols: list) -> list:
        """One request per symbol. A bad or unreachable symbol is skipped so the rest of the batch
        survives. RateLimited aborts the loop (more requests would only worsen the 429) and carries
        the ticks gathered so far on `exc.partial_ticks`; with a key pool it is only raised once
        EVERY key is cooling down. ProviderError (401/403) propagates at once when no key is left:
        the keys are bad for every symbol."""
        ticks = []
        net_failures = 0
        last_net_exc = None
        for sym in symbols:
            try:
                ticks.append(self._parse_quote(sym, self._get("/quote", {"symbol": sym})))
            except MalformedResponse as exc:                  # discard, log, count; keep going
                with self._count_lock:                        # += is not atomic across threads
                    self.bad_symbols += 1
                log.warning("malformed quote for %s: %s", sym, exc)
            except NetworkFailure as exc:                     # transient: skip this symbol only
                net_failures += 1
                last_net_exc = exc
                with self._count_lock:
                    self.network_errors += 1
                log.warning("network failure for %s: %s", sym, exc)
            except RateLimited as exc:                        # back off, but hand over what we have
                exc.partial_ticks = list(ticks)               # snapshot; ticks themselves untouched
                raise
        if not ticks and net_failures and last_net_exc is not None:
            raise last_net_exc          # nothing got through: let the scheduler back off
        return ticks

    def _parse_quote(self, sym: str, d) -> PriceTick:
        c = d.get("c") if isinstance(d, dict) else None
        if isinstance(c, bool) or not isinstance(c, (int, Decimal)):
            raise MalformedResponse("missing c")
        try:                                                   # junk numbers must only cost this symbol
            price = _dec(c)
            pc = _dec(d.get("pc"))
            chg, pct = _dec(d.get("d")), _dec(d.get("dp"))
            hi, lo = _dec(d.get("h")) or None, _dec(d.get("l")) or None
        except (ArithmeticError, ValueError, TypeError):
            raise MalformedResponse("bad number")
        if not price.is_finite() or price < 0:                 # 0 is allowed: it means "unknown symbol / no data"
            raise MalformedResponse("bad price")               # and the PriceFilter downstream ignores it
        t = d.get("t")
        ptime = None
        if isinstance(t, (int, Decimal)) and not isinstance(t, bool) and t:
            try:
                ptime = datetime.fromtimestamp(int(t), timezone.utc)
            except (OverflowError, OSError, ValueError):
                ptime = None
        return PriceTick(sym, price, pc if pc else None, chg, pct, hi, lo, ptime, self._now(), "finnhub")

    def fetch_market_status(self) -> Optional[dict]:
        d = self._get("/stock/market-status", {"exchange": "US"})
        if not isinstance(d, dict) or "isOpen" not in d:
            raise MalformedResponse("market status")
        return {"isOpen": bool(d["isOpen"]), "holiday": d.get("holiday"), "session": d.get("session")}

    # ---- teardown --------------------------------------------------------------------------
    def close(self) -> None:
        """Close the HTTP session's socket pool. Safe to call twice or during shutdown; it does not
        take the request lock, so it never blocks the caller behind a request in flight."""
        try:
            self._s.close()
        except Exception as exc:
            log.info("session close failed: %s", type(exc).__name__)
