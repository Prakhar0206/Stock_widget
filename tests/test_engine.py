import json
from datetime import timedelta
from decimal import Decimal as Dc

import pytest

from engine import AlertEngine, merge_events
from market_clock import Session
from messages import format_message
from models import ARMED, COOLDOWN, DISARMED, DONE, WAIT_CROSS, Alert
from conftest import Feed, T0, aapl, lvl, tick, ttwo, watch


def state_of(engine, sym, aid):
    return engine.alert_state(sym, aid)["state"]


def test_4_rearm_level_below(engine):
    s = watch([lvl("d", "level_below", price="202.73")])
    f = Feed(engine, s)
    assert f.push("205") == []                                  # first reading, not true
    ev = f.push("202.73")
    assert [e.alert_id for e in ev] == ["d"]
    assert state_of(engine, "TTWO", "d") == DISARMED
    assert f.push("202.00") == []                               # still below: no refire
    f.push("203.74364")
    assert state_of(engine, "TTWO", "d") == DISARMED            # 202.73 x 1.005 = 203.74365
    assert Dc("202.73") * Dc("1.005") == Dc("203.74365")
    f.push("203.74365")
    assert state_of(engine, "TTWO", "d") == ARMED
    assert [e.alert_id for e in f.push("202.50")] == ["d"]      # fires again


def test_5_first_reading_zone_silent_level_fires(engine):
    s = aapl(alerts=[lvl("t", "level_above", price="205", mode="once")])
    f = Feed(engine, s)
    ev = f.push("207.50")        # lands in the sell zone AND above 205 on the very first reading
    assert [e.alert_id for e in ev] == ["t"]                    # level fires, no zone entry
    assert engine.sym_state("AAPL")["zone"] == "sell"


def test_zone_entry_fires_after_first_reading(engine):
    s = aapl()
    f = Feed(engine, s)
    assert f.push("200") == []
    ev = f.push("207.50")
    assert [e.alert_id for e in ev] == ["zone:sell"] and ev[0].priority == "MEDIUM" and ev[0].label == "SELL"
    msg = format_message(ev[0], T0).splitlines()
    assert msg[0].startswith("[MEDIUM] AAPL · SELL · 207.50 ·") and msg[0].endswith("IST")
    assert msg[1] == "Entered sell zone, +3.02% above breakeven"
    assert msg[2] == "est. net +$60.72"
    assert msg[3] == "Estimate before tax. Source Mock feed, data age 4 s."


def test_slipping_zone_exit(engine):
    slip = Alert("slip", "zone_exit", "RISK", "MEDIUM", zone="sell", note="Slipping")
    s = aapl(alerts=[slip])
    f = Feed(engine, s)
    f.push("200"); f.push("207.50")
    assert f.push("206.60") == []                               # inside the band
    ev = f.push("206.30")                                       # P +2.43 < 2.5
    assert "slip" in [e.alert_id for e in ev]
    slip_ev = [e for e in ev if e.alert_id == "slip"][0]
    assert slip_ev.label == "RISK" and slip_ev.priority == "MEDIUM"
    assert f.push("207.45") == []                               # P +2.99: one cent short of the start
    assert f.push("207.46")[0].alert_id == "zone:sell"          # re-entry at the exact start price


def test_high_sell_to_sell_is_not_slipping(engine):
    slip = Alert("slip", "zone_exit", "RISK", "MEDIUM", zone="sell")
    f = Feed(engine, aapl(alerts=[slip]))
    f.push("200"); f.push("212")
    assert "slip" not in [e.alert_id for e in f.push("208")]


def test_6_wait_cross_does_not_fire_until_other_side(engine):
    s = watch([lvl("w", "level_below", price="202.73", mode="once")])
    f = Feed(engine, s)
    f.push("201")
    ev = engine.register_alert(s, s.alerts[0], tick("201"), T0, choice="wait_cross")
    assert ev == [] and state_of(engine, "TTWO", "w") == WAIT_CROSS
    assert f.push("200") == [] and state_of(engine, "TTWO", "w") == WAIT_CROSS   # still true: no fire
    assert f.push("204") == [] and state_of(engine, "TTWO", "w") == ARMED        # other side: armed
    assert [e.alert_id for e in f.push("202")] == ["w"]


