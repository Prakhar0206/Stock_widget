"""Clean toast / pop-up formatting for stock alerts and system warnings."""
from __future__ import annotations

from datetime import datetime

from market_clock import fmt_ist
from models import AlertEvent
from positions import q2

SOURCE_NAMES = {
    "finnhub": "Finnhub",
    "mock": "Mock feed",
    "test": "Test",
    "yahoo_1m": "Yahoo 1m (Offline)",
}


def describe(ev: AlertEvent) -> str:
    if ev.kind == "feed_stale":
        return "Price feed has not updated recently"
    if ev.kind == "key_invalid":
        return ev.condition or "API key invalid"
    if ev.kind == "offline_summary":
        return ev.message or f"{ev.note or 'Alerts'} triggered while offline"
    if ev.message:
        return ev.message
    note = f" ({ev.note})" if ev.note else ""
    return f"Price {ev.condition}{note}"


def format_message(ev: AlertEvent, now: datetime) -> str:
    if ev.kind == "feed_stale":
        return "\n".join([
            f"[{ev.priority}] FEED · {ev.label} · {fmt_ist(ev.time)} IST",
            describe(ev),
            "Alerts for stale prices are paused",
            f"Source {SOURCE_NAMES.get(ev.source, ev.source)}.",
        ])
    if ev.kind == "key_invalid":
        return f"[{ev.priority}] {ev.condition or 'API key invalid'}"
    if ev.kind == "offline_summary":
        return "\n".join([
            f"[{ev.priority}] OFFLINE CATCH-UP · {fmt_ist(ev.time)} IST",
            describe(ev),
            f"Source {SOURCE_NAMES.get(ev.source, ev.source)}. Open Alerts Inbox to review.",
        ])
    title_name = f" · {ev.name}" if ev.name else ""
    sess = f" ({ev.session})" if ev.session else ""
    line1 = f"[{ev.priority}] {ev.symbol}{title_name} · ${q2(ev.price):.2f} · {fmt_ist(ev.time)} IST{sess}"
    line2 = describe(ev)
    src = SOURCE_NAMES.get(ev.source, ev.source)
    line3 = f"Source {src}, data age {ev.data_age_s} s."
    return "\n".join([line1, line2, line3])


def format_notification(note, now: datetime) -> str:
    """One notification for one reading: the top event in full, then a line per extra event."""
    lines = [format_message(note.headline, now)]
    for ev in note.events[1:4]:
        lines.append(f"Also [{ev.priority}] {ev.symbol}: {describe(ev)}")
    if len(note.events) > 4:
        lines.append(f"... and {len(note.events) - 4} more (see the alert log)")
    return "\n".join(lines)
