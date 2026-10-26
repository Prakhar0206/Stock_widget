"""Zones, hysteresis and zone price equivalents (blueprint section 5).

Zone decisions use the UNROUNDED percent distance. Rounding P to 2 decimals first would put
195.37 in the loss zone (P rounds to -3.00) while the displayed zone start is 195.36.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Optional

from models import ValidationError
from positions import D, CENT

TAGS = {
    "deep_loss": "Deep loss", "loss": "Loss zone", "below_be": "Below breakeven",
    "breakeven": "Breakeven", "above_be": "Above breakeven",
    "sell": "Sell zone", "high_sell": "High sell zone", "none": None,
    "deep_drop": "Deep drop · review", "entry_hit": "Entry hit",
    "near_entry": "Near entry", "above_entry": "Above entry", "far_above": "Far above entry",
}

# name -> (priority, label, enabled)
ZONE_DEFAULTS = {
    "deep_loss": ("HIGH", "RISK", False), "loss": ("HIGH", "RISK", True),
    "below_be": ("LOW", "WATCH", True), "breakeven": ("MEDIUM", "WATCH", True),
    "above_be": ("LOW", "WATCH", True), "sell": ("MEDIUM", "SELL", True),
    "high_sell": ("HIGH", "SELL", True), "none": ("LOW", "WATCH", False),
    "deep_drop": ("HIGH", "RISK", True), "entry_hit": ("HIGH", "BUY", True),
    "near_entry": ("MEDIUM", "BUY", True), "above_entry": ("LOW", "WATCH", True),
    "far_above": ("LOW", "WATCH", True),
}

# editable thresholds per category (name of the zone whose range starts/ends there)
THRESHOLD_KEYS = {
    "SELL": ("deep_loss", "loss", "sell", "high_sell"),
    "HOLD": ("deep_loss", "loss"),
    "BUY": ("deep_drop", "near_entry", "far_above"),
    "WATCH": (),
}

# A zone_exit alert for "sell" means leaving the whole sell range.
ZONE_GROUPS = {"sell": frozenset({"sell", "high_sell"})}

FIXED_LOW = Decimal("-0.5")
FIXED_HIGH = Decimal("0.5")


@dataclass(frozen=True)
class ZoneDef:
    name: str
    lo: Optional[Decimal]
    hi: Optional[Decimal]
    lo_incl: bool
    hi_incl: bool
    priority: str
    label: str
    enabled: bool          # enabled = may fire an entry alert
    widen_lo: bool = False  # hysteresis: the band is added on the edge(s) this zone is LEFT through
    widen_hi: bool = False

    @property
    def tag(self) -> Optional[str]:
        return TAGS[self.name]

    def contains(self, p: Decimal, band: Decimal = Decimal(0)) -> bool:
        """band == 0: the exact range (used to ENTER a zone, so a zone starts at its stated threshold).
        band > 0: the range used to KEEP the current zone. It is widened only on the flagged edge(s),
        never on the edge the zone is entered through."""
        lo_band = band if self.widen_lo else Decimal(0)
        hi_band = band if self.widen_hi else Decimal(0)
        if self.lo is not None:
            if lo_band > 0:
                if p < self.lo - lo_band:
                    return False
            elif p < self.lo or (p == self.lo and not self.lo_incl):
                return False
        if self.hi is not None:
            if hi_band > 0:
                if p > self.hi + hi_band:
                    return False
            elif p > self.hi or (p == self.hi and not self.hi_incl):
                return False
        return True


def thresholds(stock, defaults_pct: dict) -> dict:
    """Effective threshold percents (stock override, else config default)."""
    out = {}
    for key in THRESHOLD_KEYS.get(stock.category, ()):
        ov = stock.zones.get(key, {})
        out[key] = D(ov["pct"]) if ov.get("pct") is not None else D(defaults_pct[key])
    return out


def _zprops(stock, name):
    pr, lab, en = ZONE_DEFAULTS[name]
    ov = stock.zones.get(name, {})
    return ov.get("priority", pr), lab, bool(ov.get("enabled", en))


def _z(stock, name, lo, hi, lo_incl, hi_incl, widen_lo=False, widen_hi=False) -> ZoneDef:
    pr, lab, en = _zprops(stock, name)
    return ZoneDef(name, lo, hi, lo_incl, hi_incl, pr, lab, en, widen_lo, widen_hi)


def build_zone_defs(stock, defaults_pct: dict) -> list:
    """Ordered (low to high) zones for the stock's category. WATCH has none."""
    cat = stock.category
    if cat == "WATCH":
        return []
    t = thresholds(stock, defaults_pct)
    zs = []
    if cat in ("SELL", "HOLD"):
        deep_on = _zprops(stock, "deep_loss")[2]
        if deep_on:
            zs.append(_z(stock, "deep_loss", None, t["deep_loss"], False, True, widen_hi=True))
        zs.append(_z(stock, "loss", t["deep_loss"] if deep_on else None, t["loss"], False, True, widen_hi=True))
        zs.append(_z(stock, "below_be", t["loss"], FIXED_LOW, False, False))
        if cat == "HOLD":
            zs.append(_z(stock, "none", FIXED_LOW, None, True, False, widen_lo=True))
        else:
            zs.append(_z(stock, "breakeven", FIXED_LOW, FIXED_HIGH, True, True, widen_lo=True, widen_hi=True))
            zs.append(_z(stock, "above_be", FIXED_HIGH, t["sell"], False, False))
            zs.append(_z(stock, "sell", t["sell"], t["high_sell"], True, False, widen_lo=True))
            zs.append(_z(stock, "high_sell", t["high_sell"], None, True, False, widen_lo=True))
    else:  # BUY
        zs.append(_z(stock, "deep_drop", None, t["deep_drop"], False, True, widen_hi=True))
        zs.append(_z(stock, "entry_hit", t["deep_drop"], Decimal(0), False, True, widen_hi=True))
        zs.append(_z(stock, "near_entry", Decimal(0), t["near_entry"], False, True, widen_hi=True))
        zs.append(_z(stock, "above_entry", t["near_entry"], t["far_above"], False, False))
        zs.append(_z(stock, "far_above", t["far_above"], None, True, False, widen_lo=True))
    return zs


