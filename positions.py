"""Money math (blueprint section 4 / 4.1). Decimal only, ROUND_HALF_UP to cents."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from typing import Optional

CENT = Decimal("0.01")
ZERO = Decimal(0)


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def q2(x) -> Decimal:
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FeeRates:
    buy_rate: Decimal = Decimal("0.0033")
    buy_min: Decimal = Decimal("0.02")
    sell_rate: Decimal = Decimal("0.0037")
    sell_min: Decimal = Decimal("0.05")


DEFAULT_RATES = FeeRates()


def order_value(shares, price) -> Decimal:
    return q2(D(shares) * D(price))


def buy_fee(order_val, rates: FeeRates = DEFAULT_RATES) -> Decimal:
    """Projected buy fee = MAX(min, ROUND(order value x rate, 2))."""
    return max(rates.buy_min, q2(D(order_val) * rates.buy_rate))


def sell_fee(gross, rates: FeeRates = DEFAULT_RATES) -> Decimal:
    """Projected sell fee = MAX(min, ROUND(gross proceeds x rate, 2))."""
    return max(rates.sell_min, q2(D(gross) * rates.sell_rate))


def total_cost_basis(shares, avg_price, actual_buy_fee: Optional[Decimal] = None,
                     rates: FeeRates = DEFAULT_RATES) -> Decimal:
    """Order value + buy fee. A ledger (actual) fee replaces the projection."""
    ov = order_value(shares, avg_price)
    fee = q2(actual_buy_fee) if actual_buy_fee is not None else buy_fee(ov, rates)
    return ov + fee


def net_profit(shares, basis, price, rates: FeeRates = DEFAULT_RATES) -> Decimal:
    """Projected net profit for a full sale at `price`, before tax."""
    gross = q2(D(shares) * D(price))
    return (gross - sell_fee(gross, rates)) - D(basis)


def breakeven(shares, basis, rates: FeeRates = DEFAULT_RATES) -> Optional[Decimal]:
    """Lowest whole-cent price whose projected net profit is >= 0, else None."""
    shares, basis = D(shares), D(basis)
    if shares <= 0 or basis <= 0:
        return None
    p_min_fee = (basis + rates.sell_min - CENT) / shares
    p_pct_fee = basis / (shares * (1 - rates.sell_rate)) - CENT
    p = max(p_min_fee, p_pct_fee).quantize(CENT, rounding=ROUND_UP)
    while net_profit(shares, basis, p, rates) < 0:
        p += CENT
    # Safety: the start value is meant to be a lower bound; make sure it is, so the
    # result is always the *first* whole cent (matters for tiny or fractional positions).
    while p > CENT and net_profit(shares, basis, p - CENT, rates) >= 0:
        p -= CENT
    return p


def pct_distance(price, base) -> Optional[Decimal]:
    """Unrounded percent distance of price from base (used for zone decisions)."""
    base = D(base) if base is not None else None
    if base is None or base <= 0:
        return None
    return (D(price) - base) / base * 100


def pct_distance_2dp(price, base) -> Optional[Decimal]:
    """Rounded to 2 decimals, as the blueprint shows it."""
    p = pct_distance(price, base)
    return None if p is None else q2(p)


def price_from_pct(base, pct) -> Decimal:
    """Level price = base x (1 + pct/100), unrounded (use q2 for display)."""
    return D(base) * (1 + D(pct) / 100)


def pct_from_price(base, price) -> Optional[Decimal]:
    return pct_distance(price, base)


def stock_base(stock, rates: FeeRates = DEFAULT_RATES) -> Optional[Decimal]:
    """Base for percent math: breakeven (HOLD, SELL) or planned entry (BUY)."""
    if stock.category in ("HOLD", "SELL"):
        pos = stock.position
        if pos is None:
            return None
        basis = total_cost_basis(pos.shares, pos.avg_price, pos.actual_buy_fee, rates)
        return breakeven(pos.shares, basis, rates)
    if stock.category == "BUY":
        pe = stock.planned_entry
        return pe.price if pe is not None and pe.price > 0 else None
    return None


def net_estimate(stock, price, rates: FeeRates = DEFAULT_RATES) -> Optional[Decimal]:
    if stock.category not in ("HOLD", "SELL") or stock.position is None:
        return None
    pos = stock.position
    basis = total_cost_basis(pos.shares, pos.avg_price, pos.actual_buy_fee, rates)
    return net_profit(pos.shares, basis, price, rates)


def position_summary(stock, price, rates: FeeRates = DEFAULT_RATES) -> dict:
    """Numbers for the expanded section: cost basis, breakeven, net at price."""
    pos = stock.position
    if pos is None:
        return {}
    ov = order_value(pos.shares, pos.avg_price)
    fee = q2(pos.actual_buy_fee) if pos.actual_buy_fee is not None else buy_fee(ov, rates)
    basis = ov + fee
    gross = q2(pos.shares * D(price))
    return {"order_value": ov, "buy_fee": fee, "cost_basis": basis,
            "breakeven": breakeven(pos.shares, basis, rates),
            "sell_fee": sell_fee(gross, rates),
            "net_estimate": net_profit(pos.shares, basis, price, rates)}
