"""config.json loading, defaults, API-budget check, paths."""
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

from models import PRIORITIES

IST = ZoneInfo("Asia/Kolkata")
MAX_CALLS_PER_MIN = 48          # 80% of the 60/min free-tier limit, PER API KEY
SLOT_POLL_S = 15                # 15-second slot cadence during regular market hours with 4-key rotation
_KEY_SPLIT = re.compile(r"[,;\s]+")           # keys never contain commas, semicolons or whitespace


class BudgetError(ValueError):
    pass


def calls_per_minute(symbols: int, poll_seconds: int) -> Decimal:
    return Decimal(symbols + 1) * 60 / Decimal(poll_seconds)


def max_calls_per_minute(num_keys: int = 1) -> int:
    """The safe ceiling grows with the pool: 48 calls/min for every independent key."""
    return MAX_CALLS_PER_MIN * max(1, int(num_keys))


def check_budget(symbols: int, poll_seconds: int, num_keys: int = 1) -> Decimal:
    """Refuse any setting above 48 calls/min per key."""
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
    level_rearm_band_pct: Decimal = Decimal("0.5")
    default_priority: str = "MEDIUM"
    pop_seconds: int = 10
    escalation_seconds: int = 120
    sound: bool = True
    toast: bool = True
    quiet_hours_ist: Optional[tuple] = None      # (start, end) as datetime.time
    hotkeys_enabled: bool = True
    extended_hours: bool = True
    look: dict = field(default_factory=dict)     # desktop-layer look settings
    api_keys: list = field(default_factory=list, repr=False)
    num_keys: int = 1


def _dec(v) -> Decimal:
    if isinstance(v, bool):
        raise ValueError("expected a number")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except ArithmeticError:
        raise ValueError("expected a number")
    if not d.is_finite():
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
    if not isinstance(raw, str):
        return []
    out = []
    for part in _KEY_SPLIT.split(raw):
        key = clean_api_key(part)
        if key and key not in out:
            out.append(key)
    return out


def _parse_api_keys(v) -> list:
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
    Legacy keys (`fees`, `zone_band`, `zone_defaults`, `level_alert_mode`, `cooldown_min`)
    are silently ignored so existing config.json files continue to load cleanly."""
    if not isinstance(d, dict):
        raise ValueError("top level must be an object")
    rearm_band = _dec(d.get("level_rearm_band_pct", "0.5"))
    if not (Decimal(0) <= rearm_band < Decimal(100)):
        raise ValueError("level_rearm_band_pct must be between 0 and 100")
    prio = d.get("default_priority", "MEDIUM")
    if prio not in PRIORITIES:
        raise ValueError("default_priority must be LOW, MEDIUM, HIGH or CUSTOM")
    api_keys = _parse_api_keys(d.get("api_keys"))
    num_keys = max(1, num_env_keys if num_env_keys > 0 else max(1, len(api_keys)))
    default_poll = 15 if num_keys >= 4 else 60
    raw_poll = d.get("poll_seconds")
    if raw_poll is None or (raw_poll == 60 and num_keys >= 4):
        poll_s = default_poll
    else:
        poll_s = _int(d, "poll_seconds", default_poll)
    cfg = Config(
        poll_seconds=poll_s,
        stale_after_seconds=_int(d, "stale_after_seconds", 180),
        max_symbols=_int(d, "max_symbols", 30),
        level_rearm_band_pct=rearm_band,
        default_priority=prio,
        pop_seconds=_int(d, "pop_seconds", 10),
        escalation_seconds=_int(d, "escalation_seconds", 120),
        sound=_bool(d, "sound", True),
        toast=_bool(d, "toast", True),
        quiet_hours_ist=_parse_quiet(d.get("quiet_hours_ist")),
        hotkeys_enabled=_bool(d, "hotkeys_enabled", True),
        extended_hours=_bool(d, "extended_hours", True),
        look=dict(_obj(d, "look")),
        api_keys=api_keys,
        num_keys=num_keys,
    )
    check_budget(cfg.max_symbols, cfg.poll_seconds, num_keys=cfg.num_keys)
    return cfg


def load_config(path: Path, num_env_keys: int = 0):
    """Returns (Config, warnings). Missing file -> defaults, no warning. Invalid -> defaults + warning."""
    path = Path(path)
    num_keys = max(1, int(num_env_keys))
    default_poll = 15 if num_keys >= 4 else 60
    if not path.exists():
        return Config(poll_seconds=default_poll, num_keys=num_keys), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        return parse_config(data, num_env_keys=num_env_keys), []
    except Exception as exc:
        return Config(poll_seconds=default_poll, num_keys=num_keys), [f"config.json is invalid ({exc}); using built-in defaults."]


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
    if not isinstance(raw, str):
        return None
    key = raw.strip().strip('"\'').strip()
    return key or None


def get_api_key(environ: Optional[dict[str, str]] = None) -> Optional[str]:
    env = os.environ if environ is None else environ
    return clean_api_key(env.get("FINNHUB_API_KEY"))


def get_api_keys(environ: Optional[dict[str, str]] = None) -> list[str]:
    env = os.environ if environ is None else environ
    keys: list[str] = []
    names = ["FINNHUB_API_KEY", "FINNHUB_API_KEYS"] + [f"FINNHUB_API_KEY_{i}" for i in range(1, 11)]
    for name in names:
        for key in _split_keys(env.get(name)):
            if key not in keys:
                keys.append(key)
    return keys
