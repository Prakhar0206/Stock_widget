"""Application controller: no Tk, no Win32. Everything the UI shows or changes goes through here,
which is why it can be tested with the mock provider and a fake notifier."""
from __future__ import annotations

import copy
import inspect
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional
from uuid import uuid4

from config import Config
from data_checks import FeedHealth
from engine import AlertEngine, Notification, compute_expires_at, merge_events, sort_by_priority
from market_clock import ET, IST, MarketCalendar, Session, is_active_session, resolve_session, utcnow
from models import (ARMED, COOLDOWN, DISABLED, DISARMED, DONE, EXPIRED, WAIT_CROSS, Alert, AlertEvent,
                    PriceTick, StockConfig, ValidationError, Watchlist, resolve_notification_spec)
from positions import q2
from scheduler import CLOSED_POLL_S
from store import Store

log = logging.getLogger("saw.app")


def _safe_dec(v) -> Optional[Decimal]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except Exception:
        return None
    return d if (d.is_finite() and d > 0) else None


def _fmt_session_price(p: Optional[Decimal]) -> str:
    return f"{q2(p):.2f}" if (p is not None and p > 0) else "—"


def _parse_iso_utc(raw) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class RowState:
    tick: Optional[PriceTick] = None       # last good reading
    last_ok: Optional[datetime] = None
    paused: bool = False
    pre_price: Optional[Decimal] = None
    reg_price: Optional[Decimal] = None
    ah_price: Optional[Decimal] = None


@dataclass
class RowView:
    symbol: str
    nickname: str
    price_text: str
    change_text: str
    arrow: str
    category: str
    zone_tag: str
    alerts: int
    color: str          # up | down | neutral | amber | dim
    age_s: Optional[int]
    warning: str = ""
    ph_price_text: str = "—"
    rh_price_text: str = "—"
    ah_price_text: str = "—"


@dataclass
class ProblemIconView:
    kind: str           # key_invalid | rate_limited | offline | server_down | custom
    glyph: str          # 🔑❌ | ⏳ | 🚫📶 | ☁️❌
    color: str          # red | amber
    badge: str          # e.g. "2" or "1, 3" or ""
    tooltip: str        # crisp single-line hover tooltip


@dataclass
class StatusStripView:
    session_icon: str                        # "🌅" PRE, "☀️" REGULAR, "🌇" AFTER, "🌙" CLOSED
    session_tooltip: str                     # e.g. "US market open (regular session)" / "US market closed"
    heartbeat_color: str                     # "green" | "amber" | "red"
    heartbeat_dim: bool                      # True when market polling session is inactive
    heartbeat_tooltip: str                   # e.g. "Live, updated 8s ago" / "Market closed, next update in 11 min"
    last_heartbeat_at: Optional[datetime]    # updated on every successful fetch
    problems: list[ProblemIconView] = field(default_factory=list)   # max 3 visible problem icons
    overflow_count: int = 0                  # >0 when >3 problems active (renders "+N")
    overflow_tooltip: str = ""
    session: str = "REGULAR"


@dataclass
class HeaderView:
    market: str
    ist: str
    ny: str
    feed_dot: str       # green | amber | red
    feed_text: str
    muted: bool
    # Redesigned status strip fields attached for direct access
    session_icon: str = "🌙"
    heartbeat_dim: bool = False
    heartbeat_tooltip: str = ""
    problems: list[ProblemIconView] = field(default_factory=list)
    overflow_count: int = 0


