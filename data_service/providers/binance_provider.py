"""Binance: crypto, from the free public spot API.

Moved into the service from McTrader's ``BinanceMarketDataAdapter``. Unmetered, so like Yahoo
it earns its place on capability: EODHD serves crypto as ``BTC.CC`` and renders it less
reliably than a venue that trades it natively.

TWO THINGS THAT WILL BITE

1. **The pair suffix.** McTrader renders ``BTC-USD`` as ``BTCUSD`` (strip ``-`` and ``/``),
   but Binance spot quotes most pairs against **USDT**, not USD, so ``BTCUSD`` is not
   universally listed. ``_pair_candidates`` tries the rendered pair first and then appends
   ``T``, which is the difference between "crypto works" and "crypto returns nothing" for the
   common case. The successful pair is reported in ``raw_ticker`` so a caller can see which
   one answered.
2. **A rejected pair is a 400, not an empty body.** Binance answers
   ``{"code":-1121,"msg":"Invalid symbol."}`` with HTTP 400. That is a *routing* answer, not a
   transport failure, so it is reported as ``unknown-pair`` and the router falls through
   rather than treating crypto as an outage.

Daily candles are UTC-day aligned and the current one is in progress, so the newest candle is
dropped for the same reason the daily bars drop today: a partial candle has a partial range.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import requests

from .base import CRYPTO, ProviderBar, ProviderError, ProviderQuote
from .symbols import BINANCE, symbol_for_provider

log = logging.getLogger(__name__)

BASE_URL = "https://api.binance.com"
TICKER_24H = "/api/v3/ticker/24hr"
KLINES = "/api/v3/klines"

#: Binance serves ~1,000 candles per call; this is far more than any ATR needs.
MAX_CANDLES = 500


class BinanceProvider:
    """Quotes and daily bars for crypto pairs."""

    name = BINANCE

    def __init__(self, base_url: str = BASE_URL, timeout: float = 20.0,
                 session: Optional[requests.Session] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._session = session or requests.Session()
        self.calls = 0
        self.failures = 0

    # -- routing -----------------------------------------------------------

    def supports(self, asset_class: str) -> bool:
        """Crypto only. Asking it for an index is a wasted request and a misleading failure."""
        return asset_class == CRYPTO

    # -- plumbing ----------------------------------------------------------

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        self.calls += 1
        try:
            response = self._session.get(
                self.base_url + path, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            self.failures += 1
            raise ProviderError("transport", f"{type(exc).__name__}: {exc}") from exc

        if response.status_code == 400:
            # An unlisted pair. A routing answer, not an outage.
            self.failures += 1
            raise ProviderError("unknown-pair", response.text[:200], status=400)
        if response.status_code != 200:
            self.failures += 1
            raise ProviderError(
                "http-error", f"HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            self.failures += 1
            raise ProviderError("unparseable", f"non-JSON body: {exc}") from exc

    @staticmethod
    def _pair_candidates(symbol: str) -> List[str]:
        """The rendered pair, then the USDT variant that spot most often lists.

        ``BTCUSD`` is a real pair on Binance for some assets but not most; ``BTCUSDT`` is the
        liquid one. Trying the literal rendering first keeps a caller who names a real pair
        getting exactly that pair.
        """
        rendered = symbol_for_provider(symbol, CRYPTO, None, BINANCE)
        if not rendered:
            return []
        candidates = [rendered]
        if rendered.endswith("USD"):
            candidates.append(rendered + "T")
        elif rendered.endswith("USDT"):
            candidates.append(rendered[:-1])  # let a USD-only pair also be tried
        return candidates

    def _first_answering_pair(self, symbol: str, path: str, params: Dict[str, Any]
                              ) -> tuple[str, Any]:
        """Try each candidate pair; return the one that answered, with its payload."""
        candidates = self._pair_candidates(symbol)
        if not candidates:
            raise ProviderError("unrenderable", f"{symbol!r} is not a crypto pair")
        last: Optional[ProviderError] = None
        for pair in candidates:
            try:
                payload = self._get(path, {**params, "symbol": pair})
                return pair, payload
            except ProviderError as exc:
                if exc.reason == "unknown-pair":
                    last = exc
                    continue
                raise
        raise last or ProviderError("unknown-pair", f"no pair for {symbol!r}")

    # -- answers -----------------------------------------------------------

    def quote(self, symbol: str, asset_class: str = CRYPTO,
              venue: Optional[str] = None) -> Optional[ProviderQuote]:
        pair, payload = self._first_answering_pair(symbol, TICKER_24H, {})
        if not isinstance(payload, dict):
            return None
        price = _number(payload.get("lastPrice"))
        if price is None or price <= 0:
            return None
        previous = _number(payload.get("prevClosePrice"))
        as_of = None
        stamp = _number(payload.get("closeTime"))
        if stamp:
            as_of = datetime.fromtimestamp(stamp / 1000.0, tz=timezone.utc).replace(tzinfo=None)
        return ProviderQuote(
            symbol=str(symbol).strip().upper(),
            price=price,
            provider=self.name,
            previous_close=previous,
            as_of=as_of,
            currency="USDT" if pair.endswith("USDT") else None,
            raw_ticker=pair,
        )

    def bars(self, symbol: str, asset_class: str = CRYPTO, sessions: int = 60,
             venue: Optional[str] = None) -> List[ProviderBar]:
        limit = min(MAX_CANDLES, max(1, int(sessions) + 2))
        pair, payload = self._first_answering_pair(
            symbol, KLINES, {"interval": "1d", "limit": str(limit)})
        if not isinstance(payload, list):
            return []

        today = datetime.now(timezone.utc).date()
        out: List[ProviderBar] = []
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            open_ms = _number(row[0])
            open_ = _number(row[1])
            high = _number(row[2])
            low = _number(row[3])
            close = _number(row[4])
            volume = _number(row[5])
            if None in (open_ms, open_, high, low, close) or close <= 0:
                continue
            session = datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc).date()
            if session >= today:
                continue  # in-progress candle: a partial range would shrink a stop
            out.append(ProviderBar(
                session=session, open=open_, high=high, low=low, close=close,
                volume=int(volume or 0),
            ))

        out.sort(key=lambda bar: bar.session)
        if out:
            log.debug("binance %s -> %s: %d bars", symbol, pair, len(out))
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
