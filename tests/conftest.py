import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from engine import AlertEngine
from market_clock import Session
from models import Alert, PriceTick, StockConfig, TradeInfo

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)   # 11:00 ET, regular session


def tick(price, symbol="AAPL", t=T0, prev_close=None):
    return PriceTick(symbol, Decimal(str(price)), None if prev_close is None else Decimal(str(prev_close)),
                     None, None, None, None, t - timedelta(seconds=4), t, "mock")


def aapl(category="SELL", noisy=False, alerts=None):
    return StockConfig("AAPL", category, "Apple", noisy, TradeInfo(buy_price=Decimal("200.00"), shares=Decimal(10)),
                       alerts or [])


def ttwo(alerts=None):
    return StockConfig("TTWO", "BUY", "Take-Two", False, TradeInfo(buy_price=Decimal("202.73"), shares=Decimal(5)),
                       alerts or [])


def watch(alerts=None, symbol="TTWO"):
    return StockConfig(symbol, "WATCH", "", False, None, alerts or [])


def al(
    id_,
    operator="crossing_up",
    value=None,
    *,
    condition_mode="price",
    upper=None,
    lower=None,
    window_min=5,
    pct_base="auto",
    ref_price=None,
    trigger="rearm",
    cooldown=1,
    expiration="open_ended",
    expires_at=None,
    session_scope="all",
    name="",
    message="",
    prio="MEDIUM",
    custom_notification=None,
    enabled=True,
    label="WATCH",
):
    return Alert(
        id=id_,
        operator=operator,
        condition_mode=condition_mode,
        value=None if value is None else Decimal(str(value)),
        upper=None if upper is None else Decimal(str(upper)),
        lower=None if lower is None else Decimal(str(lower)),
        window_min=window_min,
        pct_base=pct_base,
        ref_price=None if ref_price is None else Decimal(str(ref_price)),
        trigger=trigger,
        cooldown_min=cooldown,
        expiration=expiration,
        expires_at=expires_at,
        session_scope=session_scope,
        name=name,
        message=message,
        priority=prio,
        custom_notification=custom_notification,
        enabled=enabled,
        label=label,
    )


def lvl(id_, typ, price=None, pct=None, mode="rearm", prio="MEDIUM", label="WATCH", cooldown=1, **kw):
    op_map = {
        "level_above": "greater_than",
        "level_below": "less_than",
        "cross_above": "crossing_up",
        "cross_below": "crossing_down",
        "pct_move_day": "moving_up_pct",
    }
    op = op_map.get(typ, typ)
    cmode = kw.pop("condition_mode", "pct" if (pct is not None and price is None and op not in ("moving_up_pct", "moving_down_pct")) else "price")
    val = price if price is not None else pct
    if typ == "pct_move_day" and "window_min" not in kw:
        kw["window_min"] = 1440
    return al(id_, operator=op, value=val, condition_mode=cmode, trigger=mode, cooldown=cooldown,
              prio=prio, label=label, **kw)


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
