"""Application controller: no Tk, no Win32. Everything the UI shows or changes goes through here,
which is why it can be tested with the mock provider and a fake notifier."""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from config import Config
from data_checks import FeedHealth
from engine import AlertEngine, merge_events
from market_clock import ET, IST, MarketCalendar, Session, utcnow
from models import ARMED, DISABLED, DONE, AlertEvent, PriceTick, StockConfig, ValidationError, Watchlist
from positions import q2
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
class HeaderView:
    market: str
    ist: str
    ny: str
    feed_dot: str       # green | amber | red
    feed_text: str
    summary_dot: bool
    muted: bool


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
            elif kind == "error":
                self.on_error(msg[1], msg[2], msg[3])
            elif kind == "call":
                try:
                    msg[1](msg[2], msg[3])
                except Exception:
                    log.exception("callback failed")

    def session(self, now: datetime) -> Session:
        """Provider market status when it answered recently, else the bundled calendar."""
        if self.session_override is not None:
            return self.session_override
        st = self.provider_status
        if st and self.provider_status_at and now - self.provider_status_at < timedelta(minutes=30):
            s = (st.get("session") or "").lower()
            if st.get("isOpen") and s in ("", "regular"):
                return Session.REGULAR
            if s == "pre-market":
                return Session.PRE
            if s == "post-market":
                return Session.AFTER
            if not st.get("isOpen"):
                return Session.CLOSED
        return self.calendar.session_at(now)

    def on_ticks(self, ticks: list, now: datetime) -> None:
        by = {t.symbol: t for t in ticks}
        session = self.session(now)
        self.feed.record_success(now)
        self.feed.note_session(session == Session.REGULAR, now)
        self.feed_kind = "ok"
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
                # the summary uses the last regular-session reading, so later display polls
                # (or a restart / wake before the card is built) cannot overwrite it
                self.engine.state.setdefault("last_regular", {})[stock.symbol] = {
                    "price": str(t.price), "prev_close": str(t.prev_close) if t.prev_close else None,
                    "zone": self.engine.state["symbols"].get(stock.symbol, {}).get("zone"), "time": now.isoformat()}
        self.dispatch(events)
        self._save_state()

    def on_error(self, kind: str, text: str, now: datetime) -> None:
        self.feed_kind = {"network": "offline", "rate": "degraded", "malformed": "degraded"}.get(kind, kind)
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
        """Call every few seconds: stale-feed alert, summary at the close."""
        session = self.session(now)
        self.feed.note_session(session == Session.REGULAR, now)
        if self.polling_enabled:
            r = self.feed.check(now)
            if r == "stale":
                self.dispatch([AlertEvent("FEED", "feed-stale", "feed_stale", "RISK", "MEDIUM", Decimal(0), now,
                                          "finnhub", condition="feed stale")])
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

    # ---- views ---------------------------------------------------------------------------------
    def header(self, now: datetime) -> HeaderView:
        sess = self.session(now)
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
        return HeaderView(sess.value, now.astimezone(IST).strftime("%H:%M IST"), now.astimezone(ET).strftime("%H:%M ET"),
                          dot, text, self.summary_unseen, self.notifier.muted)

    def all_warnings(self) -> list:
        w = list(self.warnings)
        if not self.polling_enabled:
            w.append("FINNHUB_API_KEY is not set, so prices are not polled. Set it and restart.")
        w += list(self.notices.values())
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
