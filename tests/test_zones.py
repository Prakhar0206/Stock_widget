import pytest
from decimal import Decimal as Dc

from config import ZONE_DEFAULTS_PCT, Config
from models import ValidationError
from positions import pct_distance, stock_base
from zones import (ZoneTracker, build_zone_defs, check_thresholds, classify, validate_thresholds)
from conftest import aapl, ttwo

BAND = Dc("0.5")


def test_3_hysteresis_example():
    s = aapl()
    be = stock_base(s)
    defs = build_zone_defs(s, ZONE_DEFAULTS_PCT)
    z = classify(pct_distance(Dc("207.50"), be), defs, None, BAND)
    assert z == "sell"
    assert classify(pct_distance(Dc("206.60"), be), defs, "sell", BAND) == "sell"       # stays
    assert classify(pct_distance(Dc("206.30"), be), defs, "sell", BAND) == "above_be"   # leaves


def test_without_hysteresis_boundary_is_exact():
    s, = [aapl()]
    be = stock_base(s)
    defs = build_zone_defs(s, ZONE_DEFAULTS_PCT)
    assert classify(pct_distance(Dc("207.46"), be), defs, None, BAND) == "sell"
    assert classify(pct_distance(Dc("207.45"), be), defs, None, BAND) == "above_be"
    assert classify(pct_distance(Dc("195.36"), be), defs, None, BAND) == "loss"
    assert classify(pct_distance(Dc("195.37"), be), defs, None, BAND) == "below_be"


def test_deep_loss_off_by_default_and_on():
    s = aapl()
    names = [d.name for d in build_zone_defs(s, ZONE_DEFAULTS_PCT)]
    assert "deep_loss" not in names
    s.zones = {"deep_loss": {"enabled": True}}
    be = stock_base(s)
    defs = build_zone_defs(s, ZONE_DEFAULTS_PCT)
    assert classify(pct_distance(Dc("150"), be), defs, None, BAND) == "deep_loss"


def test_hold_has_only_loss_side_zones():
    s = aapl("HOLD")
    names = [d.name for d in build_zone_defs(s, ZONE_DEFAULTS_PCT)]
    assert names == ["loss", "below_be", "none"]
    be = stock_base(s)
    defs = build_zone_defs(s, ZONE_DEFAULTS_PCT)
    assert classify(pct_distance(Dc("230"), be), defs, None, BAND) == "none"


def test_buy_zones():
    s = ttwo()
    defs = build_zone_defs(s, ZONE_DEFAULTS_PCT)
    be = stock_base(s)
    for price, want in [("180", "deep_drop"), ("200", "entry_hit"), ("202.73", "entry_hit"),
                        ("203.50", "near_entry"), ("208", "above_entry"), ("215", "far_above")]:
        assert classify(pct_distance(Dc(price), be), defs, None, BAND) == want, price


def test_noisy_needs_two_readings_to_change_zone():
    s = aapl(noisy=True)
    be = stock_base(s)
    defs = build_zone_defs(s, ZONE_DEFAULTS_PCT)
    tr = ZoneTracker()
    cur = tr.update(pct_distance(Dc("200"), be), defs, None, Dc("1.0"), True)
    p_sell = pct_distance(Dc("212"), be)
    assert tr.update(p_sell, defs, cur, Dc("1.0"), True) == cur            # first reading: wait
    assert tr.update(p_sell, defs, cur, Dc("1.0"), True) == "high_sell"    # second: commit


def test_14_out_of_order_thresholds_rejected():
    bad = {"deep_loss": Dc(-2), "loss": Dc(-3), "sell": Dc(3), "high_sell": Dc("4.5")}
    assert validate_thresholds("SELL", bad)
    assert validate_thresholds("SELL", {"deep_loss": Dc(-10), "loss": Dc("-0.2"), "sell": Dc(3), "high_sell": Dc(4)})
    assert validate_thresholds("SELL", {"deep_loss": Dc(-10), "loss": Dc(-3), "sell": Dc(5), "high_sell": Dc(4)})
    assert validate_thresholds("SELL", {"deep_loss": Dc(-10), "loss": Dc(-3), "sell": Dc("0.3"), "high_sell": Dc(4)})
    assert validate_thresholds("BUY", {"deep_drop": Dc(1), "near_entry": Dc(1), "far_above": Dc(5)})
    assert validate_thresholds("BUY", {"deep_drop": Dc(-10), "near_entry": Dc(6), "far_above": Dc(5)})
    assert validate_thresholds("SELL", {"deep_loss": Dc(-95), "loss": Dc(-3), "sell": Dc(3), "high_sell": Dc(600)})
    assert not validate_thresholds("SELL", {"deep_loss": Dc(-10), "loss": Dc(-3), "sell": Dc(3), "high_sell": Dc("4.5")})
    s = aapl(zones={"loss": {"pct": Dc("-12")}})       # loss below deep_loss default -10
    with pytest.raises(ValidationError):
        check_thresholds(s, ZONE_DEFAULTS_PCT)


def test_editor_rows_match_engine_ranges_when_deep_loss_off_and_on():
    from zones import editor_rows
    s = aapl()
    rows = {r["name"]: r for r in editor_rows(s, ZONE_DEFAULTS_PCT, stock_base(s))}
    assert rows["loss"]["lo_pct"] is None and rows["loss"]["hi_price"] == Dc("195.36")      # open-ended, like the engine
    assert rows["deep_loss"]["enabled"] is False and rows["deep_loss"]["lo_pct"] is None
    s.zones = {"deep_loss": {"enabled": True}}
    rows = {r["name"]: r for r in editor_rows(s, ZONE_DEFAULTS_PCT, stock_base(s))}
    assert rows["loss"]["lo_pct"] == Dc(-10) and rows["deep_loss"]["enabled"] is True


