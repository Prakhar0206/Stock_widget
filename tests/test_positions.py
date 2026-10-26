from decimal import Decimal as Dc

from positions import (breakeven, buy_fee, net_profit, order_value, q2, sell_fee, total_cost_basis,
                       pct_distance_2dp, stock_base, FeeRates)
from zones import build_zone_defs, zone_price_table
from config import ZONE_DEFAULTS_PCT
from conftest import aapl

BASIS = total_cost_basis(10, "200.00")


def test_1_breakeven_and_zone_starts():
    assert BASIS == Dc("2006.60")
    assert breakeven(10, BASIS) == Dc("201.41")
    s = aapl()
    rows = {r["name"]: r for r in zone_price_table(build_zone_defs(s, ZONE_DEFAULTS_PCT), stock_base(s))}
    assert rows["loss"]["hi_price"] == Dc("195.36")      # "195.36 or below"
    assert rows["sell"]["lo_price"] == Dc("207.46")
    assert rows["high_sell"]["lo_price"] == Dc("210.48")


def test_breakeven_is_first_whole_cent():
    assert net_profit(10, BASIS, "201.40") == Dc("-0.05")
    assert net_profit(10, BASIS, "201.41") == Dc("0.05")


def test_2_net_estimate_at_207_50():
    gross = q2(10 * Dc("207.50"))
    assert sell_fee(gross) == Dc("7.68")
    assert net_profit(10, BASIS, "207.50") == Dc("60.72")


def test_fees():
    assert buy_fee(Dc("2000.00")) == Dc("6.60")
    assert buy_fee(Dc("5.00")) == Dc("0.02")             # minimum
    assert sell_fee(Dc("10.00")) == Dc("0.05")           # minimum
    # a ledger fee replaces the projection
    assert total_cost_basis(10, "200.00", Dc("1.00")) == Dc("2001.00")
    assert total_cost_basis(10, "200.00", Dc("0")) == Dc("2000.00")


def test_breakeven_edge_cases():
    assert breakeven(0, Dc("100")) is None
    assert breakeven(5, Dc("0")) is None
    b = breakeven(Dc("0.5"), total_cost_basis(Dc("0.5"), "10.00"))     # fractional shares
    basis = total_cost_basis(Dc("0.5"), "10.00")
    assert net_profit(Dc("0.5"), basis, b) >= 0 and net_profit(Dc("0.5"), basis, b - Dc("0.01")) < 0


def test_custom_fee_rates_still_exact():
    r = FeeRates(Dc("0.001"), Dc("0.02"), Dc("0.002"), Dc("0.05"))
    basis = total_cost_basis(100, "50.00", None, r)
    b = breakeven(100, basis, r)
    assert net_profit(100, basis, b, r) >= 0 and net_profit(100, basis, b - Dc("0.01"), r) < 0


def test_pct_distance_and_buy_base():
    assert pct_distance_2dp("207.50", "201.41") == Dc("3.02")
    from conftest import ttwo
    assert stock_base(ttwo()) == Dc("202.73")
