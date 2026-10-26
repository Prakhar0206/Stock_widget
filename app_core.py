"""Application controller: no Tk, no Win32. Everything the UI shows or changes goes through here,
which is why it can be tested with the mock provider and a fake notifier."""
from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from config import Config
from data_checks import FeedHealth
from engine import AlertEngine, merge_events
from market_clock import ET, IST, MarketCalendar, Session, utcnow
from models import ARMED, DISABLED, DONE, AlertEvent, PriceTick, StockConfig, ValidationError, Watchlist
from positions import q2
from scheduler import CLOSED_POLL_S
from store import Store
from summary import build_card
from watchlist import ensure_slipping_alert
from zones import TAGS

log = logging.getLogger("saw.app")


@dataclass
class RowState:
    tick: Optional[PriceTick] = None       # last good reading
    last_ok: Optional[datetime] = None
    paused: bool = False


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


@dataclass
class ProblemIconView:
    kind: str           # key_invalid | rate_limited | offline | server_down | custom
    glyph: str          # 🔑❌ | ⏳ | 🚫📶 | ☁️❌
    color: str          # red | amber
    badge: str          # e.g. "2" or "1, 3" or ""
    tooltip: str        # crisp single-line hover tooltip


@dataclass
class StatusStripView:
    session_icon: str                        # "☀️" when regular session open, else "🌙"
    session_tooltip: str                     # e.g. "US market open" / "US market closed"
    heartbeat_color: str                     # "green" | "amber" | "red"
    heartbeat_dim: bool                      # True when market is closed
    heartbeat_tooltip: str                   # e.g. "Live, updated 8s ago" / "Market closed, next update in 11 min"
    last_heartbeat_at: Optional[datetime]    # updated on every successful fetch
    problems: list[ProblemIconView] = field(default_factory=list)   # max 3 visible problem icons
    overflow_count: int = 0                  # >0 when >3 problems active (renders "+N")
    overflow_tooltip: str = ""


