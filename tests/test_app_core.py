import json
import queue
from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dc

import pytest

from app_core import AppController
from config import Config
from engine import Notification
from market_clock import Session
from models import Alert, PlannedEntry, Position, StockConfig, ValidationError
from providers.mock import MockProvider, ZERO
from store import Store
from test_config_store import EXAMPLE, write
from conftest import tick

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)       # regular session


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
    assert rows["AAPL"].change_text == "+2.00 (+1.01%)" and rows["AAPL"].alerts == 2
    assert rows["TTWO"].color == "down" and rows["TTWO"].zone_tag == "Above entry"


def test_alert_is_logged_before_notification_and_merged(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"), TTWO=("205", "206"))
    feed(c, T0 + timedelta(minutes=1), AAPL=("212", "198"), TTWO=("194", "206"))   # both fire on one poll
    assert len(rec.notes) == 1                                  # merged into one notification
    assert rec.log_len_at_notify == [len(rec.notes[0].events)]  # all events already logged at notify time
    ids = {e["alert_id"] for e in st.read_alert_log()}
    assert {"aapl-target", "ttwo-danger", "zone:high_sell"} <= ids
    assert rec.notes[0].priority == "HIGH"
    log_text = st.alert_log_path.read_text()
    assert "shares" not in log_text and "200.0" not in log_text


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
    h = c.header(T0); assert (h.feed_dot, h.feed_text) == ("red", "offline, retrying")
    c.on_error("rate", "HTTP 429", T0)
    h = c.header(T0); assert (h.feed_dot, h.feed_text) == ("amber", "feed degraded")
    feed(c, T0, AAPL=("200", "198"))
    assert c.header(T0).feed_dot == "green"                     # recovery is automatic
    assert c.header(T0).ist == "20:30 IST" and c.header(T0).ny == "11:00 ET"


def test_rows_amber_when_offline_dim_when_closed(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"))
    c.on_error("network", "x", T0)
    assert c.row_views(T0)[0].color == "amber"
    c.on_error("rate", "", T0); feed(c, T0, AAPL=("200", "198"))
    sat = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    assert c.row_views(sat)[0].color == "dim" and c.header(sat).market == "CLOSED"


def test_stale_feed_alert_once_per_outage(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"))
    c.housekeeping(T0 + timedelta(seconds=100)); assert not rec.notes
    c.housekeeping(T0 + timedelta(seconds=181))
    assert len(rec.notes) == 1 and rec.notes[0].headline.kind == "feed_stale" and rec.notes[0].priority == "MEDIUM"
    c.housekeeping(T0 + timedelta(seconds=300)); assert len(rec.notes) == 1
    assert c.header(T0 + timedelta(seconds=300)).feed_dot == "amber"
    feed(c, T0 + timedelta(seconds=310), AAPL=("200", "198"))
    c.housekeeping(T0 + timedelta(seconds=311))
    assert c.header(T0 + timedelta(seconds=311)).feed_dot == "green"


def test_provider_market_status_overrides_calendar_for_a_holiday(tmp_path):
    c, rec, st = make(tmp_path)
    c.provider_status, c.provider_status_at = {"isOpen": False, "holiday": "Special closure", "session": None}, T0
    assert c.session(T0) == Session.CLOSED
    c.provider_status_at = T0 - timedelta(hours=2)              # too old: back to the calendar
    assert c.session(T0) == Session.REGULAR


def test_add_stock_flow(tmp_path):
    mp = MockProvider({"MSFT": [410], "DEAD": [ZERO]})
    c, rec, st = make(tmp_path, provider=mp)
    woke = []; c.poll_now = lambda: woke.append(1)
    got = []
    c.check_symbol("msft", lambda t, e: got.append((t, e)))
    assert got[0][0].price == 410 and got[0][1] is None
    c.check_symbol("DEAD", lambda t, e: got.append((t, e)))
    assert isinstance(got[1][1], ValidationError)
    c.check_symbol("AAPL", lambda t, e: got.append((t, e)))
    assert "already" in str(got[2][1])
    s = StockConfig("MSFT", "HOLD", "Microsoft", False, Position(Dc(3), Dc("400")))
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
    bad = StockConfig("BAD", "HOLD", "", False, Position(Dc(-1), Dc(5)))
    c2, *_ = make(tmp_path / "b", example={"version": 1, "symbols": []})
    with pytest.raises(ValidationError):
        c2.add_stock(bad)
    assert c2.wl.symbols == [] and st.watchlist_path.read_text() == before


def test_new_stock_first_reading_only_sets_zone(tmp_path):
    c, rec, st = make(tmp_path, example={"version": 1, "symbols": []})
    c.add_stock(StockConfig("TTWO", "BUY", "", False, None, PlannedEntry(Dc("202.73"))))
    feed(c, T0, TTWO=("180", "200"))                            # lands in Deep drop on its first reading
    assert not rec.notes and c.zone_tag("TTWO") == "Deep drop · review"


def test_update_stock_registers_new_alert_with_choice(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"), TTWO=("201.9", "206"))
    new = json.loads(json.dumps({}))  # placeholder to keep flake quiet
    import copy
    stock = copy.deepcopy(c.stock("TTWO"))
    stock.alerts.append(Alert("fresh", "level_below", "RISK", "HIGH", mode="once", price=Dc("202.73")))
    c.update_stock(stock, {"fresh": "fire_now"})
    assert len(rec.notes) == 1 and rec.notes[0].headline.alert_id == "fresh"
    stock2 = copy.deepcopy(c.stock("TTWO"))
    stock2.alerts.append(Alert("w", "level_below", "RISK", "HIGH", mode="once", price=Dc("202.73")))
    c.update_stock(stock2, {"w": "wait_cross"})
    assert c.engine.alert_state("TTWO", "w")["state"] == "WAIT_CROSS"
    assert len(rec.notes) == 1


def test_update_removing_alert_and_remove_stock_clean_state(tmp_path):
    c, rec, st = make(tmp_path)
    feed(c, T0, AAPL=("200", "198"), TTWO=("205", "206"))
    import copy
    s = copy.deepcopy(c.stock("AAPL")); s.alerts = [a for a in s.alerts if a.id != "aapl-slip"]
    c.update_stock(s)
    assert "aapl-slip" not in c.engine.state["symbols"]["AAPL"]["alerts"]
    c.remove_stock("TTWO")
    assert "TTWO" not in c.engine.state["symbols"] and "TTWO" not in json.loads(st.state_path.read_text())["symbols"]
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


# ---- section 5.5: automatic "Slipping" alert (only on add / switch to SELL, never on load) ----
def _slip(stock):
    return [a for a in stock.alerts if a.type == "zone_exit" and a.zone == "sell"]


def test_adding_a_sell_stock_creates_the_slipping_alert(tmp_path):
    c, rec, st = make(tmp_path, example={"version": 1, "symbols": []})
    c.add_stock(StockConfig("MSFT", "SELL", "Microsoft", False, Position(Dc(3), Dc("400"))))
    (a,) = _slip(c.stock("MSFT"))
    assert (a.id, a.label, a.priority, a.note, a.enabled) == ("msft-slip", "RISK", "MEDIUM", "Slipping", True)
    saved = json.loads(st.watchlist_path.read_text())["symbols"][0]["alerts"]
    assert saved == [{"id": "msft-slip", "type": "zone_exit", "zone": "sell", "label": "RISK",
                      "priority": "MEDIUM", "enabled": True, "note": "Slipping"}]


def test_other_categories_do_not_get_slipping_on_add(tmp_path):
    c, rec, st = make(tmp_path, example={"version": 1, "symbols": []})
    c.add_stock(StockConfig("A", "HOLD", "", False, Position(Dc(1), Dc(10))))
    c.add_stock(StockConfig("B", "BUY", "", False, None, PlannedEntry(Dc(10))))
    c.add_stock(StockConfig("C", "WATCH"))
    assert all(_slip(c.stock(s)) == [] for s in "ABC")


def test_switching_to_sell_adds_slipping_once(tmp_path):
    import copy
    c, rec, st = make(tmp_path, example={"version": 1, "symbols": []})
    c.add_stock(StockConfig("MSFT", "HOLD", "", False, Position(Dc(3), Dc("400"))))
    s = copy.deepcopy(c.stock("MSFT")); s.category = "SELL"
    c.update_stock(s)
    assert len(_slip(c.stock("MSFT"))) == 1
    c.update_stock(copy.deepcopy(c.stock("MSFT")))                     # saving again changes nothing
    assert len(_slip(c.stock("MSFT"))) == 1


def test_deleted_slipping_alert_stays_deleted_through_saves_and_restarts(tmp_path):
    import copy
    c, rec, st = make(tmp_path, example={"version": 1, "symbols": []})
    c.add_stock(StockConfig("MSFT", "SELL", "", False, Position(Dc(3), Dc("400"))))
    s = copy.deepcopy(c.stock("MSFT")); s.alerts = []                  # the user deletes it in the editor
    c.update_stock(s)
    assert _slip(c.stock("MSFT")) == []
    c.update_stock(copy.deepcopy(c.stock("MSFT")))                     # stays a SELL stock: not re-added
    assert _slip(c.stock("MSFT")) == []
    c2 = AppController(Config(), st, MockProvider({}), Rec(st), now_fn=lambda: T0)      # restart: load from disk
    assert c2.symbols() == ["MSFT"] and _slip(c2.stock("MSFT")) == []
    assert "msft-slip" not in st.watchlist_path.read_text()


def test_loading_a_sell_stock_from_disk_never_adds_slipping(tmp_path):
    data = json.loads(json.dumps(EXAMPLE))
    data["symbols"][0]["alerts"] = [a for a in data["symbols"][0]["alerts"] if a["id"] != "aapl-slip"]
    c, rec, st = make(tmp_path, example=data)                          # SELL stock without slipping, on disk
    assert _slip(c.stock("AAPL")) == []
    c.housekeeping(T0)
    assert "aapl-slip" not in st.watchlist_path.read_text()


def test_existing_sell_range_exit_alert_is_not_duplicated_and_id_clash_is_resolved(tmp_path):
    import copy
    from watchlist import ensure_slipping_alert
    s = StockConfig("MSFT", "SELL", "", False, Position(Dc(3), Dc("400")),
                    alerts=[Alert("my-own", "zone_exit", "RISK", "HIGH", zone="sell")])
    assert ensure_slipping_alert(s) is False and len(s.alerts) == 1
    s2 = StockConfig("MSFT", "SELL", "", False, Position(Dc(3), Dc("400")),
                     alerts=[Alert("msft-slip", "level_above", "SELL", "LOW", price=Dc("500"), mode="once")])
    assert ensure_slipping_alert(s2) is True and [a.id for a in s2.alerts] == ["msft-slip", "msft-slip-2"]


def test_slipping_fires_when_a_new_sell_stock_slips_out_of_the_sell_range(tmp_path):
    c, rec, st = make(tmp_path, example={"version": 1, "symbols": []})
    c.add_stock(StockConfig("AAPL", "SELL", "", False, Position(Dc(10), Dc("200.00"))))
    for i, price in enumerate(["200", "207.50", "206.30"]):
        feed(c, T0 + timedelta(minutes=i), AAPL=(price, "198"))
    last = rec.notes[-1].events
    assert any(e.alert_id == "aapl-slip" and e.label == "RISK" and e.priority == "MEDIUM" for e in last)


def test_daily_move_percent_is_limited_to_500_and_demo_session_override(tmp_path):
    from watchlist import parse_alert
    from models import ValidationError as VE
    s = StockConfig("X", "WATCH")
    ok = parse_alert({"id": "m", "type": "pct_move_day", "pct": 500}, s, Config())
    assert ok.pct == 500
    for bad in (501, 0, -3):
        with pytest.raises(VE):
            parse_alert({"id": "m", "type": "pct_move_day", "pct": bad}, s, Config())
    from datetime import timezone as tz
    from market_clock import Session
    c, rec, st = make(tmp_path, session_override=Session.REGULAR)
    sat = datetime(2026, 10, 17, 15, 0, tzinfo=tz.utc)                 # a Saturday
    assert c.session(sat) == Session.REGULAR and c.header(sat).market == "OPEN"
    feed(c, sat, AAPL=("207.50", "200"), TTWO=("205", "206"))
    c.housekeeping(datetime(2026, 10, 15, 12, 0, tzinfo=tz.utc))


def test_session_transition_triggers_instant_poll(tmp_path):
    from market_clock import ET, Session
    c, rec, st = make(tmp_path)
    woke = []
    c.poll_now = lambda: woke.append(1)

    # 1. Closed/PRE market (09:29 ET)
    t_pre = datetime(2026, 10, 26, 9, 29, tzinfo=ET)
    c.status_strip(t_pre)
    assert woke == []

    # 2. Market opens (09:30 ET) -> transitions to Session.REGULAR -> immediate poll_now()!
    t_open = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
    c.status_strip(t_open)
    assert woke == [1]

    # 3. Subsequent poll during open session does not re-trigger transition
    c.status_strip(datetime(2026, 10, 26, 9, 31, tzinfo=ET))
    assert woke == [1]
