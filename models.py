"""Plain data classes and constants shared by every module (no logic here)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

CATEGORIES = ("BUY", "HOLD", "SELL", "WATCH")
LABELS = ("BUY", "HOLD", "SELL", "WATCH", "RISK")
DEFAULT_LABEL_BY_CATEGORY = {"BUY": "BUY", "HOLD": "HOLD", "SELL": "SELL", "WATCH": "WATCH"}

PRIORITIES = ("LOW", "MEDIUM", "HIGH", "CUSTOM")
PRIORITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CUSTOM": 1}

PRIMARY_OPERATORS = ("crossing", "crossing_up", "crossing_down", "greater_than", "less_than")
CHANNEL_OPERATORS = ("entering_channel", "exiting_channel", "inside_channel", "outside_channel")
MOVING_OPERATORS = ("moving_up", "moving_down", "moving_up_pct", "moving_down_pct")
ALERT_OPERATORS = PRIMARY_OPERATORS + CHANNEL_OPERATORS + MOVING_OPERATORS
STATE_OPERATORS = ("greater_than", "less_than", "inside_channel", "outside_channel")

OPERATOR_LABELS = {
    "crossing": "Crossing",
    "crossing_up": "Crossing Up",
    "crossing_down": "Crossing Down",
    "greater_than": "Greater Than",
    "less_than": "Less Than",
    "entering_channel": "Entering Channel",
    "exiting_channel": "Exiting Channel",
    "inside_channel": "Inside Channel",
    "outside_channel": "Outside Channel",
    "moving_up": "Moving Up ($)",
    "moving_down": "Moving Down ($)",
    "moving_up_pct": "Moving Up %",
    "moving_down_pct": "Moving Down %",
}

CONDITION_MODES = ("price", "pct")
PCT_BASES = ("auto", "buy_price", "sell_price", "prev_close", "custom")
PCT_BASE_LABELS = {
    "auto": "Auto (Buy/Sell -> Prev Close)",
    "buy_price": "Buy Price",
    "sell_price": "Sell Price",
    "prev_close": "Previous Close",
    "custom": "Custom Price",
}

TRIGGER_MODES = ("once", "every_time", "rearm")
TRIGGER_LABELS = {
    "once": "Once only",
    "every_time": "Every time",
    "rearm": "Once per cross (Re-arm)",
}

EXPIRATION_PRESETS = (
    "open_ended",
    "end_of_session",
    "end_of_next_session",
    "1_week",
    "1_month",
    "custom",
)
EXPIRATION_LABELS = {
    "open_ended": "Open-ended",
    "end_of_session": "End of trading session",
    "end_of_next_session": "End of next trading session",
    "1_week": "1 week",
    "1_month": "1 month",
    "custom": "Custom date & time",
}

SESSION_SCOPES = ("all", "regular", "extended")
SESSION_SCOPE_LABELS = {
    "all": "All Sessions (PH + RH + AH)",
    "regular": "Regular Hours Only (RH)",
    "extended": "Extended Hours Only (PH + AH)",
}

SOUND_TONES = ("none", "chime", "beep", "bell", "alarm")
SOUND_DURATIONS = (0, 5, 10, 30)

# alert states (stored in state.json)
ARMED, WAIT_CROSS, DISARMED, COOLDOWN, DONE, EXPIRED, DISABLED = (
    "ARMED", "WAIT_CROSS", "DISARMED", "COOLDOWN", "DONE", "EXPIRED", "DISABLED"
)


@dataclass
class TradeInfo:
    """Manual trade figures entered by the user (all optional)."""
    buy_price: Optional[Decimal] = None
    shares: Optional[Decimal] = None
    sell_price: Optional[Decimal] = None
    fees: Optional[Decimal] = None


@dataclass
class CustomNotification:
    """Per-alert delivery configuration (resolved from priority tier or edited via Custom...)."""
    popup: bool = True
    toast: bool = True
    sound_tone: str = "chime"        # none | chime | beep | bell | alarm
    sound_seconds: int = 5           # 0 (once) | 5 | 10 | 30
    repeat_until_ack: bool = False


def resolve_notification_spec(priority: str, custom: Optional[CustomNotification] = None) -> CustomNotification:
    """Map shifted priority presets (LOW/MEDIUM/HIGH/CUSTOM) to delivery settings."""
    if priority == "LOW":
        return CustomNotification(popup=False, toast=True, sound_tone="chime", sound_seconds=0, repeat_until_ack=False)
    if priority == "MEDIUM":
        return CustomNotification(popup=True, toast=True, sound_tone="beep", sound_seconds=5, repeat_until_ack=False)
    if priority == "HIGH":
        return CustomNotification(popup=True, toast=True, sound_tone="alarm", sound_seconds=10, repeat_until_ack=True)
    if custom is not None:
        return CustomNotification(
            popup=bool(custom.popup),
            toast=bool(custom.toast),
            sound_tone=custom.sound_tone if custom.sound_tone in SOUND_TONES else "chime",
            sound_seconds=int(custom.sound_seconds) if int(custom.sound_seconds) in SOUND_DURATIONS else 5,
            repeat_until_ack=bool(custom.repeat_until_ack),
        )
    return CustomNotification()


@dataclass
class Alert:
    id: str
    operator: str = "crossing"
    condition_mode: str = "price"          # "price" | "pct"
    value: Optional[Decimal] = None
    upper: Optional[Decimal] = None
    lower: Optional[Decimal] = None
    window_min: int = 5
    pct_base: str = "auto"                 # "auto" | "buy_price" | "sell_price" | "prev_close" | "custom"
    ref_price: Optional[Decimal] = None
    trigger: str = "once"                  # "once" | "every_time" | "rearm"
    cooldown_min: int = 1
    expiration: str = "open_ended"         # EXPIRATION_PRESETS
    expires_at: Optional[str] = None       # ISO UTC string when expiration != "open_ended"
    session_scope: str = "all"             # "all" | "regular" | "extended"
    name: str = ""
    message: str = ""
    priority: str = "MEDIUM"               # "LOW" | "MEDIUM" | "HIGH" | "CUSTOM"
    custom_notification: Optional[CustomNotification] = None
    enabled: bool = True
    label: str = "WATCH"


@dataclass
class StockConfig:
    symbol: str
    category: str = "WATCH"
    nickname: str = ""
    noisy: bool = False
    trade: Optional[TradeInfo] = None
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
    pre_price: Optional[Decimal] = None
    reg_price: Optional[Decimal] = None
    ah_price: Optional[Decimal] = None
    session_kind: Optional[str] = None

    def age_seconds(self, now: datetime) -> int:
        ref = self.provider_time or self.fetched_at
        return max(0, int((now - ref).total_seconds()))


@dataclass
class AlertEvent:
    symbol: str
    alert_id: str
    kind: str            # alert operator name | feed_stale | key_invalid
    label: str
    priority: str
    price: Decimal
    time: datetime       # aware UTC
    source: str
    category: str = ""
    condition: str = ""
    session: str = ""    # PH | RH | AH | ""
    name: str = ""
    message: str = ""
    target_text: str = ""
    change_pct: Optional[Decimal] = None
    data_age_s: int = 0
    note: str = ""
    notification: Optional[CustomNotification] = None


class ValidationError(ValueError):
    """Raised with a message that is safe to show the user."""
