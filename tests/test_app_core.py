import copy
import json
import queue
from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dc

import pytest

from app_core import AppController
from config import Config
from market_clock import ET, Session
from models import Alert, StockConfig, TradeInfo, ValidationError
from providers.mock import ZERO, MockProvider
from store import Store
from test_config_store import EXAMPLE, write
from conftest import al, tick

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # regular session


class Rec:
    muted = False

    def __init__(self, store=None):
        self.notes, self.store, self.log_len_at_notify = [], store, []

    def notify(self, note):
        if self.store:
            self.log_len_at_notify.append(len(self.store.read_alert_log()))
        self.notes.append(note)


def make(tmp_path, example=EXAMPLE, provider=None, **kw):
    st = Store(tmp_path)
    if example is not None:
        write(st.watchlist_path, example)
    rec = Rec(st)
    c = AppController(Config(), st, provider or MockProvider({}), rec, now_fn=lambda: T0, **kw)
    return c, rec, st


def feed(c, now, **prices):
    ticks = [tick(p, sym, now, prev_close=pc) for sym, (p, pc) in prices.items()]
    c.on_ticks(ticks, now)


def test_loads_example_and_shows_rows(tmp_path):
    c, rec, st = make(tmp_path)
    assert c.symbols() == ["AAPL", "TTWO"]
    feed(c, T0, AAPL=("200.00", "198.00"), TTWO=("205", "206"))
    rows = {r.symbol: r for r in c.row_views(T0)}
    assert rows["AAPL"].price_text == "200.00" and rows["AAPL"].arrow == "▲" and rows["AAPL"].color == "up"
    assert rows["AAPL"].change_text == "+2.00 (+1.01%)" and rows["AAPL"].alerts == 1
    assert rows["TTWO"].color == "down" and rows["TTWO"].zone_tag == ""


