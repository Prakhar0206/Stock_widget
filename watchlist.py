"""Parse, validate and dump watchlist.json (blueprint sections 9.8 and 10.2)."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from config import Config, check_budget, BudgetError
from models import (ALERT_TYPES, CATEGORIES, DEFAULT_LABEL_BY_CATEGORY, LABELS, MODES,
                    PRIORITIES, Alert, PlannedEntry, Position, StockConfig, ValidationError, Watchlist)
from zones import ZONE_DEFAULTS, check_thresholds

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
PCT_MIN, PCT_MAX = Decimal(-90), Decimal(500)
ZONE_NAMES_BY_CAT = {
    "SELL": {"deep_loss", "loss", "below_be", "breakeven", "above_be", "sell", "high_sell"},
    "HOLD": {"deep_loss", "loss", "below_be"},
    "BUY": {"deep_drop", "entry_hit", "near_entry", "above_entry", "far_above"},
    "WATCH": set(),
}
ALL_ZONE_NAMES = set().union(*ZONE_NAMES_BY_CAT.values()) | {"sell"}


def _dec(v, what: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        raise ValidationError(f"{what} must be a number.")
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except Exception:
        raise ValidationError(f"{what} must be a number.")


def _pos(v, what):
    d = _dec(v, what)
    if d <= 0:
        raise ValidationError(f"{what} must be above 0.")
    return d


def parse_alert(a: dict, stock: StockConfig, cfg: Config) -> Alert:
    if not isinstance(a, dict):
        raise ValidationError(f"{stock.symbol}: an alert is not an object.")
    aid = a.get("id")
    if not aid or not isinstance(aid, str):
        raise ValidationError(f"{stock.symbol}: every alert needs an id.")
    t = a.get("type")
    if t not in ALERT_TYPES:
        raise ValidationError(f"{stock.symbol}/{aid}: unknown alert type {t!r}.")
    label = a.get("label", DEFAULT_LABEL_BY_CATEGORY[stock.category])
    prio = a.get("priority", cfg.default_priority)
    mode = a.get("mode", cfg.level_alert_mode)
    if label not in LABELS:
        raise ValidationError(f"{stock.symbol}/{aid}: label must be one of {', '.join(LABELS)}.")
    if prio not in PRIORITIES:
        raise ValidationError(f"{stock.symbol}/{aid}: priority must be LOW, MEDIUM or HIGH.")
    if mode not in MODES:
        raise ValidationError(f"{stock.symbol}/{aid}: mode must be once, rearm or repeat.")
    price = _pos(a["price"], f"{stock.symbol}/{aid} price") if a.get("price") is not None else None
    pct = _dec(a["pct"], f"{stock.symbol}/{aid} pct") if a.get("pct") is not None else None
    zone = a.get("zone")
    if t == "zone_exit":
        if price is not None or pct is not None:
            raise ValidationError(f"{stock.symbol}/{aid}: a zone alert has no price or percent.")
        if zone not in ALL_ZONE_NAMES:
            raise ValidationError(f"{stock.symbol}/{aid}: unknown zone {zone!r}.")
    else:
        if (price is None) == (pct is None):
            raise ValidationError(f"{stock.symbol}/{aid}: set a price or a percent, not both and not neither.")
        if pct is not None and not (PCT_MIN <= pct <= PCT_MAX):
            raise ValidationError(f"{stock.symbol}/{aid}: percent must be between -90 and +500.")
        if pct is not None and t != "pct_move_day" and stock.category == "WATCH":
            raise ValidationError(f"{stock.symbol}/{aid}: WATCH stocks have no base for a percent alert; use a price.")
        if t == "pct_move_day" and pct is not None and pct <= 0:
            raise ValidationError(f"{stock.symbol}/{aid}: a daily move percent must be above 0.")
    cd = a.get("cooldown_min", cfg.cooldown_min)
    if isinstance(cd, bool) or not isinstance(cd, (int, Decimal)) or cd < 1:
        raise ValidationError(f"{stock.symbol}/{aid}: cooldown_min must be a whole number of minutes (1 or more).")
    return Alert(id=aid, type=t, label=label, priority=prio, enabled=bool(a.get("enabled", True)),
                 mode=mode, price=price, pct=pct, zone=zone, cooldown_min=int(cd), note=str(a.get("note", "")))


def make_default_slipping_alert(symbol: str) -> Alert:
    """Section 5.5: the sell-range zone-exit alert every SELL stock starts with (RISK, MEDIUM).
    The user can edit or delete it afterwards."""
    return Alert(id=f"{symbol.lower()}-slip", type="zone_exit", label="RISK", priority="MEDIUM",
                 zone="sell", note="Slipping")


def ensure_slipping_alert(stock: StockConfig) -> bool:
    """Add the default Slipping alert unless the stock already has a sell-range zone-exit alert.
    Called only when a SELL stock is ADDED or a stock is SWITCHED to SELL. It is never called when
    a watchlist is loaded from disk, so a deleted Slipping alert stays deleted across restarts."""
    if any(a.type == "zone_exit" and a.zone == "sell" for a in stock.alerts):
        return False
    alert = make_default_slipping_alert(stock.symbol)
    taken, base, n = {a.id for a in stock.alerts}, alert.id, 2
    while alert.id in taken:
        alert.id, n = f"{base}-{n}", n + 1
    stock.alerts.append(alert)
    return True


def parse_stock(d: dict, cfg: Config) -> StockConfig:
    if not isinstance(d, dict):
        raise ValidationError("A stock entry is not an object.")
    sym = str(d.get("symbol", "")).strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ValidationError(f"Bad ticker {d.get('symbol')!r}.")
    cat = d.get("category")
    if cat not in CATEGORIES:
        raise ValidationError(f"{sym}: category must be BUY, HOLD, SELL or WATCH.")
    st = StockConfig(symbol=sym, category=cat, nickname=str(d.get("nickname", "")),
                     noisy=bool(d.get("noisy", False)))
    p = d.get("position")
    if p is not None:
        fee = p.get("actual_buy_fee")
        fee = _dec(fee, f"{sym} actual buy fee") if fee is not None else None
        if fee is not None and fee < 0:
            raise ValidationError(f"{sym}: the buy fee cannot be below 0.")
        st.position = Position(_pos(p.get("shares"), f"{sym} shares"),
                               _pos(p.get("avg_price"), f"{sym} average price"), fee)
    e = d.get("planned_entry")
    if e is not None:
        sh = e.get("shares")
        st.planned_entry = PlannedEntry(_pos(e.get("price"), f"{sym} planned entry"),
                                        _pos(sh, f"{sym} planned shares") if sh is not None else None)
    if cat in ("HOLD", "SELL") and st.position is None:
        raise ValidationError(f"{sym}: {cat} needs shares and an average price.")
    if cat == "BUY" and st.planned_entry is None:
        raise ValidationError(f"{sym}: BUY needs a planned entry price.")
    z = d.get("zones", {}) or {}
    for name, ov in z.items():
        if name not in ZONE_DEFAULTS:
            raise ValidationError(f"{sym}: unknown zone {name!r}.")
        clean = {}
        if "pct" in ov:
            clean["pct"] = _dec(ov["pct"], f"{sym} {name} threshold")
        if "priority" in ov:
            if ov["priority"] not in PRIORITIES:
                raise ValidationError(f"{sym}: zone {name} priority must be LOW, MEDIUM or HIGH.")
            clean["priority"] = ov["priority"]
        if "enabled" in ov:
            clean["enabled"] = bool(ov["enabled"])
        st.zones[name] = clean
    check_thresholds(st, cfg.zone_defaults)
    seen = set()
    for a in d.get("alerts", []) or []:
        al = parse_alert(a, st, cfg)
        if al.id in seen:
            raise ValidationError(f"{sym}: duplicate alert id {al.id!r}.")
        seen.add(al.id)
        st.alerts.append(al)
    return st


def parse_watchlist(data: dict, cfg: Config) -> Watchlist:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValidationError("watchlist.json must be an object with version 1.")
    syms = data.get("symbols")
    if not isinstance(syms, list):
        raise ValidationError("watchlist.json needs a symbols list.")
    wl = Watchlist()
    seen = set()
    for item in syms:
        s = parse_stock(item, cfg)
        if s.symbol in seen:
            raise ValidationError(f"Duplicate ticker {s.symbol}.")
        seen.add(s.symbol)
        wl.symbols.append(s)
    if len(wl.symbols) > cfg.max_symbols:
        raise ValidationError(f"{len(wl.symbols)} stocks is above max_symbols ({cfg.max_symbols}).")
    try:
        check_budget(len(wl.symbols), cfg.poll_seconds)
    except BudgetError as exc:
        raise ValidationError(str(exc))
    return wl


def _num(x: Optional[Decimal]):
    return None if x is None else float(x)


def dump_watchlist(wl: Watchlist) -> dict:
    out = []
    for s in wl.symbols:
        d = {"symbol": s.symbol, "nickname": s.nickname, "category": s.category, "noisy": s.noisy}
        if s.position:
            d["position"] = {"shares": _num(s.position.shares), "avg_price": _num(s.position.avg_price),
                             "actual_buy_fee": _num(s.position.actual_buy_fee)}
        if s.planned_entry:
            d["planned_entry"] = {"price": _num(s.planned_entry.price), "shares": _num(s.planned_entry.shares)}
        if s.zones:
            d["zones"] = {n: {k: (_num(v) if k == "pct" else v) for k, v in ov.items()} for n, ov in s.zones.items()}
        al = []
        for a in s.alerts:
            x = {"id": a.id, "type": a.type}
            if a.price is not None:
                x["price"] = _num(a.price)
            if a.pct is not None:
                x["pct"] = _num(a.pct)
            if a.zone:
                x["zone"] = a.zone
            x.update({"label": a.label, "priority": a.priority})
            if a.type != "zone_exit":
                x["mode"] = a.mode
                if a.mode == "repeat":
                    x["cooldown_min"] = a.cooldown_min
            x.update({"enabled": a.enabled, "note": a.note})
            al.append(x)
        d["alerts"] = al
        out.append(d)
    return {"version": 1, "symbols": out}
