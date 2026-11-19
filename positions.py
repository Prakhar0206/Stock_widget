"""Manual trade figures and percentage target math (Decimal only, ROUND_HALF_UP)."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from models import StockConfig, TradeInfo

CENT = Decimal("0.01")
ZERO = Decimal(0)


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def q2(x) -> Decimal:
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def order_value(shares, price) -> Decimal:
    return q2(D(shares) * D(price))


def pct_distance(price, base) -> Optional[Decimal]:
    """Unrounded percent distance of price from base."""
    base = D(base) if base is not None else None
    if base is None or base <= 0:
        return None
    return (D(price) - base) / base * 100


def pct_distance_2dp(price, base) -> Optional[Decimal]:
    p = pct_distance(price, base)
    return None if p is None else q2(p)


def price_from_pct(base, pct) -> Decimal:
    """Level price = base x (1 + pct/100), unrounded (use q2 for display)."""
    return D(base) * (1 + D(pct) / 100)


def pct_from_price(base, price) -> Optional[Decimal]:
    return pct_distance(price, base)


def effective_buy_price(trade: Optional[TradeInfo]) -> Optional[Decimal]:
    """User-entered buy_price adjusted by manual fees/shares when both shares & fees are set."""
    if trade is None or trade.buy_price is None or trade.buy_price <= 0:
        return None
    if trade.shares is not None and trade.shares > 0 and trade.fees is not None and trade.fees > 0:
        return trade.buy_price + (trade.fees / trade.shares)
    return trade.buy_price


def resolve_pct_base(
    stock: StockConfig,
    pct_base: str = "auto",
    prev_close: Optional[Decimal] = None,
    ref_price: Optional[Decimal] = None,
) -> tuple[Optional[Decimal], str]:
    """Resolve the dollar base for a `%` alert condition and return (base_price, base_label)."""
    trade = stock.trade
    pc = D(prev_close) if (prev_close is not None and D(prev_close) > 0) else None
    custom_ref = D(ref_price) if (ref_price is not None and D(ref_price) > 0) else None

    if pct_base == "custom":
        if custom_ref is not None:
            return custom_ref, f"ref ${q2(custom_ref):.2f}"
        if pc is not None:
            return pc, "prev close"
        return None, ""

    if pct_base == "prev_close":
        return (pc, "prev close") if pc is not None else (None, "")

    if pct_base == "sell_price":
        if trade is not None and trade.sell_price is not None and trade.sell_price > 0:
            return trade.sell_price, "sell price"
        return (pc, "prev close") if pc is not None else (None, "")

    # "buy_price" or "auto"
    if trade is not None and trade.buy_price is not None and trade.buy_price > 0:
        if stock.category in ("HOLD", "SELL") and trade.shares is not None and trade.shares > 0 and trade.fees is not None and trade.fees > 0:
            return effective_buy_price(trade), "breakeven"
        lbl = "entry price" if stock.category == "BUY" else "buy price"
        return trade.buy_price, lbl

    if pct_base == "auto" and trade is not None and trade.sell_price is not None and trade.sell_price > 0:
        return trade.sell_price, "sell price"

    if pc is not None:
        return pc, "prev close"
    return None, ""


def manual_trade_summary(trade: Optional[TradeInfo], price: Optional[Decimal] = None) -> dict:
    """Summarize simple math solely from user-entered trade fields (never guesses fees)."""
    if trade is None:
        return {}
    out: dict = {}
    fees = trade.fees or ZERO
    if trade.buy_price is not None and trade.buy_price > 0:
        eff = effective_buy_price(trade)
        out["buy_price"] = trade.buy_price
        out["cost_per_share"] = eff
        if trade.shares is not None and trade.shares > 0:
            out["shares"] = trade.shares
            out["cost_total"] = q2(trade.buy_price * trade.shares + fees)
            if price is not None and price > 0:
                out["live_pnl"] = q2((D(price) - trade.buy_price) * trade.shares - fees)
            if trade.sell_price is not None and trade.sell_price > 0:
                out["target_pnl"] = q2((trade.sell_price - trade.buy_price) * trade.shares - fees)
    elif trade.sell_price is not None and trade.sell_price > 0 and trade.shares is not None and trade.shares > 0:
        out["sell_total"] = q2(trade.sell_price * trade.shares - fees)
    return out