def test_creation_check_fire_now_and_requires_choice(engine):
    s = watch([lvl("n", "level_below", price="202.73", mode="once")])
    assert engine.would_fire_now(s, s.alerts[0], Dc("201.90"))
    with pytest.raises(ValueError):
        engine.register_alert(s, s.alerts[0], tick("201.90"), T0)
    ev = engine.register_alert(s, s.alerts[0], tick("201.90"), T0, choice="fire_now")
    assert len(ev) == 1 and state_of(engine, "TTWO", "n") == DONE


def test_startup_armed_true_fires_done_never_does(engine):
    s = watch([lvl("a", "level_below", price="205", mode="once"), lvl("b", "level_below", price="205", mode="once")])
    engine.alert_state("TTWO", "b")["state"] = DONE
    ev = Feed(engine, s).push("200")
    assert [e.alert_id for e in ev] == ["a"]


def test_7_jump_fires_each_crossed_level_in_priority_order(engine):
    s = watch([lvl("low", "level_above", price="205", prio="LOW"),
              lvl("high", "level_above", price="210", prio="HIGH"),
              lvl("med", "level_above", price="208", prio="MEDIUM"),
              lvl("far", "level_above", price="230", prio="HIGH")])
    f = Feed(engine, s)
    f.push("203")
    ev = f.push("212")                                          # jumps over three levels
    assert [e.alert_id for e in ev if e.kind == "level"] == ["high", "med", "low"]
    note = merge_events(ev)
    assert note.priority == "HIGH" and len(note.events) >= 3


def test_8_repeat_cooldown_survives_restart(cfg, tmp_path):
    from store import Store
    store = Store(tmp_path)
    s = watch([lvl("r", "level_below", price="205", mode="repeat", cooldown=30)])
    e1 = AlertEngine(cfg, store.load_state())
    f = Feed(e1, s)
    assert [e.alert_id for e in f.push("200")] == ["r"]
    assert state_of(e1, "TTWO", "r") == COOLDOWN
    store.save_state(e1.state)
    e2 = AlertEngine(cfg, store.load_state())                   # restart
    f2 = Feed(e2, s); f2.t = f.t
    assert f2.push("199") == []                                 # still cooling down
    f2.t += timedelta(minutes=31)
    assert [e.alert_id for e in f2.push("199")] == ["r"]        # cooldown over, still true: fires
    # and a DONE alert is not re-fired after a restart
    s2 = watch([lvl("o", "level_below", price="205", mode="once")])
    e3 = AlertEngine(cfg, store.load_state()); f3 = Feed(e3, s2)
    assert len(f3.push("200")) == 1
    store.save_state(e3.state)
    e4 = AlertEngine(cfg, store.load_state())
    assert Feed(e4, s2).push("200") == []


def test_cooldown_ends_with_condition_false_rearms(engine):
    s = watch([lvl("r", "level_below", price="205", mode="repeat", cooldown=1)])
    f = Feed(engine, s)
    f.push("200"); f.t += timedelta(minutes=5)
    assert f.push("210") == [] and state_of(engine, "TTWO", "r") == ARMED


def test_once_mode_never_refires(engine):
    s = watch([lvl("o", "level_below", price="205", mode="once")])
    f = Feed(engine, s)
    f.push("210")
    assert len(f.push("200")) == 1
    f.push("210")
    assert f.push("200") == [] and state_of(engine, "TTWO", "o") == DONE


def test_percent_level_moves_with_base_price_level_does_not(engine):
    s = aapl(alerts=[lvl("p", "level_above", pct=3, mode="once", label="SELL"),
                     lvl("f", "level_above", price=210, mode="once")])
    base = Dc("201.41")
    assert engine.level_price(s.alerts[0], base) == base * Dc("1.03")
    s.position.avg_price = Dc("100")                            # base changes
    from positions import stock_base
    nb = stock_base(s)
    assert engine.level_price(s.alerts[0], nb) != base * Dc("1.03")
    assert engine.level_price(s.alerts[1], nb) == Dc(210)


