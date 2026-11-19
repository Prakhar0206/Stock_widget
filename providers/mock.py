"""Scripted mock provider for tests and the soak run: gaps, flat lines, zero prices, bad ticks, 429s."""
from __future__ import annotations

import math
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from models import PriceTick
from providers.base import NetworkFailure, QuoteProvider, RateLimited, MalformedResponse

ZERO = "zero"            # provider answers c = 0
RATE_LIMIT = "429"       # raises RateLimited
NETWORK = "network"      # raises NetworkFailure
MALFORMED = "malformed"  # raises MalformedResponse


def gap(before, after, n_before=3, n_after=3):
    return [before] * n_before + [after] * n_after


class MockProvider(QuoteProvider):
    """paths: {symbol: [step, ...]}. A step is a price, or one of ZERO/RATE_LIMIT/NETWORK/MALFORMED.
    Call advance() once per poll. The last step repeats when a path runs out."""
    name = "mock"

    def __init__(self, paths: dict, prev_close: Optional[dict] = None,
                 now_fn: Optional[Callable[[], datetime]] = None, data_age_s: int = 2):
        self.paths = {k.upper(): list(v) for k, v in paths.items()}
        self.prev_close = {k.upper(): Decimal(str(v)) for k, v in (prev_close or {}).items()}
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.data_age_s = data_age_s
        self.i = 0

    def advance(self, n: int = 1):
        self.i += n

    def _step(self, sym):
        p = self.paths[sym]
        return p[min(self.i, len(p) - 1)]

    def fetch_quotes(self, symbols):
        now = self.now_fn()
        ticks = []
        for sym in symbols:
            sym = sym.upper()
            step = ZERO if sym not in self.paths else self._step(sym)
            if step == RATE_LIMIT:
                raise RateLimited("HTTP 429")
            if step == NETWORK:
                raise NetworkFailure("offline")
            if step == MALFORMED:
                raise MalformedResponse("bad body")
            price = Decimal(0) if step == ZERO else Decimal(str(step))
            pc = self.prev_close.get(sym)
            chg = None if pc is None or price == 0 else price - pc
            pct = None if chg is None else chg / pc * 100
            ticks.append(PriceTick(sym, price, pc, chg, pct, None, None,
                                   now - timedelta(seconds=self.data_age_s), now, "mock"))
        return ticks


# ---- demo mode (`python main.py --mock`): scripted prices that sweep across alert levels ------------
# (base price, swing in percent, period in seconds, phase in radians)
DEMO_SPECS = {"AAPL": (201.50, 5.5, 170, 0.0), "TTWO": (205.00, 6.0, 210, 1.3), "MSFT": (418.00, 4.0, 150, 2.1),
              "NVDA": (139.80, 3.5, 190, 0.7), "VST": (129.40, 5.0, 130, 3.0)}

