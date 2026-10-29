"""EODHD spend: priced from ``dbo.RequestTypes``, recorded in ``dbo.EODHDUsage``.

WHY THE TABLE IS THE AUTHORITY, NOT THE CODE

The per-call costs used to be literals at each call site (``units=1``, ``units=100``). They were
measured, and the measurement lives in ``dbo.RequestTypes`` with its evidence -- so a re-measured
price is a row update, not a release, and the number a reader checks is the same number the code
spent.

The mapping from an internal purpose to a tariff row is here rather than at the call sites, so adding
a vendor endpoint means adding one entry instead of editing four modules.

WHY RECORDING CANNOT BREAK PRICING

A price is the reason callers use this service; a usage row is why accounting can check it. If the
ledger write fails, the price still has to be served. So every write is caught, logged once per
request type, and swallowed -- the in-process counters on the client remain the fallback.

DAY BOUNDARY IS LOCAL

``UsageDate`` and the ``FirstSeen``/``LastSeen`` timestamps are local wall-clock, the same as the
logging tables. It was UTC, which filed a Sydney morning's spend under the previous day and made
"today" in the table mean yesterday at the desk.

The trade, stated once: a local date cannot be compared across timezones without knowing this one,
and the vendor's own quota resets on UTC, so the ledger's day and the vendor's day are offset by the
number of hours this site is ahead.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

#: Internal purpose -> the ``RequestTypes.RequestType`` that prices it.
PURPOSE_REQUEST_TYPE: Dict[str, str] = {
    "real_time": "/real-time/",
    "real_time_bulk": "/real-time?ex=US",
    "eod": "/eod/",
    "eod_bulk": "/eod-bulk-last-day/US",
}

#: What a call becomes when no tariff row matches it. It still gets a row: an unpriced call is a
#: spend surface that must be visible, not a call that never happened.
UNKNOWN_REQUEST_TYPE = "unknown"

#: Who caused this spend. Set by the API from X-MarketData-Caller, or by a job.
#:
#: A ContextVar is safe here because every data endpoint is a plain ``def``: Starlette runs the
#: endpoint and the whole vendor call chain on one worker thread, so a value set in the endpoint body
#: is visible to the client below it. A FastAPI dependency could not be relied on that way -- it may
#: execute on a different pool thread than the endpoint.
_caller: ContextVar[str] = ContextVar("eodhd_caller", default="unknown")


def set_caller(name: object) -> None:
    _caller.set(str(name or "unknown").strip()[:128] or "unknown")


def current_caller() -> str:
    return _caller.get()


@dataclass(frozen=True)
class TariffEntry:
    """One ``RequestTypes`` row: how this endpoint is billed."""

    request_type: str
    cost_basis: str
    cost_each: Optional[int]

    def price(self, symbols: int = 0) -> Optional[int]:
        """Units for one call, or ``None`` when the row is unpriced."""
        if self.cost_each is None:
            return None
        if self.cost_basis == "free":
            return 0
        if self.cost_basis == "per_symbol":
            # Batching saves wall-clock, not quota: three tickers in one request billed three.
            return int(self.cost_each) * max(1, int(symbols))
        # flat and per_call both bill once per call, whatever it returned.
        return int(self.cost_each)


class Tariff:
    """The ``RequestTypes`` catalogue, loaded once and held in memory."""

    def __init__(self, entries: Iterable[TariffEntry] = ()) -> None:
        self._by_type: Dict[str, TariffEntry] = {e.request_type: e for e in entries}

    @classmethod
    def from_rows(cls, rows: Iterable[Dict[str, Any]]) -> "Tariff":
        return cls(
            TariffEntry(
                request_type=str(row["request_type"]),
                cost_basis=str(row["cost_basis"] or "").strip().lower(),
                cost_each=row["cost_each"],
            )
            for row in rows
        )

    def __len__(self) -> int:
        return len(self._by_type)

    def entry(self, request_type: str) -> Optional[TariffEntry]:
        return self._by_type.get(request_type)

    def price(self, request_type: str, symbols: int = 0) -> Optional[int]:
        entry = self.entry(request_type)
        return None if entry is None else entry.price(symbols)

    def listing(self) -> List[Dict[str, Any]]:
        return [
            {"request_type": e.request_type, "cost_basis": e.cost_basis, "cost_each": e.cost_each}
            for e in sorted(self._by_type.values(), key=lambda e: e.request_type)
        ]


class UsageLedger:
    """Writes the spend ledger, and never at the cost of the call that caused it."""

    def __init__(self, store: Any, tariff: Tariff) -> None:
        self.store = store
        self.tariff = tariff
        self._unpriced_warned: set = set()

    def record(self, request_type: str, symbols: int = 0,
               caller: Optional[str] = None) -> int:
        """Price one call, record it, and return the units it cost."""
        caller = caller or current_caller()
        cost = self.tariff.price(request_type, symbols)
        if cost is None:
            cost = 0
            if request_type not in self._unpriced_warned:
                self._unpriced_warned.add(request_type)
                log.warning(
                    "%s has no priced row in dbo.RequestTypes; recording the call with cost 0 "
                    "so the surface stays visible. It is not free -- it is unpriced.",
                    request_type)
        try:
            self.store.record_usage(
                request_type=request_type,
                caller=str(caller or "unknown"),
                calls=1,
                symbols=int(symbols or 0),
                cost=int(cost),
            )
        except Exception as exc:  # noqa: BLE001 - accounting must never fail a price
            log.warning("usage not recorded for %s (%s: %s); the call itself is unaffected",
                        request_type, type(exc).__name__, exc)
        return int(cost)

    def recorder_for(self, caller: str):
        """A callback bound to *caller*, for one request. Explicit rather than a contextvar.

        FastAPI runs these sync endpoints in a thread pool, so a ContextVar set in a dependency may
        be set on a different thread than the endpoint body reads it from. Passing the caller down is
        duller and cannot be wrong.
        """
        return lambda request_type, symbols: self.record(request_type, symbols, caller)


def today_local() -> date:
    """The ledger's day, in local time -- the same boundary the store writes under."""
    from datetime import datetime

    return datetime.now().date()
