"""Provider-specific symbol rendering — ported from McTrader's ``market_data.provider_symbols``.

Canonical identity is ``(raw_symbol, asset_class, venue)``: ``('IXIC', 'indicies', None)``,
``('GLD', 'commodities', None)``, ``('CBA', 'shares', 'AU')``. The raw symbol is never
decorated by the caller — no ``^``, no ``=F``, no ``.US`` — so each provider re-interprets the
same identity in its own convention.

WHY THIS IS A PORT AND NOT AN IMPORT

The service may not import from McTrader (§Scope in the README): its availability is part of
why it exists, so it cannot inherit McTrader's dependency tree. This module is a faithful
copy of a dependency-free file (McTrader's imports only ``typing``), and
``tests/test_symbols.py`` pins the conventions that matter so the two cannot silently drift.

``symbol_for_provider`` returns ``None`` when a provider cannot serve the instrument (Binance
has no indices; EODHD has no bare commodity futures). Callers must treat ``None`` as "this
provider cannot answer" and fall through — never as a symbol to attempt.

Owner-confirmed EODHD codes (2026-09-15, carried over from McTrader): ``TNX.INDX``,
``US10Y.INDX``, ``GLD.US``. Commodities deliberately use US-listed ETF proxies on EODHD
rather than unverified ``.COMM`` futures codes.
"""
from __future__ import annotations

from typing import Optional

EODHD = "eodhd"
YAHOO = "yahoo"
BINANCE = "binance"

#: EODHD renders these as US-listed ETF proxies (GLD confirmed; rest same pattern).
_EODHD_ETF_PROXIES = {
    "GLD": "GLD.US",  # gold          — confirmed
    "SLV": "SLV.US",  # silver        — ETF proxy
    "BNO": "BNO.US",  # Brent crude   — ETF proxy
    "UUP": "UUP.US",  # US dollar idx — ETF proxy
}

#: EODHD index/rate codes (INDX suffix).
_EODHD_INDEX_CODES = {
    "IXIC": "IXIC.INDX",
    "GSPC": "GSPC.INDX",
    "US10Y": "US10Y.INDX",  # confirmed
    "TNX": "TNX.INDX",      # confirmed alias of the same instrument
}

#: Yahoo decorations for the same instruments.
_YAHOO_INDEX_CODES = {
    "IXIC": "^IXIC",
    "GSPC": "^GSPC",
    "US10Y": "^TNX",
    "TNX": "^TNX",
}
_YAHOO_COMMODITY_CODES = {
    "GLD": "GC=F",      # gold futures
    "SLV": "SI=F",      # silver futures
    "BNO": "BZ=F",      # Brent futures
    "UUP": "DX-Y.NYB",  # ICE US dollar index
}

US_VENUE_CODES = frozenset({
    "US", "NASDAQ", "NASDAQGS", "NASDAQGM", "NASDAQCM", "NMS",
    "NYSE", "NYQ", "NYS", "NYSEARCA", "ARCA", "PCX", "AMEX", "ASE",
    "BATS", "IEX", "OTC", "OTCQX", "OTCQB", "PINK",
})

_VENUE_TICKER_SUFFIXES = {
    "ASX": "AU", "AU": "AU", "AX": "AU",
    "LSE": "L", "L": "L", "LON": "L", "UK": "L",
    "TSX": "TO", "TSE": "TO", "TO": "TO", "CA": "TO",
    "HKEX": "HK", "HKG": "HK", "HK": "HK",
    "XETRA": "F", "DAX": "F", "FRA": "F", "F": "F", "DE": "F",
    "CAC": "PA", "PA": "PA", "PAR": "PA", "FR": "PA",
}

#: Which currency a venue's shares settle in. One row per currency, naming the venues and
#: the provider suffixes that mean it, so EODHD and Yahoo cannot disagree about what an ASX
#: price is denominated in.
_VENUE_CURRENCIES = (
    ("AUD", frozenset({"ASX", "AU", "AX"}), (".AU", ".AX")),
    ("GBP", frozenset({"LSE", "L", "LON", "UK"}), (".L",)),
    ("CAD", frozenset({"TSX", "TSE", "TO", "CA"}), (".TO",)),
    ("HKD", frozenset({"HKEX", "HKG", "HK"}), (".HK",)),
    ("EUR", frozenset({"XETRA", "DAX", "FRA", "F", "DE", "CAC", "PA", "PAR", "FR"}),
     (".F", ".PA")),
)

DEFAULT_EODHD_EXCHANGE = "US"


def normalise_raw_symbol(symbol: str) -> str:
    """The undecorated identity for *symbol*, tolerating a Yahoo-style ``^`` prefix."""
    raw = str(symbol or "").strip().upper()
    while raw.startswith("^"):
        raw = raw[1:]
    return raw


