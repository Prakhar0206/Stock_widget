import json
from datetime import datetime, timezone
from decimal import Decimal as Dc
from pathlib import Path

import pytest

import store as store_mod
from config import BudgetError, Config, check_budget, in_quiet_hours, load_config, parse_config
from models import ValidationError
from store import Store, atomic_write_json
from watchlist import parse_watchlist

EXAMPLE = {
    "version": 1,
    "symbols": [
        {
            "symbol": "AAPL",
            "nickname": "Apple",
            "category": "SELL",
            "noisy": False,
            "trade": {"buy_price": 200.00, "shares": 10},
            "alerts": [
                {
                    "id": "aapl-target",
                    "operator": "greater_than",
                    "condition_mode": "price",
                    "value": 210.00,
                    "label": "SELL",
                    "priority": "HIGH",
                    "trigger": "once",
                    "enabled": True,
                    "message": "My target",
                }
            ],
        },
        {
            "symbol": "TTWO",
            "nickname": "Take-Two",
            "category": "BUY",
            "noisy": False,
            "trade": {"buy_price": 202.73, "shares": 5},
            "alerts": [
                {
                    "id": "ttwo-danger",
                    "operator": "less_than",
                    "condition_mode": "price",
                    "value": 195.00,
                    "label": "RISK",
                    "priority": "HIGH",
                    "trigger": "once",
                    "enabled": True,
                    "message": "Danger zone",
                }
            ],
        },
    ],
}

LEGACY_EXAMPLE = {
    "version": 1,
    "symbols": [
        {
            "symbol": "AAPL",
            "nickname": "Apple",
            "category": "SELL",
            "noisy": False,
            "position": {"shares": 10, "avg_price": 200.00, "actual_buy_fee": 4.00},
            "zones": {"sell": {"pct": 3.0, "priority": "MEDIUM", "enabled": True}},
            "alerts": [
                {
                    "id": "aapl-target",
                    "type": "level_above",
                    "price": 210.00,
                    "label": "SELL",
                    "priority": "HIGH",
                    "mode": "once",
                    "enabled": True,
                    "note": "My target",
                },
                {
                    "id": "aapl-slip",
                    "type": "zone_exit",
                    "zone": "sell",
                    "label": "RISK",
                    "priority": "MEDIUM",
                    "enabled": True,
                },
            ],
        },
        {
            "symbol": "TTWO",
            "nickname": "Take-Two",
            "category": "BUY",
            "noisy": False,
            "planned_entry": {"price": 202.73, "shares": 5},
            "alerts": [
                {
                    "id": "ttwo-move",
                    "type": "pct_move_day",
                    "pct": 5.0,
                    "priority": "LOW",
                    "mode": "rearm",
                }
            ],
        },
    ],
}


def write(path, data):
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def test_9_budget_refuses_30_symbols_at_30_seconds():
    assert check_budget(30, 60) == 31
    with pytest.raises(BudgetError):
        check_budget(30, 30)
    with pytest.raises(BudgetError):
        parse_config({"poll_seconds": 30})


def test_example_loads_and_roundtrips(tmp_path):
    cfg = Config()
    wl = parse_watchlist(json.loads(json.dumps(EXAMPLE), parse_float=Dc), cfg)
    assert [s.symbol for s in wl.symbols] == ["AAPL", "TTWO"]
    st = Store(tmp_path)
    st.save_watchlist(wl, cfg)
    wl2, warns = st.load_watchlist(cfg)
    assert not warns and wl2.get("AAPL").trade.buy_price == Dc("200.00")
    assert wl2.get("AAPL").alerts[0].value == Dc("210")
    assert wl2.get("AAPL").alerts[0].operator == "greater_than"


def test_legacy_watchlist_migrates_cleanly():
    cfg = Config()
    wl = parse_watchlist(json.loads(json.dumps(LEGACY_EXAMPLE), parse_float=Dc), cfg)
    aapl = wl.get("AAPL")
    assert aapl.trade.buy_price == Dc("200.00") and aapl.trade.shares == Dc("10") and aapl.trade.fees == Dc("4.00")
    assert [a.id for a in aapl.alerts] == ["aapl-target"]  # obsolete zone_exit skipped
    assert aapl.alerts[0].operator == "greater_than" and aapl.alerts[0].trigger == "once"
    ttwo = wl.get("TTWO")
    assert ttwo.trade.buy_price == Dc("202.73") and ttwo.trade.shares == Dc("5")
    assert ttwo.alerts[0].operator == "moving_up_pct"


def test_12_invalid_watchlist_loads_last_good_with_warning(tmp_path):
    cfg = Config()
    st = Store(tmp_path)
    write(st.watchlist_path, EXAMPLE)
    wl, warns = st.load_watchlist(cfg)
    assert len(wl.symbols) == 2 and not warns
    st.watchlist_path.write_text("{ this is not json", encoding="utf-8")
    wl, warns = st.load_watchlist(cfg)
    assert len(wl.symbols) == 2 and "last good" in warns[0]
    bad = json.loads(json.dumps(EXAMPLE))
    bad["symbols"][0]["trade"]["shares"] = -5
    write(st.watchlist_path, bad)
    wl, warns = st.load_watchlist(cfg)
    assert len(wl.symbols) == 2 and warns


