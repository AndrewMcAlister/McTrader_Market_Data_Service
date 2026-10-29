"""The MCP wrapper: the same four data operations, as MCP tools.

Built the way McTrader's own MCP server is built — a thin proxy in front of the REST API, with no
business logic of its own. That matters for a specific reason: if a tool reimplemented a cache
policy or an ATR definition, the two surfaces could disagree, and an MCP client would be answered
by a different implementation than the REST client. A proxy cannot drift.

    stdio          (default)   for a local MCP client
    streamable-http / sse      for a remote one

Launch:
    python -m data_service.mcp_server
    MDS_MCP_TRANSPORT=streamable-http python -m data_service.mcp_server

The base URL of the REST service comes from ``MDS_MCP_BASE_URL`` (default
``http://127.0.0.1:8003``). The service must already be running: this is a client of it, not a
second copy of it.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from .config import load

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8003"
DEFAULT_TIMEOUT = 90.0

mcp = FastMCP(
    name="McTrader_Market_Data_Service",
    instructions=(
        "Market data for the trading books: prices, daily bars and ATR, from one cache and one "
        "ATR definition. Prefer get_prices for a few symbols and get_prices_whole_market for a "
        "scan -- above about a hundred names the whole-market call is up to a hundred times "
        "cheaper. Every answer states which provider served it; do not mix providers within one "
        "decision without noting it."
    ),
)


def _base_url() -> str:
    return (os.getenv("MDS_MCP_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _timeout() -> float:
    try:
        return float(os.getenv("MDS_MCP_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


async def _request(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    """One call to the REST service, with vendor and database failures turned into messages.

    An exception escaping a tool is reported to the MCP client as a protocol error, which reads
    like a broken server rather than a vendor refusal. Returning the reason keeps "EODHD said no"
    distinguishable from "the service is down".
    """
    url = _base_url() + path
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        return {"error": "service-unreachable", "detail": f"{type(exc).__name__}: {exc}",
                "url": url}

    try:
        payload = response.json()
    except ValueError:
        return {"error": "unparseable", "status": response.status_code,
                "detail": response.text[:400]}

    if response.status_code >= 400:
        # The service already answers with {"error": reason, "detail": ...}; pass it through.
        if isinstance(payload, dict):
            payload.setdefault("status", response.status_code)
            return payload
        return {"error": "http-error", "status": response.status_code, "detail": str(payload)[:400]}
    return payload


@mcp.tool(description=(
    "Latest price for ONE symbol, cached for five minutes, with the previous close and the "
    "percentage move between them. `venue` is a venue/country code (US, AU, L, TO, HK, F, PA). "
    "`asset_class` picks the provider: shares (default) and indices/commodities/rates/forex go "
    "to EODHD then Yahoo; crypto goes to Binance first. The answer names the provider that "
    "served it and both ages -- `age_seconds` (our cache) and `as_of_age_seconds` (the vendor's "
    "observation, which can be older). bid/ask are always null: this vendor has no quote side."
))
async def get_price(
    symbol: str,
    venue: str = "US",
    asset_class: Optional[str] = None,
    max_age_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"venue": venue}
    if asset_class:
        params["asset_class"] = asset_class
    if max_age_seconds is not None:
        params["max_age_seconds"] = max_age_seconds
    return await _request("GET", f"/price/{symbol}", params=params)


@mcp.tool(description=(
    "Latest prices for a LIST of symbols in one call, cached for five minutes. The service "
    "picks the cheaper route itself from the list length, because the tariff breaks even at 100 "
    "symbols (1 unit per symbol against 100 flat for the whole market) -- so a 6-symbol request "
    "and a 2,800-symbol request are both priced correctly without the caller knowing about it. "
    "`route` in the answer says which way it went. Symbols the vendor cannot price come back "
    "under `missing`, not as zeros."
))
async def get_prices(
    symbols: List[str],
    venue: str = "US",
    asset_class: Optional[str] = None,
    max_age_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"symbols": symbols, "venue": venue}
    if asset_class:
        body["asset_class"] = asset_class
    if max_age_seconds is not None:
        body["max_age_seconds"] = max_age_seconds
    return await _request("POST", "/prices", json=body)


@mcp.tool(description=(
    "Average True Range for one or more symbols, from the precomputed IndicatorCache, so it "
    "costs no vendor call when it has been calculated for the session. TRUE RANGE IS "
    "GAP-INCLUSIVE: max(High-Low, |High-PrevClose|, |Low-PrevClose|), averaged over the last N "
    "bars -- the standard method, and what every existing implementation in the estate uses. "
    "`method` may be 'tr' (default) or 'high_low', and `smoothing` 'simple' (default, the N-bar "
    "mean) or 'wilder'. Both are echoed back, because a stop level without a stated definition "
    "cannot be reproduced. Supply `price` and `multiplier` together to also get `stop_level`; "
    "the multiplier is never defaulted, since the books use 1.0, 2.0 and 3.0."
))
async def get_atr(
    symbols: List[str],
    period: int = 14,
    method: Optional[str] = None,
    smoothing: Optional[str] = None,
    venue: str = "US",
    asset_class: Optional[str] = None,
    price: Optional[float] = None,
    multiplier: Optional[float] = None,
    direction: str = "long",
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"symbols": symbols, "period": period, "venue": venue,
                            "direction": direction}
    if method:
        body["method"] = method
    if smoothing:
        body["smoothing"] = smoothing
    if asset_class:
        body["asset_class"] = asset_class
    if price is not None:
        body["price"] = price
    if multiplier is not None:
        body["multiplier"] = multiplier
    return await _request("POST", "/atr", json=body)


@mcp.tool(description=(
    "Completed daily bars for one or more symbols, from DailySnapshot, with the vendor called "
    "only when the cache is short. The session still in progress is always excluded: a partial "
    "session has a partial range and using it shrinks a stop exactly when the morning is quiet. "
    "Each answer carries `as_of_session`, `adjusted` (whether the series is split-adjusted) and "
    "`provider`. `allow_fetch=false` answers only from the cache and never calls a vendor."
))
async def get_bars(
    symbols: List[str],
    sessions: int = 15,
    venue: str = "US",
    asset_class: Optional[str] = None,
    allow_fetch: bool = True,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"symbols": symbols, "sessions": sessions, "venue": venue,
                            "allow_fetch": allow_fetch}
    if asset_class:
        body["asset_class"] = asset_class
    return await _request("POST", "/bars", json=body)


@mcp.tool(description=(
    "Is the market data service healthy, and what has it spent? Returns the database it is "
    "pointed at, whether that database is reachable, the cache hit/fetch counters, routing "
    "outcomes per provider, and the vendor units spent since it started. Use it before "
    "concluding that a missing price is a market fact."
))
async def get_service_status() -> Dict[str, Any]:
    return await _request("GET", "/stats")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    transport = (os.getenv("MDS_MCP_TRANSPORT", "stdio").strip().lower() or "stdio")
    if transport not in {"stdio", "streamable-http", "sse"}:
        transport = "stdio"
    log.info("McTrader_Market_Data_Service MCP on %s (transport=%s)", _base_url(), transport)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