@dataclass
class HeaderView:
    market: str
    ist: str
    ny: str
    feed_dot: str       # green | amber | red
    feed_text: str
    summary_dot: bool
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
        pop_fn = getattr(self.provider, "pop_invalid_key_events", None)
        if callable(pop_fn):
            for key_num, code in pop_fn():
                self.on_invalid_key(key_num, code, now)

    def session(self, now: datetime) -> Session:
        """Provider market status when it answered recently, else the bundled calendar."""
        if self.session_override is not None:
            return self.session_override
        cal_sess = self.calendar.session_at(now)
        st = self.provider_status
        if st and self.provider_status_at and now - self.provider_status_at < timedelta(minutes=30):
            # An explicit holiday closure from the provider overrides the calendar:
            if st.get("holiday"):
                return Session.CLOSED
            s = (st.get("session") or "").lower()
            if st.get("isOpen") and s in ("", "regular"):
                return Session.REGULAR
            # A pre-market status reading from before open cannot block regular trading hours:
            if cal_sess == Session.REGULAR:
                return Session.REGULAR
            if s == "pre-market":
                return Session.PRE
            if s == "post-market":
                return Session.AFTER
            if not st.get("isOpen"):
                return Session.CLOSED
        return cal_sess

    def on_ticks(self, ticks: list, now: datetime) -> None:
        self._check_provider_invalid_keys(now)
        by = {t.symbol: t for t in ticks}
        session = self.session(now)
        if ticks:
            self.feed.record_success(now)
            self.last_heartbeat_at = now
            self.last_poll_at = now
            self.feed_kind = "ok"
        self.feed.note_session(session == Session.REGULAR, now)
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
            if session == Session.REGULAR and t.price > 0 and not row.paused:
                self.engine.state.setdefault("last_regular", {})[stock.symbol] = {
                    "price": str(t.price), "prev_close": str(t.prev_close) if t.prev_close else None,
                    "zone": self.engine.state["symbols"].get(stock.symbol, {}).get("zone"), "time": now.isoformat()}
        self.dispatch(events)
        self._save_state()

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
        for ev in events:
            try:
                self.store.log_alert(ev)
            except OSError:
                log.exception("could not write the alert log")
        try:
            self.notifier.notify(merge_events(events))
        except Exception:
            log.exception("notification failed")      # a broken backend never stops evaluation

    def housekeeping(self, now: datetime) -> None:
        """Call every few seconds: stale-feed alert (>3 min offline during market hours), summary at the close."""
        self._check_provider_invalid_keys(now)
        session = self.session(now)
        self.feed.note_session(session == Session.REGULAR, now)
        if self.polling_enabled:
            r = self.feed.check(now)
            if r == "stale":
                self.dispatch([AlertEvent("FEED", "feed-stale", "feed_stale", "RISK", "MEDIUM", Decimal(0), now,
                                          "finnhub", condition="Feed offline: price data is stale",
                                          note="Feed offline: price data is stale")])
        self._maybe_summary(now)

    def on_wake(self, now: datetime) -> None:
        """Wake, unlock or remote-desktop reconnect: poll immediately. Old prices never fire alerts,
        because alerts are only evaluated on readings that arrive after this."""
        self.feed.reset_clock(now)
        self.poll_now()
        self._maybe_summary(now)

    # ---- summary -------------------------------------------------------------------------------
    def _maybe_summary(self, now: datetime) -> None:
        if self.session_override is not None:
            return                                   # demo mode: no (fake) summary cards
        close = self.calendar.last_close_before(now)
        if close is None:
            return
        day = close.date().isoformat()
        if (self.store.dir / "summaries" / f"{day}.json").exists():
            return
        lr = self.engine.state.get("last_regular", {})
        if not any(lr.get(s.symbol) and datetime.fromisoformat(lr[s.symbol]["time"]).astimezone(ET).date().isoformat() == day
                   for s in self.wl.symbols):
            return                                   # no regular-session reading that day: no card
        after = now - close > timedelta(minutes=10)
        card = build_card(self.wl, self.engine.state, self.store.read_alert_log(), close, self.cfg, now, after)
        self.store.save_summary(day, card)
        self.engine.state["summary"] = {"latest": day, "unseen": True}
        self._save_state()

    @property
    def summary_unseen(self) -> bool:
        return bool(self.engine.state.get("summary", {}).get("unseen"))

    def open_summary(self) -> Optional[dict]:
        """Returns the latest card and clears the header dot."""
        import json
        day = self.engine.state.get("summary", {}).get("latest")
        if not day:
            return None
        p = self.store.dir / "summaries" / f"{day}.json"
        if not p.exists():
            return None
        self.engine.state["summary"]["unseen"] = False
        self._save_state()
        return json.loads(p.read_text(encoding="utf-8"))

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
        if new.category == "SELL":
            ensure_slipping_alert(new)               # section 5.5: every new SELL stock gets Slipping
        cand = copy.deepcopy(self.wl)
        cand.symbols.append(new)
        self._commit(cand)
        self.engine.remove_stock(stock.symbol)       # new stock: its first reading only sets state
        self.poll_now()

    def update_stock(self, new: StockConfig, choices: Optional[dict] = None) -> None:
        old = self.wl.get(new.symbol)
        if old is None:
            raise ValidationError(f"{new.symbol} is not in the watchlist.")
        new = copy.deepcopy(new)
        if new.category == "SELL" and old.category != "SELL":
            ensure_slipping_alert(new)               # switched TO sell: add Slipping (an unchanged SELL stock never gets it back)
        cand = copy.deepcopy(self.wl)
        cand.symbols = [new if s.symbol == new.symbol else s for s in cand.symbols]
        self._commit(cand)
        stock = self.wl.get(new.symbol)
        old_alerts = {a.id: a for a in old.alerts}
        for aid in set(old_alerts) - {a.id for a in stock.alerts}:
            self.engine.remove_alert(stock.symbol, aid)
        events = []
        tick = (self.rows.get(stock.symbol) or RowState()).tick
        for a in stock.alerts:
            if old_alerts.get(a.id) == a:
                continue                              # unchanged: keep its state
            if tick is not None:
                choice = (choices or {}).get(a.id) or "wait_cross"
                events += self.engine.register_alert(stock, a, tick, self.now_fn(), choice)
            else:
                st = self.engine.alert_state(stock.symbol, a.id)
                st.update({"state": ARMED if a.enabled else DISABLED, "cooldown_until": None})
        self.dispatch(events)
        self._save_state()

    def remove_stock(self, symbol: str) -> None:
        cand = copy.deepcopy(self.wl)
        cand.symbols = [s for s in cand.symbols if s.symbol != symbol]
        self._commit(cand)
        self.engine.remove_stock(symbol)             # delete its entries in state.json too
        self.engine.state.get("last_regular", {}).pop(symbol, None)
        self.rows.pop(symbol, None)
        self._save_state()

    def _save_state(self) -> None:
        try:
            self.store.save_state(self.engine.state)
        except OSError:
            log.exception("could not save state.json")

    def symbols(self) -> list:
        return [s.symbol for s in self.wl.symbols]

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
        regular = sess == Session.REGULAR
        if self._last_session is not None and self._last_session != Session.REGULAR and regular:
            log.info("Market opened: session switched to REGULAR, requesting immediate poll")
            self.poll_now()
        self._last_session = sess
        session_icon = "☀️" if regular else "🌙"
        session_tooltip = "US market open (regular session)" if regular else f"US market closed ({sess.value})"

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
        if self.last_heartbeat_at is not None and regular:
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

        hb_dim = not regular

        # Heartbeat tooltip
        if not regular:
            ref = self.last_poll_at or self.last_heartbeat_at
            if ref is not None:
                elapsed = max(0.0, (now - ref).total_seconds())
                rem_min = max(1, int(math.ceil(max(0.0, CLOSED_POLL_S - elapsed) / 60.0)))
            else:
                rem_min = 15
            nxt_open = getattr(self.calendar, "next_regular_open", None)
            if callable(nxt_open):
                t_open = nxt_open(now)
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
            summary_dot=self.summary_unseen,
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

    def zone_tag(self, symbol: str) -> str:
        z = self.engine.state.get("symbols", {}).get(symbol, {}).get("zone")
        return TAGS.get(z) or "" if z else ""

    def row_views(self, now: datetime) -> list:
        sess = self.session(now)
        out = []
        for s in self.wl.symbols:
            row = self.rows.get(s.symbol) or RowState()
            t = row.tick
            active = sum(1 for a in s.alerts if a.enabled and self.engine.alert_state(s.symbol, a.id)["state"] not in (DONE, DISABLED))
            if t is None:
                out.append(RowView(s.symbol, s.nickname, "…", "", "", s.category, "", active, "neutral", None))
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
            regular = sess == Session.REGULAR
            warn = ""
            # Rows turn amber when feed offline/degraded, stale_after_seconds (3 min), or price jump paused
            stale = (self.feed_kind in ("offline", "degraded") or self.feed.stale or row.paused or
                     (regular and age is not None and age >= self.cfg.stale_after_seconds))
            if row.paused:
                warn = "price jump paused"
            if stale:
                color = "amber"
            elif not regular:
                color = "dim"
            elif chg is None or chg == 0:
                color = "neutral"
            else:
                color = "up" if chg > 0 else "down"
            out.append(RowView(s.symbol, s.nickname, f"{t.price:.2f}", ctext, arrow, s.category,
                               self.zone_tag(s.symbol), active, color, age, warn))
        return out