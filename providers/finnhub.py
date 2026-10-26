"""Finnhub REST provider (blueprint 8.4) with 15s slot-based 4-key rotation, in-slot failover,
and per-key progressive backoff.

The key goes in the X-Finnhub-Token header only: never in a URL, a log line or an error message.
Keys are named "key #1", "key #2", ... everywhere they are logged or shown.

Key pool & Slot Rotation:
  * Slot rotation: In each 15s poll slot, ONE key is assigned to fetch all watched stocks.
    Next slot advances to the next healthy key after the one that last SUCCEEDED.
  * In-slot failover: If the assigned key fails partway through a batch (429, 401/403, 5xx, timeout),
    all prices received so far are preserved and the remaining stocks are immediately dispatched to
    the next healthy key in the same slot. Each key is tried at most once per slot.
  * Per-key 429 backoff: 60s on first 429, then 120s, then 240s on repeated 429s.
    Problem icon state for a key clears ONLY when that key's next attempt succeeds.
  * HTTP 401/403: Marked invalid (red), emits an immediate one-time notification event, and is only
    probed occasionally (every 15 min) or immediately if keys are updated.
  * Internet loss pre-check: Network connectivity loss is detected before blaming keys; offline
    failures never quarantine or mark keys as failed.
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
RATE_LIMIT_QUARANTINE_S = 60.0       # initial 429 cooldown (60s -> 120s -> 240s)
RATE_LIMIT_BACKOFFS_S = (60.0, 120.0, 240.0)
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
                 clock_fn: Callable[[], float] = time.monotonic,
                 internet_check_fn: Optional[Callable[[], bool]] = None):
        if session is None:
            import requests
            session = requests.Session()
        keys = normalize_keys(api_key)
        self._s = session
        headers = {"Accept": "application/json"}
        if keys:
            headers[TOKEN_HEADER] = keys[0]
        self._s.headers.update(headers)
        self._timeout = timeout
        self._now, self._sleep, self._clock = now_fn, sleep_fn, clock_fn
        self._internet_check_fn = internet_check_fn
        self._last_call = None
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        self.bad_symbols = 0          # malformed answers seen (for the error counter)
        self.network_errors = 0       # per-symbol transient network failures skipped in a batch

        # ---- key pool. _pool_lock guards everything below and is never held during I/O --------
        self._keys = keys
        self._slots = keys or [None]            # keyless mode: one anonymous slot, no token header
        self._multi = len(keys) > 1             # failover and quarantine gating only exist with 2+ keys
        self._pool_lock = threading.Lock()
        self._next = 0                          # slot pointer: next key to lead a slot
        self._last_succeeded_idx: Optional[int] = None
        self._calls = [0] * len(self._slots)    # attempts per slot (diagnostics)
        self.quarantined_until: dict = {}       # key -> monotonic deadline after a 429
        self.rejected_until: dict = {}          # key -> monotonic deadline after a 401/403
        self._rate_limit_strikes: dict = {}     # key -> consecutive 429 count (1->60s, 2->120s, 3+->240s)
        self._rate_limited_active: set = set()  # keys whose 429 icon stays until next attempt succeeds
        self._rejected_active: set = set()      # keys whose 401/403 icon stays until next attempt succeeds
        self._notified_invalid_keys: set = set()  # 1-based key numbers already notified once for 401/403
        self._invalid_key_events: deque = deque(maxlen=20)  # (key_num, code) for immediate notification
        self._server_down: bool = False         # True when 5xx encountered, cleared on next 200 OK
        self._offline: bool = False             # True when internet check or all-symbol network fails
        self._last_reject_code = 401
        self._notices: deque = deque(maxlen=20)

    def __repr__(self) -> str:                  # never include a key
        return f"<FinnhubProvider keys={len(self._keys)}>"

    # ---- key pool & status -----------------------------------------------------------------
    @property
    def key_count(self) -> int:
        return len(self._keys)

    def update_keys(self, api_key: str | list[str]) -> None:
        """Update keys at runtime and immediately clear cooldown/rejection states so updated keys
        are probed right away."""
        keys = normalize_keys(api_key)
        with self._pool_lock:
            self._keys = keys
            self._slots = keys or [None]
            self._multi = len(keys) > 1
            self._next = 0
            self._last_succeeded_idx = None
            self._calls = [0] * len(self._slots)
            self.quarantined_until.clear()
            self.rejected_until.clear()
            self._rate_limit_strikes.clear()
            self._rate_limited_active.clear()
            self._rejected_active.clear()
            self._notified_invalid_keys.clear()
            self._invalid_key_events.clear()
        with self._lock:
            if keys:
                self._s.headers[TOKEN_HEADER] = keys[0]
            else:
                self._s.headers.pop(TOKEN_HEADER, None)

    def _usable(self, key, now: float) -> bool:           # caller holds _pool_lock
        return (self.quarantined_until.get(key, 0.0) <= now
                and self.rejected_until.get(key, 0.0) <= now)

    def healthy_key_count(self) -> int:
        now = self._clock()
        with self._pool_lock:
            return sum(1 for k in self._keys if self._usable(k, now)
                       and k not in self._rate_limited_active
                       and k not in self._rejected_active)

    def pool_status(self) -> list:
        """One dict per key for the UI:
        - num: 1-based key index
        - label: 'key #N'
        - state: 'ok' | 'rate_limited' | 'rejected'
          Note: per spec 3.4, a problem icon disappears ONLY when that key's next attempt SUCCEEDS,
          not merely when its cooldown timer expires.
        - retry_in_s: seconds remaining on current cooldown/probe timer (0.0 if ready to retry)
        - calls: total HTTP calls made with this key
        """
        now = self._clock()
        out = []
        with self._pool_lock:
            for i, k in enumerate(self._keys):
                rejected_wait = max(0.0, self.rejected_until.get(k, 0.0) - now)
                cooling_wait = max(0.0, self.quarantined_until.get(k, 0.0) - now)
                if k in self._rejected_active or rejected_wait > 0:
                    state, wait = "rejected", rejected_wait
                elif k in self._rate_limited_active or cooling_wait > 0:
                    state, wait = "rate_limited", cooling_wait
                else:
                    state, wait = "ok", 0.0
                out.append({
                    "num": i + 1,
                    "label": f"key #{i + 1}",
                    "state": state,
                    "retry_in_s": round(wait, 1),
                    "calls": self._calls[i],
                })
        return out

    @property
    def server_down(self) -> bool:
        with self._pool_lock:
            return self._server_down

    @property
    def is_offline(self) -> bool:
        with self._pool_lock:
            return self._offline

    def pop_notices(self) -> list:
        """Pool events since the last call (key rejected, key back). Each is returned once."""
        with self._pool_lock:
            out = list(self._notices)
            self._notices.clear()
        return out

    def pop_invalid_key_events(self) -> list[tuple[int, int]]:
        """Returns list of (key_number_1_based, http_code) for keys newly marked 401/403.
        Each invalid key is emitted at most once per outage so the user receives a single immediate
        desktop notification."""
        with self._pool_lock:
            out = list(self._invalid_key_events)
            self._invalid_key_events.clear()
        return out

    def _acquire_slot_key(self, tried: set) -> Optional[int]:
        """Select the next healthy key for the current slot, starting from `self._next`,
        skipping keys already tried in this slot or currently in cooldown/rejected quarantine."""
        now = self._clock()
        with self._pool_lock:
            n = len(self._slots)
            for step in range(n):
                i = (self._next + step) % n
                if i in tried:
                    continue
                if self._multi and not self._usable(self._slots[i], now):
                    continue
                return i
        return None

    def _record_call(self, idx: int) -> None:
        with self._pool_lock:
            self._calls[idx] += 1

    def _on_ok(self, idx: int) -> None:
        key = self._slots[idx]
        with self._pool_lock:
            self._offline = False
            self._server_down = False
            was_rejected = (self.rejected_until.pop(key, None) is not None) or (key in self._rejected_active)
            self.quarantined_until.pop(key, None)
            self._rate_limit_strikes.pop(key, None)
            self._rate_limited_active.discard(key)
            self._rejected_active.discard(key)
            self._notified_invalid_keys.discard(idx + 1)
            if was_rejected and self._multi:
                self._notices.append(f"Finnhub key #{idx + 1} works again and is back in rotation.")
        if was_rejected:
            log.info("key #%d accepted again; back in rotation", idx + 1)

    def _on_slot_key_succeeded(self, idx: int) -> None:
        """Rotation advances to the NEXT key after the one that last SUCCEEDED."""
        with self._pool_lock:
            n = len(self._slots)
            self._last_succeeded_idx = idx
            self._next = (idx + 1) % n

    def _on_rate_limited(self, idx: int) -> RateLimited:
        key = self._slots[idx]
        now = self._clock()
        with self._pool_lock:
            strikes = self._rate_limit_strikes.get(key, 0) + 1
            self._rate_limit_strikes[key] = strikes
            cooldown = RATE_LIMIT_BACKOFFS_S[min(strikes - 1, len(RATE_LIMIT_BACKOFFS_S) - 1)]
            self.quarantined_until[key] = now + cooldown
            self._rate_limited_active.add(key)
            # A 429 proves the key itself is valid (not 401/403)
            self.rejected_until.pop(key, None)
            self._rejected_active.discard(key)
            self._notified_invalid_keys.discard(idx + 1)
            if self._next == idx and len(self._slots) > 1:
                self._next = (idx + 1) % len(self._slots)
            healthy = sum(1 for k in self._keys if self._usable(k, now))
        log.warning("key #%d of %d rate limited (HTTP 429, strike #%d); quarantined %.0f s; %d healthy key(s) left",
                    idx + 1, len(self._slots), strikes, cooldown, healthy)
        return RateLimited(f"HTTP 429 on key #{idx + 1}")

    def _on_rejected(self, idx: int, code: int) -> ProviderError:
        key = self._slots[idx]
        now = self._clock()
        key_num = idx + 1
        with self._pool_lock:
            self.rejected_until[key] = now + REJECTED_RETRY_S
            self._rejected_active.add(key)
            self.quarantined_until.pop(key, None)
            self._rate_limited_active.discard(key)
            self._rate_limit_strikes.pop(key, None)
            self._last_reject_code = code
            if self._next == idx and len(self._slots) > 1:
                self._next = (idx + 1) % len(self._slots)
            if key_num not in self._notified_invalid_keys:
                self._notified_invalid_keys.add(key_num)
                self._invalid_key_events.append((key_num, code))
            if self._multi:
                self._notices.append(f"Finnhub key #{key_num} was rejected (HTTP {code}) and is skipped.")
        log.warning("key #%d of %d rejected (HTTP %d); skipped, re-probe in %.0f min",
                    key_num, len(self._slots), code, REJECTED_RETRY_S / 60)
        return ProviderError(f"API key rejected (HTTP {code})")

    def _on_server_error(self, idx: int, code: int) -> RateLimited:
        with self._pool_lock:
            self._server_down = True
            if self._next == idx and len(self._slots) > 1:
                self._next = (idx + 1) % len(self._slots)
        log.warning("Finnhub server error (HTTP %d) on key #%d", code, idx + 1)
        return RateLimited(f"HTTP {code}")

    def _exhausted(self, last: Optional[Exception]) -> Exception:
        """The exception to raise when no key is left for this slot."""
        if not self._multi:
            return last if last is not None else RateLimited("no usable key")
        if isinstance(last, NetworkFailure):
            return last
        now = self._clock()
        n = len(self._slots)
        with self._pool_lock:
            waits = [self.quarantined_until[k] - now for k in self._keys
                     if self.quarantined_until.get(k, 0.0) > now and self.rejected_until.get(k, 0.0) <= now]
            any_rejected = any(self.rejected_until.get(k, 0.0) > now or k in self._rejected_active
                               for k in self._keys)
            server_down = self._server_down
            code = self._last_reject_code
        if waits:
            return RateLimited(f"HTTP 429 on all {n} keys; the next frees up in {min(waits):.0f} s")
        if server_down and isinstance(last, RateLimited):
            return last
        if any_rejected:
            return ProviderError(f"API key rejected (HTTP {code}); all {n} keys in the pool were refused")
        if last is not None:
            return last
        return RateLimited("HTTP 429")

    # ---- connectivity pre-check & HTTP -----------------------------------------------------
    def _check_internet(self) -> None:
        """Detect network/internet connectivity failure BEFORE assigning blame to keys."""
        if self._internet_check_fn is not None:
            try:
                online = bool(self._internet_check_fn())
            except Exception:
                online = False
            if not online:
                with self._pool_lock:
                    self._offline = True
                raise NetworkFailure("No internet connection")

    def _request_with_key(self, idx: int, path: str, params: dict):
        """Perform a single HTTP GET using slot `idx`. Raises NetworkFailure, RateLimited,
        ProviderError, or MalformedResponse."""
        self._check_internet()
        key = self._slots[idx]
        self._record_call(idx)
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
            raise self._on_rate_limited(idx)
        if code in (401, 403):
            raise self._on_rejected(idx, code)
        if code >= 500:
            raise self._on_server_error(idx, code)
        if code != 200:
            raise MalformedResponse(f"HTTP {code}")
        self._on_ok(idx)
        try:
            return json.loads(r.text, parse_float=Decimal)
        except Exception:
            raise MalformedResponse("not JSON")

    def _get(self, path: str, params: dict):
        """Single-endpoint call (e.g. market status) with failover across available keys."""
        tried: set = set()
        last: Optional[Exception] = None
        while True:
            idx = self._acquire_slot_key(tried)
            if idx is None:
                raise self._exhausted(last)
            tried.add(idx)
            try:
                data = self._request_with_key(idx, path, params)
                self._on_slot_key_succeeded(idx)
                return data
            except (RateLimited, ProviderError) as exc:
                last = exc
                if not self._multi:
                    raise
                # For 5xx when single-call, if all keys aren't tried we can either fail over or stop;
                # trying next key at most once per slot satisfies section 2.2.
                continue

    # ---- interface -------------------------------------------------------------------------
    def fetch_quotes(self, symbols: list) -> list:
        """In each 15s slot, ONE key is assigned to fetch all watched stocks.
        If that key fails partway through the batch (HTTP 429, 401/403, 5xx, or timeout),
        KEEP all prices received so far and immediately dispatch the remaining stocks to the
        next available healthy key in the same slot.
        Try each key at most once per slot. Never enter an infinite retry loop."""
        symbols = list(symbols)
        if not symbols:
            return []

        self._check_internet()

        ticks: list[PriceTick] = []
        tried_keys: set[int] = set()
        last_exc: Optional[Exception] = None
        net_failures = 0
        last_net_exc: Optional[NetworkFailure] = None

        sym_idx = 0
        current_key_idx = self._acquire_slot_key(tried_keys)
        if current_key_idx is None:
            raise self._exhausted(last_exc)
        tried_keys.add(current_key_idx)
        key_had_success = False

        while sym_idx < len(symbols):
            sym = symbols[sym_idx]
            try:
                raw = self._request_with_key(current_key_idx, "/quote", {"symbol": sym})
                key_had_success = True
                ticks.append(self._parse_quote(sym, raw))
                sym_idx += 1
            except MalformedResponse as exc:
                # Bad payload for one symbol: discard, count, continue with the same key
                with self._count_lock:
                    self.bad_symbols += 1
                log.warning("malformed quote for %s: %s", sym, exc)
                sym_idx += 1
            except NetworkFailure as exc:
                net_failures += 1
                last_net_exc = exc
                with self._count_lock:
                    self.network_errors += 1
                log.warning("network failure for %s: %s", sym, exc)
                # Check if internet is completely down via pre-check hook if provided
                if self._internet_check_fn is not None:
                    try:
                        if not self._internet_check_fn():
                            with self._pool_lock:
                                self._offline = True
                            exc.partial_ticks = list(ticks)
                            raise exc
                    except NetworkFailure:
                        raise
                    except Exception:
                        pass
                # Transient per-symbol timeout/connection error: skip symbol without blaming the key
                sym_idx += 1
            except (RateLimited, ProviderError) as exc:
                last_exc = exc
                # Single-key mode: abort batch immediately (or on 5xx) carrying partial_ticks
                if not self._multi:
                    if isinstance(exc, RateLimited):
                        exc.partial_ticks = list(ticks)
                    raise
                # Multi-key mode: fail over remaining symbols (starting with current `sym`)
                # to the next healthy key in the same 15s slot
                next_key_idx = self._acquire_slot_key(tried_keys)
                if next_key_idx is None:
                    final_exc = self._exhausted(last_exc)
                    if isinstance(final_exc, RateLimited):
                        final_exc.partial_ticks = list(ticks)
                    elif ticks:
                        # Attach partial_ticks even on ProviderError so partial batch data is never lost
                        setattr(final_exc, "partial_ticks", list(ticks))
                    raise final_exc
                tried_keys.add(next_key_idx)
                current_key_idx = next_key_idx
                key_had_success = False

        if not ticks and net_failures and last_net_exc is not None:
            with self._pool_lock:
                self._offline = True
            raise last_net_exc

        if key_had_success and current_key_idx is not None:
            self._on_slot_key_succeeded(current_key_idx)

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