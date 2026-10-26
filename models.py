"""Plain data classes and constants shared by every module (no logic here)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

CATEGORIES = ("BUY", "HOLD", "SELL", "WATCH")
LABELS = ("BUY", "SELL", "WATCH", "RISK")
PRIORITIES = ("LOW", "MEDIUM", "HIGH")
PRIORITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
ALERT_TYPES = ("level_above", "level_below", "zone_exit",
               "cross_above", "cross_below", "pct_move_day")
LEVEL_TYPES = ("level_above", "level_below")
EDITOR_TYPES = ("level_above", "level_below", "zone_exit")  # what the v1 editor may create
MODES = ("once", "rearm", "repeat")

# alert states (stored in state.json)
ARMED, WAIT_CROSS, DISARMED, COOLDOWN, DONE, DISABLED = (
    "ARMED", "WAIT_CROSS", "DISARMED", "COOLDOWN", "DONE", "DISABLED")

DEFAULT_LABEL_BY_CATEGORY = {"BUY": "BUY", "SELL": "SELL", "HOLD": "WATCH", "WATCH": "WATCH"}


@dataclass
class Position:
    shares: Decimal
    avg_price: Decimal
    actual_buy_fee: Optional[Decimal] = None


@dataclass
class PlannedEntry:
    price: Decimal
    shares: Optional[Decimal] = None


@dataclass
class Alert:
    id: str
    type: str
    label: str
    priority: str
    enabled: bool = True
    mode: str = "rearm"
    price: Optional[Decimal] = None
    pct: Optional[Decimal] = None
    zone: Optional[str] = None
    cooldown_min: int = 30
    note: str = ""


@dataclass
class StockConfig:
    symbol: str
    category: str
    nickname: str = ""
    noisy: bool = False
    position: Optional[Position] = None
    planned_entry: Optional[PlannedEntry] = None
    zones: dict = field(default_factory=dict)   # name -> {"pct","priority","enabled"} overrides
    alerts: list = field(default_factory=list)


@dataclass
class Watchlist:
    version: int = 1
    symbols: list = field(default_factory=list)

    def get(self, symbol: str) -> Optional[StockConfig]:
        for s in self.symbols:
            if s.symbol == symbol:
                return s
        return None


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    price: Decimal
    prev_close: Optional[Decimal]
    change: Optional[Decimal]
    change_pct: Optional[Decimal]
    day_high: Optional[Decimal]
    day_low: Optional[Decimal]
    provider_time: Optional[datetime]   # aware UTC, time of last trade
    fetched_at: datetime                # aware UTC
    source: str

    def age_seconds(self, now: datetime) -> int:
        ref = self.provider_time or self.fetched_at
        return max(0, int((now - ref).total_seconds()))


@dataclass
class AlertEvent:
    symbol: str
    alert_id: str
    kind: str            # level | cross | pct_move | zone_entry | zone_exit | feed_stale
    label: str
    priority: str
    price: Decimal
    time: datetime       # aware UTC
    source: str
    category: str = ""
    condition: str = ""  # zone name or condition text (goes to the log)
    zone: Optional[str] = None
    pct_vs_base: Optional[Decimal] = None   # unrounded; for message text only
    base_name: str = ""                     # "breakeven" or "planned entry"
    net_estimate: Optional[Decimal] = None  # message only, never logged
    data_age_s: int = 0
    note: str = ""


class ValidationError(ValueError):
    """Raised with a message that is safe to show the user."""
