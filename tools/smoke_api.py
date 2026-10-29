"""Smoke-test the four endpoints against a running service.

Spends a few vendor units on purpose (prices and bars for a handful of symbols); writes
nothing to the database. Run with the service up on 127.0.0.1:8003.
"""
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8003"


def call(method, path, body=None, label=""):
    url = BASE + path
    t0 = time.perf_counter()
    try:
        if method == "GET":
            response = requests.get(url, timeout=90)
        else:
            response = requests.post(url, json=body, timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"\n### {label or path}\n  TRANSPORT FAILURE: {type(exc).__name__}: {exc}")
        return None
    ms = (time.perf_counter() - t0) * 1000
    print(f"\n### {label or path}   [{method} {path}]  {response.status_code}  {ms:.0f} ms")
    try:
        payload = response.json()
    except ValueError:
        print("  non-JSON:", response.text[:400])
        return None
    return payload


def summarise_quote(quote):
    if not quote:
        return
    print(f"  {quote.get('symbol'):<8} price={quote.get('price')} "
          f"prev={quote.get('previous_close')} chg={quote.get('change_pct')} "
          f"provider={quote.get('provider')} age={quote.get('age_seconds')}s "
          f"as_of_age={quote.get('as_of_age_seconds')}s")


# 1. health
health = call("GET", "/healthz", label="healthz")
if health:
    print(json.dumps(health, indent=2))

# 2. one price
price = call("GET", "/price/AAPL", label="one price")
summarise_quote(price)

# 3. a batch small enough to go per-symbol
batch = call("POST", "/prices",
             {"symbols": ["MSFT", "NVDA", "SPY"], "venue": "US"}, label="small batch")
if batch:
    print(f"  route={batch.get('route')} count={batch.get('count')} "
          f"fetched={batch.get('fetched')} missing={batch.get('missing')}")
    for symbol, quote in (batch.get("quotes") or {}).items():
        summarise_quote(quote)

# 4. cache behaviour: the same batch again must cost nothing
cached = call("POST", "/prices",
              {"symbols": ["MSFT", "NVDA", "SPY"], "venue": "US"}, label="same batch again (cache)")
if cached:
    print(f"  route={cached.get('route')} fetched={cached.get('fetched')}  "
          f"(empty fetched = cache hit)")

# 5. bars from the cache seeded by the daily job
bars = call("POST", "/bars", {"symbols": ["AAPL", "MSFT"], "sessions": 15}, label="bars")
if bars:
    for symbol, entry in (bars.get("bars") or {}).items():
        print(f"  {symbol:<6} sessions={entry.get('sessions')} "
              f"as_of_session={entry.get('as_of_session')} adjusted={entry.get('adjusted')} "
              f"source={entry.get('source')}")
        rows = entry.get("rows") or []
        if rows:
            print(f"         latest row: {rows[-1]}")

# 6. ATR from the indicator cache, no stop
atr = call("POST", "/atr", {"symbols": ["AAPL", "MSFT", "NVDA"], "period": 14}, label="atr")
if atr:
    for symbol, reading in (atr.get("atr") or {}).items():
        print(f"  {symbol:<6} value={reading.get('value')} pct={reading.get('percent')} "
              f"period={reading.get('period')} method={reading.get('method')} "
              f"smoothing={reading.get('smoothing')} sessions={reading.get('sessions_used')} "
              f"as_of={reading.get('as_of_session')} source={reading.get('source')}")

# 7. ATR + stop level, the actual requirement
stop = call("POST", "/atr",
            {"symbols": ["AAPL"], "period": 14, "price": 231.40, "multiplier": 1.0,
             "direction": "long"},
            label="atr + 1x stop")
if stop:
    reading = (stop.get("atr") or {}).get("AAPL") or {}
    print(f"  AAPL ATR14={reading.get('value')}  price=231.40  "
          f"1x stop={reading.get('stop_level')}")

# 8. refusals must be reasons, not zeros
bad = call("POST", "/atr", {"symbols": ["AAPL"], "period": 14, "price": 231.40},
           label="stop without multiplier (must refuse)")
if bad:
    print("  ", json.dumps(bad)[:200])

bad2 = call("GET", "/price/ZZZZ", label="unknown symbol (must refuse or report missing)")
if bad2:
    print("  ", json.dumps(bad2)[:200])

# 9. what has been spent
stats = call("GET", "/stats", label="stats")
if stats:
    print("  counters:", json.dumps(stats.get("counters"), indent=2))
    print("  vendor  :", json.dumps(stats.get("vendor")))
    print("  database:", json.dumps(stats.get("database")))
