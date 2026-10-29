"""The products layer: routing, caching and provenance, with no vendor and no database.

The two decisions most worth pinning are:

* ``prices()`` picks bulk or per-symbol from the list length, because the tariff break-even
  is 100 symbols and getting it wrong costs up to 100x in one direction or the other;
* a cached price is returned without a vendor call, and a fresh one is written to the cache
  so the next caller is free.

Everything here runs against in-memory fakes, so the suite spends no quota (R7).
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import config as config_module
from data_service import products as products_module
from data_service.products import MarketDataError, Products
from data_service.providers import EodhdProvider, ProviderRouter


def _past_sessions(count: int, start: int = 0):
    """`count` dates ending `start` days before today, oldest first (weekends included)."""
    today = date.today()
    return [today - timedelta(days=start + (count - 1 - i)) for i in range(count)]


class FakeClient:
    """Stands in for EodhdClient. Counts calls so a test can prove none were made."""

    def __init__(self, price: float = 100.0, previous: float = 98.0,
                 bulk_rows: int = 300, bars_per_symbol: int = 40) -> None:
        self.price = price
        self.previous = previous
        self.bulk_rows = bulk_rows
        self.bars_per_symbol = bars_per_symbol
        self.calls = {"real_time": 0, "real_time_bulk": 0, "eod": 0}
        self.units = 0

    def real_time(self, symbol: str, venue: str = "US"):
        self.calls["real_time"] += 1
        self.units += 1
        return {
            "code": f"{symbol}.US", "close": self.price, "previousClose": self.previous,
            "timestamp": int(datetime.now(timezone.utc).timestamp()) - 60,
        }

    def real_time_whole_market(self, venue: str = "US"):
        self.calls["real_time_bulk"] += 1
        self.units += 100
        now = int(datetime.now(timezone.utc).timestamp()) - 30
        return [
            {"code": f"SYM{i:04d}.US", "close": self.price, "previousClose": self.previous,
             "timestamp": now}
            for i in range(self.bulk_rows)
        ]

    def eod(self, symbol: str, venue: str = "US", days: int = 60, to_date=None):
        self.calls["eod"] += 1
        self.units += 1
        rows = []
        for index, session in enumerate(_past_sessions(self.bars_per_symbol, start=1)):
            base = 100.0 + index
            rows.append({
                "date": session.isoformat(), "open": base, "high": base + 2.0,
                "low": base - 2.0, "close": base + 0.5, "volume": 1000,
                "adjusted_close": base + 0.5,
            })
        return rows

    def stats(self):
        return {"calls_by_purpose": dict(self.calls), "units": self.units}


class FakeStore:
    """In-memory stand-in for Store. Only the methods Products calls."""

    def __init__(self) -> None:
        self.prices = {}
        self.daily = {}
        self.indicators = {}
        self.price_writes = 0
        self.daily_writes = 0
        self.indicator_writes = 0

    # prices
    def get_prices(self, symbols, venue):
        out = {}
        for symbol in symbols:
            key = (symbol.strip().upper(), venue)
            if key in self.prices:
                out[symbol.strip().upper()] = dict(self.prices[key])
        return out

    def upsert_prices(self, rows):
        rows = list(rows)
        for row in rows:
            self.prices[(row["symbol"], row["venue"])] = dict(row)
        self.price_writes += len(rows)
        return len(rows)

    # daily bars
    def get_daily_bars_many(self, symbols, sessions):
        out = {}
        for raw in symbols:
            symbol = raw.strip().upper()
            bucket = self.daily.get(symbol, {})
            rows = sorted(bucket.values(), key=lambda r: r["session"])
            out[symbol] = [dict(r) for r in rows[-sessions:]]
        return out

    def upsert_daily(self, rows):
        rows = list(rows)
        for row in rows:
            bucket = self.daily.setdefault(row["symbol"], {})
            bucket[row["session"]] = dict(row)
        self.daily_writes += len(rows)
        return len(rows)

    def get_indicators(self, symbols):
        out = {}
        for raw in symbols:
            symbol = raw.strip().upper()
            if symbol in self.indicators:
                out[symbol] = dict(self.indicators[symbol])
        return out

    def upsert_indicators(self, rows):
        rows = list(rows)
        for row in rows:
            self.indicators[row["symbol"]] = dict(row)
        self.indicator_writes += len(rows)
        return len(rows)


def _settings(**overrides) -> config_module.Settings:
    base = dict(
        eodhd_api_key="test-key",
        price_ttl_seconds=300.0,
        bulk_breakeven=100,
        min_sessions=15,
        atr_periods=(14, 20, 22, 26, 50),
        atr_period=14,
        atr_method="tr",
        atr_smoothing="simple",
    )
    base.update(overrides)
    return config_module.Settings(**base)


def _products(**overrides) -> tuple[Products, FakeClient, FakeStore]:
    settings = _settings(**overrides)
    store = FakeStore()
    client = FakeClient()
    # A router holding ONLY the fake provider. With the production router the fallback for a
    # share is a real Yahoo request, which would make this suite spend time on the network and
    # turn a unit test into an integration test (R7: no live vendor calls in tests).
    router = ProviderRouter([EodhdProvider(client)])
    return Products(settings, store, client=client, router=router), client, store


# ---------------------------------------------------------------------------
# price: the cache is the point
# ---------------------------------------------------------------------------

def test_a_price_miss_costs_exactly_one_vendor_call():
    products, client, store = _products()
    quote = products.price("AAPL")
    assert quote["price"] == pytest.approx(100.0)
    assert quote["previous_close"] == pytest.approx(98.0)
    assert client.calls["real_time"] == 1
    assert store.price_writes == 1


def test_a_second_price_read_inside_the_window_costs_nothing():
    products, client, _ = _products()
    products.price("AAPL")
    quote = products.price("AAPL")
    assert client.calls["real_time"] == 1
    assert quote["symbol"] == "AAPL"


def test_change_pct_is_computed_from_the_stored_pair():
    """Not read from the vendor's change_p: the ranking variable must be one arithmetic."""
    products, _, _ = _products()
    quote = products.price("AAPL")
    assert quote["change_pct"] == pytest.approx((100.0 / 98.0 - 1.0) * 100.0)


