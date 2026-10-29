"""The EODHD client: four vendor calls, and no blind retries.

Everything the service needs from the vendor is one of four requests:

| Purpose                    | Request                                          | Billed      |
|----------------------------|--------------------------------------------------|-------------|
| one symbol's latest price  | ``/real-time/{SYM}.{EX}``                        | 1/symbol    |
| the whole US market        | ``/real-time/{placeholder}.US?ex=US``            | 100 flat    |
| one symbol's daily history | ``/eod/{SYM}.{EX}?from=...&period=d``            | 1 flat      |
| one session, whole market  | ``/eod-bulk-last-day/{MARKET}?date=YYYY-MM-DD``  | 100 flat    |

Two facts about the tariff drive the code:

1. **A rejected request is still billed** (measured: a 404 from ``/eod/`` cost 1 unit, a 404
   from ``/intraday/`` cost 5). So a 4xx is never retried -- retrying a malformed request
   pays again for the same mistake. Only a transport failure (connection, timeout) is
   retried, once.
2. **``/eod/`` is flat regardless of window**, so the history call asks for the whole window
   it needs in one request rather than paging.

There is deliberately no caching in this module. The cache lives in products.py, which is
the layer that knows about TTLs; a cache here would be a second lifetime for the same data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .usage import PURPOSE_REQUEST_TYPE
from typing import Any, Dict, List, Optional

import requests

#: EODHD's venue codes, plus the aliases this estate actually types. Taken at run time from
#: what McTrader's MCP surface advertises: "US, AU/ASX, LSE/UK, TO, HK, F/DE, PA".
_VENUE_ALIASES: Dict[str, str] = {
    "US": "US", "NYSE": "US", "NASDAQ": "US", "AMEX": "US", "NYSEARCA": "US",
    "AU": "AU", "ASX": "AU", "AX": "AU", "AUS": "AU",
    "L": "L", "LSE": "L", "UK": "L", "GB": "L", "LON": "L",
    "TO": "TO", "TSX": "TO", "CA": "TO",
    "V": "V", "TSXV": "V",
    "HK": "HK", "HKG": "HK",
    "F": "F", "DE": "F", "XETRA": "F", "FRANKFURT": "F", "GER": "F",
    "PA": "PA", "PARIS": "PA", "FR": "PA",
    "AS": "AS", "AMS": "AS",
    "SW": "SW", "SWX": "SW",
    "MI": "MI", "MIL": "MI",
    "MC": "MC", "MAD": "MC",
    "ST": "ST", "STO": "ST",
    "OL": "OL", "OSL": "OL",
    "CO": "CO", "CPH": "CO",
    "HE": "HE", "HEL": "HE",
    "NZ": "NZ", "NZX": "NZ",
    "SI": "SI", "SGX": "SI",
    "TSE": "TSE", "JP": "TSE",
    "KO": "KO", "KRX": "KO",
    "NSE": "NSE", "BSE": "BSE",
    "JSE": "JSE",
    "SA": "SA", "BOVESPA": "SA",
    "MX": "MX", "BMV": "MX",
}

#: The only venue the whole-market real-time feed was measured for. A request for BHP.AU
#: returns nothing from it, so other venues are refused rather than priced off a US tape.
BULK_REAL_TIME_VENUE = "US"

#: The path ticker for the ``ex=`` form. EODHD ignores it once ``ex`` is set; it is present
#: because the path is not optional. Fixed, so no caller's symbol can end up in it.
BULK_PLACEHOLDER = "AAPL"


class EodhdError(RuntimeError):
    """A vendor call that did not produce usable JSON, with a reason to branch on."""

    def __init__(self, reason: str, detail: str = "", status: Optional[int] = None) -> None:
        self.reason = reason
        self.detail = detail
        self.status = status
        super().__init__(f"{reason}: {detail}" if detail else reason)


def normalise_venue(text: Optional[str]) -> str:
    """The EODHD venue code for a venue name, or raise.

    Refusing an unknown venue rather than defaulting to US is deliberate: silently pricing
    an Australian listing from the US feed produces a wrong number that looks right, which
    is worse than an error.
    """
    key = str(text or "").strip().upper()
    if not key:
        return "US"
    resolved = _VENUE_ALIASES.get(key)
    if resolved is None:
        raise EodhdError(
            "unknown-venue",
            f"{text!r} is not a known venue; pass one of US, AU, L, TO, HK, F, PA, ...")
    return resolved


def render_symbol(symbol: str, venue: str) -> str:
    """``("AAPL", "US") -> "AAPL.US"``; an already-suffixed symbol is left alone.

    Only a suffix that matches a known venue is treated as one, so a genuinely dotted
    ticker (``BRK.B``) is not mistaken for a venue-qualified code.
    """
    text = str(symbol or "").strip().upper()
    if not text:
        raise EodhdError("empty-symbol", "a blank symbol is not a symbol")
    if "." in text:
        root, _, tail = text.rpartition(".")
        if tail in _VENUE_ALIASES and root:
            return f"{root}.{_VENUE_ALIASES[tail]}"
    return f"{text}.{normalise_venue(venue)}"


def bare_symbol(code: str) -> str:
    """``"AAPL.US" -> "AAPL"``. Used to key the bulk feed's rows."""
    text = str(code or "").strip().upper()
    return text.split(".", 1)[0] if text else ""