# ---- per-edge hysteresis: enter at the exact start, leave only after falling back by the band ----
def _walk(stock, prices, band="0.5", start=None):
    be = stock_base(stock)
    defs = build_zone_defs(stock, ZONE_DEFAULTS_PCT)
    cur, out = start, []
    for p in prices:
        cur = classify(pct_distance(Dc(p), be), defs, cur, Dc(band))
        out.append(cur)
    return out


def test_sell_side_enters_at_exact_start_and_leaves_after_band():
    up = _walk(aapl(), ["206.00", "207.45", "207.46", "210.47", "210.48"])
    assert up == ["above_be", "above_be", "sell", "sell", "high_sell"]
    down = _walk(aapl(), ["212", "210.00", "209.47", "209.40", "207.00", "206.60", "206.30"])
    # high sell is kept down to P +4.0 (209.47), sell is kept down to P +2.5 (206.44)
    assert down == ["high_sell", "high_sell", "high_sell", "sell", "sell", "sell", "above_be"]


def test_loss_side_enters_at_exact_start_and_leaves_after_band():
    down = _walk(aapl(), ["200", "196", "195.37", "195.36", "195.00"])
    assert down == ["below_be", "below_be", "below_be", "loss", "loss"]
    up = _walk(aapl(), ["195.00", "196.30", "196.37", "196.38", "199"], start="loss")
    assert up == ["loss", "loss", "loss", "below_be", "below_be"]          # kept to P -2.5 (196.37), gone at 196.38


def test_deep_loss_when_on_has_its_own_band():
    s = aapl(zones={"deep_loss": {"enabled": True}})                        # deep_loss -10, loss -3
    assert _walk(s, ["183", "181.27", "181.26", "182.00", "182.27", "182.28"], start="loss") == \
        ["loss", "loss", "deep_loss", "deep_loss", "deep_loss", "loss"]     # kept to P -9.5 (182.27), gone at 182.28


def test_breakeven_zone_is_sticky_on_both_edges():
    up = _walk(aapl(), ["198", "200.41", "202.41", "203.40", "203.43"])
    assert up == ["below_be", "breakeven", "breakeven", "breakeven", "above_be"]      # kept up to P +1.0
    down = _walk(aapl(), ["204", "202.41", "199.40", "199.39"], start="above_be")
    assert down[:2] == ["above_be", "breakeven"] and down[2:] == ["breakeven", "below_be"]   # kept down to P -1.0


def test_noisy_stock_uses_the_wider_band():
    s = aapl(noisy=True)
    # sell starts at 207.46; with band 1.0 it is kept down to P +2.0 (205.44) instead of +2.5
    assert _walk(s, ["207.46", "205.50", "205.44", "205.43"], band="1.0") == ["sell", "sell", "sell", "above_be"]


def test_buy_zones_enter_exactly_and_leave_after_band():
    s = ttwo()                                                              # entry 202.73
    be = stock_base(s)
    up = _walk(s, ["210", "208.5", "205.7", "205.6", "202.73", "202.74", "205.70"], start=None)
    assert up[0] == "far_above" or up[0] == "above_entry"
    walk_down = _walk(s, ["207", "205.75", "205.50", "202.74", "202.73", "202.70"])
    assert walk_down[0] == "above_entry"
    assert walk_down[-2:] == ["entry_hit", "entry_hit"]
    # entry_hit is kept up to P +0.5 (203.74), then near_entry
    assert _walk(s, ["202.00", "203.74", "203.75"], start="entry_hit")[1:] == ["entry_hit", "near_entry"]
    # far_above starts at +5.0 (212.87); kept down to +4.5 (211.86)
    assert _walk(s, ["212.86", "212.87", "212.00", "211.86", "211.85"]) == \
        ["above_entry", "far_above", "far_above", "far_above", "above_entry"]


def test_hold_none_zone_is_kept_down_to_minus_one():
    s = aapl("HOLD")
    out = _walk(s, ["198", "200.41", "199.50", "199.40", "199.38"], band="0.5")
    assert out == ["below_be", "none", "none", "none", "below_be"]


def test_classify_always_returns_a_zone_that_contains_p():
    """Property: over random walks the result is always either the kept zone or the exact zone."""
    import random
    rng = random.Random(7)
    for stock in (aapl(), aapl("HOLD"), ttwo(), aapl(noisy=True)):
        be = stock_base(stock)
        defs = build_zone_defs(stock, ZONE_DEFAULTS_PCT)
        by = {d.name: d for d in defs}
        band = Dc("1.0") if stock.noisy else Dc("0.5")
        cur, price = None, be
        for _ in range(3000):
            price = max(Dc("1"), price * Dc(str(1 + rng.gauss(0, 0.004))))
            p = pct_distance(price, be)
            new = classify(p, defs, cur, band)
            assert new is not None and (by[new].contains(p) or by[new].contains(p, band))
            exact = next(d.name for d in defs if d.contains(p))
            if new != exact:                                  # staying put must have been allowed by the band
                assert new == cur and by[cur].contains(p, band)
            cur = new
