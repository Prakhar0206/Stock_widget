"""config.json loading, defaults, API-budget check, paths (blueprint sections 3, 8.3, 10)."""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from models import MODES, PRIORITIES
from positions import FeeRates

IST = ZoneInfo("Asia/Kolkata")
MAX_CALLS_PER_MIN = 48          # 80% of the 60/min free-tier limit, PER API KEY
ZONE_DEFAULTS_PCT = {"deep_loss": -10, "loss": -3, "sell": 3, "high_sell": 4.5,
                     "deep_drop": -10, "near_entry": 1, "far_above": 5}
_KEY_SPLIT = re.compile(r"[,;\s]+")           # keys never contain commas, semicolons or whitespace


class BudgetError(ValueError):
    pass


def calls_per_minute(symbols: int, poll_seconds: int) -> Decimal:
    return Decimal(symbols + 1) * 60 / Decimal(poll_seconds)


def max_calls_per_minute(num_keys: int = 1) -> int:
    """The safe ceiling grows with the pool: 48 calls/min for every independent key."""
    return MAX_CALLS_PER_MIN * max(1, int(num_keys))


def check_budget(symbols: int, poll_seconds: int, num_keys: int = 1) -> Decimal:
    """Refuse any setting above 48 calls/min per key.
    1 key:  30 symbols at 60 s = 31 (ok); at 30 s = 62 (refused).
    4 keys: ceiling is 192, so 30 symbols at 15 s = 124 (ok)."""
    if poll_seconds <= 0:
        raise BudgetError("poll_seconds must be above 0.")
    max_cpm = max_calls_per_minute(num_keys)
    cpm = calls_per_minute(symbols, poll_seconds)
    if cpm > max_cpm:
        pool = f" ({num_keys} keys x {MAX_CALLS_PER_MIN})" if num_keys > 1 else ""
        raise BudgetError(f"{symbols} symbols every {poll_seconds} s is {cpm:.0f} calls per minute; "
                          f"the limit is {max_cpm}{pool}.")
    return cpm


def min_poll_seconds(symbols: int, num_keys: int = 1) -> int:
    """Fastest poll interval (whole seconds) that stays inside the budget."""
    return max(1, math.ceil(Decimal(symbols + 1) * 60 / max_calls_per_minute(num_keys)))


@dataclass(frozen=True)
class Config:
    poll_seconds: int = 60
    stale_after_seconds: int = 180
    max_symbols: int = 30
    rates: FeeRates = FeeRates()
    zone_band_standard: Decimal = Decimal("0.5")
    zone_band_noisy: Decimal = Decimal("1.0")
    level_rearm_band_pct: Decimal = Decimal("0.5")
    level_alert_mode: str = "rearm"
    cooldown_min: int = 30
    default_priority: str = "MEDIUM"
    pop_seconds: int = 10
    escalation_seconds: int = 120
    sound: bool = True
    toast: bool = True
    quiet_hours_ist: Optional[tuple] = None      # (start, end) as datetime.time
    hotkeys_enabled: bool = True
    extended_hours: bool = False
    zone_defaults: dict = field(default_factory=lambda: dict(ZONE_DEFAULTS_PCT))
    look: dict = field(default_factory=dict)     # desktop-layer look settings (Step B)
    # Finnhub keys from config.json (plaintext fallback; the environment wins). repr=False so a
    # logged Config can never print a key.
    api_keys: list = field(default_factory=list, repr=False)


def _dec(v) -> Decimal:
    if isinstance(v, bool):
        raise ValueError("expected a number")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except ArithmeticError:                      # decimal.InvalidOperation: "abc", "", ...
        raise ValueError("expected a number")
    if not d.is_finite():                        # NaN / Infinity would poison every comparison
        raise ValueError("expected a finite number")
    return d


def _obj(d, key):
    v = d.get(key, {})
    if not isinstance(v, dict):
        raise ValueError(f"{key} must be an object")
    return v


def _int(d, key, default, lo=1):
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)) or int(v) != v or v < lo:
        raise ValueError(f"{key} must be a whole number >= {lo}")
    return int(v)


def _bool(d, key, default):
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ValueError(f"{key} must be true or false")
    return v


def _parse_quiet(v):
    if v in (None, "none"):
        return None
    try:
        s = datetime.strptime(v["start"], "%H:%M").time()
        e = datetime.strptime(v["end"], "%H:%M").time()
    except Exception:
        raise ValueError('quiet_hours_ist must be null or {"start": "HH:MM", "end": "HH:MM"}')
    return (s, e)


def _split_keys(raw) -> list:
    """'k1, "k2" ;k3' -> ['k1', 'k2', 'k3']. Cleaned, empties dropped, duplicates removed, order kept."""
    if not isinstance(raw, str):
        return []
    out = []
    for part in _KEY_SPLIT.split(raw):
        key = clean_api_key(part)
        if key and key not in out:
            out.append(key)
    return out


def _parse_api_keys(v) -> list:
    """config.json "api_keys": a list of strings (or one comma-separated string). Error messages
    never contain a key value, because load_config shows them on the widget."""
    if v is None:
        return []
    if isinstance(v, str):
        return _split_keys(v)
    if isinstance(v, list):
        out = []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("api_keys must be a list of strings")
            for key in _split_keys(item):
                if key not in out:
                    out.append(key)
        return out
    raise ValueError("api_keys must be a list of strings")


