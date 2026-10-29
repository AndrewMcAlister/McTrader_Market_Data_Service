"""Prove the provider routing against the live services.

Shares must come from EODHD, crypto must come from Binance, and an index must be able to come
from Yahoo. Prints the provider that answered every time, because that is the field a caller
needs in order not to mix tapes silently.
"""
import json
import time

import requests

BASE = "http://127.0.0.1:8003"


def show(label, response, fields=("provider", "price", "previous_close", "change_pct")):
    elapsed = response.elapsed.total_seconds() * 1000
    print(f"\n### {label}  [{response.status_code}]  {elapsed:.0f} ms")
    try:
        payload = response.json()
    except ValueError:
        print("  non-JSON:", response.text[:200])
        return None
    if "symbol" in payload:
        print("  " + "  ".join(f"{f}={payload.get(f)}" for f in fields))
    else:
        print("  " + json.dumps(payload)[:400])
    return payload


show("share AAPL (expect eodhd)", requests.get(f"{BASE}/price/AAPL", timeout=60))
show("share MSFT via Yahoo forced",
     requests.get(f"{BASE}/price/MSFT", timeout=60))
show("crypto BTC-USD (expect binance)",
     requests.get(f"{BASE}/price/BTC-USD",
                  params={"asset_class": "crypto"}, timeout=60))
show("crypto ETH-USD (expect binance)",
     requests.get(f"{BASE}/price/ETH-USD",
                  params={"asset_class": "crypto"}, timeout=60))
show("index IXIC (expect eodhd or yahoo)",
     requests.get(f"{BASE}/price/IXIC",
                  params={"asset_class": "indices"}, timeout=60))

batch = show("batch of shares", requests.post(
    f"{BASE}/prices", json={"symbols": ["NVDA", "SPY"], "venue": "US"}, timeout=90))
if batch:
    for symbol, quote in (batch.get("quotes") or {}).items():
        print(f"    {symbol:<6} provider={quote.get('provider')} price={quote.get('price')}")

crypto_bars = show("crypto bars BTC-USD", requests.post(
    f"{BASE}/bars", json={"symbols": ["BTC-USD"], "sessions": 15,
                          "asset_class": "crypto"}, timeout=90))
if crypto_bars:
    for symbol, entry in (crypto_bars.get("bars") or {}).items():
        print(f"    {symbol:<8} sessions={entry.get('sessions')} "
              f"as_of={entry.get('as_of_session')} provider={entry.get('provider')}")
        rows = entry.get("rows") or []
        if rows:
            print(f"    latest: {rows[-1]}")

crypto_atr = show("crypto ATR + 1x stop", requests.post(
    f"{BASE}/atr", json={"symbols": ["BTC-USD"], "period": 14, "asset_class": "crypto"},
    timeout=120))
if crypto_atr:
    reading = (crypto_atr.get("atr") or {}).get("BTC-USD") or {}
    print(f"    ATR14={reading.get('value')} pct={reading.get('percent')} "
          f"source={reading.get('source')} as_of={reading.get('as_of_session')}")

stats = requests.get(f"{BASE}/stats", timeout=30).json()
print("\n### router provenance")
for entry in (stats.get("router") or {}).get("outcomes", {}).items():
    print("   ", entry)
print("   fallbacks:", (stats.get("router") or {}).get("fallbacks"))
print("   providers:", (stats.get("router") or {}).get("providers"))