@dataclass
class EodhdClient:
    """The four vendor calls. Counts what it spent so /stats can report it."""

    api_key: str
    base_url: str = "https://eodhd.com/api"
    timeout_seconds: float = 30.0
    bulk_timeout_seconds: float = 60.0
    session: Optional[requests.Session] = None
    #: Calls made, by purpose, since start. Not the vendor's own counter -- /user is that
    #: -- but enough to see whether the cache is working.
    calls: Dict[str, int] = field(default_factory=dict)
    units: int = 0
    #: Called as ``recorder(request_type, symbols)`` and returns the units it cost. Attached by the
    #: product layer, which loads the tariff from dbo.RequestTypes. ``None`` means the hardcoded unit
    #: counts below are the only accounting -- which is what a unit test without a database gets.
    recorder: Optional[Any] = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise EodhdError(
                "no-api-key",
                "no EODHD key: set EODHD_API_KEY or store it in the keyring at "
                "market_data:eodhd:global / eodhd:api_key")
        if self.session is None:
            self.session = requests.Session()

    # -- plumbing ----------------------------------------------------------

    def _count(self, purpose: str, units: int) -> None:
        self.calls[purpose] = self.calls.get(purpose, 0) + 1
        self.units += units

    def _spent(self, purpose: str, units: int, symbols: int = 0) -> None:
        """Count one billable call, priced from the tariff when a recorder is attached.

        The tariff wins over the literal ``units`` because dbo.RequestTypes carries the measurement
        and its evidence: a re-measured price should be a row update, not a code change.
        """
        cost = units
        request_type = PURPOSE_REQUEST_TYPE.get(purpose)
        if self.recorder is not None and request_type:
            try:
                priced = self.recorder(request_type, symbols)
            except Exception:  # noqa: BLE001 - accounting must not fail the call
                priced = None
            if priced is not None:
                cost = int(priced)
        self._count(purpose, cost)

    def _get(self, path: str, params: Dict[str, Any], *, purpose: str,
             units: int, symbols: int = 0, timeout: Optional[float] = None) -> Any:
        """One GET, at most one retry, and never on a 4xx.

        A 4xx is billed, so retrying it pays twice for the same rejected question. Only a
        transport-level failure is retried, and only once, because the second attempt is
        free in the case that matters (the request never landed).
        """
        query = dict(params)
        query["api_token"] = self.api_key
        query.setdefault("fmt", "json")
        url = f"{self.base_url}/{path}"
        leash = timeout if timeout is not None else self.timeout_seconds

        last: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                response = self.session.get(url, params=query, timeout=leash)
            except requests.RequestException as exc:
                last = exc
                if attempt == 1:
                    time.sleep(0.5)
                    continue
                raise EodhdError("transport", f"{type(exc).__name__}: {exc}") from exc

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise EodhdError(
                        "unparseable", f"200 with a non-JSON body ({exc})",
                        status=200) from exc
                self._spent(purpose, units, symbols)
                return payload

            # 4xx/5xx: billed, so no retry -- and billed, so it is recorded before raising.
            # Measured: a 404 from /eod/ cost 1 and a 404 from /intraday/ cost 5. A transport
            # failure below is the one case that costs nothing, because the request never landed.
            self._spent(purpose, units, symbols)
            raise EodhdError(
                "http-error",
                f"HTTP {response.status_code} for /{path}: {response.text[:200]}",
                status=response.status_code)

        raise EodhdError("transport", str(last) if last else "unknown transport failure")

    # -- the four calls ----------------------------------------------------

    def real_time(self, symbol: str, venue: str = "US") -> Dict[str, Any]:
        """One symbol's latest price. Billed 1."""
        ticker = render_symbol(symbol, venue)
        payload = self._get(f"real-time/{ticker}", {}, purpose="real_time", units=1, symbols=1)
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            raise EodhdError("unparseable", f"expected an object, got {type(payload).__name__}")
        return payload

    def real_time_whole_market(self, venue: str = BULK_REAL_TIME_VENUE) -> List[Dict[str, Any]]:
        """Every US listing's latest price in one request. Billed 100 flat.

        Returns the raw rows unfiltered: the feed carries inactive and delisted symbols
        holding the last price they ever traded at, and deciding what is too old is a policy
        call that belongs to the caller, where the freshness rule is known.
        """
        market = normalise_venue(venue)
        if market != BULK_REAL_TIME_VENUE:
            raise EodhdError(
                "non-us-venue",
                f"the whole-market real-time feed was measured for {BULK_REAL_TIME_VENUE} "
                f"only; {venue!r} is refused rather than priced off a US tape")
        payload = self._get(
            f"real-time/{BULK_PLACEHOLDER}.{BULK_REAL_TIME_VENUE}",
            {"ex": BULK_REAL_TIME_VENUE},
            purpose="real_time_bulk",
            units=100,
            symbols=0,
            timeout=self.bulk_timeout_seconds,
        )
        if not isinstance(payload, list):
            raise EodhdError("unparseable", f"expected a list, got {type(payload).__name__}")
        return payload

    def eod(self, symbol: str, venue: str = "US", days: int = 60,
            to_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """One symbol's daily bars, oldest first. Billed 1 whatever the window.

        Asks for a window generous enough to supply ``period + 1`` sessions after weekends
        and holidays, because a wider window costs exactly the same as a narrow one.
        """
        from datetime import date, timedelta

        ticker = render_symbol(symbol, venue)
        end = to_date or date.today().isoformat()
        start = (date.fromisoformat(end) - timedelta(days=max(1, days))).isoformat()
        payload = self._get(
            f"eod/{ticker}",
            {"from": start, "to": end, "period": "d", "order": "a"},
            purpose="eod",
            units=1,
            symbols=1,
        )
        if not isinstance(payload, list):
            raise EodhdError("unparseable", f"expected a list, got {type(payload).__name__}")
        return payload

    def bulk_last_day(self, market: str = "US",
                      session_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every listing's daily bar for one session. Billed 100 flat.

        With ``session_date`` omitted this is the latest session. With it set, the response
        carries listings that have since stopped trading -- ~50,270 codes against ~27,714
        for the current session -- so a dated pull must be intersected with the current
        session's codes rather than read alone.
        """
        params: Dict[str, Any] = {}
        if session_date:
            params["date"] = session_date
        payload = self._get(
            f"eod-bulk-last-day/{normalise_venue(market)}",
            params,
            purpose="eod_bulk",
            units=100,
            symbols=0,
            timeout=self.bulk_timeout_seconds,
        )
        if not isinstance(payload, list):
            raise EodhdError("unparseable", f"expected a list, got {type(payload).__name__}")
        return payload

    # -- housekeeping ------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        return {"calls_by_purpose": dict(self.calls), "units": self.units}
