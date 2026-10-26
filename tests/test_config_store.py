import json
import os
from decimal import Decimal as Dc
from pathlib import Path

import pytest

import store as store_mod
from config import BudgetError, Config, check_budget, in_quiet_hours, load_config, parse_config
from models import ValidationError
from store import Store, atomic_write_json, atomic_write_text
from watchlist import parse_watchlist

EXAMPLE = {
    "version": 1,
    "symbols": [
        {"symbol": "AAPL", "nickname": "Apple", "category": "SELL", "noisy": False,
         "position": {"shares": 10, "avg_price": 200.00, "actual_buy_fee": None},
         "zones": {"deep_loss": {"enabled": False, "pct": -10.0},
                   "loss": {"pct": -3.0, "priority": "HIGH", "enabled": True},
                   "sell": {"pct": 3.0, "priority": "MEDIUM", "enabled": True},
                   "high_sell": {"pct": 4.5, "priority": "HIGH", "enabled": True}},
         "alerts": [{"id": "aapl-target", "type": "level_above", "price": 210.00, "label": "SELL",
                     "priority": "HIGH", "mode": "once", "enabled": True, "note": "My target"},
                    {"id": "aapl-slip", "type": "zone_exit", "zone": "sell", "label": "RISK",
                     "priority": "MEDIUM", "enabled": True, "note": "Slipping"}]},
        {"symbol": "TTWO", "nickname": "Take-Two", "category": "BUY", "noisy": False,
         "planned_entry": {"price": 202.73, "shares": 5},
         "alerts": [{"id": "ttwo-danger", "type": "level_below", "price": 195.00, "label": "RISK",
                     "priority": "HIGH", "mode": "once", "enabled": True, "note": "Danger zone"}]}]}


def write(path, data):
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def test_9_budget_refuses_30_symbols_at_30_seconds():
    assert check_budget(30, 60) == 31
    with pytest.raises(BudgetError):
        check_budget(30, 30)
    with pytest.raises(BudgetError):
        parse_config({"poll_seconds": 30})                   # default max_symbols 30 -> 62 calls


def test_blueprint_example_loads_and_roundtrips(tmp_path):
    cfg = Config()
    wl = parse_watchlist(json.loads(json.dumps(EXAMPLE), parse_float=Dc), cfg)
    assert [s.symbol for s in wl.symbols] == ["AAPL", "TTWO"]
    st = Store(tmp_path)
    st.save_watchlist(wl, cfg)
    wl2, warns = st.load_watchlist(cfg)
    assert not warns and wl2.get("AAPL").position.avg_price == Dc("200.00")
    assert wl2.get("AAPL").alerts[0].price == Dc("210")


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
    bad["symbols"][0]["zones"]["loss"]["pct"] = -20           # out of order vs deep_loss -10
    write(st.watchlist_path, bad)
    wl, warns = st.load_watchlist(cfg)
    assert len(wl.symbols) == 2 and warns


def test_invalid_and_missing_files_never_crash(tmp_path):
    st = Store(tmp_path)
    wl, warns = st.load_watchlist(Config())                   # nothing at all
    assert wl.symbols == [] and warns == []
    st.watchlist_path.write_text("garbage", encoding="utf-8")
    wl, warns = st.load_watchlist(Config())                   # broken and no last good
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
    assert [f.name for f in tmp_path.iterdir()] == ["x.json"]   # temp file cleaned up


def test_failed_save_writes_nothing(tmp_path):
    cfg = Config()
    st = Store(tmp_path)
    wl = parse_watchlist(json.loads(json.dumps(EXAMPLE), parse_float=Dc), cfg)
    st.save_watchlist(wl, cfg)
    before = st.watchlist_path.read_text()
    wl.symbols[0].position.shares = Dc(-1)
    wl.symbols[0].zones["loss"]["pct"] = Dc(5)
    with pytest.raises(ValidationError):
        st.save_watchlist(wl, cfg)
    assert st.watchlist_path.read_text() == before


@pytest.mark.parametrize("mutate,expect", [
    (lambda s: s["position"].update(shares=0), "shares"),
    (lambda s: s["position"].update(avg_price=-5), "average price"),
    (lambda s: s["position"].update(actual_buy_fee=-1), "fee"),
    (lambda s: s["alerts"][0].update(price=0), "price"),
    (lambda s: s["alerts"][0].update(pct=600), "price or a percent"),
    (lambda s: s["alerts"][0].update(price=None, pct=-95), "-90"),
    (lambda s: s["zones"]["sell"].update(pct=0.2), "Sell"),
    (lambda s: s.update(category="HOLDX"), "category"),
    (lambda s: s.update(symbol="aa pl"), "ticker"),
])
def test_validation_blocks_bad_input(mutate, expect):
    d = json.loads(json.dumps(EXAMPLE))
    mutate(d["symbols"][0])
    with pytest.raises(ValidationError) as e:
        parse_watchlist(json.loads(json.dumps(d), parse_float=Dc), Config())
    assert expect.lower() in str(e.value).lower()


def test_duplicates_and_cap_rejected():
    d = json.loads(json.dumps(EXAMPLE))
    d["symbols"].append(dict(d["symbols"][0]))
    with pytest.raises(ValidationError):
        parse_watchlist(d, Config())
    many = {"version": 1, "symbols": [{"symbol": f"A{i}", "category": "WATCH"} for i in range(31)]}
    with pytest.raises(ValidationError):
        parse_watchlist(many, Config())


def test_watch_percent_alert_rejected_buy_needs_entry():
    d = {"version": 1, "symbols": [{"symbol": "ZZ", "category": "WATCH",
                                    "alerts": [{"id": "a", "type": "level_above", "pct": 3}]}]}
    with pytest.raises(ValidationError):
        parse_watchlist(d, Config())
    with pytest.raises(ValidationError):
        parse_watchlist({"version": 1, "symbols": [{"symbol": "ZZ", "category": "BUY"}]}, Config())


def test_category_change_keeps_numbers():
    d = json.loads(json.dumps(EXAMPLE))
    d["symbols"][0]["category"] = "WATCH"
    d["symbols"][0]["alerts"] = []
    wl = parse_watchlist(json.loads(json.dumps(d), parse_float=Dc), Config())
    assert wl.get("AAPL").position.shares == 10


def test_bad_config_falls_back_with_warning(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"poll_seconds": "fast"}')
    cfg, warns = load_config(p)
    assert cfg.poll_seconds == 60 and "invalid" in warns[0]
    p.write_text('{"poll_seconds": 120, "fees": {"sell_rate_pct": 0.5}, "quiet_hours_ist": {"start": "23:00", "end": "07:00"}}')
    cfg, warns = load_config(p)
    assert not warns and cfg.poll_seconds == 120 and cfg.rates.sell_rate == Dc("0.005")
    from datetime import datetime, timezone
    assert in_quiet_hours(cfg, datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc))      # 00:30 IST
    assert not in_quiet_hours(cfg, datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc))  # 15:30 IST
    assert load_config(tmp_path / "missing.json")[1] == []
