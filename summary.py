"""Daily summary card (blueprint 9.10). Local only; not written to the alert log."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from config import Config
from market_clock import ET, MarketCalendar  # noqa: F401  (MarketCalendar re-exported for callers)
from positions import net_estimate, q2
from zones import TAGS


def _dec(x) -> Optional[Decimal]:
    if x in (None, ""):
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None                                  # unreadable number -> treated as "no reading"


def _log_date(rec: dict) -> date:
    return datetime.fromisoformat(rec["time"]).astimezone(ET).date()


def _alerts_for_day(alert_log: list, day: date) -> dict:
    """One pass over the log: {symbol: [alert rows fired on `day`]} in log order.
    Each record's timestamp is parsed once, however many stocks are on the watchlist."""
    by_symbol: dict = {}
    for a in alert_log or ():
        try:
            if _log_date(a) != day:
                continue
            symbol = a["symbol"]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue                                 # malformed record: skip, never break the card
        by_symbol.setdefault(symbol, []).append(
            {"time": a["time"], "price": a.get("price"),
             "alert_id": a.get("alert_id"), "priority": a.get("priority")})
    return by_symbol


def _reading_for_day(engine_state: dict, symbol: str, day: date) -> dict:
    last = (engine_state or {}).get("last_regular") or {}
    ss = last.get(symbol)
    if not isinstance(ss, dict) or not ss:
        return {}
    try:
        if datetime.fromisoformat(ss["time"]).astimezone(ET).date() != day:
            return {}                                # that stock was not read on that day
    except (KeyError, TypeError, ValueError):
        return {}
    return ss


def build_card(wl, engine_state: dict, alert_log: list, close_dt: datetime, cfg: Config,
               built_at: datetime, built_after_close: bool) -> dict:
    """Uses each stock's last successful regular-session reading (state.json, "last_regular")."""
    day = close_dt.astimezone(ET).date()
    alerts_by_symbol = _alerts_for_day(alert_log, day)   # filtered once, not once per stock
    stocks = []
    for s in wl.symbols:
        ss = _reading_for_day(engine_state, s.symbol, day)
        price, pc = _dec(ss.get("price")), _dec(ss.get("prev_close"))
        zone = ss.get("zone")
        row = {"symbol": s.symbol, "category": s.category,
               "last_price": None if price is None else str(q2(price))}
        if price is not None and pc:
            row["change_today"] = str(q2(price - pc))
            row["change_today_pct"] = str(q2((price - pc) / pc * 100))
        if s.category in ("HOLD", "SELL"):
            row["zone"] = TAGS.get(zone or "") if zone else None
            n = net_estimate(s, price, cfg.rates) if price is not None else None
            row["est_net_pl"] = None if n is None else str(n)
        row["alerts_fired"] = list(alerts_by_symbol.get(s.symbol, ()))
        stocks.append(row)
    return {"date": day.isoformat(), "built_at": built_at.isoformat(timespec="seconds"),
            "built_after_close": built_after_close, "estimate_before_tax": True, "stocks": stocks}
