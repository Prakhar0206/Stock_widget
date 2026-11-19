"""Parse, validate and dump watchlist.json."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Optional

from config import Config, BudgetError, check_budget
from models import (
    ALERT_OPERATORS,
    CATEGORIES,
    CHANNEL_OPERATORS,
    CONDITION_MODES,
    DEFAULT_LABEL_BY_CATEGORY,
    EXPIRATION_PRESETS,
    LABELS,
    MOVING_OPERATORS,
    PCT_BASES,
    PRIORITIES,
    SESSION_SCOPES,
    SOUND_DURATIONS,
    SOUND_TONES,
    TRIGGER_MODES,
    Alert,
    CustomNotification,
    StockConfig,
    TradeInfo,
    ValidationError,
    Watchlist,
)

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
PCT_MIN, PCT_MAX = Decimal(-90), Decimal(500)

_LEGACY_OP_MAP = {
    "level_above": "greater_than",
    "level_below": "less_than",
    "cross_above": "crossing_up",
    "cross_below": "crossing_down",
    "pct_move_day": "moving_up_pct",
}
_LEGACY_TRIGGER_MAP = {
    "once": "once",
    "rearm": "rearm",
    "repeat": "every_time",
    "every_time": "every_time",
}


def _dec(v, what: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        raise ValidationError(f"{what} must be a number.")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except Exception:
        raise ValidationError(f"{what} must be a number.")
    if not d.is_finite():
        raise ValidationError(f"{what} must be a finite number.")
    return d


def _pos(v, what: str) -> Decimal:
    d = _dec(v, what)
    if d <= 0:
        raise ValidationError(f"{what} must be above 0.")
    return d


def _nonneg(v, what: str) -> Decimal:
    d = _dec(v, what)
    if d < 0:
        raise ValidationError(f"{what} cannot be negative.")
    return d


def _whole_int(v, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool):
        raise ValidationError(f"{what} must be a whole number ({lo} to {hi}).")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except Exception:
        raise ValidationError(f"{what} must be a whole number ({lo} to {hi}).")
    if not d.is_finite() or d != int(d) or not (lo <= int(d) <= hi):
        raise ValidationError(f"{what} must be a whole number ({lo} to {hi}).")
    return int(d)


def _parse_custom_notif(raw, prefix: str) -> Optional[CustomNotification]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationError(f"{prefix}: custom_notification must be an object.")
    tone = raw.get("sound_tone", "chime")
    if tone not in SOUND_TONES:
        raise ValidationError(f"{prefix}: sound_tone must be one of {', '.join(SOUND_TONES)}.")
    sec = _whole_int(raw.get("sound_seconds", 5), f"{prefix} sound_seconds", 0, 30)
    if sec not in SOUND_DURATIONS:
        raise ValidationError(f"{prefix}: sound_seconds must be 0, 5, 10 or 30.")
    return CustomNotification(
        popup=bool(raw.get("popup", True)),
        toast=bool(raw.get("toast", True)),
        sound_tone=tone,
        sound_seconds=sec,
        repeat_until_ack=bool(raw.get("repeat_until_ack", False)),
    )


def _migrate_legacy_alert_dict(a: dict) -> Optional[dict]:
    """Convert pre-revamp `type`-based alert dicts to the new `operator` schema.
    Returns None for obsolete `zone_exit` entries so old watchlists still load."""
    if "operator" in a or "type" not in a:
        return a
    t = a.get("type")
    if t == "zone_exit":
        return None
    op = _LEGACY_OP_MAP.get(t)
    if op is None:
        return a
    out = dict(a)
    out["operator"] = op
    if op in ("moving_up_pct", "moving_down_pct"):
        out["condition_mode"] = "pct"
        out["pct_base"] = "prev_close"
        out["value"] = a.get("pct")
        out.setdefault("window_min", 1440)
    elif a.get("price") is not None:
        out["condition_mode"] = "price"
        out["value"] = a.get("price")
    else:
        out["condition_mode"] = "pct"
        out["pct_base"] = "auto"
        out["value"] = a.get("pct")
    if "trigger" not in out and "mode" in out:
        out["trigger"] = _LEGACY_TRIGGER_MAP.get(out["mode"], "once")
    note = str(a.get("note", "") or "").strip()
    if note and not out.get("name"):
        out["name"] = note
    return out


def parse_alert(a: dict, stock: StockConfig, cfg: Config) -> Alert:
    if not isinstance(a, dict):
        raise ValidationError(f"{stock.symbol}: an alert is not an object.")
    if "operator" not in a and "type" in a:
        migrated = _migrate_legacy_alert_dict(a)
        if migrated is None:
            raise ValidationError(f"{stock.symbol}/{a.get('id', '?')}: legacy zone_exit alerts are removed.")
        a = migrated

    aid = a.get("id")
    if not aid or not isinstance(aid, str):
        raise ValidationError(f"{stock.symbol}: every alert needs an id.")

    op = a.get("operator", "crossing")
    if op not in ALERT_OPERATORS:
        raise ValidationError(f"{stock.symbol}/{aid}: unknown alert operator {op!r}.")

    cmode = a.get("condition_mode", "price")
    if cmode not in CONDITION_MODES:
        raise ValidationError(f"{stock.symbol}/{aid}: condition_mode must be 'price' or 'pct'.")
    if op in ("moving_up_pct", "moving_down_pct"):
        cmode = "pct"
    elif op in ("moving_up", "moving_down"):
        cmode = "price"

    pct_base = a.get("pct_base", "auto")
    if pct_base not in PCT_BASES:
        raise ValidationError(f"{stock.symbol}/{aid}: unknown pct_base {pct_base!r}.")
    ref_price = _pos(a["ref_price"], f"{stock.symbol}/{aid} ref_price") if a.get("ref_price") is not None else None
    if cmode == "pct" and op not in MOVING_OPERATORS and pct_base == "custom" and ref_price is None:
        raise ValidationError(f"{stock.symbol}/{aid}: custom % reference requires a price above 0.")

    val = upper = lower = None
    win = _whole_int(a.get("window_min", 5), f"{stock.symbol}/{aid} window_min", 1, 1440)

    if op in CHANNEL_OPERATORS:
        if a.get("lower") is None or a.get("upper") is None:
            raise ValidationError(f"{stock.symbol}/{aid}: channel alerts require both Lower and Upper bounds.")
        if cmode == "price":
            lower = _pos(a["lower"], f"{stock.symbol}/{aid} Lower")
            upper = _pos(a["upper"], f"{stock.symbol}/{aid} Upper")
        else:
            lower = _dec(a["lower"], f"{stock.symbol}/{aid} Lower")
            upper = _dec(a["upper"], f"{stock.symbol}/{aid} Upper")
            if not (PCT_MIN <= lower <= PCT_MAX and PCT_MIN <= upper <= PCT_MAX):
                raise ValidationError(f"{stock.symbol}/{aid}: channel percents must be between -90 and +500.")
        if lower >= upper:
            raise ValidationError(f"{stock.symbol}/{aid}: Lower bound must be less than Upper bound.")
    elif op in MOVING_OPERATORS:
        if a.get("value") is None:
            raise ValidationError(f"{stock.symbol}/{aid}: moving alerts require an amount above 0.")
        val = _pos(a["value"], f"{stock.symbol}/{aid} value")
        if op in ("moving_up_pct", "moving_down_pct") and val > PCT_MAX:
            raise ValidationError(f"{stock.symbol}/{aid}: moving percent must be at most +500.")
    else:
        if a.get("value") is None:
            raise ValidationError(f"{stock.symbol}/{aid}: enter a target value.")
        if cmode == "price":
            val = _pos(a["value"], f"{stock.symbol}/{aid} value")
        else:
            val = _dec(a["value"], f"{stock.symbol}/{aid} percent")
            if not (PCT_MIN <= val <= PCT_MAX):
                raise ValidationError(f"{stock.symbol}/{aid}: percent must be between -90 and +500.")

    trigger = a.get("trigger", "once")
    if trigger not in TRIGGER_MODES:
        raise ValidationError(f"{stock.symbol}/{aid}: trigger must be once, every_time or rearm.")
    cd = _whole_int(a.get("cooldown_min", 1), f"{stock.symbol}/{aid} cooldown_min", 1, 10080)

    exp = a.get("expiration", "open_ended")
    if exp not in EXPIRATION_PRESETS:
        raise ValidationError(f"{stock.symbol}/{aid}: invalid expiration {exp!r}.")
    exp_at = a.get("expires_at")
    if exp_at is not None:
        if not isinstance(exp_at, str) or not exp_at.strip():
            raise ValidationError(f"{stock.symbol}/{aid}: expires_at must be an ISO timestamp string.")
        try:
            datetime.fromisoformat(exp_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError(f"{stock.symbol}/{aid}: invalid expires_at timestamp.")

    scope = a.get("session_scope", "all")
    if scope not in SESSION_SCOPES:
        raise ValidationError(f"{stock.symbol}/{aid}: session_scope must be all, regular or extended.")

    label = a.get("label", DEFAULT_LABEL_BY_CATEGORY[stock.category])
    if label not in LABELS:
        raise ValidationError(f"{stock.symbol}/{aid}: label must be one of {', '.join(LABELS)}.")

    prio = a.get("priority", cfg.default_priority)
    if prio not in PRIORITIES:
        raise ValidationError(f"{stock.symbol}/{aid}: priority must be LOW, MEDIUM, HIGH or CUSTOM.")
    custom_notif = _parse_custom_notif(a.get("custom_notification"), f"{stock.symbol}/{aid}")

    return Alert(
        id=aid,
        operator=op,
        condition_mode=cmode,
        value=val,
        upper=upper,
        lower=lower,
        window_min=win,
        pct_base=pct_base,
        ref_price=ref_price,
        trigger=trigger,
        cooldown_min=cd,
        expiration=exp,
        expires_at=exp_at,
        session_scope=scope,
        name=str(a.get("name", "") or ""),
        message=str(a.get("message", "") or ""),
        priority=prio,
        custom_notification=custom_notif,
        enabled=bool(a.get("enabled", True)),
        label=label,
    )


def _parse_trade(d: dict, sym: str) -> Optional[TradeInfo]:
    """Parse manual `trade` dict or migrate legacy `position` / `planned_entry`."""
    t_raw = d.get("trade")
    if t_raw is not None:
        if not isinstance(t_raw, dict):
            raise ValidationError(f"{sym}: trade must be an object.")
        bp = _pos(t_raw["buy_price"], f"{sym} buy_price") if t_raw.get("buy_price") is not None else None
        sh = _pos(t_raw["shares"], f"{sym} shares") if t_raw.get("shares") is not None else None
        sp = _pos(t_raw["sell_price"], f"{sym} sell_price") if t_raw.get("sell_price") is not None else None
        fe = _nonneg(t_raw["fees"], f"{sym} fees") if t_raw.get("fees") is not None else None
        if bp is None and sh is None and sp is None and fe is None:
            return None
        return TradeInfo(buy_price=bp, shares=sh, sell_price=sp, fees=fe)

    # Migrate legacy position / planned_entry cleanly
    pos = d.get("position")
    pe = d.get("planned_entry")
    bp = sh = fe = None
    if isinstance(pos, dict):
        if pos.get("avg_price") is not None:
            bp = _pos(pos["avg_price"], f"{sym} average price")
        if pos.get("shares") is not None:
            sh = _pos(pos["shares"], f"{sym} shares")
        if pos.get("actual_buy_fee") is not None:
            fe = _nonneg(pos["actual_buy_fee"], f"{sym} fee")
    elif isinstance(pe, dict):
        if pe.get("price") is not None:
            bp = _pos(pe["price"], f"{sym} planned entry")
        if pe.get("shares") is not None:
            sh = _pos(pe["shares"], f"{sym} planned shares")
    if bp is None and sh is None and fe is None:
        return None
    return TradeInfo(buy_price=bp, shares=sh, fees=fe)


def parse_stock(d: dict, cfg: Config) -> StockConfig:
    if not isinstance(d, dict):
        raise ValidationError("A stock entry is not an object.")
    sym = str(d.get("symbol", "")).strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ValidationError(f"Bad ticker {d.get('symbol')!r}.")
    cat = d.get("category", "WATCH")
    if cat not in CATEGORIES:
        raise ValidationError(f"{sym}: category must be BUY, HOLD, SELL or WATCH.")
    trade = _parse_trade(d, sym)
    st = StockConfig(
        symbol=sym,
        category=cat,
        nickname=str(d.get("nickname", "")),
        noisy=bool(d.get("noisy", False)),
        trade=trade,
    )
    alerts_raw = d.get("alerts")
    if alerts_raw is not None and not isinstance(alerts_raw, list):
        raise ValidationError(f"{sym}: alerts must be a list.")
    seen = set()
    for a in alerts_raw or []:
        if isinstance(a, dict) and "operator" not in a and a.get("type") == "zone_exit":
            continue  # drop obsolete zone_exit alerts gracefully on load
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
        check_budget(len(wl.symbols), cfg.poll_seconds, num_keys=getattr(cfg, "num_keys", 1))
    except BudgetError as exc:
        raise ValidationError(str(exc))
    return wl


def _num(x: Optional[Decimal]):
    return None if x is None else float(x)


def dump_watchlist(wl: Watchlist) -> dict:
    out = []
    for s in wl.symbols:
        d = {"symbol": s.symbol, "nickname": s.nickname, "category": s.category, "noisy": s.noisy}
        if s.trade and any(
            v is not None for v in (s.trade.buy_price, s.trade.shares, s.trade.sell_price, s.trade.fees)
        ):
            t_dict = {}
            if s.trade.buy_price is not None:
                t_dict["buy_price"] = _num(s.trade.buy_price)
            if s.trade.shares is not None:
                t_dict["shares"] = _num(s.trade.shares)
            if s.trade.sell_price is not None:
                t_dict["sell_price"] = _num(s.trade.sell_price)
            if s.trade.fees is not None:
                t_dict["fees"] = _num(s.trade.fees)
            d["trade"] = t_dict
        al = []
        for a in s.alerts:
            x = {
                "id": a.id,
                "operator": a.operator,
                "condition_mode": a.condition_mode,
            }
            if a.value is not None:
                x["value"] = _num(a.value)
            if a.lower is not None:
                x["lower"] = _num(a.lower)
            if a.upper is not None:
                x["upper"] = _num(a.upper)
            if a.operator in MOVING_OPERATORS:
                x["window_min"] = int(a.window_min)
            if a.condition_mode == "pct" and a.pct_base != "auto":
                x["pct_base"] = a.pct_base
            if a.ref_price is not None:
                x["ref_price"] = _num(a.ref_price)
            x["trigger"] = a.trigger
            if a.trigger == "every_time":
                x["cooldown_min"] = int(a.cooldown_min)
            x["expiration"] = a.expiration
            if a.expires_at:
                x["expires_at"] = a.expires_at
            x["session_scope"] = a.session_scope
            if a.name:
                x["name"] = a.name
            if a.message:
                x["message"] = a.message
            x["label"] = a.label
            x["priority"] = a.priority
            if a.custom_notification is not None:
                x["custom_notification"] = {
                    "popup": bool(a.custom_notification.popup),
                    "toast": bool(a.custom_notification.toast),
                    "sound_tone": a.custom_notification.sound_tone,
                    "sound_seconds": int(a.custom_notification.sound_seconds),
                    "repeat_until_ack": bool(a.custom_notification.repeat_until_ack),
                }
            x["enabled"] = bool(a.enabled)
            al.append(x)
        d["alerts"] = al
        out.append(d)
    return {"version": 1, "symbols": out}