def _asset_key(asset_class: Optional[object]) -> str:
    return str(getattr(asset_class, "value", asset_class) or "").strip().lower()


def _effective_exchange(exchange: Optional[str]) -> Optional[str]:
    value = str(exchange or "").strip().upper()
    return value or None


def venue_currency_code(symbol: str, exchange: Optional[str] = None) -> Optional[str]:
    """The currency *symbol*'s venue settles in, or ``None`` when unknown."""
    venue = _effective_exchange(exchange)
    upper = str(symbol or "").strip().upper()
    for currency, venues, suffixes in _VENUE_CURRENCIES:
        if venue in venues or upper.endswith(suffixes):
            return currency
    return None


def eodhd_venue_suffix(exchange: Optional[str]) -> Optional[str]:
    """The EODHD venue suffix *exchange* names, or ``None`` if unknown.

    ``None`` lets the price API refuse an unrecognised venue instead of silently falling back
    to the house US default — a non-US symbol priced off a US record is not an answer.
    """
    venue = _effective_exchange(exchange)
    if venue in US_VENUE_CODES:
        return DEFAULT_EODHD_EXCHANGE
    return _VENUE_TICKER_SUFFIXES.get(venue or "")


def eodhd_equity_ticker(
    raw_symbol: str,
    exchange: Optional[str] = None,
    default_exchange: Optional[str] = None,
) -> str:
    """The one EODHD ticker for *raw_symbol* as a share/ETF — ``""`` if empty.

    An unrecognised venue deliberately resolves to the *house* default rather than trying an
    ``AU``-first ladder: that ladder meant requesting ``DELL.AU`` — which EODHD answers with
    HTTP 200 and every field the literal string ``"NA"`` — before ``DELL.US``, so a US symbol
    was priced only because ``float("NA")`` raised.
    """
    raw = normalise_raw_symbol(raw_symbol)
    if not raw:
        return ""
    if "." in raw:
        root, suffix = raw.rsplit(".", 1)
        if not root or not suffix:
            return ""
        return f"{root}.{'AU' if suffix == 'AX' else suffix}"
    suffix = eodhd_venue_suffix(_effective_exchange(exchange))
    if suffix:
        return f"{raw}.{suffix}"
    default = str(default_exchange or "").strip().upper().lstrip(".")
    return f"{raw}.{default or DEFAULT_EODHD_EXCHANGE}"


def symbol_for_provider(
    raw_symbol: str,
    asset_class: Optional[object] = None,
    exchange: Optional[str] = None,
    provider: str = EODHD,
) -> Optional[str]:
    """Render *raw_symbol* in *provider*'s convention, or ``None`` if it cannot serve it."""
    raw = normalise_raw_symbol(raw_symbol)
    if not raw:
        return None
    provider_key = str(provider or "").strip().lower()
    kind = _asset_key(asset_class)
    exchange_key = _effective_exchange(exchange)

    if kind in {"indicies", "indices", "index"}:
        if provider_key == EODHD:
            return _EODHD_INDEX_CODES.get(raw, f"{raw}.INDX")
        if provider_key == YAHOO:
            return _YAHOO_INDEX_CODES.get(raw, f"^{raw}")
        return None  # Binance cannot serve indices.

    if kind in {"commodities", "commodity", "futures", "future"}:
        if provider_key == EODHD:
            return _EODHD_ETF_PROXIES.get(raw)
        if provider_key == YAHOO:
            return _YAHOO_COMMODITY_CODES.get(raw)
        return None

    if kind in {"rates", "rate", "bonds"}:
        if provider_key == EODHD:
            return _EODHD_INDEX_CODES.get(raw, f"{raw}.INDX")
        if provider_key == YAHOO:
            return _YAHOO_INDEX_CODES.get(raw, f"^{raw}")
        return None

    if kind in {"forex", "fx", "currency"}:
        if provider_key == EODHD:
            return f"{raw}.FOREX"
        if provider_key == YAHOO:
            return f"{raw}=X"
        return None

    if kind in {"crypto", "cryptocurrency"}:
        if provider_key == EODHD:
            return f"{raw}.CC"
        if provider_key == YAHOO:
            return raw
        if provider_key == BINANCE:
            return raw.replace("-", "").replace("/", "")
        return None

    # Shares / ETFs / options and anything else: venue-dependent.
    if provider_key == EODHD:
        return eodhd_equity_ticker(raw, exchange_key) or None
    if provider_key == YAHOO:
        if exchange_key == "AU":
            return f"{raw}.AX"
        return raw
    return None