def test_noisy_level_needs_two_readings(engine):
    s = watch([lvl("n", "level_above", price="210", mode="once")])
    s.noisy = True
    f = Feed(engine, s)
    f.push("205")
    assert f.push("211") == []                                  # first true reading
    assert [e.alert_id for e in f.push("211.5")] == ["n"]       # second consecutive
    s2 = watch([lvl("m", "level_above", price="210", mode="once")]); s2.noisy = True
    f2 = Feed(AlertEngine(engine.cfg), s2)
    f2.push("205"); f2.push("211"); assert f2.push("205") == []; assert f2.push("211") == []   # reset


def test_cross_alerts_never_fire_first_reading(engine):
    s = watch([lvl("c", "cross_above", price="205", mode="once"), lvl("d", "cross_below", price="205")])
    f = Feed(engine, s)
    assert f.push("210") == []                                  # above on first reading: nothing
    assert [e.alert_id for e in f.push("204")] == ["d"]
    assert [e.alert_id for e in f.push("206")] == ["c"]
    assert f.push("207") == []


def test_pct_move_day(engine):
    s = watch([Alert("m", "pct_move_day", "WATCH", "LOW", mode="rearm", pct=Dc(5))])
    f = Feed(engine, s)
    f.push("200", prev_close="200")
    assert [e.alert_id for e in f.push("211", prev_close="200")] == ["m"]
    assert f.push("189", prev_close="200") == []                # same day: disarmed
    assert [e.alert_id for e in f.push("160", prev_close="150")] == ["m"]   # new previous close = new day


def test_no_alerts_outside_regular_session(engine):
    s = aapl(alerts=[lvl("t", "level_above", price="205", mode="once")])
    f = Feed(engine, s, session=Session.AFTER)
    assert f.push("210") == [] and f.push("212") == []
    f.session = Session.REGULAR
    assert [e.alert_id for e in f.push("212")] == ["t"]         # armed + true at first regular reading


def test_disabled_alert_stays_quiet_and_reenables(engine):
    a = lvl("x", "level_below", price="205")
    a.enabled = False
    s = watch([a]); f = Feed(engine, s)
    assert f.push("200") == [] and state_of(engine, "TTWO", "x") == "DISABLED"
    a.enabled = True
    assert len(f.push("200")) == 1


def test_remove_stock_clears_state(engine):
    s = watch([lvl("x", "level_below", price="205")])
    Feed(engine, s).push("200")
    engine.remove_stock("TTWO")
    assert "TTWO" not in engine.state["symbols"]


def test_message_never_tells_user_to_trade(engine):
    s = aapl(); f = Feed(engine, s); f.push("200")
    text = format_message(f.push("207.50")[0], T0).lower()
    for bad in ("you should", "buy now", "sell now", "recommend"):
        assert bad not in text


def test_no_flapping_around_the_sell_boundary(engine):
    """Enter at 207.46; wobbling just below it (inside the 0.5 band) fires nothing more."""
    f = Feed(engine, aapl())
    f.push("200")
    assert [e.alert_id for e in f.push("207.46")] == ["zone:sell"]
    for p in ["207.40", "207.50", "206.90", "207.46", "206.60", "207.00"]:
        assert f.push(p) == [], p
    assert [e.alert_id for e in f.push("206.30")] == ["zone:above_be"]      # leaves at P < +2.5


def test_loss_zone_fires_at_its_start_price_from_below_breakeven(engine):
    """Regression: the HIGH 'Loss zone' alert used to wait until about -3.5%."""
    f = Feed(engine, aapl())
    f.push("200")                                               # below breakeven
    assert f.push("195.37") == []                               # -2.999%: not yet
    ev = f.push("195.36")                                       # -3.0%: the stated start
    assert [e.alert_id for e in ev] == ["zone:loss"] and ev[0].priority == "HIGH" and ev[0].label == "RISK"
    assert f.push("196.30") == []                               # still inside the exit band (P < -2.5): stays
    assert [e.alert_id for e in f.push("196.40")] == ["zone:below_be"]     # P > -2.5: left the loss zone


def test_high_sell_then_back_to_sell_keeps_band(engine):
    f = Feed(engine, aapl())
    f.push("200"); f.push("207.50")
    assert [e.alert_id for e in f.push("210.48")] == ["zone:high_sell"]
    assert f.push("209.50") == []                               # P +4.0: still high sell (exit below +4.0)
    assert [e.alert_id for e in f.push("209.40")] == ["zone:sell"]
