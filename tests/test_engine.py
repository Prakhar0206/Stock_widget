import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dc

import pytest

from config import Config
from engine import (
    AlertEngine,
    compute_expires_at,
    default_alert_message,
    merge_events,
    render_alert_message,
    resolve_alert_targets,
    session_matches_scope,
)
from market_clock import Session
from messages import format_message
from models import ARMED, COOLDOWN, DISABLED, DISARMED, DONE, EXPIRED, WAIT_CROSS, Alert, StockConfig, TradeInfo
from conftest import Feed, T0, aapl, al, lvl, tick, ttwo, watch


def state_of(engine, sym, aid):
    return engine.alert_state(sym, aid)["state"]


def test_primary_five_operators(engine):
    s = watch([
        al("x", "crossing", "100", trigger="rearm"),
        al("cu", "crossing_up", "100", trigger="rearm"),
        al("cd", "crossing_down", "100", trigger="rearm"),
        al("gt", "greater_than", "100", trigger="once"),
        al("lt", "less_than", "100", trigger="once"),
    ])
    f = Feed(engine, s)
    # First reading 98: lt fires on first reading; crossing ops wait for a cross
    ev1 = f.push("98")
    assert {e.alert_id for e in ev1} == {"lt"}

    # Move across 100 -> 102: crossing, crossing_up, greater_than fire
    ev2 = f.push("102")
    assert {e.alert_id for e in ev2} == {"x", "cu", "gt"}

    # Move down across 100 -> 99: crossing, crossing_down fire; lt/gt stay DONE
    ev3 = f.push("99")
    assert {e.alert_id for e in ev3} == {"x", "cd"}
    assert state_of(engine, "TTWO", "gt") == DONE
    assert state_of(engine, "TTWO", "lt") == DONE


def test_channel_four_operators(engine):
    s = watch([
        al("ent", "entering_channel", lower="95", upper="105", trigger="rearm"),
        al("ext", "exiting_channel", lower="95", upper="105", trigger="rearm"),
        al("ins", "inside_channel", lower="95", upper="105", trigger="once"),
        al("out", "outside_channel", lower="95", upper="105", trigger="once"),
    ])
    f = Feed(engine, s)
    # Start outside at 90 -> outside_channel triggers on first reading
    assert {e.alert_id for e in f.push("90")} == {"out"}
    # Enter channel -> 100: entering_channel & inside_channel fire
    assert {e.alert_id for e in f.push("100")} == {"ent", "ins"}
    # Stay inside -> 101: nothing fires
    assert f.push("101") == []
    # Exit channel -> 108: exiting_channel fires
    assert {e.alert_id for e in f.push("108")} == {"ext"}


def test_moving_four_operators_with_window(engine):
    s = watch([
        al("mu", "moving_up", "5", window_min=10, trigger="once"),
        al("md", "moving_down", "5", window_min=10, trigger="once"),
        al("mup", "moving_up_pct", "4", window_min=10, trigger="once"),
        al("mdp", "moving_down_pct", "4", window_min=10, trigger="once"),
    ])
    f = Feed(engine, s)
    assert f.push("100") == []
    # +6 dollars (+6%) within 1 minute -> mu and mup fire
    assert {e.alert_id for e in f.push("106")} == {"mu", "mup"}
    # Now baseline in 10-min window includes 106; drop to 99 (-7 dollars, -6.6% vs 106)
    assert {e.alert_id for e in f.push("99")} == {"md", "mdp"}


