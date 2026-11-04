"""The four symbol endpoints accept a country code, and a refusal is a 400 with a reason.

The gap these tests exist for: ``country_code`` is what ``/todays-data-is-available`` and
``/todays-big-movers`` speak, so a book that reads a country from one of those had no way to
pass it to ``/atr`` or ``/prices`` -- and sending it anyway was silently ignored, which is a
US price for an ASX ticker. The endpoint surface is what a caller actually types, so it is
pinned here rather than only in the product layer.

The app is used without its lifespan: these tests are about request parsing and error
rendering, and starting the real one would open a database and a scheduler.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient

from data_service.api import create_app
from data_service.venues import resolve_venue


class FakeProducts:
    """Stands in for the product layer: resolves the market, records what it was told."""

    def __init__(self) -> None:
        self.seen: List[Dict[str, Any]] = []

    def _market(self, symbols: List[str], venue: Optional[str],
                country_code: Optional[str], asset_class: Optional[str] = None) -> str:
        self.seen.append({"symbols": list(symbols), "venue": venue,
                          "country_code": country_code})
        return resolve_venue(venue, country_code, symbols, asset_class)

    def price(self, symbol: str, venue=None, country_code=None, **_):
        market = self._market([symbol], venue, country_code)
        return {"symbol": symbol, "venue": market, "price": 1.0}

    def prices(self, symbols, venue=None, country_code=None, **_):
        market = self._market(list(symbols), venue, country_code)
        return {"quotes": {}, "venue": market, "missing": [], "complete": True}

    def bars(self, symbols, venue=None, country_code=None, **_):
        market = self._market(list(symbols), venue, country_code)
        return {"bars": {}, "venue": market, "complete": True}

    def atr_batch(self, symbols, venue=None, country_code=None, **_):
        market = self._market(list(symbols), venue, country_code)
        return {"atr": {}, "venue": market, "complete": True}


def _client(products: Any) -> TestClient:
    app = create_app()
    app.state.products = products
    return TestClient(app)


def test_every_symbol_endpoint_takes_a_country_code():
    products = FakeProducts()
    client = _client(products)

    assert client.get("/price/CBA", params={"country_code": "AU"}).json()["venue"] == "AU"
    assert client.post("/prices", json={"symbols": ["CBA"], "country_code": "AU"}).json()[
        "venue"] == "AU"
    assert client.post("/atr", json={"symbols": ["CBA"], "country_code": "AU"}).json()[
        "venue"] == "AU"
    assert client.post("/bars", json={"symbols": ["CBA"], "country_code": "AU"}).json()[
        "venue"] == "AU"
    assert [row["country_code"] for row in products.seen] == ["AU"] * 4


def test_a_symbol_suffix_states_the_market_when_no_country_does():
    client = _client(FakeProducts())
    response = client.post("/atr", json={"symbols": ["CBA.AU"]})
    assert response.status_code == 200
    assert response.json()["venue"] == "AU"


def test_an_unknown_country_code_is_a_400_with_the_reason():
    client = _client(FakeProducts())
    response = client.post("/prices", json={"symbols": ["CBA"], "country_code": "ZZ"})
    assert response.status_code == 400
    assert response.json()["error"] == "unknown-venue"


def test_a_country_that_contradicts_the_symbol_is_a_400_not_a_guess():
    client = _client(FakeProducts())
    response = client.get("/price/CBA.AU", params={"country_code": "US"})
    assert response.status_code == 400
    assert response.json()["error"] == "venue-conflict"
