"""What a provider is, and the two things one has to be able to answer.

A provider is not an adapter class hierarchy here on purpose. The service needs exactly two
answers — a latest quote and daily bars — and every provider in it (EODHD, Yahoo, Binance)
can supply both. Narrowing the contract to those two is what keeps a third and fourth
provider cheap to add and keeps the router's fallback logic honest: if a provider cannot
answer, it says so, and the router moves on.

``supported`` is the routing hook. It answers "could you serve this instrument at all", which
is a different question from "did the call work": Binance can never serve an index, so asking
it is a wasted request and a wrong-looking failure. ``None`` from a symbol render already
answers this for symbol-shaped mismatches; ``supported`` covers the rest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

#: The asset classes the service distinguishes. Anything unrecognised is treated as shares,
#: because that is the overwhelming majority of requests and a wrong default here would show
#: up as "no price" rather than as a wrong price.
SHARES = "shares"
CRYPTO = "crypto"
INDICES = "indices"
COMMODITIES = "commodities"
RATES = "rates"
FOREX = "forex"

ASSET_CLASSES = (SHARES, CRYPTO, INDICES, COMMODITIES, RATES, FOREX)

_ALIASES = {
    "indicies": INDICES, "index": INDICES, "equities": SHARES, "equity": SHARES,
    "commodity": COMMODITIES, "futures": COMMODITIES, "future": COMMODITIES,
    "rate": RATES, "bonds": RATES, "fx": FOREX, "currency": FOREX,
    "cryptocurrency": CRYPTO,
}


def normalise_asset_class(value: Optional[object]) -> str:
    """The canonical asset class for *value*; unknown values fall back to ``shares``."""
    key = str(getattr(value, "value", value) or "").strip().lower()
    if not key:
        return SHARES
    return _ALIASES.get(key, key if key in ASSET_CLASSES else SHARES)


@dataclass
class ProviderQuote:
    """One instrument's latest price, as any provider can express it.

    ``previous_close`` is carried because the morning scan's whole question is the move since
    the prior close, and a price without its reference cannot answer it. ``as_of`` is the
    provider's own observation time, not the fetch time; providers that cannot supply one
    leave it ``None`` rather than substituting the clock, which would make a stale price look
    current.
    """

    symbol: str
    price: float
    provider: str
    previous_close: Optional[float] = None
    as_of: Optional[datetime] = None
    currency: Optional[str] = None
    raw_ticker: Optional[str] = None

    @property
    def change_pct(self) -> Optional[float]:
        """The move since the previous close, computed the same way for every provider."""
        if not self.previous_close or self.previous_close <= 0:
            return None
        return (self.price / self.previous_close - 1.0) * 100.0


@dataclass
class ProviderBar:
    """One completed daily session, as any provider can express it.

    ``adjusted`` says whether the value is split-adjusted. It travels with the bar rather than
    being inferred at the call site because this database already carries both conventions and
    documents the trap: ``BarReal5Min`` is unadjusted while ``BarHourly`` is, and a split shows
    as a jump (JDZG, 2026-03-23: $0.0928 against $20.925 the same day). An ATR mixing the two
    is silently wrong.
    """

    session: date
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    adjusted: bool = True


class ProviderError(RuntimeError):
    """A provider could not answer, with a reason the router can branch on."""

    def __init__(self, reason: str, detail: str = "", status: Optional[int] = None) -> None:
        self.reason = reason
        self.detail = detail
        self.status = status
        super().__init__(f"{reason}: {detail}" if detail else reason)


@runtime_checkable
class Provider(Protocol):
    """A market-data source. Two methods, plus a name for provenance."""

    #: The value that reaches ``provider`` in every response. Must be stable: callers use it
    #: to decide whether two numbers came from the same tape.
    name: str

    def supports(self, asset_class: str) -> bool:
        """Whether this provider can serve *asset_class* at all."""
        ...

    def quote(self, symbol: str, asset_class: str, venue: Optional[str] = None
              ) -> Optional[ProviderQuote]:
        """The latest price, or ``None`` when the instrument is not priceable here."""
        ...

    def bars(self, symbol: str, asset_class: str, sessions: int,
             venue: Optional[str] = None) -> List[ProviderBar]:
        """Completed daily bars, oldest first. Empty when the provider cannot answer."""
        ...
