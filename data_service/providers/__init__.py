"""The provider layer: EODHD, Yahoo and Binance behind one routing decision."""
from __future__ import annotations

from typing import Optional

from .base import (
    ASSET_CLASSES, COMMODITIES, CRYPTO, FOREX, INDICES, RATES, SHARES,
    Provider, ProviderBar, ProviderError, ProviderQuote, normalise_asset_class,
)
from .binance_provider import BinanceProvider
from .eodhd_provider import EodhdProvider
from .router import ProviderRouter, order_for
from .yahoo_provider import YahooProvider

__all__ = [
    "ASSET_CLASSES", "COMMODITIES", "CRYPTO", "FOREX", "INDICES", "RATES", "SHARES",
    "Provider", "ProviderBar", "ProviderError", "ProviderQuote",
    "normalise_asset_class",
    "BinanceProvider", "EodhdProvider", "YahooProvider",
    "ProviderRouter", "order_for", "build_router",
]


def build_router(eodhd_client=None, *, yahoo: Optional[YahooProvider] = None,
                 binance: Optional[BinanceProvider] = None,
                 timeout: float = 20.0) -> ProviderRouter:
    """The production router: EODHD (when a client is supplied), Yahoo and Binance.

    EODHD is added only when a client is passed, so a deployment without an API key still
    serves indices and crypto from the two free providers instead of failing at startup.
    """
    providers: list = []
    if eodhd_client is not None:
        providers.append(EodhdProvider(eodhd_client))
    providers.append(yahoo or YahooProvider(timeout=timeout))
    providers.append(binance or BinanceProvider(timeout=timeout))
    return ProviderRouter(providers)
