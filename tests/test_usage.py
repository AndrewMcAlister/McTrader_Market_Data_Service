"""Vendor spend: the tariff is the authority, and recording never breaks a price.

Everything here is database-free. The rule being pinned is that pricing comes from
``dbo.RequestTypes`` -- including its awkward cases (per_symbol, free, unpriced) -- and that a ledger
failure cannot turn a successful vendor call into an error for the caller.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.usage import (
    PURPOSE_REQUEST_TYPE,
    Tariff,
    TariffEntry,
    UsageLedger,
)


ROWS = [
    {"request_type": "/real-time/", "cost_basis": "per_symbol", "cost_each": 1},
    {"request_type": "/real-time?ex=US", "cost_basis": "flat", "cost_each": 100},
    {"request_type": "/eod/", "cost_basis": "per_call", "cost_each": 1},
    {"request_type": "/intraday/", "cost_basis": "per_call", "cost_each": 5},
    {"request_type": "/eod-bulk-last-day/US", "cost_basis": "flat", "cost_each": 100},
    {"request_type": "/screener", "cost_basis": "flat", "cost_each": 5},
    {"request_type": "/user", "cost_basis": "free", "cost_each": 0},
    {"request_type": "unknown", "cost_basis": "per_call", "cost_each": None},
]


@pytest.fixture()
def tariff() -> Tariff:
    return Tariff.from_rows(ROWS)


# ---------------------------------------------------------------------------
# the tariff
# ---------------------------------------------------------------------------

def test_per_symbol_multiplies_by_the_batch(tariff):
    """Measured: three tickers in one request billed three. Batching saves time, not quota."""
    assert tariff.price("/real-time/", 1) == 1
    assert tariff.price("/real-time/", 3) == 3


def test_per_symbol_treats_zero_symbols_as_one(tariff):
    assert tariff.price("/real-time/", 0) == 1


def test_flat_ignores_the_batch(tariff):
    assert tariff.price("/real-time?ex=US", 0) == 100
    assert tariff.price("/real-time?ex=US", 5000) == 100


def test_per_call_ignores_the_batch(tariff):
    """A wider window or a bigger response does not cost more."""
    assert tariff.price("/eod/", 0) == 1
    assert tariff.price("/eod/", 40) == 1
    assert tariff.price("/intraday/", 40) == 5


def test_free_is_zero_not_none(tariff):
    """Free and unpriced are different answers and must not be confused."""
    assert tariff.price("/user", 3) == 0


def test_an_unpriced_row_returns_none(tariff):
    """None says 'no figure is asserted', which is what the schema means by a NULL CostEach."""
    assert tariff.price("unknown", 1) is None


def test_an_unlisted_type_returns_none(tariff):
    assert tariff.price("/not-a-real-endpoint", 1) is None


def test_a_negative_or_odd_cost_each_is_just_multiplied(tariff):
    assert TariffEntry("x", "per_symbol", 2).price(4) == 8


def test_every_internal_purpose_maps_to_a_tariff_row(tariff):
    """A purpose with no row would silently record cost 0."""
    for purpose, request_type in PURPOSE_REQUEST_TYPE.items():
        assert tariff.entry(request_type) is not None, f"{purpose} -> {request_type} is not priced"


def test_the_listing_is_sorted_and_complete(tariff):
    listing = tariff.listing()
    assert len(listing) == len(ROWS)
    assert [e["request_type"] for e in listing] == sorted(r["request_type"] for r in ROWS)


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.written = []

    def record_usage(self, **kwargs):
        if self.fail:
            raise RuntimeError("database is on fire")
        self.written.append(kwargs)


def test_recording_prices_the_call_and_persists_it(tariff):
    store = FakeStore()
    ledger = UsageLedger(store, tariff)

    cost = ledger.record("/real-time/", symbols=3, caller="mctrader")

    assert cost == 3
    assert store.written == [{
        "request_type": "/real-time/", "caller": "mctrader",
        "calls": 1, "symbols": 3, "cost": 3,
    }]


def test_an_unpriced_call_is_still_recorded(tariff):
    """Invisible spend is the thing this table exists to prevent, so cost 0 is not 'skip'."""
    store = FakeStore()
    ledger = UsageLedger(store, tariff)

    cost = ledger.record("unknown", symbols=2, caller="mctrader")

    assert cost == 0
    assert store.written[0]["cost"] == 0
    assert store.written[0]["symbols"] == 2


def test_a_ledger_failure_does_not_raise(tariff):
    """The price has already been fetched; accounting failing must not turn that into an error."""
    ledger = UsageLedger(FakeStore(fail=True), tariff)
    assert ledger.record("/eod/", 1, "mctrader") == 1


def test_the_caller_defaults_to_the_context(tariff):
    from data_service.usage import current_caller, set_caller

    store = FakeStore()
    ledger = UsageLedger(store, tariff)
    set_caller("papertrader")
    try:
        ledger.record("/eod/", symbols=1)
        assert store.written[0]["caller"] == "papertrader"
        assert current_caller() == "papertrader"
    finally:
        set_caller("unknown")


def test_an_explicit_caller_wins_over_the_context(tariff):
    from data_service.usage import set_caller

    store = FakeStore()
    ledger = UsageLedger(store, tariff)
    set_caller("from-context")
    try:
        ledger.record("/eod/", 1, "explicit")
        assert store.written[0]["caller"] == "explicit"
    finally:
        set_caller("unknown")


# ---------------------------------------------------------------------------
# the client honours the ledger
# ---------------------------------------------------------------------------

def test_the_client_uses_the_recorded_cost_over_its_literal():
    """The literal units at the call site are the fallback; the tariff is authoritative."""
    from data_service.eodhd import EodhdClient

    client = EodhdClient(api_key="x")
    seen = []

    def recorder(request_type, symbols):
        seen.append((request_type, symbols))
        return 42

    client.recorder = recorder
    client._spent("real_time", units=1, symbols=3)

    assert seen == [("/real-time/", 3)]
    assert client.units == 42
    assert client.calls == {"real_time": 1}


def test_without_a_recorder_the_literal_is_used():
    from data_service.eodhd import EodhdClient

    client = EodhdClient(api_key="x")
    client._spent("eod_bulk", units=100, symbols=0)

    assert client.units == 100


def test_a_broken_recorder_falls_back_to_the_literal():
    from data_service.eodhd import EodhdClient

    client = EodhdClient(api_key="x")

    def recorder(request_type, symbols):
        raise RuntimeError("no tariff")

    client.recorder = recorder
    client._spent("eod", units=1, symbols=1)

    assert client.units == 1
