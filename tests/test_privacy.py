"""Test 15: scan every file the app writes. The API key must appear nowhere. Position numbers
(shares, average price, fees, P/L) may appear only in watchlist.json and its last-good copy,
which are the user's own position store."""
import os
from datetime import timedelta
from decimal import Decimal as Dc
from pathlib import Path

from config import Config
from engine import AlertEngine
from market_clock import Session
from messages import format_message
from models import Alert, Position, StockConfig, Watchlist
from store import Store, setup_logging
from conftest import Feed, lvl

KEY = "TESTKEY_9f8e7d6c5b4a"
POSITION_FILES = {"watchlist.json", "watchlist.last_good.json"}


def test_15_no_key_no_position_data_in_written_files(tmp_path, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", KEY)
    cfg = Config()
    st = Store(tmp_path)
    setup_logging(tmp_path)
    s = StockConfig("AAPL", "SELL", "Apple", False, Position(Dc("7.31"), Dc("123.456"), Dc("4.77")), None, {},
                    [lvl("t", "level_above", price="130", mode="once", label="SELL"),
                     Alert("slip", "zone_exit", "RISK", "MEDIUM", zone="sell")])
    st.save_watchlist(Watchlist(1, [s]), cfg)
    e = AlertEngine(cfg, st.load_state())
    f = Feed(e, s)
    for price in ["125", "131", "128", "133"]:
        for ev in f.push(price):
            st.log_alert(ev)                                    # logged first ...
            format_message(ev, f.t)                             # ... then notified
    st.save_state(e.state)
    st.save_summary("2026-10-14", {"date": "2026-10-14", "stocks": []})
    import logging
    logging.getLogger("saw.test").info("polled AAPL price 131 ok")

    secrets = ["7.31", "123.456", "4.77", "123.46", "TESTKEY"]
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert {p.name for p in files} >= {"watchlist.json", "state.json", "alerts_log.jsonl", "app.log"}
    assert st.read_alert_log(), "the scenario should have fired alerts"
    for p in files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        assert KEY not in text, p.name
        if p.name not in POSITION_FILES:
            for sec in secrets:
                assert sec not in text, f"{sec} found in {p.name}"
            assert '"shares"' not in text and "avg_price" not in text and "net" not in text.lower().replace("network", ""), p.name


# ---- logging redaction filter (defence in depth) ----------------------------------------------------
def _fresh_logger(tmp_path):
    import logging
    for h in list(logging.getLogger("saw").handlers):
        logging.getLogger("saw").removeHandler(h)
        h.close()
    setup_logging(tmp_path)
    return logging.getLogger("saw.redact_test")


def _flush_and_read(tmp_path):
    import logging
    for h in logging.getLogger("saw").handlers:
        h.flush()
    return (tmp_path / "app.log").read_text(encoding="utf-8")


def test_log_filter_masks_key_and_position_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", KEY)
    log = _fresh_logger(tmp_path)
    log.info("request failed for %s with header X-Finnhub-Token: %s", "AAPL", KEY)
    log.info("debug position avg_price=123.456 shares: 7.31 actual_buy_fee = 4.77 {'avg_price': 99.5}")
    log.info("plain message about AAPL at 207.50")
    text = _flush_and_read(tmp_path)
    for secret in (KEY, "123.456", "7.31", "4.77", "99.5"):
        assert secret not in text, secret
    assert "[REDACTED_KEY]" in text and "avg_price=[REDACTED]" in text
    assert "plain message about AAPL at 207.50" in text                 # normal lines are untouched


def test_log_filter_masks_secrets_inside_tracebacks(tmp_path, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", KEY)
    log = _fresh_logger(tmp_path)
    try:
        raise RuntimeError(f"connection error for token {KEY} avg_price=88.88")
    except RuntimeError:
        log.exception("poll crashed")
    text = _flush_and_read(tmp_path)
    assert "poll crashed" in text and "RuntimeError" in text
    assert KEY not in text and "88.88" not in text


def test_redact_is_a_noop_without_secrets_and_never_raises(monkeypatch):
    from store import redact
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert redact("AAPL zone sell price 207.50 source finnhub") == "AAPL zone sell price 207.50 source finnhub"
    assert redact("") == ""
