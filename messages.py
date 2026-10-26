"""Four-line toast / pop-up text (blueprint section 7). Messages describe what happened;
they never tell the user to trade. BUY/SELL/WATCH/RISK are labels only."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from market_clock import fmt_ist
from models import AlertEvent
from positions import q2
from zones import TAGS

SOURCE_NAMES = {"finnhub": "Finnhub", "mock": "Mock feed"}


def _signed(x: Decimal) -> str:
    return f"{'+' if x >= 0 else '-'}{abs(x):.2f}"


def _pct_phrase(ev: AlertEvent) -> str:
    if ev.pct_vs_base is None:
        return ""
    p = q2(ev.pct_vs_base)
    way = "above" if p >= 0 else "below"
    return f", {_signed(p)}% {way} {ev.base_name}".replace("+-", "-")


def describe(ev: AlertEvent) -> str:
    if ev.kind == "zone_entry":
        tag = (TAGS.get(ev.zone or "") or "").lower()
        return f"Entered {tag}{_pct_phrase(ev)}"
    if ev.kind == "zone_exit":
        what = f"Left the {ev.condition.replace('left ', '')} range"
        note = f" ({ev.note})" if ev.note else ""
        return f"{what}{note}{_pct_phrase(ev)}"
    if ev.kind == "feed_stale":
        return "Price feed has not updated recently"
    note = f" ({ev.note})" if ev.note else ""
    return f"Price {ev.condition}{note}{_pct_phrase(ev)}"


def format_message(ev: AlertEvent, now: datetime) -> str:
    if ev.kind == "feed_stale":
        return "\n".join([f"[{ev.priority}] FEED · {ev.label} · {fmt_ist(ev.time)} IST", describe(ev),
                          "alerts for stale prices are paused", f"Source {SOURCE_NAMES.get(ev.source, ev.source)}."])
    line1 = f"[{ev.priority}] {ev.symbol} · {ev.label} · {ev.price:.2f} · {fmt_ist(ev.time)} IST"
    if ev.net_estimate is not None:
        n = ev.net_estimate
        line3 = f"est. net {'+' if n >= 0 else '-'}${abs(n):.2f}"
        tail = "Estimate before tax. "
    elif ev.category == "BUY":
        line3, tail = "compared with your planned entry", ""
    else:
        line3, tail = "no position entered", ""
    src = SOURCE_NAMES.get(ev.source, ev.source)
    line4 = f"{tail}Source {src}, data age {ev.data_age_s} s."
    return "\n".join([line1, describe(ev), line3, line4])


def format_notification(note, now: datetime) -> str:
    """One notification for one reading: the top event in full, then a line per extra event."""
    lines = [format_message(note.headline, now)]
    for ev in note.events[1:4]:
        lines.append(f"Also [{ev.priority}] {ev.symbol} · {ev.label}: {describe(ev)}")
    if len(note.events) > 4:
        lines.append(f"... and {len(note.events) - 4} more (see the alert log)")
    return "\n".join(lines)