def test_invalid_and_missing_files_never_crash(tmp_path):
    st = Store(tmp_path)
    wl, warns = st.load_watchlist(Config())
    assert wl.symbols == [] and warns == []
    st.watchlist_path.write_text("garbage", encoding="utf-8")
    wl, warns = st.load_watchlist(Config())
    assert wl.symbols == [] and warns


def test_13_failed_atomic_write_leaves_old_file(tmp_path, monkeypatch):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"v": 1})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text()) == {"v": 1}
    assert [f.name for f in tmp_path.iterdir()] == ["x.json"]


def test_failed_save_writes_nothing(tmp_path):
    cfg = Config()
    st = Store(tmp_path)
    wl = parse_watchlist(json.loads(json.dumps(EXAMPLE), parse_float=Dc), cfg)
    st.save_watchlist(wl, cfg)
    before = st.watchlist_path.read_text()
    wl.symbols[0].trade.shares = Dc(-1)
    with pytest.raises(ValidationError):
        st.save_watchlist(wl, cfg)
    assert st.watchlist_path.read_text() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["trade"].update(shares=0),
        lambda s: s["trade"].update(buy_price=-5),
        lambda s: s["trade"].update(fees=-1),
        lambda s: s["alerts"][0].update(value=0),
        lambda s: s["alerts"][0].update(condition_mode="pct", value=600),
        lambda s: s["alerts"][0].update(condition_mode="pct", value=-95),
        lambda s: s["alerts"][0].update(operator="unknown_op"),
        lambda s: s.update(category="HOLDX"),
        lambda s: s.update(symbol="aa pl"),
    ],
)
def test_validation_blocks_bad_input(mutate):
    d = json.loads(json.dumps(EXAMPLE))
    mutate(d["symbols"][0])
    with pytest.raises(ValidationError):
        parse_watchlist(json.loads(json.dumps(d), parse_float=Dc), Config())


def test_duplicates_and_cap_rejected():
    d = json.loads(json.dumps(EXAMPLE))
    d["symbols"].append(dict(d["symbols"][0]))
    with pytest.raises(ValidationError):
        parse_watchlist(d, Config())
    many = {"version": 1, "symbols": [{"symbol": f"A{i}", "category": "WATCH"} for i in range(31)]}
    with pytest.raises(ValidationError):
        parse_watchlist(many, Config())


def test_watch_pct_alert_allowed_and_buy_needs_no_required_trade_fields():
    d = {
        "version": 1,
        "symbols": [
            {
                "symbol": "ZZ",
                "category": "WATCH",
                "alerts": [{"id": "a", "operator": "greater_than", "condition_mode": "pct", "value": 3}],
            },
            {"symbol": "BB", "category": "BUY"},
        ],
    }
    wl = parse_watchlist(json.loads(json.dumps(d), parse_float=Dc), Config())
    assert wl.get("ZZ").alerts[0].condition_mode == "pct"
    assert wl.get("BB").trade is None


def test_hold_stock_allows_hold_label_and_defaults_to_hold():
    d = {
        "version": 1,
        "symbols": [
            {
                "symbol": "TTWO",
                "category": "HOLD",
                "alerts": [{"id": "h1", "operator": "crossing", "value": 209.55}],
            },
            {
                "symbol": "MSFT",
                "category": "HOLD",
                "alerts": [{"id": "h2", "operator": "greater_than", "value": 420.0, "label": "HOLD"}],
            },
        ],
    }
    wl = parse_watchlist(json.loads(json.dumps(d), parse_float=Dc), Config())
    assert wl.get("TTWO").alerts[0].label == "HOLD"
    assert wl.get("MSFT").alerts[0].label == "HOLD"


def test_category_change_keeps_trade_numbers():
    d = json.loads(json.dumps(EXAMPLE))
    d["symbols"][0]["category"] = "WATCH"
    wl = parse_watchlist(json.loads(json.dumps(d), parse_float=Dc), Config())
    assert wl.get("AAPL").trade.shares == 10


def test_bad_config_falls_back_with_warning_and_ignores_legacy_keys(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"poll_seconds": "fast"}')
    cfg, warns = load_config(p)
    assert cfg.poll_seconds == 60 and "invalid" in warns[0]
    p.write_text('{"poll_seconds": 120, "fees": {"sell_rate_pct": 0.5}, "quiet_hours_ist": {"start": "23:00", "end": "07:00"}}')
    cfg, warns = load_config(p)
    assert not warns and cfg.poll_seconds == 120
    assert in_quiet_hours(cfg, datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc))
    assert not in_quiet_hours(cfg, datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc))
    assert load_config(tmp_path / "missing.json")[1] == []
