"""Yahoo Finance: the free fallback for equities, indices, commodities and rates.

Moved into the service from McTrader's ``YFinanceClient``. It is not metered, so it earns its
place on capability rather than on cost: it is the only provider here that can serve an index
(``^IXIC``), a commodity future (``GC=F``) or the dollar index (``DX-Y.NYB``), and EODHD
renders several of those through US-listed ETF proxies instead.

WHAT IS WORTH KNOWING ABOUT THIS API

1. **It answers a rate-limit/range problem with HTTP 422, not 404.** A range longer than a
   young listing's history is refused rather than truncated. McTrader records the consequence
   as Lesson 9: a fetch loop that caught exceptions and continued silently dropped every
   recent listing (GEV, RDDT, ALAB) and "looked like a clean run with a slightly smaller
   universe". Hence ``_RANGE_LADDER``: ask wide, then narrower on refusal, and never treat a
   refusal as "this symbol has no history".
2. **A bare request is blocked.** Yahoo serves the chart endpoint only to a browser-looking
   User-Agent, so one is set.
3. **Nulls are normal in the arrays.** A halted session appears as ``null`` in every OHLC
   field of that index position; those positions are skipped rather than read as zero, which
   would put a zero low into a true range.

No API key, no quota, no accounting — which is exactly why it is a fallback and never the
first choice for a US share: it is another tape, and the estate has already been bitten by a
silent tape switch (Mini-McTrader logged ``FALLBACK to Alpaca IEX prices - a different tape``).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import requests

from .base import (
    COMMODITIES, CRYPTO, FOREX, INDICES, RATES, SHARES,
    ProviderBar, ProviderError, ProviderQuote,
)
from .symbols import YAHOO, symbol_for_provider, venue_currency_code

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

#: Browser-looking, because Yahoo refuses the default python-requests agent.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

#: Widest first. A range longer than the listing's history is refused with 422 rather than
#: shortened, so each rejection steps down instead of being read as "no history".
_RANGE_LADDER = ("730d", "2y", "1y", "6mo", "3mo")


class YahooProvider:
    """Quotes and daily bars from Yahoo's chart endpoint."""

    name = YAHOO

    def __init__(self, timeout: float = 20.0,
                 session: Optional[requests.Session] = None) -> None:
        self.timeout = float(timeout)
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self.calls = 0
        self.failures = 0

    # -- routing -----------------------------------------------------------

    def supports(self, asset_class: str) -> bool:
        """Yahoo can serve everything the service distinguishes except crypto pairs it lacks.

        Crypto is included deliberately: Yahoo serves ``BTC-USD`` where Binance may not have
        the exact pair, so it is a genuine second source rather than a duplicate.
        """
        return asset_class in {SHARES, CRYPTO, INDICES, COMMODITIES, RATES, FOREX}

    # -- plumbing ----------------------------------------------------------

    def _chart(self, ticker: str, interval: str, range_: str) -> Dict[str, Any]:
        """One chart request. Raises :class:`ProviderError` on refusal or transport failure."""
        url = CHART_URL.format(ticker=requests.utils.quote(ticker, safe="=^.-"))
        params = {"interval": interval, "range": range_, "includePrePost": "false"}
        self.calls += 1
        try:
            response = self._session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            self.failures += 1
            raise ProviderError("transport", f"{type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            self.failures += 1
            raise ProviderError(
                "range-refused" if response.status_code == 422 else "http-error",
                f"HTTP {response.status_code} for {ticker} range={range_}",
                status=response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            self.failures += 1
            raise ProviderError("unparseable", f"non-JSON body: {exc}") from exc

        chart = payload.get("chart") or {}
        if chart.get("error"):
            self.failures += 1
            raise ProviderError("vendor-error", str(chart.get("error"))[:200])
        results = chart.get("result") or []
        if not results:
            raise ProviderError("no-result", f"no chart result for {ticker}")
        return results[0]

    def _chart_with_ladder(self, ticker: str, interval: str,
                           wanted_sessions: int) -> Dict[str, Any]:
        """Ask wide and step down on refusal, until one range yields enough sessions."""
        last: Optional[ProviderError] = None
        best: Optional[Dict[str, Any]] = None
        for range_ in _RANGE_LADDER:
            try:
                result = self._chart(ticker, interval, range_)
            except ProviderError as exc:
                last = exc
                if exc.reason in {"range-refused", "no-result"}:
                    continue
                raise
            best = result
            stamps = result.get("timestamp") or []
            if len(stamps) >= wanted_sessions:
                return result
        if best is not None:
            # Shorter history than asked for is still a usable answer; the caller decides
            # whether it is enough for an ATR.
            return best
        raise last or ProviderError("no-result", f"no range answered for {ticker}")

    # -- answers -----------------------------------------------------------

    def quote(self, symbol: str, asset_class: str = SHARES,
              venue: Optional[str] = None) -> Optional[ProviderQuote]:
        """The latest price and the previous close.

        THE PREVIOUS CLOSE IS READ FROM THE SERIES, NOT FROM ``chartPreviousClose``

        ``meta.chartPreviousClose`` is the close *before the returned range*, which is not the
        same thing as the prior session. Measured on ``^IXIC`` with ``range=1d`` it came back
        14,258 against a 26,550 price -- an 86% "move" that is simply the wrong reference. Two
        sessions of daily closes give an unambiguous pair: the newest close, and the one
        before it.
        """
        ticker = symbol_for_provider(symbol, asset_class, venue, YAHOO)
        if ticker is None:
            return None

        result = None
        last_error: Optional[ProviderError] = None
        for range_ in ("5d", "1mo"):
            try:
                result = self._chart(ticker, "1d", range_)
                break
            except ProviderError as exc:
                last_error = exc
                continue
        if result is None:
            raise last_error or ProviderError("no-result", f"no quote for {ticker}")

        meta = result.get("meta") or {}
        price = _number(meta.get("regularMarketPrice"))
        if price is None:
            # Fall back to the last non-null close in the series before giving up.
            price = _last_close(result)
        if price is None or price <= 0:
            return None

        previous = _previous_close_from_series(result)
        if previous is None:
            previous = _number(meta.get("previousClose")) or _number(
                meta.get("chartPreviousClose"))
        # Last guard: a reference more than 100% away is not a previous close, it is a data
        # fault or a corporate action. Report no reference rather than a false move.
        if previous is not None and (previous <= 0 or abs(price / previous - 1.0) > 1.0):
            previous = None
        as_of = None
        stamp = _number(meta.get("regularMarketTime"))
        if stamp:
            as_of = datetime.fromtimestamp(stamp, tz=timezone.utc).replace(tzinfo=None)

        return ProviderQuote(
            symbol=str(symbol).strip().upper(),
            price=price,
            provider=self.name,
            previous_close=previous,
            as_of=as_of,
            currency=meta.get("currency") or venue_currency_code(symbol, venue),
            raw_ticker=ticker,
        )

    def bars(self, symbol: str, asset_class: str = SHARES, sessions: int = 60,
             venue: Optional[str] = None) -> List[ProviderBar]:
        """Completed daily bars, oldest first. The session in progress is dropped."""
        ticker = symbol_for_provider(symbol, asset_class, venue, YAHOO)
        if ticker is None:
            return []

        result = self._chart_with_ladder(ticker, "1d", sessions)
        stamps: Sequence[Any] = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quotes = indicators.get("quote") or [{}]
        series = quotes[0] if quotes else {}
        opens = series.get("open") or []
        highs = series.get("high") or []
        lows = series.get("low") or []
        closes = series.get("close") or []
        volumes = series.get("volume") or []

        today = datetime.now(timezone.utc).date()
        out: List[ProviderBar] = []
        for index, stamp in enumerate(stamps):
            if stamp is None or index >= len(closes):
                continue
            open_ = _at(opens, index)
            high = _at(highs, index)
            low = _at(lows, index)
            close = _at(closes, index)
            # A halted session is null in every field; reading it as zero would put a zero
            # low into a true range and inflate the ATR.
            if None in (open_, high, low, close) or close <= 0:
                continue
            session = datetime.fromtimestamp(stamp, tz=timezone.utc).date()
            if session >= today:
                continue  # the session in progress is partial and would shrink a stop
            out.append(ProviderBar(
                session=session, open=open_, high=high, low=low, close=close,
                volume=int(_number(_at(volumes, index)) or 0),
            ))

        out.sort(key=lambda bar: bar.session)
        return out[-sessions:] if sessions > 0 else out

    def stats(self) -> Dict[str, Any]:
        return {"provider": self.name, "calls": self.calls, "failures": self.failures,
                "units": 0}


def _number(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _at(values: Sequence[Any], index: int) -> Optional[float]:
    if index >= len(values):
        return None
    return _number(values[index])


def _previous_close_from_series(result: Dict[str, Any]) -> Optional[float]:
    """The second-newest non-null close, which is the prior session's close.

    Two sessions of daily closes are an unambiguous pair; ``chartPreviousClose`` is the close
    before the *range*, which for ``range=1d`` on an index was measured a year out.
    """
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = [value for value in (quotes.get("close") or []) if _number(value) is not None]
    if len(closes) < 2:
        return None
    return _number(closes[-2])


def _last_close(result: Dict[str, Any]) -> Optional[float]:
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    for value in reversed(quotes.get("close") or []):
        number = _number(value)
        if number is not None and number > 0:
            return number
    return None