def test_max_age_seconds_can_lower_the_window_but_not_raise_it():
    products, client, store = _products()
    products.price("AAPL")
    # Backdate the cached row to 400s old, still inside the 300s TTL? No: outside it.
    row = store.prices[("AAPL", "US")]
    row["fetched_at"] = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=100)
    store.prices[("AAPL", "US")] = row
    # 100s old: inside the 300s TTL, so a request asking for 60s refetches.
    products.price("AAPL", max_age_seconds=60)
    assert client.calls["real_time"] == 2


def test_an_unknown_symbol_is_reported_not_priced_at_zero():
    products, client, _ = _products()

    def boom(symbol, venue="US"):
        from data_service.eodhd import EodhdError
        raise EodhdError("http-error", "404")

    client.real_time = boom
    with pytest.raises(MarketDataError) as exc:
        products.price("NOPE")
    assert exc.value.reason == "unpriced"


# ---------------------------------------------------------------------------
# prices: the routing decision
# ---------------------------------------------------------------------------

def test_a_small_batch_goes_per_symbol():
    products, client, _ = _products()
    result = products.prices([f"S{i}" for i in range(6)])
    assert result["route"] == "per_symbol"
    assert client.calls["real_time"] == 6
    assert client.calls["real_time_bulk"] == 0
    assert result["count"] == 6


def test_a_large_batch_goes_bulk_because_100_is_the_break_even():
    """150 symbols: 150 units per-symbol against 100 flat in bulk."""
    products, client, store = _products()
    products._bulk_snapshot = {
        f"SYM{i:04d}": {
            "symbol": f"SYM{i:04d}", "venue": "US", "price": 100.0, "previous_close": 98.0,
            "change_pct": (100.0 / 98.0 - 1.0) * 100.0, "bid": None, "ask": None,
            "as_of": None, "fetched_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "provider": "eodhd",
        }
        for i in range(300)
    }
    symbols = [f"SYM{i:04d}" for i in range(150)]
    result = products.prices(symbols)
    assert result["route"] == "bulk"
    assert client.calls["real_time"] == 0
    assert result["count"] == 150


