"""The whole-market snapshot must carry volume through to ProviderQuote.

Found while wiring todays-big-movers: `/real-time?ex=US` rows include `volume`, the
provider parsed every other field and dropped that one, so SymbolPriceCache held volume 0
for every US name -- and a movers query that requires volume > 0 would have returned two
empty lists forever, looking like a quiet market rather than a dropped field.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.providers.eodhd_provider import EodhdProvider  # noqa: E402


class FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def real_time_whole_market(self, market):
        return self._rows


def provider_with(rows):
    return EodhdProvider(FakeClient(rows))


def test_volume_survives_the_whole_market_parse():
    provider = provider_with([
        {"code": "ADT.US", "close": 9.86, "previousClose": 9.27, "volume": 163_754_638,
         "timestamp": 1793398800},
    ])
    quotes = provider.whole_market_quotes("US")
    assert len(quotes) == 1
    assert quotes[0].symbol == "ADT"
    assert quotes[0].volume == 163_754_638


def test_a_row_without_volume_is_zero_rather_than_rejected():
    provider = provider_with([{"code": "XYZ.US", "close": 1.0, "previousClose": 0.9}])
    quotes = provider.whole_market_quotes("US")
    assert quotes[0].volume == 0


def test_a_row_with_no_price_is_still_dropped():
    provider = provider_with([{"code": "BAD.US", "close": 0, "volume": 1000}])
    assert provider.whole_market_quotes("US") == []
