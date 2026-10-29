"""Routing precedence and payload parsing, with no network access.

Two things are worth pinning here:

* **the order is per asset class** — a US share must go to EODHD, a crypto pair to Binance,
  and a provider that cannot serve a class must not be asked at all;
* **failover is declared** — the provider that answered travels on the quote, because a stop
  computed on one tape while the level was chosen on another is a silent error, and this
  estate has already been bitten by one.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.providers import (
    ProviderBar, ProviderError, ProviderQuote, ProviderRouter, order_for,
)
from data_service.providers.binance_provider import BinanceProvider
from data_service.providers.yahoo_provider import YahooProvider


# ---------------------------------------------------------------------------
# a provider whose every answer is scripted
# ---------------------------------------------------------------------------

class FakeProvider:
    def __init__(self, name: str, supports=("shares", "crypto", "indices",
                                            "commodities", "rates", "forex"),
                 quote_result=None, bars_result=None, raise_on=None) -> None:
        self.name = name
        self._supports = set(supports)
        self._quote = quote_result
        self._bars = bars_result
        self._raise_on = raise_on
        self.quote_calls = 0
        self.bars_calls = 0

    def supports(self, asset_class: str) -> bool:
        return asset_class in self._supports

    def quote(self, symbol, asset_class="shares", venue=None):
        self.quote_calls += 1
        if self._raise_on == "quote":
            raise ProviderError("scripted", "boom")
        return self._quote

    def bars(self, symbol, asset_class="shares", sessions=60, venue=None):
        self.bars_calls += 1
        if self._raise_on == "bars":
            raise ProviderError("scripted", "boom")
        return list(self._bars or [])


def _quote(provider: str, price: float = 100.0) -> ProviderQuote:
    return ProviderQuote(symbol="TEST", price=price, provider=provider,
                         previous_close=98.0)


def _bar(provider: str) -> ProviderBar:
    return ProviderBar(session=date.today() - timedelta(days=1), open=10.0, high=11.0,
                       low=9.0, close=10.5, volume=100)


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------

def test_a_us_share_goes_to_eodhd_first():
    assert order_for("shares")[0] == "eodhd"


def test_crypto_goes_to_binance_first():
    assert order_for("crypto")[0] == "binance"


def test_every_class_ends_with_a_free_fallback():
    for asset_class in ("shares", "crypto", "indices", "commodities", "rates", "forex"):
        assert "yahoo" in order_for(asset_class), asset_class


def test_an_unknown_asset_class_defaults_to_shares():
    assert order_for("something-else") == order_for("shares")


# ---------------------------------------------------------------------------
# routing behaviour
# ---------------------------------------------------------------------------

def test_the_first_provider_that_answers_wins_and_the_rest_are_not_asked():
    primary = FakeProvider("eodhd", quote_result=_quote("eodhd"))
    secondary = FakeProvider("yahoo", quote_result=_quote("yahoo"))
    router = ProviderRouter([primary, secondary])

    result = router.quote("AAPL", "shares")
    assert result.provider == "eodhd"
    assert primary.quote_calls == 1
    assert secondary.quote_calls == 0


def test_a_declining_provider_falls_through_and_the_tape_is_declared():
    primary = FakeProvider("eodhd", quote_result=None)
    secondary = FakeProvider("yahoo", quote_result=_quote("yahoo"))
    router = ProviderRouter([primary, secondary])

    result = router.quote("AAPL", "shares")
    assert result.provider == "yahoo"      # the tape that answered is carried
    assert router.fallbacks == 1


def test_a_raising_provider_falls_through_rather_than_failing_the_request():
    primary = FakeProvider("eodhd", raise_on="quote")
    secondary = FakeProvider("yahoo", quote_result=_quote("yahoo"))
    router = ProviderRouter([primary, secondary])
    assert router.quote("AAPL", "shares").provider == "yahoo"


def test_a_provider_that_cannot_serve_the_class_is_never_asked():
    """Binance has no indices, so asking it is a wasted request and a misleading failure."""
    binance = FakeProvider("binance", supports=("crypto",), quote_result=_quote("binance"))
    yahoo = FakeProvider("yahoo", quote_result=_quote("yahoo"))
    router = ProviderRouter([binance, yahoo])

    result = router.quote("IXIC", "indices")
    assert result.provider == "yahoo"
    assert binance.quote_calls == 0


def test_crypto_takes_binance_not_eodhd():
    eodhd = FakeProvider("eodhd", quote_result=_quote("eodhd"))
    binance = FakeProvider("binance", quote_result=_quote("binance"))
    router = ProviderRouter([eodhd, binance])
    assert router.quote("BTC-USD", "crypto").provider == "binance"
    assert eodhd.quote_calls == 0


def test_nobody_can_answer_yields_none_rather_than_an_error():
    router = ProviderRouter([FakeProvider("eodhd", quote_result=None)])
    assert router.quote("NOPE", "shares") is None


def test_bars_falls_through_on_an_empty_answer():
    """A provider that returns no bars has not answered the question."""
    empty = FakeProvider("eodhd", bars_result=[])
    useful = FakeProvider("yahoo", bars_result=[_bar("yahoo")])
    router = ProviderRouter([empty, useful])

    bars, provider = router.bars("AAPL", "shares", sessions=15)
    assert provider == "yahoo"
    assert len(bars) == 1
    assert empty.bars_calls == 1 and useful.bars_calls == 1


def test_the_whole_market_feed_is_an_eodhd_only_capability():
    router = ProviderRouter([FakeProvider("yahoo")])
    with pytest.raises(ProviderError) as exc:
        router.whole_market_quotes("US")
    assert exc.value.reason == "no-bulk-provider"


# ---------------------------------------------------------------------------
# Yahoo payload parsing
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status_code=200, text="") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response) -> None:
        self._response = response
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        if isinstance(self._response, list):
            # A queue of responses, so a range ladder can be exercised.
            return self._response.pop(0)
        return self._response


def _yahoo_chart_payload(timestamps, meta=None):
    n = len(timestamps)
    return {
        "chart": {
            "result": [{
                "meta": meta or {"regularMarketPrice": 343.83,
                                 "previousClose": 347.10,
                                 "currency": "USD",
                                 "regularMarketTime": 1761000000},
                "timestamp": timestamps,
                "indicators": {"quote": [{
                    "open": [100.0] * n, "high": [102.0] * n, "low": [99.0] * n,
                    "close": [101.0] * n, "volume": [1000] * n,
                }]},
            }],
            "error": None,
        }
    }


def test_yahoo_quote_reads_price_and_previous_close():
    session = FakeSession(FakeResponse(_yahoo_chart_payload([1761000000])))
    provider = YahooProvider(session=session)
    quote = provider.quote("AAPL", "shares")
    assert quote.price == pytest.approx(343.83)
    assert quote.previous_close == pytest.approx(347.10)
    assert quote.provider == "yahoo"
    assert quote.change_pct == pytest.approx((343.83 / 347.10 - 1.0) * 100.0)


def test_yahoo_drops_the_session_in_progress():
    """A partial session has a partial range and would shrink a stop."""
    now = datetime.now(timezone.utc)
    today_stamp = int(now.timestamp())
    old_stamp = int((now - timedelta(days=3)).timestamp())
    session = FakeSession(FakeResponse(_yahoo_chart_payload([old_stamp, today_stamp])))
    provider = YahooProvider(session=session)
    bars = provider.bars("AAPL", "shares", sessions=10)
    assert len(bars) == 1
    assert bars[0].session < now.date()


def test_yahoo_skips_a_halted_session_rather_than_reading_it_as_zero():
    """A null OHLC row read as zero would put a zero low into a true range."""
    stamps = [int((datetime.now(timezone.utc) - timedelta(days=d)).timestamp())
              for d in (4, 3, 2)]
    payload = _yahoo_chart_payload(stamps)
    quote_block = payload["chart"]["result"][0]["indicators"]["quote"][0]
    quote_block["open"][1] = None   # a halted session
    quote_block["close"][1] = None
    payload["chart"]["result"][0]["indicators"]["quote"][0] = quote_block

    provider = YahooProvider(session=FakeSession(FakeResponse(payload)))
    bars = provider.bars("AAPL", "shares", sessions=10)
    assert len(bars) == 2   # the halted session is absent, not a zero


def test_yahoo_steps_down_the_range_ladder_on_a_422():
    """Lesson 9: Yahoo refuses an over-long range with 422 rather than shortening it."""
    stamps = [int((datetime.now(timezone.utc) - timedelta(days=d)).timestamp())
              for d in (3, 2, 1)]
    session = FakeSession([
        FakeResponse({}, status_code=422, text="range too long"),
        FakeResponse(_yahoo_chart_payload(stamps)),
    ])
    provider = YahooProvider(session=session)
    bars = provider.bars("GEV", "shares", sessions=2)
    assert bars
    assert len(session.calls) == 2
    assert session.calls[0]["params"]["range"] == "730d"
    assert session.calls[1]["params"]["range"] == "2y"


def test_yahoo_renders_an_index_as_a_caret_ticker():
    session = FakeSession(FakeResponse(_yahoo_chart_payload([1761000000])))
    provider = YahooProvider(session=session)
    provider.quote("IXIC", "indices")
    assert session.calls[0]["url"].endswith("^IXIC")


# ---------------------------------------------------------------------------
# Binance payload parsing
# ---------------------------------------------------------------------------

def _binance_klines():
    now = datetime.now(timezone.utc)
    rows = []
    for days_ago in (3, 2, 1, 0):
        stamp = int((now - timedelta(days=days_ago)).timestamp() * 1000)
        rows.append([stamp, "100.0", "110.0", "95.0", "105.0", "1234.5", 0, "0", 0,
                     "0", "0", "0"])
    return rows


def test_binance_quote_reads_last_and_previous_close():
    session = FakeSession(FakeResponse({
        "symbol": "BTCUSDT", "lastPrice": "105000.5", "prevClosePrice": "104000.0",
        "closeTime": 1761000000000,
    }))
    provider = BinanceProvider(session=session)
    quote = provider.quote("BTC-USD", "crypto")
    assert quote.price == pytest.approx(105000.5)
    assert quote.previous_close == pytest.approx(104000.0)
    assert quote.provider == "binance"


def test_binance_tries_the_usdt_variant_when_the_literal_pair_is_unlisted():
    """BTCUSD is not universally listed on spot; BTCUSDT is the liquid one."""
    session = FakeSession([
        FakeResponse({}, status_code=400, text='{"code":-1121,"msg":"Invalid symbol."}'),
        FakeResponse({"symbol": "BTCUSDT", "lastPrice": "105000.5",
                      "prevClosePrice": "104000.0", "closeTime": 1761000000000}),
    ])
    provider = BinanceProvider(session=session)
    quote = provider.quote("BTC-USD", "crypto")
    assert quote.raw_ticker == "BTCUSDT"
    assert [c["params"]["symbol"] for c in session.calls] == ["BTCUSD", "BTCUSDT"]


def test_binance_drops_the_in_progress_candle():
    provider = BinanceProvider(session=FakeSession(FakeResponse(_binance_klines())))
    bars = provider.bars("BTC-USD", "crypto", sessions=5)
    assert len(bars) == 3   # the four rows include today, which is dropped
    assert all(bar.session < datetime.now(timezone.utc).date() for bar in bars)


def test_binance_declines_an_index_without_calling():
    session = FakeSession(FakeResponse({}))
    provider = BinanceProvider(session=session)
    assert provider.supports("indices") is False
    assert session.calls == []
