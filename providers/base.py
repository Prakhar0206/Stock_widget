"""Provider interface. Finnhub (Step B) and the mock both implement it. Alpaca: interface only."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class ProviderError(Exception):
    """Base class; the message must never contain the API key."""


class RateLimited(ProviderError):
    """HTTP 429 or 5xx: retry with backoff."""


class NetworkFailure(ProviderError):
    """No connection / timeout."""


class MalformedResponse(ProviderError):
    """Unexpected body: discard, log, count as an error."""


class QuoteProvider(ABC):
    name = "provider"

    @abstractmethod
    def fetch_quotes(self, symbols: list) -> list:
        """Return a list of PriceTick. A price of 0 means unknown symbol / no data."""

    def fetch_market_status(self) -> Optional[dict]:
        """Optional: {"isOpen": bool, "holiday": str|None}. None means 'use the bundled calendar'."""
        return None
