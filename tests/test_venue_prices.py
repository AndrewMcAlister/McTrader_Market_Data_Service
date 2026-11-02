"""The scheduled price collection for a venue with no whole-market real-time feed.

Database-free and vendor-free: the store, the product layer and the clock are faked or
read from the shipped reference. What is pinned is the decision -- seed from the store,
price it, then ask whether an observation now exists after the open, because that is the
question the scheduler retries on.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.api import _price_venue_universe
from data_service.clock import MarketClock

UTC = timezone.utc


class FakeStore:
    def __init__(self, symbols, latest=None):
        self.symbols = list(symbols)
        self.latest = latest
        self.seeded = []

    def top_symbols_by_dollar_volume(self, venue, limit):
        self.seeded.append((venue, limit))
        return list(self.symbols)

    def latest_venue_price(self, venue):
        return self.latest


class FakeProducts:
    def __init__(self):
        self.calls = []

    def prices(self, symbols, venue="US", **_):
        self.calls.append((list(symbols), venue))
        return {"requested": len(symbols),
                "fetched": list(symbols[:-1]) if symbols else [],
                "missing": [symbols[-1]] if symbols else [],
                "count": max(0, len(symbols) - 1),
                "quotes": {}}


def _asx_clock_and_open():
    """The clock, and the ASX open in the naive-UTC shape the store returns."""
    clock = MarketClock()
    exchange = next(row for row in clock.exchanges.values() if row["code"] == "ASX")
    session = clock.exchange_session(exchange, datetime.now(UTC))
    return clock, session.open_utc.replace(tzinfo=None)


def test_the_universe_is_seeded_from_the_store_and_priced_at_the_venue():
    clock, _ = _asx_clock_and_open()
    store = FakeStore(["BHP", "CBA", "COL"])
    products = FakeProducts()

    _price_venue_universe(products=products, store=store, venue="AU", limit=3, clock=clock)

    assert store.seeded == [("AU", 3)]
    assert products.calls == [(["BHP", "CBA", "COL"], "AU")]


def test_available_when_the_newest_price_is_after_the_open():
    clock, opened = _asx_clock_and_open()
    store = FakeStore(["BHP", "CBA"], latest=opened + timedelta(minutes=5))

    result = _price_venue_universe(products=FakeProducts(), store=store, venue="AU",
                                   limit=2, clock=clock)

    assert result["available"] is True
    assert result["universe"] == 2
    assert result["priced"] == 1
    assert result["fetched"] == 1
    assert result["missing"] == 1


def test_not_available_when_the_newest_price_predates_the_open():
    clock, opened = _asx_clock_and_open()
    store = FakeStore(["BHP"], latest=opened - timedelta(minutes=5))

    result = _price_venue_universe(products=FakeProducts(), store=store, venue="AU",
                                   limit=1, clock=clock)

    assert result["available"] is False
    assert result["latest_price_utc"] is not None


def test_an_empty_universe_is_named_and_prices_nothing():
    """No broad session is a reportable outcome, not an empty POST /prices that raises."""
    products = FakeProducts()
    store = FakeStore([])

    result = _price_venue_universe(products=products, store=store, venue="AU",
                                   limit=100, clock=MarketClock())

    assert result == {"venue": "AU", "universe": 0, "available": False,
                      "reason": "no-universe"}
    assert products.calls == []