def test_pct_condition_uses_trade_price_and_falls_back_to_prev_close_for_watch(engine):
    # SELL stock with buy=200, shares=10, fees=14.10 -> effective buy = 201.41
    s_sell = StockConfig(
        "AAPL",
        "SELL",
        "Apple",
        False,
        TradeInfo(buy_price=Dc("200.00"), shares=Dc("10"), fees=Dc("14.10")),
        [al("pct-target", "greater_than", "3", condition_mode="pct", trigger="once")],
    )
    single, _, target_text, base_desc = resolve_alert_targets(s_sell, s_sell.alerts[0], prev_close=Dc("195.00"))
    # 201.41 * 1.03 = 207.4523 -> 207.45 (or 207.4523)
    assert single is not None and Dc("207.44") <= single <= Dc("207.46")
    assert "breakeven" in base_desc.lower() or "buy" in base_desc.lower() or "201.41" in base_desc

    # WATCH stock without TradeInfo falls back cleanly to prev_close
    s_watch = watch([al("w-pct", "greater_than", "5", condition_mode="pct", trigger="once")])
    f = Feed(engine, s_watch)
    assert f.push("104", prev_close="100") == []
    assert [e.alert_id for e in f.push("106", prev_close="100")] == ["w-pct"]


def test_triggers_once_every_time_and_rearm_and_restart_persistence(cfg, tmp_path):
    from store import Store

    store = Store(tmp_path)
    s = watch([
        al("et", "less_than", "205", trigger="every_time", cooldown=30),
        al("once", "less_than", "205", trigger="once"),
        al("ra", "less_than", "205", trigger="rearm"),
    ])
    e1 = AlertEngine(cfg, store.load_state())
    f = Feed(e1, s)
    assert {e.alert_id for e in f.push("200")} == {"et", "once", "ra"}
    assert state_of(e1, "TTWO", "et") == COOLDOWN
    assert state_of(e1, "TTWO", "once") == DONE
    assert state_of(e1, "TTWO", "ra") == DISARMED
    store.save_state(e1.state)

    e2 = AlertEngine(cfg, store.load_state())
    f2 = Feed(e2, s)
    f2.t = f.t
    assert f2.push("199") == []  # still cooling down / DONE / DISARMED
    f2.t += timedelta(minutes=31)
    assert [e.alert_id for e in f2.push("199")] == ["et"]  # every_time refires after cooldown even while true

    # Returning false rearms 'ra', not 'once'
    f2.push("210")
    assert state_of(e2, "TTWO", "ra") == ARMED
    assert state_of(e2, "TTWO", "once") == DONE


def test_expiration_presets_and_expired_transition(engine):
    assert compute_expires_at("open_ended", T0) is None
    eos = datetime.fromisoformat(compute_expires_at("end_of_session", T0))
    eonext = datetime.fromisoformat(compute_expires_at("end_of_next_session", T0))
    assert eos > T0 and eonext > eos
    assert datetime.fromisoformat(compute_expires_at("1_week", T0)) == T0 + timedelta(days=7)
    assert datetime.fromisoformat(compute_expires_at("1_month", T0)) == T0 + timedelta(days=30)

    exp_past = T0 - timedelta(minutes=1)
    s = watch([al("exp", "greater_than", "100", trigger="once", expiration="custom", expires_at=exp_past)])
    f = Feed(engine, s)
    assert f.push("150") == []
    assert state_of(engine, "TTWO", "exp") == EXPIRED


def test_session_scopes_and_extended_hours_toggle():
    assert session_matches_scope(Session.REGULAR, "regular", True) is True
    assert session_matches_scope(Session.PRE, "regular", True) is False
    assert session_matches_scope(Session.AFTER, "regular", True) is False
    assert session_matches_scope(Session.PRE, "extended", True) is True
    assert session_matches_scope(Session.REGULAR, "extended", True) is False
    assert session_matches_scope(Session.CLOSED, "all", True) is False
    assert session_matches_scope(Session.PRE, "all", False) is False

    e = AlertEngine(Config())
    s = watch([
        al("reg", "greater_than", "100", session_scope="regular", trigger="once"),
        al("ext", "greater_than", "100", session_scope="extended", trigger="once"),
        al("all", "greater_than", "100", session_scope="all", trigger="once"),
    ])
    # CLOSED ignores all alerts
    f_closed = Feed(e, s, session=Session.CLOSED)
    assert f_closed.push("150") == []
    # PRE fires only ext and all
    f_closed.session = Session.PRE
    assert {ev.alert_id for ev in f_closed.push("150")} == {"ext", "all"}
    # REGULAR fires reg
    f_closed.session = Session.REGULAR
    assert [ev.alert_id for ev in f_closed.push("150")] == ["reg"]


