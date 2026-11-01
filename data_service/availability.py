"""Is today's data in yet, for a country?

The question a caller actually asks before it trades: "has this venue's data been
collected since the session opened?" -- not "is the database reachable" and not "is there
a row". A row stamped before the open is yesterday's, however recently it was written.

    available = latest observed price for the venue  >=  the venue's most recent open

Consequences worth stating, because each is a case that would otherwise look like a bug:

* Before the open, today's open is in the FUTURE, so yesterday's data is not "after the
  session open" and the answer is false. That is what a screener wants at 09:00 New York.
* On a Saturday the clock resolves the venue to Monday's session, so the answer is false
  all weekend -- there is no "today" to have data for.
* A price fetched at 02:00 is not counted: it is stamped before the open, so it cannot
  make the answer true. This is the same rule the collection guard enforces, read back.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

UTC = timezone.utc

#: Country code -> (venue code in SymbolPriceCache, exchange code in the clock reference).
COUNTRIES: Dict[str, Tuple[str, str]] = {
    "US": ("US", "NYSE"),
    "AU": ("AU", "ASX"),
}


@dataclass(frozen=True)
class Availability:
    """The answer, with the evidence that produced it."""

    available: bool
    country: str
    venue: str
    exchange: str
    latest_utc: Optional[datetime]
    session_open_utc: Optional[datetime]
    session_close_utc: Optional[datetime]
    session_is_open: bool

    def as_dict(self) -> Dict[str, object]:
        def iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat() if value else None

        return {
            "available": self.available,
            "country": self.country,
            "venue": self.venue,
            "exchange": self.exchange,
            "latest_price_utc": iso(self.latest_utc),
            "session_open_utc": iso(self.session_open_utc),
            "session_close_utc": iso(self.session_close_utc),
            "session_is_open": self.session_is_open,
        }


def country_key(country_code: str) -> str:
    """'us', 'US', ' au ' -> 'US'. Raises KeyError for anything else."""
    key = str(country_code or "").strip().upper()
    if key not in COUNTRIES:
        raise KeyError(f"country code must be one of {sorted(COUNTRIES)}, got {country_code!r}")
    return key


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """A stored stamp as UTC.

    The columns are ``datetime2`` and the service writes naive UTC into them
    (``_utc_naive``), so a naive value here is UTC that lost its label -- NOT local time.
    Calling ``astimezone`` on it would read it as the host's zone and shift every answer by
    the UTC offset: measured on this machine, Sydney's +11 turned a 19:31Z observation into
    08:31Z and would have made a correct "true" look like "false" for eleven hours.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def check(instance, latest_utc: Optional[datetime], country_code: str,
          ref: Optional[datetime] = None) -> Availability:
    """Decide the answer. Pure: the clock and the latest stamp come in as arguments."""
    country = country_key(country_code)
    venue, exchange_code = COUNTRIES[country]
    exchange = next((row for row in instance.exchanges.values()
                     if row["code"] == exchange_code), None)
    if exchange is None:
        raise KeyError(f"{exchange_code} is not in the clock reference")

    session = instance.exchange_session(exchange, ref or datetime.now(UTC))
    latest = _as_utc(latest_utc)

    available = bool(
        latest is not None
        and session.open_utc is not None
        and latest >= session.open_utc
    )
    return Availability(
        available=available,
        country=country,
        venue=venue,
        exchange=exchange_code,
        latest_utc=latest,
        session_open_utc=session.open_utc,
        session_close_utc=session.close_utc,
        session_is_open=session.is_open,
    )
