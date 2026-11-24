"""Alert evaluation engine: 13 TradingView operators, price/% targets, sessions, expiration, triggers."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from config import Config
from data_checks import IGNORED, PAUSED, PriceFilter
from market_clock import AFTER_END_EARLY_T, AFTER_END_T, ET, MarketCalendar, Session, fmt_ist, is_active_session
from models import (
    ARMED,
    CHANNEL_OPERATORS,
    COOLDOWN,
    DISABLED,
    DISARMED,
    DONE,
    EXPIRED,
    MOVING_OPERATORS,
    OPERATOR_LABELS,
    STATE_OPERATORS,
    WAIT_CROSS,
    Alert,
    AlertEvent,
    CustomNotification,
    OHLCBar,
    PriceTick,
    StockConfig,
    resolve_notification_spec,
)
from positions import D, pct_distance_2dp, price_from_pct, q2, resolve_pct_base

VALID_ALERT_STATES = frozenset({ARMED, WAIT_CROSS, DISARMED, COOLDOWN, DONE, EXPIRED, DISABLED})
_SESSION_SHORT = {
    Session.PRE: "PH",
    Session.REGULAR: "RH",
    Session.AFTER: "AH",
    Session.CLOSED: "",
}


def event_urgency_rank(ev: AlertEvent) -> int:
    spec = ev.notification or resolve_notification_spec(ev.priority)
    if spec.repeat_until_ack or ev.priority == "HIGH":
        return 2
    if spec.popup or ev.priority == "MEDIUM":
        return 1
    return 0


def sort_by_priority(events: list) -> list:
    return sorted(events, key=lambda e: -event_urgency_rank(e))


class Notification:
    """Several events from one reading, merged into one notification (highest urgency first)."""

    def __init__(self, events: list):
        self.events = sort_by_priority(events)

    @property
    def priority(self) -> str:
        top = self.events[0]
        spec = top.notification or resolve_notification_spec(top.priority)
        if spec.repeat_until_ack or top.priority == "HIGH":
            return "HIGH"
        if spec.popup or top.priority == "MEDIUM":
            return "MEDIUM"
        return "LOW"

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


def _parse_iso_utc(raw: Optional[object], fallback_tz=timezone.utc) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=fallback_tz)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def compute_expires_at(preset: str, now: datetime, calendar: Optional[MarketCalendar] = None) -> Optional[str]:
    """Resolve an expiration preset into a UTC ISO-8601 string (None for open_ended)."""
    if preset == "open_ended":
        return None
    now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    if preset == "1_week":
        return (now_utc + timedelta(days=7)).isoformat(timespec="seconds")
    if preset == "1_month":
        return (now_utc + timedelta(days=30)).isoformat(timespec="seconds")
    if preset in ("end_of_session", "end_of_next_session"):
        cal = calendar or MarketCalendar()
        et = now_utc.astimezone(ET)
        found = []
        for i in range(15):
            d = et.date() + timedelta(days=i)
            if not cal.is_trading_day(d):
                continue
            end_t = AFTER_END_EARLY_T if cal.is_early_close(d) else AFTER_END_T
            end_dt = datetime.combine(d, end_t, ET)
            if end_dt > et:
                found.append(end_dt)
                if len(found) == (1 if preset == "end_of_session" else 2):
                    return found[-1].astimezone(timezone.utc).isoformat(timespec="seconds")
    return None


def session_matches_scope(session: Session, scope: str, extended_hours: bool = True) -> bool:
    """Return True if an alert with `scope` is eligible to evaluate during `session`."""
    if session == Session.CLOSED:
        return False
    if not extended_hours and session != Session.REGULAR:
        return False
    if scope == "regular":
        return session == Session.REGULAR
    if scope == "extended":
        return session in (Session.PRE, Session.AFTER)
    return session in (Session.PRE, Session.REGULAR, Session.AFTER)


def resolve_alert_targets(
    stock: StockConfig,
    alert: Alert,
    prev_close: Optional[Decimal] = None,
) -> tuple[Optional[Decimal], Optional[tuple[Decimal, Decimal]], str, str]:
    """Resolve alert condition target(s) in dollars and readable text.
    Returns (single_target, channel_bounds, target_text, base_desc)."""
    op = alert.operator
    if op in MOVING_OPERATORS:
        if alert.condition_mode == "pct" and alert.value is not None:
            txt = f"{q2(alert.value):.2f}% ({alert.window_min}m)"
        elif alert.value is not None:
            txt = f"${q2(alert.value):.2f} ({alert.window_min}m)"
        else:
            txt = f"({alert.window_min}m)"
        return None, None, txt, ""

    if alert.condition_mode == "price":
        if op in CHANNEL_OPERATORS:
            if alert.lower is None or alert.upper is None:
                return None, None, "", ""
            return None, (alert.lower, alert.upper), f"{q2(alert.lower):.2f}–{q2(alert.upper):.2f}", ""
        if alert.value is None:
            return None, None, "", ""
        return alert.value, None, f"{q2(alert.value):.2f}", ""

    # Percentage mode -> resolve base price
    base, base_label = resolve_pct_base(stock, alert.pct_base, prev_close=prev_close, ref_price=alert.ref_price)
    if base is None or base <= 0:
        return None, None, "", ""
    if op in CHANNEL_OPERATORS:
        if alert.lower is None or alert.upper is None:
            return None, None, "", base_label
        lo_p = price_from_pct(base, alert.lower)
        hi_p = price_from_pct(base, alert.upper)
        return None, (lo_p, hi_p), f"{q2(lo_p):.2f}–{q2(hi_p):.2f}", base_label
    if alert.value is None:
        return None, None, "", base_label
    tgt = price_from_pct(base, alert.value)
    return tgt, None, f"{q2(tgt):.2f}", base_label


def default_alert_message(symbol: str, alert: Alert, target_text: str = "") -> str:
    op_lbl = OPERATOR_LABELS.get(alert.operator, alert.operator)
    if not target_text:
        if alert.operator in CHANNEL_OPERATORS and alert.lower is not None and alert.upper is not None:
            suffix = "%" if alert.condition_mode == "pct" else ""
            target_text = f"{alert.lower}{suffix}–{alert.upper}{suffix}"
        elif alert.value is not None:
            suffix = "%" if alert.condition_mode == "pct" else ""
            target_text = f"{alert.value}{suffix}"
    return f"{symbol} {op_lbl} {target_text}".strip()


def render_alert_message(template: str, ctx: dict) -> str:
    out = template
    for key in ("ticker", "price", "target", "change_pct", "session", "time"):
        out = out.replace("{{" + key + "}}", str(ctx.get(key, "")))
    return out


class _Runtime:
    """In-memory per-stock data (never persisted across restarts)."""

    def __init__(self):
        self.filter = PriceFilter()
        self.prev_price: Optional[Decimal] = None
        self.history: deque[tuple[datetime, Decimal]] = deque(maxlen=1500)
        self.confirm: dict[str, int] = {}

    @property
    def paused(self) -> bool:
        return self.filter.paused


class AlertEngine:
    def __init__(self, cfg: Config, state: Optional[dict] = None):
        self.cfg = cfg
        self.state = state if isinstance(state, dict) else {"version": 1, "symbols": {}}
        if not isinstance(self.state.get("symbols"), dict):
            self.state["symbols"] = {}
        self._rt: dict[str, _Runtime] = {}

    # ---- state helpers ---------------------------------------------------------------------
    def runtime(self, symbol: str) -> _Runtime:
        return self._rt.setdefault(symbol, _Runtime())

    def sym_state(self, symbol: str) -> dict:
        s = self.state["symbols"].get(symbol)
        if not isinstance(s, dict):
            s = {"last_price": None, "prev_close": None, "alerts": {}}
            self.state["symbols"][symbol] = s
        s.setdefault("last_price", None)
        s.setdefault("prev_close", None)
        if not isinstance(s.get("alerts"), dict):
            s["alerts"] = {}
        return s

    def alert_state(self, symbol: str, alert_id: str) -> dict:
        alerts = self.sym_state(symbol)["alerts"]
        st = alerts.get(alert_id)
        if not isinstance(st, dict):
            st = {"state": ARMED, "cooldown_until": None}
            alerts[alert_id] = st
        if st.get("state") not in VALID_ALERT_STATES:
            st["state"] = ARMED
        st.setdefault("cooldown_until", None)
        return st

    def is_paused(self, symbol: str) -> bool:
        return self.runtime(symbol).paused

    def remove_stock(self, symbol: str) -> None:
        self.state["symbols"].pop(symbol, None)
        self._rt.pop(symbol, None)

    def remove_alert(self, symbol: str, alert_id: str) -> None:
        self.sym_state(symbol)["alerts"].pop(alert_id, None)
        self.runtime(symbol).confirm.pop(alert_id, None)

    # ---- main entry ------------------------------------------------------------------------
    def process_tick(self, stock: StockConfig, tick: PriceTick, now: datetime, session: Session) -> list:
        if (tick.price is None or not isinstance(tick.price, Decimal)
                or not tick.price.is_finite() or tick.price <= 0):
            return []
        rt = self.runtime(stock.symbol)
        status = rt.filter.submit(tick.price)
        if status in (IGNORED, PAUSED):
            rt.confirm.clear()
            return []

        price = tick.price
        ss = self.sym_state(stock.symbol)
        ss["last_price"] = str(price)
        ss["prev_close"] = str(tick.prev_close) if tick.prev_close else None
        if tick.pre_price is not None:
            ss["pre_price"] = str(tick.pre_price)
        elif session == Session.PRE:
            ss["pre_price"] = str(price)
        if tick.reg_price is not None:
            ss["reg_price"] = str(tick.reg_price)
        elif session == Session.REGULAR:
            ss["reg_price"] = str(price)
        if tick.ah_price is not None:
            ss["ah_price"] = str(tick.ah_price)
        elif session == Session.AFTER:
            ss["ah_price"] = str(price)

        if session == Session.CLOSED:
            return []

        now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        rt.history.append((now_utc, price))
        cutoff = now_utc - timedelta(hours=24)
        while rt.history and rt.history[0][0] < cutoff:
            rt.history.popleft()

        events = []
        for a in stock.alerts:
            st = self.alert_state(stock.symbol, a.id)
            if not a.enabled:
                st["state"] = DISABLED
                continue
            if st["state"] == DISABLED:
                st["state"] = ARMED

            exp_dt = _parse_iso_utc(a.expires_at)
            if exp_dt is not None and now_utc >= exp_dt:
                st["state"] = EXPIRED
                st["cooldown_until"] = None
                rt.confirm.pop(a.id, None)
                continue
            if st["state"] in (DONE, EXPIRED):
                continue

            if not session_matches_scope(session, a.session_scope, self.cfg.extended_hours):
                continue

            ev = self._eval_alert(stock, a, st, rt, tick, now_utc, session)
            if ev is not None:
                events.append(ev)

        rt.prev_price = price
        return sort_by_priority(events)

    def evaluate_offline_bars(
        self,
        stock: StockConfig,
        bars: list[OHLCBar],
        calendar: MarketCalendar,
        now: datetime,
    ) -> list[AlertEvent]:
        """Replay chronological 1m OHLC bars missed while offline.
        Evaluates intra-minute High/Low/Open/Close across enabled alerts, deduplicates
        to at most one summary AlertEvent per alert rule, and tracks the offline peak price."""
        if not bars or not stock.alerts:
            return []
        now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        rt = self.runtime(stock.symbol)
        ss = self.sym_state(stock.symbol)
        if rt.prev_price is None:
            rt.prev_price = _safe_dec(ss.get("last_price"))

        dedup: dict[str, AlertEvent] = {}
        fallback_pc = _safe_dec(ss.get("prev_close"))

        for bar in bars:
            if (bar.close is None or bar.close <= 0
                    or bar.high is None or bar.high <= 0
                    or bar.low is None or bar.low <= 0
                    or bar.open is None or bar.open <= 0):
                continue
            bar_utc = bar.time.replace(tzinfo=timezone.utc) if bar.time.tzinfo is None else bar.time.astimezone(timezone.utc)
            rt.history.append((bar_utc, bar.close))
            cutoff = bar_utc - timedelta(hours=24)
            while rt.history and rt.history[0][0] < cutoff:
                rt.history.popleft()

            session = calendar.session_at(bar_utc)
            if session == Session.CLOSED:
                rt.prev_price = bar.close
                continue

            if bar.close >= bar.open:
                raw_probes = (bar.open, bar.low, bar.high, bar.close)
            else:
                raw_probes = (bar.open, bar.high, bar.low, bar.close)
            probes: list[Decimal] = []
            for p in raw_probes:
                if not probes or probes[-1] != p:
                    probes.append(p)

            pc = bar.prev_close or fallback_pc
            for a in stock.alerts:
                st = self.alert_state(stock.symbol, a.id)
                if not a.enabled:
                    st["state"] = DISABLED
                    continue
                if st["state"] == DISABLED:
                    st["state"] = ARMED
                exp_dt = _parse_iso_utc(a.expires_at)
                if exp_dt is not None and bar_utc >= exp_dt:
                    st["state"] = EXPIRED
                    st["cooldown_until"] = None
                    rt.confirm.pop(a.id, None)
                    continue
                if st["state"] in (DONE, EXPIRED):
                    continue
                if not session_matches_scope(session, a.session_scope, self.cfg.extended_hours):
                    continue

            prev_before_bar = rt.prev_price
            for p in probes:
                probe_tick = PriceTick(
                    symbol=stock.symbol,
                    price=p,
                    prev_close=pc,
                    change=None,
                    change_pct=None,
                    day_high=bar.high,
                    day_low=bar.low,
                    provider_time=bar_utc,
                    fetched_at=now_utc,
                    source=bar.source or "yahoo_1m",
                )
                for a in stock.alerts:
                    st = self.alert_state(stock.symbol, a.id)
                    if not a.enabled or st["state"] in (DONE, EXPIRED, DISABLED):
                        continue
                    if not session_matches_scope(session, a.session_scope, self.cfg.extended_hours):
                        continue
                    cond, rearm_ok, _, tgt_txt = self._eval_condition(
                        stock, a, rt, p, pc, bar_utc
                    )
                    state = st["state"]
                    if state == WAIT_CROSS:
                        if not cond:
                            st["state"] = ARMED
                        continue
                    if state == DISARMED:
                        if not rearm_ok:
                            continue
                        st["state"] = ARMED
                    if st["state"] == COOLDOWN:
                        cd_until = _parse_iso_utc(st.get("cooldown_until"))
                        if cd_until is not None and bar_utc < cd_until:
                            continue
                        st["state"], st["cooldown_until"] = ARMED, None
                    if not cond:
                        continue
                    rt.confirm[a.id] = 0
                    self._after_fire(a, st, bar_utc)
                    if a.id not in dedup:
                        ev = self._make_event(stock, a, probe_tick, bar_utc, session, tgt_txt, offline=True)
                        ev.peak_price = p
                        dedup[a.id] = ev
                rt.prev_price = p

            if prev_before_bar is None:
                rt.prev_price = bar.close
            for ev in dedup.values():
                self._update_offline_peak(ev, bar)

        return sort_by_priority(list(dedup.values()))

    @staticmethod
    def _update_offline_peak(ev: AlertEvent, bar: OHLCBar) -> None:
        cur = ev.peak_price if ev.peak_price is not None else ev.price
        if ev.kind in ("less_than", "crossing_down", "moving_down", "moving_down_pct"):
            if bar.low < cur:
                ev.peak_price = bar.low
        elif ev.kind in ("greater_than", "crossing_up", "moving_up", "moving_up_pct"):
            if bar.high > cur:
                ev.peak_price = bar.high
        else:
            cand = bar.high if abs(bar.high - ev.price) >= abs(bar.low - ev.price) else bar.low
            if abs(cand - ev.price) > abs(cur - ev.price):
                ev.peak_price = cand

    def _eval_condition(
        self,
        stock: StockConfig,
        a: Alert,
        rt: _Runtime,
        price: Decimal,
        prev_close: Optional[Decimal],
        now_utc: datetime,
    ) -> tuple[bool, bool, bool, str]:
        """Returns (cond_met, rearm_ok, needs_confirm, target_text).
        `needs_confirm` is True for state/moving conditions when `noisy=True`."""
        op = a.operator
        prev = rt.prev_price
        band = self.cfg.level_rearm_band_pct / Decimal("100")

        if op in MOVING_OPERATORS:
            if a.value is None or a.value <= 0:
                return False, False, False, ""
            win_start = now_utc - timedelta(minutes=a.window_min)
            prices = [p for t, p in rt.history if t >= win_start]
            if len(prices) < 2:
                return False, False, False, f"{a.value} ({a.window_min}m)"
            if op in ("moving_up", "moving_up_pct"):
                ref_low = min(prices[:-1])
                if ref_low <= 0:
                    return False, False, False, ""
                if op == "moving_up":
                    cond = (price - ref_low) >= a.value
                    tgt_txt = f"+${q2(a.value):.2f} ({a.window_min}m)"
                else:
                    cond = ((price - ref_low) / ref_low * Decimal("100")) >= a.value
                    tgt_txt = f"+{q2(a.value):.2f}% ({a.window_min}m)"
            else:
                ref_high = max(prices[:-1])
                if ref_high <= 0:
                    return False, False, False, ""
                if op == "moving_down":
                    cond = (ref_high - price) >= a.value
                    tgt_txt = f"-${q2(a.value):.2f} ({a.window_min}m)"
                else:
                    cond = ((ref_high - price) / ref_high * Decimal("100")) >= a.value
                    tgt_txt = f"-{q2(a.value):.2f}% ({a.window_min}m)"
            return cond, (not cond), True, tgt_txt

        tgt, bounds, tgt_txt, _ = resolve_alert_targets(stock, a, prev_close)

        if op in CHANNEL_OPERATORS:
            if bounds is None:
                return False, False, False, ""
            lo, hi = bounds
            inside_now = lo <= price <= hi
            inside_prev = (lo <= prev <= hi) if prev is not None else None
            if op == "entering_channel":
                cond = bool(inside_prev is False and inside_now)
                rearm_ok = price <= lo * (1 - band) or price >= hi * (1 + band) if band > 0 else (not inside_now)
                return cond, rearm_ok, False, tgt_txt
            if op == "exiting_channel":
                cond = bool(inside_prev is True and not inside_now)
                rearm_ok = inside_now
                return cond, rearm_ok, False, tgt_txt
            if op == "inside_channel":
                rearm_ok = price <= lo * (1 - band) or price >= hi * (1 + band) if band > 0 else (not inside_now)
                return inside_now, rearm_ok, True, tgt_txt
            # outside_channel
            outside_now = not inside_now
            return outside_now, inside_now, True, tgt_txt

        if tgt is None or tgt <= 0:
            return False, False, False, ""

        if op == "crossing":
            cond = prev is not None and ((prev < tgt <= price) or (prev > tgt >= price))
            rearm_ok = (abs(price - tgt) >= tgt * band) if band > 0 else (price != tgt)
            return cond, rearm_ok, False, tgt_txt
        if op == "crossing_up":
            cond = prev is not None and (prev < tgt <= price)
            rearm_ok = (price <= tgt * (1 - band)) if band > 0 else (price < tgt)
            return cond, rearm_ok, False, tgt_txt
        if op == "crossing_down":
            cond = prev is not None and (prev > tgt >= price)
            rearm_ok = (price >= tgt * (1 + band)) if band > 0 else (price > tgt)
            return cond, rearm_ok, False, tgt_txt
        if op == "greater_than":
            cond = price > tgt
            rearm_ok = (price <= tgt * (1 - band)) if band > 0 else (price <= tgt)
            return cond, rearm_ok, True, tgt_txt
        # less_than
        cond = price < tgt
        rearm_ok = (price >= tgt * (1 + band)) if band > 0 else (price >= tgt)
        return cond, rearm_ok, True, tgt_txt

    def _eval_alert(
        self,
        stock: StockConfig,
        a: Alert,
        st: dict,
        rt: _Runtime,
        tick: PriceTick,
        now_utc: datetime,
        session: Session,
    ) -> Optional[AlertEvent]:
        cond, rearm_ok, needs_confirm, tgt_txt = self._eval_condition(
            stock, a, rt, tick.price, tick.prev_close, now_utc
        )
        state = st["state"]

        if state == WAIT_CROSS:
            if not cond:
                st["state"] = ARMED
            return None

        if state == DISARMED:
            if not rearm_ok:
                return None
            st["state"] = state = ARMED

        if state == COOLDOWN:
            cd_until = _parse_iso_utc(st.get("cooldown_until"))
            if cd_until is not None and now_utc < cd_until:
                return None
            st["state"], st["cooldown_until"] = ARMED, None
            state = ARMED

        # ARMED
        if not cond:
            rt.confirm[a.id] = 0
            return None

        if stock.noisy and needs_confirm:
            rt.confirm[a.id] = rt.confirm.get(a.id, 0) + 1
            if rt.confirm[a.id] < 2:
                return None

        rt.confirm[a.id] = 0
        self._after_fire(a, st, now_utc)
        return self._make_event(stock, a, tick, now_utc, session, tgt_txt)

    @staticmethod
    def _after_fire(a: Alert, st: dict, now_utc: datetime) -> None:
        if a.trigger == "once":
            st["state"] = DONE
            st["cooldown_until"] = None
        elif a.trigger == "rearm":
            st["state"] = DISARMED
            st["cooldown_until"] = None
        else:  # every_time
            st["state"] = COOLDOWN
            st["cooldown_until"] = (now_utc + timedelta(minutes=a.cooldown_min)).isoformat()

    # ---- creation check --------------------------------------------------------------------
    def would_fire_now(
        self,
        stock: StockConfig,
        alert: Alert,
        price: Optional[Decimal],
        prev_close: Optional[Decimal] = None,
    ) -> bool:
        if alert.operator not in STATE_OPERATORS or price is None:
            return False
        p = _safe_dec(price)
        if p is None or p <= 0:
            return False
        tgt, bounds, _, _ = resolve_alert_targets(stock, alert, prev_close)
        if alert.operator == "greater_than":
            return tgt is not None and p > tgt
        if alert.operator == "less_than":
            return tgt is not None and p < tgt
        if bounds is None:
            return False
        lo, hi = bounds
        if alert.operator == "inside_channel":
            return lo <= p <= hi
        return p < lo or p > hi

    def register_alert(
        self,
        stock: StockConfig,
        alert: Alert,
        tick: Optional[PriceTick],
        now: datetime,
        choice: Optional[str] = None,
        session: Session = Session.REGULAR,
    ) -> list:
        """Register or update alert state when created/edited/enabled.
        If `would_fire_now` is True and `choice == 'wait_cross'`, sets WAIT_CROSS;
        if `choice == 'fire_now'` and the session matches `session_scope`, fires immediately."""
        now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        st = self.alert_state(stock.symbol, alert.id)
        exp_dt = _parse_iso_utc(alert.expires_at)
        if not alert.enabled:
            st.update({"state": DISABLED, "cooldown_until": None})
            return []
        if exp_dt is not None and now_utc >= exp_dt:
            st.update({"state": EXPIRED, "cooldown_until": None})
            return []
        st.update({"state": ARMED, "cooldown_until": None})
        self.runtime(stock.symbol).confirm.pop(alert.id, None)

        if tick is None or tick.price <= 0:
            return []
        if not self.would_fire_now(stock, alert, tick.price, tick.prev_close):
            return []
        if choice == "wait_cross":
            st["state"] = WAIT_CROSS
            return []
        if choice != "fire_now":
            raise ValueError("This condition is already met: choose Fire now or Wait for next cross.")
        if not session_matches_scope(session, alert.session_scope, self.cfg.extended_hours):
            return []
        _, _, tgt_txt, _ = resolve_alert_targets(stock, alert, tick.prev_close)
        self._after_fire(alert, st, now_utc)
        return [self._make_event(stock, alert, tick, now_utc, session, tgt_txt)]

    # ---- event factory ---------------------------------------------------------------------
    def _make_event(
        self,
        stock: StockConfig,
        a: Alert,
        tick: PriceTick,
        now_utc: datetime,
        session: Session,
        target_text: str,
        offline: bool = False,
    ) -> AlertEvent:
        sess_code = _SESSION_SHORT.get(session, "")
        chg_pct = tick.change_pct
        if chg_pct is None and tick.prev_close and tick.prev_close > 0:
            chg_pct = pct_distance_2dp(tick.price, tick.prev_close)
        if chg_pct is not None:
            chg_val = q2(chg_pct)
            chg_str = f"{'+' if chg_val >= 0 else ''}{chg_val:.2f}%"
        else:
            chg_str = "N/A"
        default_msg = default_alert_message(stock.symbol, a, target_text)
        tmpl = a.message.strip() or default_msg
        rendered_msg = render_alert_message(
            tmpl,
            {
                "ticker": stock.symbol,
                "price": f"{q2(tick.price):.2f}",
                "target": target_text,
                "change_pct": chg_str,
                "session": sess_code or "CLOSED",
                "time": f"{fmt_ist(now_utc)} IST",
            },
        )
        spec: CustomNotification = resolve_notification_spec(a.priority, a.custom_notification)
        op_lbl = OPERATOR_LABELS.get(a.operator, a.operator)
        cond_str = f"{op_lbl} {target_text}".strip()
        return AlertEvent(
            symbol=stock.symbol,
            alert_id=a.id,
            kind=a.operator,
            label=a.label,
            priority=a.priority,
            price=tick.price,
            time=now_utc,
            source=tick.source,
            category=stock.category,
            condition=cond_str,
            session=sess_code,
            name=a.name,
            message=rendered_msg,
            target_text=target_text,
            change_pct=chg_pct,
            data_age_s=tick.age_seconds(now_utc),
            note=a.name,
            notification=spec,
            event_id=uuid4().hex[:12],
            offline=offline,
        )