def parse_config(d: dict, num_env_keys: int = 0) -> Config:
    """Raises ValueError with a readable message when anything is wrong.
    num_env_keys: how many keys the environment already supplies. They win over config.json, so the
    budget check counts them instead of the "api_keys" list."""
    if not isinstance(d, dict):
        raise ValueError("top level must be an object")
    f = _obj(d, "fees")
    rates = FeeRates(
        buy_rate=_dec(f.get("buy_rate_pct", Decimal("0.33"))) / 100,
        buy_min=_dec(f.get("buy_min", Decimal("0.02"))),
        sell_rate=_dec(f.get("sell_rate_pct", Decimal("0.37"))) / 100,
        sell_min=_dec(f.get("sell_min", Decimal("0.05"))))
    if not (0 <= rates.buy_rate < 1 and 0 <= rates.sell_rate < 1 and rates.buy_min >= 0 and rates.sell_min >= 0):
        raise ValueError("fee rates/minimums out of range")
    zb = _obj(d, "zone_band")
    zd = dict(ZONE_DEFAULTS_PCT)
    zd.update({k: _dec(v) for k, v in _obj(d, "zone_defaults").items() if k in zd})
    mode = d.get("level_alert_mode", "rearm")
    prio = d.get("default_priority", "MEDIUM")
    if mode not in MODES:
        raise ValueError("level_alert_mode must be once, rearm or repeat")
    if prio not in PRIORITIES:
        raise ValueError("default_priority must be LOW, MEDIUM or HIGH")
    api_keys = _parse_api_keys(d.get("api_keys"))
    cfg = Config(
        poll_seconds=_int(d, "poll_seconds", 60), stale_after_seconds=_int(d, "stale_after_seconds", 180),
        max_symbols=_int(d, "max_symbols", 30), rates=rates,
        zone_band_standard=_dec(zb.get("standard", "0.5")), zone_band_noisy=_dec(zb.get("noisy", "1.0")),
        level_rearm_band_pct=_dec(d.get("level_rearm_band_pct", "0.5")), level_alert_mode=mode,
        cooldown_min=_int(d, "cooldown_min", 30), default_priority=prio,
        pop_seconds=_int(d, "pop_seconds", 10), escalation_seconds=_int(d, "escalation_seconds", 120),
        sound=_bool(d, "sound", True), toast=_bool(d, "toast", True),
        quiet_hours_ist=_parse_quiet(d.get("quiet_hours_ist")),
        hotkeys_enabled=_bool(d, "hotkeys_enabled", True), extended_hours=_bool(d, "extended_hours", False),
        zone_defaults=zd, look=dict(_obj(d, "look")), api_keys=api_keys)
    num_keys = num_env_keys if num_env_keys > 0 else len(api_keys)
    check_budget(cfg.max_symbols, cfg.poll_seconds, num_keys=num_keys)
    return cfg


def load_config(path: Path, num_env_keys: int = 0):
    """Returns (Config, warnings). Missing file -> defaults, no warning. Invalid -> defaults + warning.
    num_env_keys is passed through to parse_config (keys that come from the environment)."""
    path = Path(path)
    if not path.exists():
        return Config(), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        return parse_config(data, num_env_keys=num_env_keys), []
    except Exception as exc:  # any problem: built-in defaults, shown on the widget
        return Config(), [f"config.json is invalid ({exc}); using built-in defaults."]


def in_quiet_hours(cfg: Config, now_utc: datetime) -> bool:
    if cfg.quiet_hours_ist is None:
        return False
    s, e = cfg.quiet_hours_ist
    t = now_utc.astimezone(IST).time()
    return (s <= t < e) if s <= e else (t >= s or t < e)


def data_dir(override: Optional[Path] = None) -> Path:
    """%APPDATA%\\StockAlertWidget (falls back to ~/.StockAlertWidget when APPDATA is unset)."""
    if override is not None:
        p = Path(override)
    else:
        base = os.environ.get("APPDATA")
        p = (Path(base) if base else Path.home()) / ("StockAlertWidget" if base else ".StockAlertWidget")
    p.mkdir(parents=True, exist_ok=True)
    return p


def clean_api_key(raw) -> Optional[str]:
    """Trim whitespace and stray quotes (e.g. from `setx FINNHUB_API_KEY "abc "`). Returns None when
    nothing is left, so an empty or quotes-only value counts as unset. Never log this value."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().strip('"\'').strip()
    return key or None


def get_api_key(environ: Optional[dict[str, str]] = None) -> Optional[str]:
    """Read from the environment only. Never log, print or write this value.
    Kept for backward compatibility; the pool uses get_api_keys()."""
    env = os.environ if environ is None else environ
    return clean_api_key(env.get("FINNHUB_API_KEY"))


def get_api_keys(environ: Optional[dict[str, str]] = None) -> list[str]:
    """Every Finnhub key found in FINNHUB_API_KEY, then FINNHUB_API_KEYS. Either may hold several
    keys separated by commas, e.g. "key1, key2, key3, key4". Each key is cleaned like
    clean_api_key(); empties and duplicates are dropped; order is kept. [] when nothing is set.
    Never log, print or write these values."""
    env = os.environ if environ is None else environ
    keys: list[str] = []
    for name in ("FINNHUB_API_KEY", "FINNHUB_API_KEYS"):
        for key in _split_keys(env.get(name)):
            if key not in keys:
                keys.append(key)
    return keys
