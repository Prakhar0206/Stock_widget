"""Alert engine (blueprint sections 5.5, 6, 8.5). Pure logic: no clock, no network, no UI.

Per reading: filter the price, update the zone, then (regular session only) evaluate alerts.
Alert state lives in a plain dict so it can be saved to state.json and restored.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from config import Config
from data_checks import IGNORED, PAUSED, PriceFilter
from market_clock import Session
from models import (ARMED, COOLDOWN, DISABLED, DISARMED, DONE, LEVEL_TYPES, PRIORITY_RANK, WAIT_CROSS,
                    Alert, AlertEvent, PriceTick, StockConfig)
from positions import net_estimate, pct_distance, price_from_pct, stock_base
from zones import TAGS, ZONE_GROUPS, ZoneTracker, band_for, build_zone_defs

VALID_ALERT_STATES = frozenset({ARMED, WAIT_CROSS, DISARMED, COOLDOWN, DONE, DISABLED})


def sort_by_priority(events: list) -> list:
    return sorted(events, key=lambda e: -PRIORITY_RANK[e.priority])


class Notification:
    """Several events from one reading, merged into one notification (highest priority first)."""

    def __init__(self, events: list):
        self.events = sort_by_priority(events)

    @property
    def priority(self) -> str:
        return self.events[0].priority

    @property
    def headline(self) -> AlertEvent:
        return self.events[0]


def merge_events(events: list) -> Optional[Notification]:
    return Notification(events) if events else None


def _safe_dec(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except Exception:
        return None


def _cooldown_active(until_str, now: datetime) -> bool:
    if not until_str or not isinstance(until_str, str):
        return False
    try:
        dt = datetime.fromisoformat(until_str)
        if (dt.tzinfo is None) != (now.tzinfo is None):
            dt = dt.replace(tzinfo=now.tzinfo)
        return now < dt
    except Exception:
        return False


class _Runtime:
    """In-memory per-stock data that is deliberately NOT saved: a restart starts a new session."""

    def __init__(self):
        self.filter = PriceFilter()
        self.tracker = ZoneTracker()
        self.seen = False
        self.prev_price: Optional[Decimal] = None
        self.confirm: dict = {}

    @property
    def paused(self) -> bool:
        return self.filter.paused


class AlertEngine:
    def __init__(self, cfg: Config, state: Optional[dict] = None):
        self.cfg = cfg
        self.state = state if isinstance(state, dict) else {"version": 1, "symbols": {}}
        if not isinstance(self.state.get("symbols"), dict):
            self.state["symbols"] = {}
        self._rt: dict = {}

    # ---- state helpers ---------------------------------------------------------------------
    def runtime(self, symbol) -> _Runtime:
        return self._rt.setdefault(symbol, _Runtime())

    def sym_state(self, symbol) -> dict:
        s = self.state["symbols"].get(symbol)
        if not isinstance(s, dict):
            s = {"last_price": None, "prev_close": None, "zone": None, "alerts": {}}
            self.state["symbols"][symbol] = s
        s.setdefault("last_price", None)
        s.setdefault("prev_close", None)
        s.setdefault("zone", None)
        if not isinstance(s.get("alerts"), dict):
            s["alerts"] = {}
        return s

    def alert_state(self, symbol, alert_id) -> dict:
        alerts = self.sym_state(symbol)["alerts"]
        st = alerts.get(alert_id)
        if not isinstance(st, dict):
            st = {"state": ARMED, "cooldown_until": None, "armed_prev_close": None}
            alerts[alert_id] = st
        if st.get("state") not in VALID_ALERT_STATES:
            st["state"] = ARMED
        st.setdefault("cooldown_until", None)
        st.setdefault("armed_prev_close", None)
        return st

    def is_paused(self, symbol) -> bool:
        return self.runtime(symbol).paused

    def remove_stock(self, symbol):
        self.state["symbols"].pop(symbol, None)
        self._rt.pop(symbol, None)

    def remove_alert(self, symbol, alert_id):
        self.sym_state(symbol)["alerts"].pop(alert_id, None)
        self.runtime(symbol).confirm.pop(alert_id, None)

    # ---- main entry ------------------------------------------------------------------------
    def process_tick(self, stock: StockConfig, tick: PriceTick, now: datetime,
                     session: Session) -> list:
        rt = self.runtime(stock.symbol)
        status = rt.filter.submit(tick.price)
        if status in (IGNORED, PAUSED):
            rt.tracker.pending = None
            rt.confirm.clear()
            return []                      # old/odd prices never fire alerts
        price = tick.price
        ss = self.sym_state(stock.symbol)
        ss["last_price"] = str(price)
        ss["prev_close"] = str(tick.prev_close) if tick.prev_close else None

        base = stock_base(stock, self.cfg.rates)
        p = pct_distance(price, base) if base is not None else None
        old_zone, new_zone = self._update_zone(stock, p, ss, rt)
        regular = session == Session.REGULAR
        if not regular:
            return []                      # outside the regular session: display only
        first = not rt.seen
        prev = rt.prev_price
        rt.seen, rt.prev_price = True, price
        events = []
        if not first and old_zone is not None and old_zone != new_zone:
            events += self._zone_events(stock, tick, old_zone, new_zone, p, base, now)
        events += self._alert_events(stock, tick, now, base, prev, rt)
        return sort_by_priority(events)

    # ---- zones -----------------------------------------------------------------------------
    def _update_zone(self, stock, p, ss, rt):
        defs = build_zone_defs(stock, self.cfg.zone_defaults)
        if not defs or p is None:
            ss["zone"] = None
            return None, None
        names = {d.name for d in defs}
        cur = ss.get("zone")
        if cur not in names:
            cur = None                     # first reading, or the category changed: just set state
        new = rt.tracker.update(p, defs, cur, band_for(stock, self.cfg), stock.noisy)
        ss["zone"] = new
        return cur, new

    def _zone_events(self, stock, tick, old, new, p, base, now: Optional[datetime] = None):
        defs = {d.name: d for d in build_zone_defs(stock, self.cfg.zone_defaults)}
        out = []
        d = defs.get(new)
        if d is not None and d.tag is not None and d.enabled:
            out.append(self._event(stock, tick, f"zone:{new}", "zone_entry", d.label, d.priority,
                                   condition=f"entered {new}", zone=new, p=p, base=base, now=now))
        for a in stock.alerts:
            if a.type != "zone_exit" or not a.enabled:
                continue
            group = ZONE_GROUPS.get(a.zone, frozenset({a.zone}))
            if old in group and new not in group:
                zname = "sell" if a.zone == "sell" else (TAGS.get(a.zone) or a.zone).lower()
                out.append(self._event(stock, tick, a.id, "zone_exit", a.label, a.priority,
                                       condition=f"left {zname}", zone=old, p=p, base=base,
                                       note=a.note, now=now))
        return out

    # ---- level / cross / daily-move alerts -------------------------------------------------------
    def level_price(self, alert: Alert, base) -> Optional[Decimal]:
        if alert.price is not None:
            return alert.price
        if alert.pct is not None and base is not None:
            return price_from_pct(base, alert.pct)
        return None

    def _sync_enabled(self, alert, st):
        if not alert.enabled:
            st["state"] = DISABLED
        elif st["state"] == DISABLED:
            st["state"] = ARMED

    def _alert_events(self, stock, tick, now, base, prev, rt):
        out = []
        price = tick.price
        for a in stock.alerts:
            if a.type == "zone_exit":
                continue
            st = self.alert_state(stock.symbol, a.id)
            self._sync_enabled(a, st)
            if st["state"] in (DISABLED, DONE):
                continue
            fired = False
            if a.type in LEVEL_TYPES:
                lvl = self.level_price(a, base)
                if lvl is None:
                    continue
                above = a.type == "level_above"
                cond = price >= lvl if above else price <= lvl
                band = self.cfg.level_rearm_band_pct / 100
                rearm_ok = price <= lvl * (1 - band) if above else price >= lvl * (1 + band)
                fired = self._step(a, st, rt, cond, rearm_ok, now, stock.noisy)
                cname = f"{'at or above' if above else 'at or below'} {lvl:.2f}"
                kind = "level"
            elif a.type in ("cross_above", "cross_below"):
                lvl = self.level_price(a, base)
                if lvl is None or prev is None:        # never fires on the first reading
                    continue
                above = a.type == "cross_above"
                crossed = (prev < lvl <= price) if above else (prev > lvl >= price)
                band = self.cfg.level_rearm_band_pct / 100
                rearm_ok = price <= lvl * (1 - band) if above else price >= lvl * (1 + band)
                if st["state"] == DISARMED and rearm_ok:
                    st["state"] = ARMED
                elif st["state"] == COOLDOWN and not _cooldown_active(st.get("cooldown_until"), now):
                    st["state"], st["cooldown_until"] = ARMED, None
                if st["state"] == ARMED and crossed:
                    fired = True
                    self._after_fire(a, st, now)
                cname = f"crossed {'above' if above else 'below'} {lvl:.2f}"
                kind = "cross"
            else:  # pct_move_day
                pc = tick.prev_close
                if a.pct is None or not pc or pc <= 0:
                    continue
                cond = abs(price - pc) / pc * 100 >= a.pct
                armed_pc = _safe_dec(st.get("armed_prev_close"))
                rearm_ok = armed_pc is None or armed_pc != pc    # new trading day
                if rearm_ok and st["state"] in (DISARMED, COOLDOWN):
                    st["state"], st["cooldown_until"], st["armed_prev_close"] = ARMED, None, None
                    rt.confirm.pop(a.id, None)
                fired = self._step(a, st, rt, cond, rearm_ok, now, stock.noisy, prev_close=pc)
                cname = f"moved {a.pct}% or more from previous close"
                kind = "pct_move"
            if fired:
                out.append(self._event(stock, tick, a.id, kind, a.label, a.priority, condition=cname,
                                       p=pct_distance(price, base) if base is not None else None,
                                       base=base, note=a.note, now=now))
        return out

    def _step(self, a, st, rt, cond, rearm_ok, now, noisy, prev_close=None) -> bool:
        """One state-machine step for a level or daily-move alert. True when it fires."""
        s = st["state"]
        if s in (DONE, DISABLED):
            return False
        if s == WAIT_CROSS:
            if not cond:
                st["state"] = ARMED        # price is now on the other side of the level
            return False
        if s == DISARMED:
            if not rearm_ok:
                return False
            st["state"] = s = ARMED
        if s == COOLDOWN:
            if _cooldown_active(st.get("cooldown_until"), now):
                return False
            if not cond:
                st["state"], st["cooldown_until"] = ARMED, None
                rt.confirm[a.id] = 0
                return False
            self._after_fire(a, st, now, prev_close)       # still holds after cooldown: fire again
            return True
        # ARMED
        if not cond:
            rt.confirm[a.id] = 0
            return False
        if noisy:                           # noisy stocks: the condition must hold on 2 readings
            rt.confirm[a.id] = rt.confirm.get(a.id, 0) + 1
            if rt.confirm[a.id] < 2:
                return False
        rt.confirm[a.id] = 0
        self._after_fire(a, st, now, prev_close)
        return True

    @staticmethod
    def _after_fire(a, st, now, prev_close=None):
        if a.mode == "once":
            st["state"] = DONE
        elif a.mode == "rearm":
            st["state"] = DISARMED
            st["armed_prev_close"] = str(prev_close) if prev_close is not None else None
        else:
            st["state"] = COOLDOWN
            st["cooldown_until"] = (now + timedelta(minutes=a.cooldown_min)).isoformat()
            st["armed_prev_close"] = str(prev_close) if prev_close is not None else None

    # ---- creation check (6.4) -----------------------------------------------------------------
    def would_fire_now(self, stock, alert, price) -> bool:
        if alert.type not in LEVEL_TYPES or price is None:
            return False
        lvl = self.level_price(alert, stock_base(stock, self.cfg.rates))
        if lvl is None:
            return False
        p = _safe_dec(price)
        if p is None or p <= 0:
            return False
        return p >= lvl if alert.type == "level_above" else p <= lvl

    def register_alert(self, stock, alert, tick: PriceTick, now, choice: Optional[str] = None,
                       session: Session = Session.REGULAR) -> list:
        """Call when an alert is saved. If it is already true, `choice` must be "fire_now" or
        "wait_cross" (the editor asks the user). Returns the events fired right now."""
        st = self.alert_state(stock.symbol, alert.id)
        st.update({"state": ARMED if alert.enabled else DISABLED,
                   "cooldown_until": None,
                   "armed_prev_close": None})
        self.runtime(stock.symbol).confirm.pop(alert.id, None)
        if not (alert.enabled and self.would_fire_now(stock, alert, tick.price)):
            return []
        if choice == "wait_cross":
            st["state"] = WAIT_CROSS
            return []
        if choice != "fire_now":
            raise ValueError("this alert is already true: choose fire_now or wait_cross")
        if session != Session.REGULAR:
            return []                      # outside regular hours: stays ARMED until regular session
        self._after_fire(alert, st, now)
        base = stock_base(stock, self.cfg.rates)
        lvl = self.level_price(alert, base)
        above = alert.type == "level_above"
        cname = f"{'at or above' if above else 'at or below'} {lvl:.2f}" if lvl is not None else "fired on creation"
        return [self._event(stock, tick, alert.id, "level", alert.label, alert.priority,
                            condition=cname, base=base, note=alert.note,
                            p=pct_distance(tick.price, base) if base is not None else None, now=now)]

    # ---- event factory -------------------------------------------------------------------------
    def _event(self, stock, tick, alert_id, kind, label, priority, *, condition="", zone=None,
               p=None, base=None, note="", now: Optional[datetime] = None):
        ev_time = now if now is not None else tick.fetched_at
        if stock.category in ("HOLD", "SELL"):
            base_name = "breakeven"
        elif stock.category == "BUY":
            base_name = "planned entry"
        else:
            base_name = ""
        return AlertEvent(
            symbol=stock.symbol, alert_id=alert_id, kind=kind, label=label, priority=priority,
            price=tick.price, time=ev_time, source=tick.source, category=stock.category,
            condition=condition, zone=zone, pct_vs_base=p,
            base_name=base_name,
            net_estimate=net_estimate(stock, tick.price, self.cfg.rates),
            data_age_s=tick.age_seconds(ev_time), note=note)