def classify(p: Optional[Decimal], defs: list, current: Optional[str], band) -> Optional[str]:
    """Hysteresis (5.4). A zone is ENTERED at its exact threshold and KEPT until P falls back past the
    threshold by `band`. Only the exit edge is widened (see ZoneDef.contains): Loss/Deep loss keep on
    their upper edge, Sell/High sell on their lower edge, Breakeven on both, and so on."""
    if p is None or not defs:
        return current
    band = D(band)
    cur = next((d for d in defs if d.name == current), None)
    if cur is not None and cur.contains(p, band):
        return cur.name
    for d in defs:
        if d.contains(p):
            return d.name
    return None


def band_for(stock, cfg) -> Decimal:
    return cfg.zone_band_noisy if stock.noisy else cfg.zone_band_standard


class ZoneTracker:
    """Per-stock zone state with optional 2-reading confirmation for noisy stocks."""

    def __init__(self):
        self.pending: Optional[str] = None

    def update(self, p, defs, current, band, noisy: bool) -> Optional[str]:
        new = classify(p, defs, current, band)
        if not noisy or current is None or new == current:
            self.pending = None
            return new
        if self.pending == new:          # same candidate on 2 consecutive readings
            self.pending = None
            return new
        self.pending = new
        return current


def _floor_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_FLOOR)


def _ceil_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_CEILING)


def zone_price_table(defs: list, base) -> list:
    """Price equivalents for display. Lower bound: first whole cent inside the zone (round up);
    upper bound: last whole cent inside it (round down)."""
    base = D(base)
    rows = []
    for d in defs:
        lo_p = _ceil_cent(base * (1 + d.lo / 100)) if d.lo is not None else None
        hi_p = _floor_cent(base * (1 + d.hi / 100)) if d.hi is not None else None
        rows.append({"name": d.name, "tag": d.tag, "lo_pct": d.lo, "hi_pct": d.hi,
                     "lo_price": lo_p, "hi_price": hi_p,
                     "priority": d.priority, "label": d.label, "enabled": d.enabled})
    return rows


def validate_thresholds(category: str, t: dict, *, pct_min=-90, pct_max=500) -> list:
    """Return a list of user-readable problems (empty when fine)."""
    errs = []
    for k, v in t.items():
        if not (pct_min <= v <= pct_max):
            errs.append(f"{k}: {v}% is outside {pct_min} to +{pct_max}.")
    if category in ("SELL", "HOLD"):
        if not t["deep_loss"] < t["loss"]:
            errs.append("Deep loss must be lower than Loss.")
        if not t["loss"] < FIXED_LOW:
            errs.append("Loss must be below -0.5.")
        if category == "SELL":
            if not FIXED_HIGH < t["sell"]:
                errs.append("Sell must be above +0.5.")
            if not t["sell"] < t["high_sell"]:
                errs.append("Sell must be lower than High sell.")
    elif category == "BUY":
        if not t["deep_drop"] < 0:
            errs.append("Deep drop must be below 0.")
        if not 0 < t["near_entry"]:
            errs.append("Near entry must be above 0.")
        if not t["near_entry"] < t["far_above"]:
            errs.append("Near entry must be lower than Far above.")
    return errs


def check_thresholds(stock, defaults_pct: dict) -> None:
    errs = validate_thresholds(stock.category, thresholds(stock, defaults_pct))
    if errs:
        raise ValidationError(f"{stock.symbol}: " + " ".join(errs))


# Which editable threshold belongs to which zone (shown in the expanded section).
THRESHOLD_OF_ZONE = {"deep_loss": "deep_loss", "loss": "loss", "sell": "sell", "high_sell": "high_sell",
                     "deep_drop": "deep_drop", "near_entry": "near_entry", "far_above": "far_above"}


def editor_rows(stock, defaults_pct: dict, base) -> list:
    """Every zone of the category, for the expanded section. Deep loss is listed even when it is
    off (so it can be switched on). Each row: name, tag, lo/hi pct, lo/hi price, priority,
    enabled, threshold key, threshold pct."""
    defs = [d for d in build_zone_defs(stock, defaults_pct) if d.name != "none"]       # as the engine sees them
    if stock.category in ("SELL", "HOLD") and not any(d.name == "deep_loss" for d in defs):
        s2 = copy.copy(stock)                       # Deep loss is off: still list it, for its own range only
        s2.zones = {k: dict(v) for k, v in stock.zones.items()}
        s2.zones.setdefault("deep_loss", {})["enabled"] = True
        defs.insert(0, next(d for d in build_zone_defs(s2, defaults_pct) if d.name == "deep_loss"))
    table = zone_price_table(defs, base) if base is not None else [
        {"name": d.name, "tag": d.tag, "lo_pct": d.lo, "hi_pct": d.hi, "lo_price": None, "hi_price": None,
         "priority": d.priority, "label": d.label, "enabled": d.enabled} for d in defs]
    t = thresholds(stock, defaults_pct)
    for row in table:
        row["enabled"] = _zprops(stock, row["name"])[2]               # the user's real switch
        key = THRESHOLD_OF_ZONE.get(row["name"])
        row["threshold_key"] = key if key in t else None
        row["threshold_pct"] = t.get(key) if key else None
    return table
