"""Which provider answers, and the fallback order between them.

THE ORDER IS PER ASSET CLASS, NOT ONE GLOBAL LIST

Routing by asset class is the whole point of having three providers: a US share belongs on
EODHD (metered, but adjusted and consistent), an index belongs on Yahoo where ``^IXIC`` is a
first-class ticker, and a crypto pair belongs on Binance where the pair is traded natively
rather than rendered as ``BTC.CC``.

| asset class                          | order                                     |
|--------------------------------------|-------------------------------------------|
| shares                               | EODHD → Yahoo                             |
| indices / commodities / rates / forex | EODHD → Yahoo                            |
| crypto                               | Binance → EODHD → Yahoo                   |

FAILOVER CHANGES THE TAPE, SO IT IS ALWAYS DECLARED

Every quote and bar carries the ``provider`` that answered. That is not decoration: the
estate has already been bitten by a silent tape switch — Mini-McTrader logged ``FALLBACK to
Alpaca IEX prices - a different tape`` with nothing flagging it, and a stop computed on one
tape while a level was chosen on another is a silent error. A caller may combine providers,
but it must be able to see that it did.

A provider that cannot serve an asset class is not asked at all. Binance has no indices, so
asking it and reading the failure as "no data" would be both a wasted request and a
misleading one.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .base import (
    COMMODITIES, CRYPTO, FOREX, INDICES, RATES, SHARES,
    Provider, ProviderBar, ProviderError, ProviderQuote, normalise_asset_class,
)

log = logging.getLogger(__name__)

#: Provider names, used to build the order tables without importing the concrete classes
#: (the router must stay usable with fakes in tests).
EODHD = "eodhd"
YAHOO = "yahoo"
BINANCE = "binance"

#: Who answers first, by asset class. Order is a deliberate policy, not a preference:
#: EODHD leads for everything it is *good* at, and the free providers lead where they are
#: strictly better than it.
_ORDER: Dict[str, Tuple[str, ...]] = {
    SHARES: (EODHD, YAHOO),
    INDICES: (EODHD, YAHOO),
    COMMODITIES: (EODHD, YAHOO),
    RATES: (EODHD, YAHOO),
    FOREX: (EODHD, YAHOO),
    CRYPTO: (BINANCE, EODHD, YAHOO),
}


def order_for(asset_class: Optional[object]) -> Tuple[str, ...]:
    """The provider order for *asset_class*."""
    return _ORDER.get(normalise_asset_class(asset_class), _ORDER[SHARES])


class ProviderRouter:
    """Dispatches a quote or a bar request to the first provider that can answer."""

    def __init__(self, providers: Sequence[Provider],
                 names: Optional[Dict[str, str]] = None) -> None:
        """*providers* is an ordered sequence; *names* maps provider name to its owner.

        ``names`` exists so a test can inject fakes without the router having to know the
        concrete classes. With no mapping, a provider is keyed by its own ``name``.
        """
        self._by_name: Dict[str, Provider] = {}
        for provider in providers:
            key = (names or {}).get(provider.name, provider.name)
            self._by_name[key] = provider
        self.attempts: List[Dict[str, Any]] = []
        self.fallbacks = 0

    def provider(self, name: str) -> Optional[Provider]:
        return self._by_name.get(name)

    def _candidates(self, asset_class: str) -> List[Tuple[str, Provider]]:
        """The providers to try, in order, skipping those that cannot serve the class."""
        out: List[Tuple[str, Provider]] = []
        for key in order_for(asset_class):
            provider = self._by_name.get(key)
            if provider is None:
                continue
            try:
                if not provider.supports(asset_class):
                    continue
            except Exception:  # noqa: BLE001 - a broken supports() must not stop routing
                continue
            out.append((key, provider))
        return out

    # ------------------------------------------------------------------
    # quote
    # ------------------------------------------------------------------

    def quote(self, symbol: str, asset_class: Optional[object] = None,
              venue: Optional[str] = None) -> Optional[ProviderQuote]:
        """The first provider's quote, or ``None`` when nobody could price it.

        Every failure is recorded rather than raised: a symbol Yahoo cannot serve is not an
        error, it is a reason to ask the next one. Only when every candidate has declined does
        the answer become ``None``, which the caller reports as ``missing``.
        """
        kind = normalise_asset_class(asset_class)
        candidates = self._candidates(kind)
        if not candidates:
            return None

        for index, (key, provider) in enumerate(candidates):
            try:
                result = provider.quote(symbol, kind, venue)
            except ProviderError as exc:
                self._record(symbol, kind, key, f"{exc.reason}: {exc.detail}")
                continue
            except Exception as exc:  # noqa: BLE001 - one provider must not fail a request
                self._record(symbol, kind, key, f"{type(exc).__name__}: {exc}")
                continue
            if result is None:
                self._record(symbol, kind, key, "declined")
                continue
            if index > 0:
                self.fallbacks += 1
                log.info("quote %s served by fallback %s after %s", symbol, key,
                         ", ".join(c[0] for c in candidates[:index]))
            self._record(symbol, kind, key, "ok")
            return result
        return None

    # ------------------------------------------------------------------
    # bars
    # ------------------------------------------------------------------

    def bars(self, symbol: str, asset_class: Optional[object] = None, sessions: int = 60,
             venue: Optional[str] = None) -> Tuple[List[ProviderBar], Optional[str]]:
        """``(bars, provider_name)`` from the first provider that returns any.

        Unlike a quote, an *empty* list is treated as a refusal worth falling through: a
        provider that answers with no bars has not answered the question, and a share with a
        Yahoo history but no EODHD one is exactly the case this exists for.
        """
        kind = normalise_asset_class(asset_class)
        for index, (key, provider) in enumerate(self._candidates(kind)):
            try:
                result = provider.bars(symbol, kind, sessions, venue)
            except ProviderError as exc:
                self._record(symbol, kind, key, f"bars {exc.reason}: {exc.detail}")
                continue
            except Exception as exc:  # noqa: BLE001
                self._record(symbol, kind, key, f"bars {type(exc).__name__}: {exc}")
                continue
            if not result:
                self._record(symbol, kind, key, "bars declined")
                continue
            if index > 0:
                self.fallbacks += 1
                log.info("bars %s served by fallback %s", symbol, key)
            self._record(symbol, kind, key, f"bars ok ({len(result)})")
            return list(result), key
        return [], None

    # ------------------------------------------------------------------
    # whole market (EODHD only)
    # ------------------------------------------------------------------

    def whole_market_quotes(self, venue: str = "US") -> List[ProviderQuote]:
        """Every US listing's latest price from the one provider that has such a feed.

        Not a generic operation: no Yahoo or Binance equivalent exists, so this asks EODHD
        directly rather than pretending the capability is portable.
        """
        provider = self._by_name.get(EODHD)
        if provider is None or not hasattr(provider, "whole_market_quotes"):
            raise ProviderError(
                "no-bulk-provider",
                "the whole-market feed is an EODHD capability and EODHD is not configured")
        return provider.whole_market_quotes(venue)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------

    def _record(self, symbol: str, asset_class: str, provider: str, outcome: str) -> None:
        self.attempts.append({
            "symbol": symbol, "asset_class": asset_class,
            "provider": provider, "outcome": outcome,
        })
        # Keep the log bounded: this is diagnostics, not an audit trail.
        if len(self.attempts) > 500:
            del self.attempts[:250]

    def stats(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for entry in self.attempts:
            key = f"{entry['provider']}:{entry['outcome'].split(' ')[0]}"
            counts[key] = counts.get(key, 0) + 1
        return {"fallbacks": self.fallbacks, "outcomes": counts,
                "providers": sorted(self._by_name)}