class AppController:
    def __init__(self, cfg: Config, store: Store, provider, notifier, *, calendar: MarketCalendar = None,
                 now_fn: Callable[[], datetime] = utcnow, warnings: list = None, polling_enabled: bool = True,
                 run_call: Callable = None, poll_now: Callable = lambda: None,
                 session_override: Optional[Session] = None):
        self.cfg, self.store, self.provider, self.notifier = cfg, store, provider, notifier
        self.calendar = calendar or MarketCalendar()
        self.now_fn = now_fn
        self.warnings = list(warnings or [])
        self.polling_enabled = polling_enabled
        self.poll_now = poll_now
        self.session_override = session_override      # demo mode only: pretend the market is open
        self.run_call = run_call or self._run_inline
        wl, w = store.load_watchlist(cfg)
        self.wl: Watchlist = wl
        self.warnings += w
        self.engine = AlertEngine(cfg, store.load_state())
        self.rows: dict = {}
        self.feed = FeedHealth(cfg.stale_after_seconds)
        self.feed_kind = "ok" if polling_enabled else "nokey"
        self.provider_status: Optional[dict] = None
        self.provider_status_at: Optional[datetime] = None
        self.notices: dict = {}
        self.last_heartbeat_at: Optional[datetime] = None
        self.last_poll_at: Optional[datetime] = None
        self.server_down: bool = False
        self.extra_problems: list[ProblemIconView] = []   # extensible for >3 simultaneous problem tests
        self._notified_invalid_keys: set[int] = set()
        self._last_session: Optional[Session] = None
        self._quote_listeners: list[Callable] = []
        self._inbox_listeners: list[Callable] = []
        self._catchup_in_flight: bool = False
        self._last_catchup_at: Optional[datetime] = None
        self._pending_catchup_since: Optional[datetime] = None
        self._syncing_ack: bool = False
        if not isinstance(self.engine.state.get("unacked_alerts"), list):
            self.engine.state["unacked_alerts"] = []
        self._startup_last_active_at: Optional[datetime] = _parse_iso_utc(
            self.engine.state.get("last_active_at")
        )
        self._last_active_saved_at: Optional[datetime] = None
        add_ack = getattr(self.notifier, "add_ack_listener", None)
        if callable(add_ack):
            add_ack(self.acknowledge_live_events)

    @staticmethod
    def _run_inline(fn, cb):
        try:
            res = fn()
        except Exception as exc:
            cb(None, exc)
        else:
            cb(res, None)

    # ---- data in (main thread only) --------------------------------------------------------
    def drain(self, q) -> None:
        """Handle everything the poller thread has queued."""
        while not q.empty():
            msg = q.get_nowait()
            kind = msg[0]
            if kind == "ticks":
                self.on_ticks(msg[1], msg[2])
            elif kind == "status":
                self.provider_status, self.provider_status_at = msg[1], msg[2]
            elif kind == "invalid_key":
                self.on_invalid_key(msg[1], msg[2], msg[3])
            elif kind == "error":
                self.on_error(msg[1], msg[2], msg[3])
            elif kind == "call":
                try:
                    msg[1](msg[2], msg[3])
                except Exception:
                    log.exception("callback failed")
        self._check_provider_invalid_keys(self.now_fn())

    def _check_provider_invalid_keys(self, now: datetime) -> None:
        pool_fn = getattr(self.provider, "pool_status", None)
        if callable(pool_fn):
            for item in pool_fn():
                num = item.get("num")
                if item.get("state") == "ok" and num is not None:
                    self._notified_invalid_keys.discard(num)
                    self.notices.pop(f"auth_{num}", None)
        pop_fn = getattr(self.provider, "pop_invalid_key_events", None)
        if callable(pop_fn):
            for key_num, code in pop_fn():
                self.on_invalid_key(key_num, code, now)

    def _notify_quote_listeners(self) -> None:
        for fn in list(self._quote_listeners):
            try:
                fn()
            except Exception:
                log.exception("quote listener failed")

    def add_inbox_listener(self, fn: Callable) -> None:
        if fn not in self._inbox_listeners:
            self._inbox_listeners.append(fn)

    def remove_inbox_listener(self, fn: Callable) -> None:
        if fn in self._inbox_listeners:
            self._inbox_listeners.remove(fn)

    def _notify_inbox_listeners(self) -> None:
        for fn in list(self._inbox_listeners):
            try:
                fn()
            except Exception:
                log.exception("inbox listener failed")

    def _touch_active(self, now: datetime) -> None:
        now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        self.engine.state["last_active_at"] = now_utc.isoformat(timespec="seconds")

    def _check_session_transition(self, sess: Session) -> None:
        if self._last_session is not None and self._last_session != sess:
            if is_active_session(sess, getattr(self.cfg, "extended_hours", True)):
                log.info("Session changed (%s -> %s), requesting immediate poll",
                         self._last_session.name, sess.name)
                self.poll_now()
        self._last_session = sess

    def session(self, now: datetime) -> Session:
        """Resolve authoritative US session from calendar rules, holiday closures, and live exchange periods."""
        if self.session_override is not None:
            return self.session_override
        return resolve_session(
            self.calendar,
            now,
            provider_status=self.provider_status,
            provider_status_at=self.provider_status_at,
            trading_periods=getattr(self.provider, "trading_periods", None),
        )

    def on_ticks(self, ticks: list, now: datetime) -> None:
        self._check_provider_invalid_keys(now)
        by = {t.symbol: t for t in ticks}
        session = self.session(now)
        active_sess = is_active_session(session, getattr(self.cfg, "extended_hours", True))
        if ticks:
            self.feed.record_success(now)
            self.last_heartbeat_at = now
            self.last_poll_at = now
            self.feed_kind = "ok"
            self.server_down = False
        self.feed.note_session(active_sess, now)
        et_date = now.astimezone(ET).strftime("%Y-%m-%d")
        events = []
        for stock in list(self.wl.symbols):
            t = by.get(stock.symbol)
            if t is None:
                continue
            row = self.rows.setdefault(stock.symbol, RowState())
            if t.price > 0:
                row.tick, row.last_ok = t, now
            events += self.engine.process_tick(stock, t, now, session)
            row.paused = self.engine.is_paused(stock.symbol)
            ss = self.engine.state.setdefault("symbols", {}).setdefault(stock.symbol, {})
            if ss.get("session_date") != et_date:
                ss["session_date"] = et_date
            if t.price > 0 and not row.paused:
                t_pre = _safe_dec(getattr(t, "pre_price", None))
                t_reg = _safe_dec(getattr(t, "reg_price", None))
                t_ah = _safe_dec(getattr(t, "ah_price", None))
                if t_pre is not None:
                    row.pre_price = t_pre
                    ss["pre_price"] = str(t_pre)
                elif session == Session.PRE:
                    row.pre_price = t.price
                    ss["pre_price"] = str(t.price)
                elif session == Session.CLOSED and row.pre_price is None:
                    prev_pre = _safe_dec(ss.get("pre_price"))
                    if prev_pre is not None:
                        row.pre_price = prev_pre
                if t_reg is not None:
                    row.reg_price = t_reg
                    ss["reg_price"] = str(t_reg)
                elif session == Session.REGULAR:
                    row.reg_price = t.price
                    ss["reg_price"] = str(t.price)
                elif session == Session.CLOSED:
                    if row.reg_price is None:
                        row.reg_price = _safe_dec(ss.get("reg_price"))
                    if row.reg_price is None:
                        row.reg_price = t.price
                        ss["reg_price"] = str(t.price)
                if t_ah is not None:
                    row.ah_price = t_ah
                    ss["ah_price"] = str(t_ah)
                elif session == Session.AFTER:
                    row.ah_price = t.price
                    ss["ah_price"] = str(t.price)
                elif session == Session.CLOSED and row.ah_price is None:
                    prev_ah = _safe_dec(ss.get("ah_price"))
                    if prev_ah is not None:
                        row.ah_price = prev_ah
            if session == Session.REGULAR and t.price > 0 and not row.paused:
                self.engine.state.setdefault("last_regular", {})[stock.symbol] = {
                    "price": str(t.price),
                    "prev_close": str(t.prev_close) if t.prev_close else None,
                    "time": now.isoformat(),
                }
        if ticks:
            self._touch_active(now)
            if self._pending_catchup_since is not None and not self._catchup_in_flight:
                pending_since = self._pending_catchup_since
                self._pending_catchup_since = None
                self.start_offline_catchup(now, since=pending_since)
        self.dispatch(events)
        self._save_state()
        self._notify_quote_listeners()

    def on_invalid_key(self, key_num: int, code: int, now: datetime) -> None:
        """Immediate one-time desktop notification when a key returns HTTP 401/403."""
        if key_num in self._notified_invalid_keys:
            return
        self._notified_invalid_keys.add(key_num)
        msg = f"Finnhub key #{key_num} is invalid (HTTP {code})"
        self.notices[f"auth_{key_num}"] = msg
        ev = AlertEvent(
            symbol="KEY",
            alert_id=f"key-invalid-{key_num}",
            kind="key_invalid",
            label="RISK",
            priority="HIGH",
            price=Decimal(0),
            time=now,
            source="finnhub",
            condition=msg,
            note=msg,
        )
        self.dispatch([ev])

    def on_error(self, kind: str, text: str, now: datetime) -> None:
        self._check_provider_invalid_keys(now)
        self.last_poll_at = now
        if kind == "network":
            self.feed_kind = "offline"
        elif kind == "server" or "HTTP 5" in text:
            self.feed_kind = "degraded"
            self.server_down = True
        elif kind in ("rate", "malformed"):
            self.feed_kind = "degraded"
        else:
            self.feed_kind = kind
        if kind in ("auth", "budget"):
            self.notices[kind] = text
        log.warning("poll error: %s %s", kind, text)

    def dispatch(self, events: list) -> None:
        """Every alert is logged BEFORE any notification is sent."""
        if not events:
            return
        added_inbox = False
        for ev in events:
            try:
                self.store.log_alert(ev)
            except OSError:
                log.exception("could not write the alert log")
            if self._record_unacked_event(ev):
                added_inbox = True
        if added_inbox:
            self._save_state()
            self._notify_inbox_listeners()
        try:
            self.notifier.notify(merge_events(events))
        except Exception:
            log.exception("notification failed")      # a broken backend never stops evaluation

    def _record_unacked_event(self, ev: AlertEvent) -> bool:
        if ev.kind in ("feed_stale", "key_invalid", "offline_summary") or ev.alert_id == "__test_alert__":
            return False
        items = self.engine.state.setdefault("unacked_alerts", [])
        if not isinstance(items, list):
            items = []
            self.engine.state["unacked_alerts"] = items
        stock = self.wl.get(ev.symbol)
        alert = next((a for a in stock.alerts if a.id == ev.alert_id), None) if stock else None
        trigger_type = alert.trigger if alert else "once"
        peak = ev.peak_price if ev.peak_price is not None else ev.price
        if ev.offline:
            for existing in items:
                if (existing.get("offline")
                        and existing.get("symbol") == ev.symbol
                        and existing.get("alert_id") == ev.alert_id):
                    existing["peak_price"] = str(peak)
                    return True
        if not ev.event_id:
            ev.event_id = uuid4().hex[:12]
        ev_utc = ev.time.replace(tzinfo=timezone.utc) if ev.time.tzinfo is None else ev.time.astimezone(timezone.utc)
        rec = {
            "event_id": ev.event_id,
            "symbol": ev.symbol,
            "alert_id": ev.alert_id,
            "name": ev.name or "",
            "operator": ev.kind,
            "priority": ev.priority,
            "condition": ev.condition or ev.kind,
            "target_text": ev.target_text or "",
            "trigger_price": str(ev.price),
            "peak_price": str(peak),
            "trigger_time": ev_utc.isoformat(timespec="seconds"),
            "session": ev.session or "",
            "source": ev.source or "",
            "offline": bool(ev.offline),
            "trigger_type": trigger_type,
            "message": ev.message or "",
        }
        items.append(rec)
        if len(items) > 300:
            del items[: len(items) - 300]
        return True

    def unacked_alerts(self, source_filter: str = "all", symbol: Optional[str] = None) -> list[dict]:
        items = self.engine.state.get("unacked_alerts")
        if not isinstance(items, list):
            return []
        sym_u = symbol.strip().upper() if symbol else None
        out = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            is_off = bool(raw.get("offline"))
            if source_filter == "offline" and not is_off:
                continue
            if source_filter == "live" and is_off:
                continue
            if sym_u and raw.get("symbol") != sym_u:
                continue
            sym = raw.get("symbol", "")
            aid = raw.get("alert_id", "")
            stock = self.wl.get(sym)
            alert = next((a for a in stock.alerts if a.id == aid), None) if stock else None
            row = self.rows.get(sym)
            live_p = row.tick.price if (row and row.tick and row.tick.price > 0) else None
            if live_p is None:
                ss = self.engine.state.get("symbols", {}).get(sym, {})
                live_p = _safe_dec(ss.get("last_price"))
            rec = dict(raw)
            rec["live_price"] = str(live_p) if live_p is not None else None
            rec["live_price_text"] = f"${q2(live_p):.2f}" if live_p is not None else "—"
            rec["rule_exists"] = alert is not None
            rec["alert_status"] = self.alert_status_label(sym, alert) if alert is not None else "Deleted"
            rec["can_rearm"] = self.can_rearm(sym, aid)
            out.append(rec)
        out.sort(key=lambda r: r.get("trigger_time", ""), reverse=True)
        return out

    def unacked_summary(self) -> dict:
        items = self.engine.state.get("unacked_alerts")
        if not isinstance(items, list):
            return {"total": 0, "offline": 0, "live": 0}
        off = sum(1 for r in items if isinstance(r, dict) and r.get("offline"))
        live = sum(1 for r in items if isinstance(r, dict) and not r.get("offline"))
        return {"total": off + live, "offline": off, "live": live}

    def acknowledge_alert(self, event_id: str) -> bool:
        items = self.engine.state.get("unacked_alerts")
        if not isinstance(items, list):
            return False
        removed_live: set[str] = set()
        kept = []
        removed = False
        for r in items:
            if isinstance(r, dict) and r.get("event_id") == event_id:
                removed = True
                if not r.get("offline"):
                    removed_live.add(event_id)
            else:
                kept.append(r)
        if not removed:
            return False
        self.engine.state["unacked_alerts"] = kept
        self._save_state()
        self._notify_inbox_listeners()
        self._sync_notifier_after_inbox_ack(removed_live)
        return True

    def acknowledge_all_alerts(self, source_filter: str = "all", symbol: Optional[str] = None) -> int:
        items = self.engine.state.get("unacked_alerts")
        if not isinstance(items, list):
            return 0
        sym_u = symbol.strip().upper() if symbol else None
        kept = []
        removed = 0
        removed_live: set[str] = set()
        for r in items:
            if not isinstance(r, dict):
                continue
            is_off = bool(r.get("offline"))
            match_src = (
                source_filter == "all"
                or (source_filter == "offline" and is_off)
                or (source_filter == "live" and not is_off)
            )
            match_sym = (not sym_u) or (r.get("symbol") == sym_u)
            if match_src and match_sym:
                removed += 1
                if not is_off and r.get("event_id"):
                    removed_live.add(r["event_id"])
            else:
                kept.append(r)
        if removed:
            self.engine.state["unacked_alerts"] = kept
            self._save_state()
            self._notify_inbox_listeners()
            self._sync_notifier_after_inbox_ack(removed_live)
        return removed

    def _sync_notifier_after_inbox_ack(self, removed_live_ids: set[str]) -> None:
        if self._syncing_ack or not removed_live_ids:
            return
        latest = getattr(self.notifier, "_latest", None)
        pending = getattr(self.notifier, "_pending_high", None)
        batch_live: set[str] = set()
        if isinstance(latest, dict) and not latest.get("acked"):
            batch_live.update(latest.get("live_event_ids") or ())
        if isinstance(pending, dict) and not pending.get("acked"):
            batch_live.update(pending.get("live_event_ids") or ())
        if not batch_live or not (batch_live & removed_live_ids):
            return
        remaining_live = {
            r.get("event_id")
            for r in (self.engine.state.get("unacked_alerts") or [])
            if isinstance(r, dict) and not r.get("offline") and r.get("event_id")
        }
        if batch_live & remaining_live:
            return
        ack_fn = getattr(self.notifier, "acknowledge_latest", None)
        if not callable(ack_fn):
            return
        self._syncing_ack = True
        try:
            ack_fn()
        finally:
            self._syncing_ack = False

    def acknowledge_live_events(self, event_ids: list[str]) -> int:
        if self._syncing_ack or not event_ids:
            return 0
        id_set = set(event_ids)
        items = self.engine.state.get("unacked_alerts")
        if not isinstance(items, list):
            return 0
        kept = []
        removed = 0
        for r in items:
            if not isinstance(r, dict):
                continue
            if not r.get("offline") and r.get("event_id") in id_set:
                removed += 1
            else:
                kept.append(r)
        if removed:
            self.engine.state["unacked_alerts"] = kept
            self._save_state()
            self._notify_inbox_listeners()
        return removed

    def can_rearm(self, symbol: str, alert_id: str) -> bool:
        stock = self.wl.get(symbol)
        if stock is None:
            return False
        alert = next((a for a in stock.alerts if a.id == alert_id), None)
        if alert is None:
            return False
        return self.engine.alert_state(symbol, alert_id).get("state") == DONE

    def rearm_alert(self, symbol: str, alert_id: str, now: Optional[datetime] = None) -> bool:
        stock = self.wl.get(symbol)
        if stock is None:
            return False
        alert = next((a for a in stock.alerts if a.id == alert_id), None)
        if alert is None:
            return False
        cur_state = self.engine.alert_state(symbol, alert_id).get("state")
        if cur_state != DONE and alert.enabled:
            return False
        t_now = now or self.now_fn()
        cand = copy.deepcopy(self.wl)
        cand_stock = cand.get(symbol)
        target = next(a for a in cand_stock.alerts if a.id == alert_id)
        target.enabled = True
        if target.expiration in ("end_of_session", "end_of_next_session", "1_week", "1_month"):
            target.expires_at = compute_expires_at(target.expiration, t_now, self.calendar)
        elif target.expiration == "custom":
            target.expiration = "open_ended"
            target.expires_at = None
        self._commit(cand)
        committed_stock = self.wl.get(symbol)
        committed_alert = next(a for a in committed_stock.alerts if a.id == alert_id)
        tick = (self.rows.get(symbol) or RowState()).tick
        sess = self.session(t_now)
        self.engine.register_alert(committed_stock, committed_alert, tick, t_now, choice="wait_cross", session=sess)
        self._save_state()
        self._notify_quote_listeners()
        self._notify_inbox_listeners()
        return True

    def housekeeping(self, now: datetime) -> None:
        """Call every few seconds: stale-feed alert (>3 min offline during market hours)."""
        self._check_provider_invalid_keys(now)
        session = self.session(now)
        self._check_session_transition(session)
        self.feed.note_session(is_active_session(session, getattr(self.cfg, "extended_hours", True)), now)
        if self.polling_enabled:
            r = self.feed.check(now)
            if r == "stale":
                self.dispatch([AlertEvent("FEED", "feed-stale", "feed_stale", "RISK", "MEDIUM", Decimal(0), now,
                                          "finnhub", condition="Feed offline: price data is stale",
                                          note="Feed offline: price data is stale")])
        now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        if not self._catchup_in_flight and self._pending_catchup_since is None:
            if self._last_active_saved_at is None or (now_utc - self._last_active_saved_at).total_seconds() >= 60:
                self._touch_active(now_utc)
                self._save_state()
                self._last_active_saved_at = now_utc

    def mark_shutdown(self, now: Optional[datetime] = None) -> None:
        t_now = now or self.now_fn()
        self._touch_active(t_now)
        self._save_state()

    def on_wake(self, now: datetime) -> None:
        """Wake, unlock or remote-desktop reconnect: poll immediately and run async offline catch-up."""
        prev_active = _parse_iso_utc(self.engine.state.get("last_active_at"))
        self.feed.reset_clock(now)
        self.poll_now()
        self.start_offline_catchup(now, since=prev_active)

    def start_offline_catchup(self, now: Optional[datetime] = None, since: Optional[datetime] = None) -> bool:
        """Fetch missed 1m OHLC bars since `last_active_at` on background thread and evaluate alerts."""
        if self._catchup_in_flight:
            return False
        t_now = now or self.now_fn()
        now_utc = t_now.replace(tzinfo=timezone.utc) if t_now.tzinfo is None else t_now.astimezone(timezone.utc)
        if since is None:
            since = self._startup_last_active_at
            self._startup_last_active_at = None
        if since is None:
            since = _parse_iso_utc(self.engine.state.get("last_active_at"))
        if since is None:
            self._touch_active(now_utc)
            self._save_state()
            return False
        if (now_utc - since).total_seconds() <= 60:
            self._touch_active(now_utc)
            self._save_state()
            return False
        oldest = now_utc - timedelta(days=6)
        if since < oldest:
            since = oldest
        active_syms = [
            s.symbol
            for s in self.wl.symbols
            if any(
                a.enabled and self.engine.alert_state(s.symbol, a.id).get("state") not in (DONE, EXPIRED)
                for a in s.alerts
            )
        ]
        fetch_fn = getattr(self.provider, "fetch_intraday_bars", None)
        if not active_syms or not callable(fetch_fn):
            self._touch_active(now_utc)
            self._save_state()
            return False

        self._catchup_in_flight = True

        def work():
            return fetch_fn(active_syms, since, now_utc)

        def on_done(bars_by_sym, exc):
            self._catchup_in_flight = False
            self._last_catchup_at = now_utc
            if exc is not None:
                log.warning("offline catch-up failed: %s", exc)
                if self._pending_catchup_since is None or since < self._pending_catchup_since:
                    self._pending_catchup_since = since
                return
            self._pending_catchup_since = None
            if bars_by_sym:
                self._apply_offline_catchup(bars_by_sym, now_utc)
            self._touch_active(now_utc)
            self._save_state()

        self.run_call(work, on_done)
        return True

    def _apply_offline_catchup(self, bars_by_sym: dict, now: datetime) -> list[AlertEvent]:
        offline_events: list[AlertEvent] = []
        for stock in list(self.wl.symbols):
            bars = bars_by_sym.get(stock.symbol) or []
            if bars:
                offline_events.extend(self.engine.evaluate_offline_bars(stock, bars, self.calendar, now))
        offline_events = sort_by_priority(offline_events)
        if not offline_events:
            return []
        for ev in offline_events:
            try:
                self.store.log_alert(ev)
            except OSError:
                log.exception("could not write offline alert log")
            self._record_unacked_event(ev)
        self._save_state()
        self._notify_inbox_listeners()
        self._notify_quote_listeners()

        syms = list(dict.fromkeys(ev.symbol for ev in offline_events))
        if len(offline_events) == 1:
            e0 = offline_events[0]
            summary_msg = f"1 alert triggered while offline: {e0.symbol} {e0.condition} @ ${q2(e0.price):.2f}"
        else:
            sym_list = ", ".join(syms[:4]) + ("..." if len(syms) > 4 else "")
            summary_msg = f"{len(offline_events)} alerts triggered while offline ({sym_list})"
        top_ev = offline_events[0]
        summary_ev = AlertEvent(
            symbol=top_ev.symbol if len(syms) == 1 else "OFFLINE",
            alert_id="offline-summary",
            kind="offline_summary",
            label=top_ev.label,
            priority=top_ev.priority,
            price=top_ev.price,
            time=now,
            source="yahoo_1m",
            category=top_ev.category,
            condition=summary_msg,
            message=summary_msg,
            note=f"{len(offline_events)} offline alert(s)",
            notification=resolve_notification_spec(top_ev.priority),
            event_id=uuid4().hex[:12],
            offline=False,
        )
        try:
            self.notifier.notify(Notification([summary_ev]))
        except Exception:
            log.exception("offline summary notification failed")
        return offline_events

    # ---- watchlist edits (save first; memory changes only if the save worked) --------------------
    def _commit(self, cand: Watchlist) -> None:
        self.store.save_watchlist(cand, self.cfg)    # validates, then writes atomically; raises on failure
        self.wl = cand

    def stock(self, symbol: str) -> Optional[StockConfig]:
        return self.wl.get(symbol)

    def check_symbol(self, symbol: str, callback: Callable) -> None:
        """Validate a ticker with the provider on the worker thread. callback(tick, exception)."""
        symbol = symbol.strip().upper()
        if self.wl.get(symbol):
            callback(None, ValidationError(f"{symbol} is already in the watchlist."))
            return

        def work():
            ticks = self.provider.fetch_quotes([symbol])
            if not ticks or ticks[0].price <= 0:
                raise ValidationError(f"No price data for {symbol}. Check the ticker.")
            return ticks[0]
        self.run_call(work, callback)

    def add_stock(self, stock: StockConfig) -> None:
        if self.wl.get(stock.symbol):
            raise ValidationError(f"{stock.symbol} is already in the watchlist.")
        new = copy.deepcopy(stock)
        cand = copy.deepcopy(self.wl)
        cand.symbols.append(new)
        self._commit(cand)
        self.engine.remove_stock(stock.symbol)       # new stock: its first reading only sets state
        self._notify_quote_listeners()
        self.poll_now()

    def update_stock(self, new: StockConfig, choices: Optional[dict] = None) -> None:
        old = self.wl.get(new.symbol)
        if old is None:
            raise ValidationError(f"{new.symbol} is not in the watchlist.")
        new = copy.deepcopy(new)
        cand = copy.deepcopy(self.wl)
        cand.symbols = [new if s.symbol == new.symbol else s for s in cand.symbols]
        self._commit(cand)
        stock = self.wl.get(new.symbol)
        old_alerts = {a.id: a for a in old.alerts}
        for aid in set(old_alerts) - {a.id for a in stock.alerts}:
            self.engine.remove_alert(stock.symbol, aid)
        events = []
        tick = (self.rows.get(stock.symbol) or RowState()).tick
        sess = self.session(self.now_fn())
        for a in stock.alerts:
            if old_alerts.get(a.id) == a:
                continue                              # unchanged: keep its state
            if tick is not None:
                choice = (choices or {}).get(a.id) or "wait_cross"
                events += self.engine.register_alert(stock, a, tick, self.now_fn(), choice, session=sess)
            else:
                st = self.engine.alert_state(stock.symbol, a.id)
                st.update({"state": ARMED if a.enabled else DISABLED, "cooldown_until": None})
        self.dispatch(events)
        self._save_state()
        self._notify_quote_listeners()

    def save_alert(self, symbol: str, alert: Alert, choice: Optional[str] = None) -> None:
        """Immediately add or update a single alert on `symbol` and persist atomically."""
        old = self.wl.get(symbol)
        if old is None:
            raise ValidationError(f"{symbol} is not in the watchlist.")
        cand = copy.deepcopy(self.wl)
        stock = cand.get(symbol)
        saved = copy.deepcopy(alert)
        replaced = False
        for i, existing in enumerate(stock.alerts):
            if existing.id == saved.id:
                stock.alerts[i] = saved
                replaced = True
                break
        if not replaced:
            stock.alerts.append(saved)
        self._commit(cand)
        committed_stock = self.wl.get(symbol)
        committed_alert = next(a for a in committed_stock.alerts if a.id == saved.id)
        tick = (self.rows.get(symbol) or RowState()).tick
        sess = self.session(self.now_fn())
        events = self.engine.register_alert(committed_stock, committed_alert, tick, self.now_fn(), choice, session=sess)
        self.dispatch(events)
        self._save_state()
        self._notify_quote_listeners()

    def delete_alert(self, symbol: str, alert_id: str) -> None:
        """Immediately remove an alert from `symbol` and persist atomically."""
        if self.wl.get(symbol) is None:
            raise ValidationError(f"{symbol} is not in the watchlist.")
        cand = copy.deepcopy(self.wl)
        stock = cand.get(symbol)
        stock.alerts = [a for a in stock.alerts if a.id != alert_id]
        self._commit(cand)
        self.engine.remove_alert(symbol, alert_id)
        self._save_state()
        self._notify_quote_listeners()

    def toggle_alert(self, symbol: str, alert_id: str, enabled: bool) -> None:
        """Immediately enable/disable an alert (re-arming DONE/EXPIRED alerts) and persist."""
        if self.wl.get(symbol) is None:
            raise ValidationError(f"{symbol} is not in the watchlist.")
        cand = copy.deepcopy(self.wl)
        stock = cand.get(symbol)
        target = next((a for a in stock.alerts if a.id == alert_id), None)
        if target is None:
            raise ValidationError(f"{symbol}/{alert_id}: alert not found.")
        target.enabled = bool(enabled)
        now = self.now_fn()
        cur_state = self.engine.alert_state(symbol, alert_id).get("state")
        if target.enabled and cur_state in (DONE, EXPIRED):
            if target.expiration in ("end_of_session", "end_of_next_session", "1_week", "1_month"):
                target.expires_at = compute_expires_at(target.expiration, now, self.calendar)
            elif target.expiration == "custom":
                target.expiration = "open_ended"
                target.expires_at = None
        self._commit(cand)
        committed_stock = self.wl.get(symbol)
        committed_alert = next(a for a in committed_stock.alerts if a.id == alert_id)
        tick = (self.rows.get(symbol) or RowState()).tick
        sess = self.session(now)
        self.engine.register_alert(committed_stock, committed_alert, tick, now, choice="wait_cross", session=sess)
        self._save_state()
        self._notify_quote_listeners()

    def alert_status_label(self, symbol: str, alert: Alert) -> str:
        if not alert.enabled:
            return "Paused"
        st = self.engine.alert_state(symbol, alert.id).get("state", ARMED)
        return {
            ARMED: "Active",
            WAIT_CROSS: "Waiting",
            DISARMED: "Re-arming",
            COOLDOWN: "Cooldown",
            DONE: "Triggered",
            EXPIRED: "Expired",
            DISABLED: "Paused",
        }.get(st, "Active")

    def remove_stock(self, symbol: str) -> None:
        cand = copy.deepcopy(self.wl)
        cand.symbols = [s for s in cand.symbols if s.symbol != symbol]
        self._commit(cand)
        self.engine.remove_stock(symbol)             # delete its entries in state.json too
        self.engine.state.get("last_regular", {}).pop(symbol, None)
        self.rows.pop(symbol, None)
        self._save_state()
        self._notify_quote_listeners()

    def _save_state(self) -> None:
        try:
            self.store.save_state(self.engine.state)
        except OSError:
            log.exception("could not save state.json")

    def get_mini_tracker_state(self) -> dict:
        raw = self.engine.state.get("mini_tracker")
        if isinstance(raw, dict):
            raw.pop("pinned", None)
            return dict(raw)
        return {}

    def set_mini_tracker_state(self, patch: dict) -> None:
        st = self.engine.state.setdefault("mini_tracker", {})
        st.update(patch)
        st.pop("pinned", None)
        self._save_state()

    def add_quote_listener(self, fn: Callable) -> None:
        if fn not in self._quote_listeners:
            self._quote_listeners.append(fn)

    def remove_quote_listener(self, fn: Callable) -> None:
        if fn in self._quote_listeners:
            self._quote_listeners.remove(fn)

    def symbols(self) -> list:
        return [s.symbol for s in self.wl.symbols]

    def key_pool_info(self) -> dict:
        """Read-only summary of configured API keys and their pool health."""
        pool_fn = getattr(self.provider, "pool_status", None)
        keys = list(pool_fn()) if callable(pool_fn) else []
        source = getattr(self.provider, "key_source", None)
        if not source or source == "unknown":
            source = "config.json" if self.cfg.api_keys else ("environment" if keys else "none")
        return {
            "source": source,
            "total": len(keys),
            "ok": sum(1 for k in keys if k.get("state") == "ok"),
            "rate_limited": sum(1 for k in keys if k.get("state") == "rate_limited"),
            "rejected": sum(1 for k in keys if k.get("state") == "rejected"),
            "keys": keys,
            "reject_code": getattr(self.provider, "_last_reject_code", 401),
        }

    def send_test_alert(self, now: Optional[datetime] = None) -> None:
        """Send a synthetic HIGH test alert (audio, toast & popup) without logging or mutating state."""
        t_now = now or self.now_fn()
        if self.wl.symbols:
            first = self.wl.symbols[0]
            sym, cat = first.symbol, first.category
            row = self.rows.get(sym)
            price = row.tick.price if (row and row.tick and row.tick.price > 0) else Decimal("100.00")
        else:
            sym, cat = "TEST", "WATCH"
            price = Decimal("100.00")
        ev = AlertEvent(
            symbol=sym,
            alert_id="__test_alert__",
            kind="crossing",
            label="TEST",
            priority="HIGH",
            price=price,
            time=t_now,
            source="test",
            category=cat,
            condition="Test notification",
            session="RH",
            name="Test Alert",
            message="Test notification (popup, toast & alarm)",
            note="Edit Mode test check",
            notification=resolve_notification_spec("HIGH"),
        )
        note = Notification([ev])
        try:
            sig = inspect.signature(self.notifier.notify)
            params = [
                p for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
            ]
            accepts_two = any(p.kind == p.VAR_POSITIONAL for p in params) or len(params) >= 2
        except (TypeError, ValueError):
            accepts_two = None
        if accepts_two is False:
            self.notifier.notify(note)
        elif accepts_two is True:
            self.notifier.notify(note, t_now)
        else:
            try:
                self.notifier.notify(note, t_now)
            except TypeError:
                self.notifier.notify(note)

    # ---- status strip & views ------------------------------------------------------------------
    @staticmethod
    def _fmt_age(seconds: float) -> str:
        s = max(0, int(round(seconds)))
        if s < 60:
            return f"{s}s"
        m = max(1, int(round(s / 60)))
        return f"{m} min"

    def _build_problems(self, now: datetime) -> list[ProblemIconView]:
        """Collect active problem icons, respecting offline exclusivity and severity ordering
        (Red first, then Amber)."""
        offline = (self.feed_kind == "offline") or bool(getattr(self.provider, "is_offline", False))
        if offline:
            # Spec 3.4 Offline exclusivity: When internet is down, show ONLY the No-WiFi icon.
            if self.last_heartbeat_at is not None:
                age_str = self._fmt_age((now - self.last_heartbeat_at).total_seconds())
                tip = f"No internet, last update {age_str} ago"
            else:
                tip = "No internet, waiting for connection"
            return [ProblemIconView(
                kind="offline",
                glyph="🚫📶",
                color="amber",
                badge="",
                tooltip=tip,
            )]

        pool_fn = getattr(self.provider, "pool_status", None)
        pool = pool_fn() if callable(pool_fn) else []

        rejected_nums: list[int] = []
        rate_limited_items: list[tuple[int, float]] = []
        for idx, item in enumerate(pool):
            num = item.get("num") or (idx + 1)
            st = item.get("state")
            wait = float(item.get("retry_in_s") or 0.0)
            if st == "rejected":
                rejected_nums.append(num)
            elif st == "rate_limited":
                rate_limited_items.append((num, wait))

        if not rejected_nums and self.feed_kind in ("auth", "nokey"):
            rejected_nums.append(1)

        problems: list[ProblemIconView] = []

        # 1. Red severity: Key invalid/expired (401/403) -> 🔑❌
        if rejected_nums:
            badge = ", ".join(str(n) for n in rejected_nums)
            if len(rejected_nums) == 1:
                tip = f"Key {rejected_nums[0]} invalid, check it in settings"
            else:
                tip = f"Keys {badge} invalid, check them in settings"
            problems.append(ProblemIconView(
                kind="key_invalid",
                glyph="🔑❌",
                color="red",
                badge=badge,
                tooltip=tip,
            ))

        # Additional red problems from extra_problems
        for ep in self.extra_problems:
            if ep.color == "red":
                problems.append(ep)

        # 2. Amber severity: Rate limited (HTTP 429) -> ⏳
        if rate_limited_items:
            nums = [n for n, _ in rate_limited_items]
            waits = [w for _, w in rate_limited_items if w > 0]
            badge = ", ".join(str(n) for n in nums)
            wait_s = max(1, int(round(min(waits)))) if waits else 0
            if len(nums) == 1:
                if wait_s > 0:
                    tip = f"Key {nums[0]} rate limited, retrying in ~{wait_s}s"
                else:
                    tip = f"Key {nums[0]} rate limited, retrying on next turn"
            else:
                if wait_s > 0:
                    tip = f"Keys {badge} rate limited, retrying in ~{wait_s}s"
                else:
                    tip = f"Keys {badge} rate limited, retrying on next turn"
            problems.append(ProblemIconView(
                kind="rate_limited",
                glyph="⏳",
                color="amber",
                badge=badge,
                tooltip=tip,
            ))

        # 3. Amber severity: Finnhub server slow/down (5xx/timeout) -> ☁️❌
        srv_down = self.server_down or bool(getattr(self.provider, "server_down", False))
        if srv_down:
            problems.append(ProblemIconView(
                kind="server_down",
                glyph="☁️❌",
                color="amber",
                badge="",
                tooltip="Finnhub not responding, retrying",
            ))

        # Additional amber problems from extra_problems
        for ep in self.extra_problems:
            if ep.color != "red":
                problems.append(ep)

        return problems

    def status_strip(self, now: datetime) -> StatusStripView:
        sess = self.session(now)
        self._check_session_transition(sess)
        extended = getattr(self.cfg, "extended_hours", True)
        active = is_active_session(sess, extended)
        _session_meta = {
            Session.PRE: ("🌅", "US market pre-market (PH)"),
            Session.REGULAR: ("☀️", "US market open (regular session)"),
            Session.AFTER: ("🌇", "US market after-hours (AH)"),
            Session.CLOSED: ("🌙", f"US market closed ({sess.value})"),
        }
        session_icon, session_tooltip = _session_meta.get(sess, ("🌙", f"US market closed ({sess.value})"))

        all_problems = self._build_problems(now)
        visible_problems = all_problems[:3]
        overflow_count = max(0, len(all_problems) - 3)
        overflow_tooltip = ", ".join(p.tooltip for p in all_problems[3:]) if overflow_count > 0 else ""

        # Heartbeat dot color:
        # - Offline: turns amber initially, then red when stale (>= 3 min)
        # - Complete outage (all keys failed / auth / budget / stale): red
        # - Degraded (some stocks failing, rate-limited failover, server slow): amber
        # - Healthy: green
        stale = self.feed.stale
        if self.last_heartbeat_at is not None and active:
            if (now - self.last_heartbeat_at).total_seconds() >= self.cfg.stale_after_seconds:
                stale = True

        healthy_fn = getattr(self.provider, "healthy_key_count", None)
        key_count_fn = getattr(self.provider, "key_count", None)
        total_keys = key_count_fn if isinstance(key_count_fn, int) else (key_count_fn() if callable(key_count_fn) else 1)
        healthy_keys = healthy_fn() if callable(healthy_fn) else 1

        if not self.polling_enabled or self.feed_kind in ("nokey", "budget"):
            hb_color = "red"
        elif self.feed_kind == "offline":
            hb_color = "red" if stale else "amber"
        elif stale:
            hb_color = "red"
        elif self.feed_kind == "auth" or (total_keys > 0 and healthy_keys == 0):
            hb_color = "red"
        elif self.feed_kind == "degraded" or self.server_down:
            hb_color = "amber"
        else:
            hb_color = "green"

        hb_dim = not active

        # Heartbeat tooltip
        if not active:
            ref = self.last_poll_at or self.last_heartbeat_at
            if ref is not None:
                elapsed = max(0.0, (now - ref).total_seconds())
                rem_min = max(1, int(math.ceil(max(0.0, CLOSED_POLL_S - elapsed) / 60.0)))
            else:
                rem_min = 15
            nxt_sess = getattr(self.calendar, "next_session_open", None)
            if callable(nxt_sess):
                t_open = nxt_sess(now, extended)
            else:
                nxt_open = getattr(self.calendar, "next_regular_open", None)
                t_open = nxt_open(now) if callable(nxt_open) else None
            if t_open is not None:
                sec_to_open = (t_open - now).total_seconds()
                if 0 < sec_to_open < CLOSED_POLL_S:
                    rem_min = max(1, int(math.ceil(sec_to_open / 60.0)))
            hb_tooltip = f"Market closed, next update in {rem_min} min"
        else:
            if hb_color == "red":
                if self.last_heartbeat_at is not None:
                    age_s = self._fmt_age((now - self.last_heartbeat_at).total_seconds())
                    hb_tooltip = f"Outage, last update {age_s} ago"
                else:
                    hb_tooltip = "Outage, no data received"
            elif hb_color == "amber":
                if self.last_heartbeat_at is not None:
                    age_s = self._fmt_age((now - self.last_heartbeat_at).total_seconds())
                    hb_tooltip = f"Degraded, updated {age_s} ago"
                else:
                    hb_tooltip = "Feed degraded, retrying"
            else:
                if self.last_heartbeat_at is not None:
                    age_s = max(0, int((now - self.last_heartbeat_at).total_seconds()))
                    hb_tooltip = f"Live, updated {age_s}s ago"
                else:
                    hb_tooltip = "Live, waiting for first update"

        return StatusStripView(
            session_icon=session_icon,
            session_tooltip=session_tooltip,
            heartbeat_color=hb_color,
            heartbeat_dim=hb_dim,
            heartbeat_tooltip=hb_tooltip,
            last_heartbeat_at=self.last_heartbeat_at,
            problems=visible_problems,
            overflow_count=overflow_count,
            overflow_tooltip=overflow_tooltip,
            session=sess.name,
        )

    def header(self, now: datetime) -> HeaderView:
        sess = self.session(now)
        strip = self.status_strip(now)
        stale = self.feed.stale
        if self.feed_kind in ("offline",):
            dot, text = "red", "offline, retrying"
        elif self.feed_kind in ("degraded",):
            dot, text = "amber", "feed degraded"
        elif stale:
            dot, text = "amber", "stale"
        elif self.feed_kind in ("nokey", "auth", "budget"):
            dot, text = "red", {"nokey": "no API key", "auth": "key rejected", "budget": "poll budget"}[self.feed_kind]
        else:
            dot, text = "green", "live"
        return HeaderView(
            market=sess.value,
            ist=now.astimezone(IST).strftime("%H:%M IST"),
            ny=now.astimezone(ET).strftime("%H:%M ET"),
            feed_dot=dot,
            feed_text=text,
            muted=self.notifier.muted,
            session_icon=strip.session_icon,
            heartbeat_dim=strip.heartbeat_dim,
            heartbeat_tooltip=strip.heartbeat_tooltip,
            problems=strip.problems,
            overflow_count=strip.overflow_count,
        )

    def all_warnings(self) -> list:
        w = list(self.warnings)
        if not self.polling_enabled:
            w.append("FINNHUB_API_KEY is not set, so prices are not polled. Set it and restart.")
        return w

    def row_views(self, now: datetime) -> list:
        sess = self.session(now)
        active_sess = is_active_session(sess, getattr(self.cfg, "extended_hours", True))
        out = []
        for s in self.wl.symbols:
            row = self.rows.get(s.symbol) or RowState()
            t = row.tick
            active = sum(
                1 for a in s.alerts
                if a.enabled and self.engine.alert_state(s.symbol, a.id)["state"] not in (DONE, EXPIRED, DISABLED)
            )
            ss = self.engine.state.get("symbols", {}).get(s.symbol, {})
            pre = row.pre_price if row.pre_price is not None else _safe_dec(ss.get("pre_price"))
            reg = (row.reg_price if row.reg_price is not None else
                   (_safe_dec(ss.get("reg_price")) or
                    _safe_dec((self.engine.state.get("last_regular", {}).get(s.symbol) or {}).get("price"))))
            ah = row.ah_price if row.ah_price is not None else _safe_dec(ss.get("ah_price"))
            ph_txt, rh_txt, ah_txt = _fmt_session_price(pre), _fmt_session_price(reg), _fmt_session_price(ah)
            if t is None:
                out.append(RowView(s.symbol, s.nickname, "…", "", "", s.category, "", active, "neutral", None, "",
                                   ph_txt, rh_txt, ah_txt))
                continue
            chg, pct = t.change, t.change_pct
            if chg is None and t.prev_close:
                chg = t.price - t.prev_close
                pct = chg / t.prev_close * 100
            if chg is None:
                ctext, arrow = "", ""
            else:
                arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "–")
                ctext = f"{'+' if chg >= 0 else '-'}{abs(q2(chg)):.2f} ({'+' if pct >= 0 else '-'}{abs(q2(pct)):.2f}%)"
            age = int((now - row.last_ok).total_seconds()) if row.last_ok else None
            warn = ""
            # Rows turn amber when feed offline/degraded, stale_after_seconds (3 min), or price jump paused
            stale = (self.feed_kind in ("offline", "degraded") or self.feed.stale or row.paused or
                     (active_sess and age is not None and age >= self.cfg.stale_after_seconds))
            if row.paused:
                warn = "price jump paused"
            if stale:
                color = "amber"
            elif not active_sess:
                color = "dim"
            elif chg is None or chg == 0:
                color = "neutral"
            else:
                color = "up" if chg > 0 else "down"
            out.append(RowView(s.symbol, s.nickname, f"{t.price:.2f}", ctext, arrow, s.category,
                               "", active, color, age, warn, ph_txt, rh_txt, ah_txt))
        return out