DEMO_WATCHLIST = {
    "version": 1,
    "symbols": [
        {
            "symbol": "AAPL",
            "nickname": "Apple (demo)",
            "category": "SELL",
            "noisy": False,
            "trade": {"buy_price": 200.00, "shares": 10},
            "alerts": [
                {
                    "id": "aapl-target",
                    "operator": "crossing_up",
                    "condition_mode": "price",
                    "value": 208.00,
                    "trigger": "rearm",
                    "expiration": "open_ended",
                    "session_scope": "all",
                    "label": "SELL",
                    "priority": "HIGH",
                    "name": "Target Hit",
                    "message": "AAPL crossed above $208.00",
                    "enabled": True,
                },
                {
                    "id": "aapl-chan",
                    "operator": "entering_channel",
                    "condition_mode": "pct",
                    "lower": -1.5,
                    "upper": 1.5,
                    "trigger": "rearm",
                    "expiration": "open_ended",
                    "session_scope": "all",
                    "label": "WATCH",
                    "priority": "LOW",
                    "name": "Near Buy Price",
                    "enabled": True,
                },
            ],
        },
        {
            "symbol": "TTWO",
            "nickname": "Take-Two (demo)",
            "category": "BUY",
            "noisy": False,
            "trade": {"buy_price": 202.73, "shares": 5},
            "alerts": [
                {
                    "id": "ttwo-danger",
                    "operator": "less_than",
                    "condition_mode": "price",
                    "value": 197.00,
                    "trigger": "every_time",
                    "cooldown_min": 2,
                    "expiration": "open_ended",
                    "session_scope": "all",
                    "label": "RISK",
                    "priority": "HIGH",
                    "name": "Dip Alert",
                    "enabled": True,
                }
            ],
        },
        {
            "symbol": "MSFT",
            "nickname": "Microsoft (demo)",
            "category": "HOLD",
            "noisy": False,
            "trade": {"buy_price": 410.00, "shares": 3},
            "alerts": [],
        },
        {
            "symbol": "NVDA",
            "nickname": "Nvidia (demo)",
            "category": "WATCH",
            "noisy": False,
            "alerts": [
                {
                    "id": "nvda-up",
                    "operator": "crossing_up",
                    "condition_mode": "price",
                    "value": 142.00,
                    "trigger": "rearm",
                    "expiration": "open_ended",
                    "session_scope": "all",
                    "label": "WATCH",
                    "priority": "MEDIUM",
                    "name": "Breakout",
                    "enabled": True,
                },
                {
                    "id": "nvda-down",
                    "operator": "crossing_down",
                    "condition_mode": "price",
                    "value": 136.00,
                    "trigger": "rearm",
                    "expiration": "open_ended",
                    "session_scope": "all",
                    "label": "RISK",
                    "priority": "MEDIUM",
                    "name": "Dip",
                    "enabled": True,
                },
            ],
        },
        {
            "symbol": "VST",
            "nickname": "Vistra (demo)",
            "category": "SELL",
            "noisy": True,
            "trade": {"buy_price": 125.00, "shares": 2},
            "alerts": [
                {
                    "id": "vst-move",
                    "operator": "moving_up_pct",
                    "condition_mode": "pct",
                    "value": 3.0,
                    "window_min": 5,
                    "trigger": "once",
                    "expiration": "open_ended",
                    "session_scope": "all",
                    "label": "SELL",
                    "priority": "MEDIUM",
                    "name": "Spike",
                    "enabled": True,
                }
            ],
        },
    ],
}


class DemoProvider(QuoteProvider):
    """Deterministic fake market for trying the widget without an API key: each price is a sine wave
    around a base, so zones, alerts, pop-ups and sounds all trigger within a few minutes.
    Any ticker works (its wave is derived from the ticker); 'NOPE' answers with a zero price so the
    'unknown ticker' check can be tried."""
    name = "mock"

    def __init__(self, now_fn: Optional[Callable[[], datetime]] = None, specs: Optional[dict] = None):
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.specs = dict(specs or DEMO_SPECS)

    def _spec(self, sym: str):
        if sym in self.specs:
            return self.specs[sym]
        h = zlib.crc32(sym.encode())
        return (40.0 + h % 360, 3.0 + (h >> 8) % 4, 120 + (h >> 12) % 120, (h % 628) / 100)

    def fetch_quotes(self, symbols):
        now = self.now_fn()
        t = now.timestamp()
        ticks = []
        for sym in symbols:
            sym = sym.upper()
            if sym == "NOPE":
                price = Decimal(0)
                base = None
                pre_price = reg_price = ah_price = None
            else:
                base_f, amp, period, phase = self._spec(sym)
                base = Decimal(str(round(base_f, 2)))
                price = Decimal(str(round(base_f * (1 + amp / 100 * math.sin(2 * math.pi * t / period + phase)), 2)))
                pre_price = Decimal(str(round(base_f * 0.996, 2)))
                reg_price = price
                ah_price = Decimal(str(round(base_f * 1.003, 2)))
            chg = None if base is None else price - base
            pct = None if base is None else chg / base * 100
            ticks.append(PriceTick(sym, price, base, chg, pct, None, None,
                                   now - timedelta(seconds=1), now, "mock",
                                   pre_price=pre_price, reg_price=reg_price, ah_price=ah_price))
        return ticks