def test_wait_cross_and_register_alert_choice_contract(engine):
    s = watch([al("w", "less_than", "202.73", trigger="once")])
    assert engine.would_fire_now(s, s.alerts[0], Dc("201.90")) is True
    with pytest.raises(ValueError):
        engine.register_alert(s, s.alerts[0], tick("201.90"), T0)

    ev_wait = engine.register_alert(s, s.alerts[0], tick("201.90"), T0, choice="wait_cross")
    assert ev_wait == [] and state_of(engine, "TTWO", "w") == WAIT_CROSS
    f = Feed(engine, s)
    assert f.push("200") == [] and state_of(engine, "TTWO", "w") == WAIT_CROSS
    assert f.push("204") == [] and state_of(engine, "TTWO", "w") == ARMED
    assert [e.alert_id for e in f.push("202")] == ["w"]

    s2 = watch([al("fn", "less_than", "202.73", trigger="once")])
    ev_now = engine.register_alert(s2, s2.alerts[0], tick("201.90"), T0, choice="fire_now")
    assert len(ev_now) == 1 and state_of(engine, "TTWO", "fn") == DONE


def test_noisy_stock_needs_two_consecutive_readings(engine):
    s = watch([al("n", "greater_than", "210", trigger="once")])
    s.noisy = True
    f = Feed(engine, s)
    f.push("205")
    assert f.push("211") == []
    assert [e.alert_id for e in f.push("211.5")] == ["n"]

    s2 = watch([al("m", "greater_than", "210", trigger="once")])
    s2.noisy = True
    f2 = Feed(AlertEngine(engine.cfg), s2)
    f2.push("205")
    f2.push("211")
    assert f2.push("205") == []
    assert f2.push("211") == []


def test_jump_orders_by_priority_and_disabled_and_remove_stock(engine):
    s = watch([
        al("low", "greater_than", "205", prio="LOW", trigger="once"),
        al("high", "greater_than", "210", prio="HIGH", trigger="once"),
        al("med", "greater_than", "208", prio="MEDIUM", trigger="once"),
        al("dis", "greater_than", "205", prio="HIGH", enabled=False),
    ])
    f = Feed(engine, s)
    f.push("203")
    ev = f.push("212")
    assert [e.alert_id for e in ev] == ["high", "med", "low"]
    assert state_of(engine, "TTWO", "dis") == DISABLED
    note = merge_events(ev)
    assert note.priority == "HIGH" and len(note.events) == 3

    s.alerts[3].enabled = True
    assert [e.alert_id for e in f.push("212")] == ["dis"]
    engine.remove_stock("TTWO")
    assert "TTWO" not in engine.state["symbols"]


def test_message_placeholder_rendering_and_format(engine):
    a = al(
        "msg1",
        "crossing_up",
        "210.00",
        name="Breakout",
        message="{{ticker}} hit ${{price}} vs {{target}} ({{change_pct}}) in {{session}} at {{time}}",
        prio="MEDIUM",
    )
    rendered = render_alert_message(
        a.message,
        {
            "ticker": "AAPL",
            "price": "210.50",
            "target": "$210.00",
            "change_pct": "+3.10%",
            "session": "REGULAR",
            "time": "20:30 IST",
        },
    )
    assert rendered == "AAPL hit $210.50 vs $210.00 (+3.10%) in REGULAR at 20:30 IST"
    assert "AAPL" in default_alert_message("AAPL", a, "$210.00")

    s = aapl(alerts=[a])
    f = Feed(engine, s)
    f.push("205", prev_close="200")
    evs = f.push("211", prev_close="200")
    assert len(evs) == 1
    msg = format_message(evs[0], T0)
    assert "[MEDIUM] AAPL" in msg
    assert "Breakout" in msg
    assert "AAPL hit $211.00" in msg
    for bad in ("you should", "buy now", "sell now", "recommend"):
        assert bad not in msg.lower()