def test_alert_is_logged_before_notification_and_merged(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"), TTWO=("205", "206"))
    feed(c, T0 + timedelta(minutes=1), AAPL=("212", "198"), TTWO=("194", "206"))
    assert len(rec.notes) == 1
    assert rec.log_len_at_notify == [len(rec.notes[0].events)]
    ids = {e["alert_id"] for e in st.read_alert_log()}
    assert {"aapl-target", "ttwo-danger"} <= ids
    assert rec.notes[0].priority == "HIGH"
    log_text = st.alert_log_path.read_text()
    assert "shares" not in log_text and "buy_price" not in log_text


def test_zero_price_keeps_last_good_and_dispatches_nothing(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"))
    feed(c, T0 + timedelta(minutes=1), AAPL=("0", "198"))
    r = c.row_views(T0 + timedelta(minutes=1))[0]
    assert r.price_text == "200.00" and r.age_s == 60 and not rec.notes


def test_header_feed_states(tmp_path):
    c, rec, st = make(tmp_path)
    assert c.header(T0).feed_dot == "green" and c.header(T0).market == "OPEN"
    c.on_error("network", "x", T0)
    h = c.header(T0)
    assert (h.feed_dot, h.feed_text) == ("red", "offline, retrying")
    c.on_error("rate", "HTTP 429", T0)
    h = c.header(T0)
    assert (h.feed_dot, h.feed_text) == ("amber", "feed degraded")
    feed(c, T0, AAPL=("200", "198"))
    assert c.header(T0).feed_dot == "green"
    assert c.header(T0).ist == "20:30 IST" and c.header(T0).ny == "11:00 ET"


def test_rows_amber_when_offline_dim_when_closed(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"))
    c.on_error("network", "x", T0)
    assert c.row_views(T0)[0].color == "amber"
    c.on_error("rate", "", T0)
    feed(c, T0, AAPL=("200", "198"))
    sat = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    assert c.row_views(sat)[0].color == "dim" and c.header(sat).market == "CLOSED"


def test_stale_feed_alert_once_per_outage(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"))
    c.housekeeping(T0 + timedelta(seconds=100))
    assert not rec.notes
    c.housekeeping(T0 + timedelta(seconds=181))
    assert len(rec.notes) == 1 and rec.notes[0].headline.kind == "feed_stale" and rec.notes[0].priority == "MEDIUM"
    c.housekeeping(T0 + timedelta(seconds=300))
    assert len(rec.notes) == 1
    assert c.header(T0 + timedelta(seconds=300)).feed_dot == "amber"
    feed(c, T0 + timedelta(seconds=310), AAPL=("200", "198"))
    c.housekeeping(T0 + timedelta(seconds=311))
    assert c.header(T0 + timedelta(seconds=311)).feed_dot == "green"


def test_provider_market_status_overrides_calendar_for_a_holiday(tmp_path):
    c, rec, st = make(tmp_path)
    c.provider_status, c.provider_status_at = {"isOpen": False, "holiday": "Special closure", "session": None}, T0
    assert c.session(T0) == Session.CLOSED
    c.provider_status_at = T0 - timedelta(hours=2)
    assert c.session(T0) == Session.REGULAR


def test_add_stock_flow(tmp_path):
    mp = MockProvider({"MSFT": [410], "DEAD": [ZERO]})
    c, rec, st = make(tmp_path, provider=mp)
    woke = []
    c.poll_now = lambda: woke.append(1)
    got = []
    c.check_symbol("msft", lambda t, e: got.append((t, e)))
    assert got[0][0].price == 410 and got[0][1] is None
    c.check_symbol("DEAD", lambda t, e: got.append((t, e)))
    assert isinstance(got[1][1], ValidationError)
    c.check_symbol("AAPL", lambda t, e: got.append((t, e)))
    assert "already" in str(got[2][1])
    s = StockConfig("MSFT", "HOLD", "Microsoft", False, TradeInfo(buy_price=Dc("400"), shares=Dc(3)))
    c.add_stock(s)
    assert woke == [1]
    data = json.loads(st.watchlist_path.read_text())
    assert [x["symbol"] for x in data["symbols"]] == ["AAPL", "TTWO", "MSFT"]
    with pytest.raises(ValidationError):
        c.add_stock(s)


def test_add_stock_respects_cap_and_failed_save_changes_nothing(tmp_path):
    c, rec, st = make(tmp_path, example={"version": 1, "symbols": []})
    for i in range(30):
        c.add_stock(StockConfig(f"S{i}", "WATCH"))
    with pytest.raises(ValidationError):
        c.add_stock(StockConfig("EXTRA", "WATCH"))
    assert len(c.wl.symbols) == 30
    before = st.watchlist_path.read_text()
    bad = StockConfig("BAD", "HOLD", "", False, TradeInfo(buy_price=Dc(5), shares=Dc(-1)))
    c2, *_ = make(tmp_path / "b", example={"version": 1, "symbols": []})
    with pytest.raises(ValidationError):
        c2.add_stock(bad)
    assert c2.wl.symbols == [] and st.watchlist_path.read_text() == before


def test_update_stock_registers_new_alert_with_choice(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"), TTWO=("201.9", "206"))
    stock = copy.deepcopy(c.stock("TTWO"))
    stock.alerts.append(al("fresh", "less_than", "202.73", trigger="once", prio="HIGH", label="RISK"))
    c.update_stock(stock, {"fresh": "fire_now"})
    assert len(rec.notes) == 1 and rec.notes[0].headline.alert_id == "fresh"

    stock2 = copy.deepcopy(c.stock("TTWO"))
    stock2.alerts.append(al("w", "less_than", "202.73", trigger="once", prio="HIGH", label="RISK"))
    c.update_stock(stock2, {"w": "wait_cross"})
    assert c.engine.alert_state("TTWO", "w")["state"] == "WAIT_CROSS"
    assert len(rec.notes) == 1


def test_save_toggle_and_delete_alert_persist_immediately(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"), TTWO=("205", "206"))

    new_alert = al("aapl-imm", "crossing_up", "208.50", trigger="once", prio="HIGH", label="SELL")
    c.save_alert("AAPL", new_alert)

    # Reload from disk immediately to prove zero 2-step save bug
    wl_disk, warns = st.load_watchlist(Config())
    assert not warns and [a.id for a in wl_disk.get("AAPL").alerts] == ["aapl-target", "aapl-imm"]
    assert c.alert_status_label("AAPL", new_alert)

    c.toggle_alert("AAPL", "aapl-imm", False)
    wl_disk2, _ = st.load_watchlist(Config())
    assert [a for a in wl_disk2.get("AAPL").alerts if a.id == "aapl-imm"][0].enabled is False
    assert "Paused" in c.alert_status_label("AAPL", [a for a in c.stock("AAPL").alerts if a.id == "aapl-imm"][0])

    c.delete_alert("AAPL", "aapl-imm")
    wl_disk3, _ = st.load_watchlist(Config())
    assert [a.id for a in wl_disk3.get("AAPL").alerts] == ["aapl-target"]
    assert "aapl-imm" not in c.engine.state["symbols"]["AAPL"]["alerts"]


def test_update_removing_alert_and_remove_stock_clean_state(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"), TTWO=("205", "206"))
    s = copy.deepcopy(c.stock("AAPL"))
    s.alerts = []
    c.update_stock(s)
    assert "aapl-target" not in c.engine.state["symbols"]["AAPL"]["alerts"]
    c.remove_stock("TTWO")
    assert "TTWO" not in c.engine.state["symbols"]
    assert "TTWO" not in json.loads(st.state_path.read_text())["symbols"]
    assert c.symbols() == ["AAPL"]
    assert "ttwo" not in st.watchlist_path.read_text().lower()


def test_missing_api_key_message_and_header(tmp_path):
    c, rec, st = make(tmp_path, polling_enabled=False)
    assert any("FINNHUB_API_KEY" in w for w in c.all_warnings())
    assert c.header(T0).feed_dot == "red" and c.header(T0).feed_text == "no API key"


def test_drain_queue_and_callbacks(tmp_path):
    c, rec, st = make(tmp_path)
    q = queue.Queue()
    got = []
    q.put(("ticks", [tick("200", "AAPL", T0, 198)], T0))
    q.put(("status", {"isOpen": True, "session": "regular"}, T0))
    q.put(("error", "rate", "HTTP 429", T0))
    q.put(("call", lambda r, e: got.append((r, e)), 7, None))
    c.drain(q)
    assert c.rows["AAPL"].tick.price == 200 and got == [(7, None)] and c.feed_kind == "degraded"
    assert c.provider_status["isOpen"] is True


def test_moving_pct_limit_and_demo_session_override(tmp_path):
    from models import ValidationError as VE
    from watchlist import parse_alert

    s = StockConfig("X", "WATCH")
    ok = parse_alert({"id": "m", "operator": "moving_up_pct", "value": 500}, s, Config())
    assert ok.value == 500
    for bad in (501, 0, -3):
        with pytest.raises(VE):
            parse_alert({"id": "m", "operator": "moving_up_pct", "value": bad}, s, Config())

    c, rec, st = make(tmp_path, session_override=Session.REGULAR)
    sat = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    assert c.session(sat) == Session.REGULAR and c.header(sat).market == "OPEN"
    feed(c, sat, AAPL=("207.50", "200"), TTWO=("205", "206"))
    c.housekeeping(datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc))


def test_session_transition_triggers_instant_poll(tmp_path):
    c, rec, st = make(tmp_path)
    woke = []
    c.poll_now = lambda: woke.append(1)

    t_pre = datetime(2026, 10, 26, 9, 29, tzinfo=ET)
    c.status_strip(t_pre)
    assert woke == []

    t_open = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
    c.status_strip(t_open)
    assert woke == [1]

    c.status_strip(datetime(2026, 10, 26, 9, 31, tzinfo=ET))
    assert woke == [1]


def test_black_friday_early_close_ignores_false_finnhub_regular_status(tmp_path):
    c, rec, st = make(tmp_path)
    bf_after = datetime(2026, 11, 27, 13, 8, tzinfo=ET)
    c.provider_status = {"isOpen": True, "holiday": None, "session": "regular"}
    c.provider_status_at = bf_after

    assert c.session(bf_after) == Session.AFTER
    strip = c.status_strip(bf_after)
    assert strip.session == "AFTER"
    assert strip.session_icon == "🌇"

    # Normal day 16:05 ET with stale Finnhub regular status also resolves to AFTER
    norm_after = datetime(2026, 10, 26, 16, 5, tzinfo=ET)
    c.provider_status_at = norm_after
    assert c.session(norm_after) == Session.AFTER


def test_surprise_early_close_via_yahoo_trading_periods_syncs_ctrl_and_scheduler(tmp_path):
    from providers.finnhub import FinnhubProvider
    from scheduler import Scheduler

    st = Store(tmp_path)
    write(st.watchlist_path, EXAMPLE)
    cfg = Config()
    c = AppController(cfg, st, MockProvider({}), Rec(st), now_fn=lambda: T0)

    # Normal Monday 13:05 ET overridden by live Yahoo regular_end=13:00 ET
    surprise = datetime(2026, 10, 26, 13, 5, tzinfo=ET)
    reg_s = int(datetime(2026, 10, 26, 9, 30, tzinfo=ET).timestamp())
    reg_e = int(datetime(2026, 10, 26, 13, 0, tzinfo=ET).timestamp())
    post_e = int(datetime(2026, 10, 26, 17, 0, tzinfo=ET).timestamp())
    c.provider.trading_periods = {"regular_start": reg_s, "regular_end": reg_e, "post_end": post_e}
    c.provider_status = {"isOpen": True, "holiday": None, "session": "regular"}
    c.provider_status_at = surprise
    assert c.session(surprise) == Session.AFTER

    q = queue.Queue()
    mp = MockProvider({"AAPL": [200], "TTWO": [205]})
    mp.trading_periods = dict(c.provider.trading_periods)
    sched = Scheduler(mp, lambda: ["AAPL"], q, cfg, now_fn=lambda: surprise)
    sched.poll_once()
    assert mp.current_session == "AFTER"

    # Weekend _last_bar fallback after Black Friday places 13:30 ET bar into ah_price, not reg_price
    prov = FinnhubProvider(api_key="k", now_fn=lambda: surprise)
    t_reg = int(datetime(2026, 11, 27, 12, 55, tzinfo=ET).timestamp())
    t_ah = int(datetime(2026, 11, 27, 13, 30, tzinfo=ET).timestamp())

    class _Resp:
        status_code = 200
        text = json.dumps({
            "chart": {
                "result": [{
                    "meta": {"chartPreviousClose": 100.0},
                    "timestamp": [t_reg, t_ah],
                    "indicators": {"quote": [{"close": [101.0, 102.5]}]},
                }]
            }
        })

    class _YahooSess:
        def get(self, *a, **kw):
            return _Resp()

        def close(self):
            pass

    prov._yahoo_s = _YahooSess()
    prov._uses_real_http = True
    ext = prov._fetch_yahoo_extended("AAPL")
    assert ext is not None
    assert float(ext["reg_price"]) == 101.0
    assert float(ext["ah_price"]) == 102.5