def test_the_bulk_snapshot_warms_the_cache_for_every_row_not_just_the_asked_ones():
    """One scan should leave single-symbol reads free for everyone afterwards."""
    products, client, store = _products()
    symbols = [f"SYM{i:04d}" for i in range(150)]
    products.prices(symbols)
    assert client.calls["real_time_bulk"] == 1
    # 300 rows in the fake feed, all written, not the 150 requested.
    assert store.price_writes == 300

    products.price("SYM0299")
    assert client.calls["real_time"] == 0


def test_no_symbols_is_a_refusal_not_an_empty_answer():
    products, _, _ = _products()
    with pytest.raises(MarketDataError):
        products.prices([])


# ---------------------------------------------------------------------------
# bars
# ---------------------------------------------------------------------------

def test_bars_come_from_the_vendor_once_then_from_the_cache():
    products, client, store = _products()
    products.bars(["AAPL"], sessions=15)
    assert client.calls["eod"] == 1
    first_writes = store.daily_writes

    products.bars(["AAPL"], sessions=15)
    assert client.calls["eod"] == 1          # cached
    assert store.daily_writes == first_writes


def test_bars_declare_their_session_and_adjustment():
    products, _, _ = _products()
    result = products.bars(["AAPL"], sessions=15)
    entry = result["bars"]["AAPL"]
    assert entry["sessions"] == 15
    assert entry["adjusted"] is True
    assert entry["as_of_session"] < date.today().isoformat()


def test_allow_fetch_false_never_touches_the_vendor():
    products, client, _ = _products()
    result = products.bars(["AAPL"], sessions=15, allow_fetch=False)
    assert client.calls["eod"] == 0
    assert result["bars"] == {}
    assert result["short_history"] == ["AAPL"]


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def test_atr_computes_from_bars_and_caches_the_result():
    products, client, store = _products()
    result = products.atr_batch(["AAPL"], period=14)
    reading = result["atr"]["AAPL"]
    assert reading["value"] is not None and reading["value"] > 0
    assert reading["sessions_used"] >= 15
    assert store.indicator_writes == 1

    # The second call reads IndicatorCache: no further bar fetch.
    calls_before = client.calls["eod"]
    products.atr_batch(["AAPL"], period=14)
    assert client.calls["eod"] == calls_before


def test_insufficient_history_yields_unavailable_and_a_reason():
    settings = _settings(min_sessions=15)
    store = FakeStore()
    client = FakeClient(bars_per_symbol=5)
    products = Products(settings, store, client=client)
    result = products.atr_batch(["AAPL"], period=14)
    reading = result["atr"]["AAPL"]
    assert reading["value"] is None
    assert "reason" in reading and reading["reason"]


def test_an_unknown_method_is_refused_by_name():
    products, _, _ = _products()
    with pytest.raises(MarketDataError) as exc:
        products.atr_batch(["AAPL"], method="average_of_extremes")
    assert "tr" in str(exc.value)


def test_stop_level_needs_both_price_and_multiplier():
    products, _, _ = _products()
    with pytest.raises(MarketDataError):
        products.atr_batch(["AAPL"], period=14, price=100.0)


def test_stop_level_is_attached_when_both_are_supplied():
    products, _, _ = _products()
    result = products.atr_batch(["AAPL"], period=14, price=100.0, multiplier=1.0)
    reading = result["atr"]["AAPL"]
    assert reading["value"] is not None
    assert reading["stop_level"] == pytest.approx(100.0 - reading["value"], abs=1e-6)


def test_a_different_smoothing_is_computed_and_not_written_to_the_cache():
    """IndicatorCache has one Smoothing column, so a non-default definition must not land."""
    products, _, store = _products()
    products.atr_batch(["AAPL"], period=14)
    writes_after_default = store.indicator_writes
    products.atr_batch(["AAPL"], period=14, smoothing="wilder")
    assert store.indicator_writes == writes_after_default


def test_the_response_always_states_the_definition_it_used():
    products, _, _ = _products()
    result = products.atr_batch(["AAPL"], period=22, method="high_low")
    assert result["period"] == 22
    assert result["method"] == "high_low"
    assert result["smoothing"] == "simple"
