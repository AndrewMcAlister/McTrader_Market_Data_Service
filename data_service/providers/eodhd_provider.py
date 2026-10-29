"""EODHD as a provider: the only metered one, and the only one with a whole-market feed.

Wraps :class:`~data_service.eodhd.EodhdClient` rather than replacing it, so the four
measured vendor calls and their tariff handling stay in one place.

Two capabilities exist here and nowhere else in the service:

* **the whole-market snapshot** (``/real-time/{ph}?ex=US``, 100 units flat) — one call that
  prices ~12,700 US names, which is what makes a 2,757-name scan cost 100 units instead of
  2,757;
* **the whole-market daily bulk** (``/eod-bulk-last-day/US``, 100 units flat) — the daily job's
  single load.

Neither has a Yahoo or Binance equivalent, so the router exposes them directly instead of
pretending they are generic provider operations.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from ..eodhd import EodhdClient, EodhdError, bare_symbol, normalise_venue
from ..sessions import latest_completed_session
from .base import (
    COMMODITIES, CRYPTO, FOREX, INDICES, RATES, SHARES,
    ProviderBar, ProviderError, ProviderQuote,
)
from .symbols import EODHD, symbol_for_provider, venue_currency_code

log = logging.getLogger(__name__)


def _number(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _epoch(value: Any) -> Optional[datetime]:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


class EodhdProvider:
    """Quotes and daily bars from EODHD, plus its two whole-market calls."""

    name = EODHD

    def __init__(self, client: EodhdClient) -> None:
        self.client = client

    # -- routing -----------------------------------------------------------

    def supports(self, asset_class: str) -> bool:
        """Everything the service distinguishes. EODHD is the broadest source here.

        Crypto is included because EODHD serves ``BTC.CC``; the router puts Binance first for
        crypto anyway, so this only matters as a fallback when Binance lacks the pair.
        """
        return asset_class in {SHARES, CRYPTO, INDICES, COMMODITIES, RATES, FOREX}

    # -- answers -----------------------------------------------------------

    def quote(self, symbol: str, asset_class: str = SHARES,
              venue: Optional[str] = None) -> Optional[ProviderQuote]:
        """One instrument's latest price. Billed 1 unit."""
        market = normalise_venue(venue)
        ticker = symbol_for_provider(symbol, asset_class, venue, EODHD)
        if ticker is None:
            return None
        try:
            payload = self.client.real_time(ticker, market)
        except EodhdError as exc:
            raise ProviderError(exc.reason, exc.detail, exc.status) from exc

        price = _number(payload.get("close")) or _number(payload.get("price"))
        if price is None or price <= 0:
            return None
        return ProviderQuote(
            symbol=str(symbol).strip().upper(),
            price=price,
            provider=self.name,
            previous_close=_number(payload.get("previousClose"))
                            or _number(payload.get("previous_close")),
            as_of=_epoch(payload.get("timestamp")),
            currency=payload.get("currency") or venue_currency_code(symbol, venue),
            raw_ticker=ticker,
        )

    def bars(self, symbol: str, asset_class: str = SHARES, sessions: int = 60,
             venue: Optional[str] = None) -> List[ProviderBar]:
        """Completed daily bars, oldest first. Billed 1 unit whatever the window.

        Prefers ``adjusted_close``: an unadjusted close across a split reads as a
        catastrophic loss, and this database already documents that trap (JDZG, 2026-03-23).
        """
        market = normalise_venue(venue)
        ticker = symbol_for_provider(symbol, asset_class, venue, EODHD)
        if ticker is None:
            return []

        # `sessions` is a session count, but /eod/ takes a calendar window and bills the same
        # for any width, so ask wide enough to survive weekends and holidays.
        days = max(60, int(sessions) * 2)
        try:
            payload = self.client.eod(ticker, market, days=days)
        except EodhdError as exc:
            raise ProviderError(exc.reason, exc.detail, exc.status) from exc

        latest = latest_completed_session(market)
        out: List[ProviderBar] = []
        for raw in payload:
            text = str(raw.get("date") or "").strip()
            if not text:
                continue
            try:
                session = date.fromisoformat(text)
            except ValueError:
                continue
            if session > latest:
                continue  # in-progress session: a partial range would shrink a stop
            adjusted = _number(raw.get("adjusted_close"))
            close = adjusted if adjusted is not None else _number(raw.get("close"))
            open_ = _number(raw.get("open"))
            high = _number(raw.get("high"))
            low = _number(raw.get("low"))
            if None in (close, open_, high, low) or close <= 0:
                continue
            out.append(ProviderBar(
                session=session, open=open_, high=high, low=low, close=close,
                volume=int(_number(raw.get("volume")) or 0),
                # False only when the vendor gave no adjusted close, so a caller can tell a
                # raw series from an adjusted one instead of assuming.
                adjusted=adjusted is not None,
            ))

        out.sort(key=lambda bar: bar.session)
        return out[-sessions:] if sessions > 0 else out

    # -- capabilities that exist only here ---------------------------------

    def whole_market_quotes(self, venue: str = "US") -> List[ProviderQuote]:
        """Every US listing's latest price in one call. Billed 100 units flat.

        Rows carrying no usable price are skipped rather than returned as zeros; the
        router's caller applies the freshness rule, because "too old" depends on whether the
        question is a live entry or a daily pass.
        """
        market = normalise_venue(venue)
        try:
            rows = self.client.real_time_whole_market(market)
        except EodhdError as exc:
            raise ProviderError(exc.reason, exc.detail, exc.status) from exc

        out: List[ProviderQuote] = []
        for row in rows:
            code = bare_symbol(str(row.get("code") or ""))
            if not code:
                continue
            price = _number(row.get("close"))
            if price is None or price <= 0:
                continue
            out.append(ProviderQuote(
                symbol=code,
                price=price,
                provider=self.name,
                previous_close=_number(row.get("previousClose")),
                as_of=_epoch(row.get("timestamp")),
                currency="USD" if market == "US" else None,
                raw_ticker=f"{code}.{market}",
            ))
        return out

    def stats(self) -> Dict[str, Any]:
        payload = self.client.stats()
        payload["provider"] = self.name
        return payload
