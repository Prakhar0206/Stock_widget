import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from engine import AlertEngine
from market_clock import Session
from models import Alert, PriceTick, Position, PlannedEntry, StockConfig

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)   # 11:00 ET, regular session


def tick(price, symbol="AAPL", t=T0, prev_close=None):
    return PriceTick(symbol, Decimal(str(price)), None if prev_close is None else Decimal(str(prev_close)),
                     None, None, None, None, t - timedelta(seconds=4), t, "mock")


def aapl(category="SELL", noisy=False, alerts=None, zones=None):
    return StockConfig("AAPL", category, "Apple", noisy, Position(Decimal(10), Decimal("200.00")),
                       None, zones or {}, alerts or [])


def ttwo(alerts=None):
    return StockConfig("TTWO", "BUY", "Take-Two", False, None, PlannedEntry(Decimal("202.73"), Decimal(5)), {}, alerts or [])


def watch(alerts=None, symbol="TTWO"):
    return StockConfig(symbol, "WATCH", "", False, None, None, {}, alerts or [])


def lvl(id_, typ, price=None, pct=None, mode="rearm", prio="MEDIUM", label="WATCH", cooldown=30, **kw):
    return Alert(id=id_, type=typ, label=label, priority=prio, mode=mode, cooldown_min=cooldown,
                 price=None if price is None else Decimal(str(price)),
                 pct=None if pct is None else Decimal(str(pct)), **kw)


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def engine(cfg):
    return AlertEngine(cfg)


class Feed:
    """Feeds prices one minute apart, regular session."""
    def __init__(self, engine, stock, session=Session.REGULAR):
        self.engine, self.stock, self.session = engine, stock, session
        self.t = T0

    def push(self, price, prev_close=None):
        self.t += timedelta(minutes=1)
        return self.engine.process_tick(self.stock, tick(price, self.stock.symbol, self.t, prev_close),
                                        self.t, self.session)
