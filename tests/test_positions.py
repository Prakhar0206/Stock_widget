from decimal import Decimal as Dc

from conftest import aapl, ttwo, watch
from models import StockConfig, TradeInfo
from positions import (
    D,
    q2,
    order_value,
    pct_distance,
    pct_distance_2dp,
    pct_from_price,
    price_from_pct,
    effective_buy_price,
    resolve_pct_base,
    manual_trade_summary,
)


def test_rounding_and_order_value():
    assert D("12.345") == Dc("12.345")
    assert q2(Dc("12.345")) == Dc("12.35")
    assert order_value(10, "200.00") == Dc("2000.00")
    assert pct_distance_2dp("207.50", "200.00") == Dc("3.75")
    assert pct_distance("210.00", "200.00") > Dc("4.9")
    assert price_from_pct("200.00", "3.0") == Dc("206.00")
    assert pct_from_price("200.00", "206.00") == Dc("3.00")


def test_effective_buy_price_with_optional_fees():
    assert effective_buy_price(None) is None
    assert effective_buy_price(TradeInfo(buy_price=Dc("200.00"), shares=Dc("10"))) == Dc("200.00")
    # (200 * 10 + 14.10) / 10 = 201.41
    eff = effective_buy_price(TradeInfo(buy_price=Dc("200.00"), shares=Dc("10"), fees=Dc("14.10")))
    assert eff == Dc("201.41")


def test_resolve_pct_base_priority_and_watch_fallback():
    sell_stock = aapl()
    base, desc = resolve_pct_base(sell_stock, "auto", prev_close=Dc("195.00"))
    assert base == Dc("200.00")
    assert desc

    sell_with_fees = StockConfig(
        "AAPL",
        "SELL",
        "Apple",
        False,
        TradeInfo(buy_price=Dc("200.00"), shares=Dc("10"), fees=Dc("14.10")),
    )
    base_fee, desc_fee = resolve_pct_base(sell_with_fees, "auto", prev_close=Dc("195.00"))
    assert base_fee == Dc("201.41")
    assert desc_fee

    buy_stock = ttwo()
    base_buy, _ = resolve_pct_base(buy_stock, "auto", prev_close=Dc("190.00"))
    assert base_buy == Dc("202.73")

    # WATCH stock without buy/sell price falls back cleanly to Previous Close
    w = watch()
    base_w, desc_w = resolve_pct_base(w, "auto", prev_close=Dc("180.00"))
    assert base_w == Dc("180.00")
    assert "close" in desc_w.lower() or "prev" in desc_w.lower()

    # WATCH stock with no prev_close either -> (None, ...)
    base_none, _ = resolve_pct_base(w, "auto", prev_close=None)
    assert base_none is None


def test_resolve_pct_base_explicit_overrides():
    s = StockConfig(
        "NVDA",
        "SELL",
        "NVIDIA",
        False,
        TradeInfo(buy_price=Dc("100.00"), shares=Dc("5"), sell_price=Dc("125.00"), fees=Dc("0")),
    )
    assert resolve_pct_base(s, "buy_price", prev_close=Dc("110.00"))[0] == Dc("100.00")
    assert resolve_pct_base(s, "sell_price", prev_close=Dc("110.00"))[0] == Dc("125.00")
    assert resolve_pct_base(s, "prev_close", prev_close=Dc("110.00"))[0] == Dc("110.00")
    assert resolve_pct_base(s, "custom", prev_close=Dc("110.00"), ref_price=Dc("115.50"))[0] == Dc("115.50")


def test_manual_trade_summary_returns_dict():
    assert isinstance(manual_trade_summary(None, Dc("205.00")), dict)
    summary = manual_trade_summary(
        TradeInfo(buy_price=Dc("200.00"), shares=Dc("10"), sell_price=Dc("215.00"), fees=Dc("10.00")),
        price=Dc("210.00"),
    )
    assert isinstance(summary, dict)
    assert len(summary) > 0
