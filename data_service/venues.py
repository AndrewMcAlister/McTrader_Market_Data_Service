"""Which market a symbol request means: the venue, and every way a caller states it.

A ticker is not unique across markets. ``CBA`` is an ASX listing **and** a US listing with a
different price, a different daily series and its own ATR, so a request that names a symbol
has to name the market with it. Three spellings of that answer arrive, and they mean the
same thing:

* ``venue``        -- the vendor's code: ``US``, ``AU``/``ASX``, ``L``/``LSE``, ``TO``, ``HK``
* ``country_code`` -- the country: ``US``, ``AU`` (the vocabulary ``/todays-data-is-available``
  and ``/todays-big-movers`` already speak)
* the suffix on the symbol itself: ``CBA.AU``

THE ORDER OF PRECEDENCE, and why it is this order:

1. **A stated ``venue``/``country_code``.** The caller said which market it wants, so that is
   authoritative. Stating both is allowed while they agree and refused when they do not: two
   answers to one question is a mistake, not a preference.
2. **The suffix, when every symbol carries the same one.** ``["CBA.AU", "BHP.AU"]`` is an ASX
   request that stated no venue, and pricing it from the US tape is exactly the silent wrong
   number the venue argument exists to prevent. This also makes INTERFACE.md's promise --
   "bare with ``venue``, or already-suffixed" -- true for ``/prices``, ``/bars`` and ``/atr``,
   which strip the suffix before routing and used to fall back to US regardless.
3. **``US``, the house default.**

TWO REFUSALS RATHER THAN GUESSES. A suffix that contradicts a stated venue
(``venue: "US"`` with ``CBA.AU``) is a ``venue-conflict``, and a batch whose symbols name two
different markets is ``mixed-venues``. One request is one market, because the rows are read
and written under a single venue key; splitting the list into two calls is the caller's
decision to make, and the message says so.

Only a suffix that is in the venue table counts, so ``BRK.B`` is a dotted ticker rather than
a Belgian listing. Crypto has no venue at all -- ``BTC-USD`` is the same instrument whatever
tape answers -- so its market is ``CRYPTO`` and a stated venue is ignored rather than refused.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .availability import COUNTRIES
from .eodhd import EodhdError, normalise_venue

#: What a request means when nothing states a market. The same default the vendor's own
#: documentation uses, and the one every existing caller relies on.
DEFAULT_VENUE = "US"

#: The market code an asset with no venue is cached under. ``sessions.market_code`` uses the
#: same label, so a bar and the ATR built from it are filed in the same place.
CRYPTO_MARKET = "CRYPTO"

#: How many offending symbols a refusal quotes. Enough to recognise the mistake, short
#: enough that a 2,800-symbol request does not answer with a 2,800-symbol error.
_SAMPLE = 3


def _refusal(reason: str, detail: str) -> Exception:
    """A refusal in the shape the API renders, without importing products at module scope.

    ``MarketDataError`` lives in products.py, which imports this module, so the import is
    local to break the cycle. The shape is the one every endpoint already branches on: a
    ``reason``, a ``detail`` and a ``status``.
    """
    from .products import MarketDataError

    return MarketDataError(reason, detail, status=400)


def venue_for(code: str) -> str:
    """One country or venue code as the venue code the caches are keyed by, or refuse.

    ``us``/``NYSE`` -> ``US``, ``au``/``asx``/``AX`` -> ``AU``, ``uk``/``lse``/``GB`` -> ``L``.
    The country table the availability endpoints read wins where it has an entry, so ``AU``
    means the same market on every endpoint even if the vendor's alias table ever disagreed.
    """
    key = str(code or "").strip().upper()
    if not key:
        raise _refusal("unknown-venue", "a blank venue or country code is not a market")
    country = COUNTRIES.get(key)
    if country is not None:
        return country[0]
    try:
        return normalise_venue(key)
    except EodhdError:
        raise _refusal(
            "unknown-venue",
            f"{code!r} is not a country or venue code; use US, AU, L, TO, HK, F, PA, ..."
        ) from None


def suffix_venue(symbol: str) -> Optional[str]:
    """The venue a symbol names in its own suffix (``CBA.AU`` -> ``AU``), or ``None``.

    Only a suffix that is in the venue table counts. ``BRK.B`` is a class share and ``AX`` is
    the ASX under another name, so the two are told apart by the table rather than by the dot.
    """
    text = str(symbol or "").strip().upper()
    root, _, tail = text.rpartition(".")
    if not root or not tail:
        return None
    try:
        return normalise_venue(tail)
    except EodhdError:
        return None


def resolve_venue(
    venue: Optional[str] = None,
    country_code: Optional[str] = None,
    symbols: Iterable[str] = (),
    asset_class: Optional[Any] = None,
) -> str:
    """The market a request means, as the code the caches and the vendor are keyed by.

    *symbols* is what the suffix rule reads, so it must be the symbols as the caller sent
    them -- ``CBA.AU``, not ``CBA``. Returns a venue code (``US``, ``AU``, ...) or
    ``CRYPTO``, and raises :class:`~data_service.products.MarketDataError` (status 400) for
    an unknown code, a contradiction, or a batch that spans two markets.
    """
    kind = str(getattr(asset_class, "value", asset_class) or "").strip().lower()
    if kind == "crypto":
        return CRYPTO_MARKET

    stated = [(name, value) for name, value in
              (("venue", venue), ("country_code", country_code))
              if str(value or "").strip()]
    resolved = {name: venue_for(value) for name, value in stated}
    if len(set(resolved.values())) > 1:
        raise _refusal(
            "venue-conflict",
            "; ".join(f"{name} {value!r}" for name, value in stated)
            + f" name different markets ({', '.join(sorted(set(resolved.values())))}); "
              "state one of them"
        )

    # Which venue each symbol names in its own suffix, with the symbols that said so, so a
    # refusal can quote the ones that caused it.
    in_symbol: Dict[str, List[str]] = {}
    for raw in symbols:
        code = suffix_venue(raw)
        if code:
            in_symbol.setdefault(code, []).append(str(raw).strip().upper())

    def _examples() -> str:
        names = [name for code in sorted(in_symbol) for name in in_symbol[code]]
        shown = ", ".join(names[:_SAMPLE])
        return shown + (f" and {len(names) - _SAMPLE} more" if len(names) > _SAMPLE else "")

    if resolved:
        market = next(iter(resolved.values()))
        if in_symbol and market not in in_symbol:
            raise _refusal(
                "venue-conflict",
                f"{market!r} was asked for, but {_examples()} name the "
                f"{'/'.join(sorted(in_symbol))} market in the symbol suffix; send bare "
                "symbols with a venue, or suffixed symbols without one"
            )
        return market

    if len(in_symbol) == 1:
        return next(iter(in_symbol))
    if len(in_symbol) > 1:
        raise _refusal(
            "mixed-venues",
            f"the symbols name different markets ({', '.join(sorted(in_symbol))}) -- "
            f"{_examples()}; one request is one market, so split the list"
        )
    return DEFAULT_VENUE
