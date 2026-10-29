"""Prove the bulk routing: 150 symbols should cost 100 units, not 150.

Takes real symbols from the latest daily session, sends them to POST /prices, and reports the
route plus the vendor units the service spent. One vendor call at 100 units, so run it once.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import config
from data_service.store import Store

settings = config.load()
store = Store(settings.connection_string)

latest = store.latest_session()
conn = store.connect()
cur = conn.cursor()
cur.execute("""
    SELECT TOP (150) d.SymbolCode
    FROM dbo.DailySnapshot d
    JOIN dbo.Symbol s ON s.SymbolCode = d.SymbolCode
    WHERE d.SnapshotDate = ?
    ORDER BY s.DollarVolume DESC
""", latest)
symbols = [str(r[0]) for r in cur.fetchall()]
conn.close()
print(f"latest session {latest}; {len(symbols)} symbols from the widest tier")
print(f"first five: {symbols[:5]}")


def quota():
    url = f"{settings.eodhd_base_url}/user?api_token={settings.eodhd_api_key}&fmt=json"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response).get("apiRequests")


before = quota()
print(f"\nvendor counter before: {before}")

stats_before = requests.get("http://127.0.0.1:8003/stats", timeout=30).json()
units_before = stats_before["vendor"]["units"]

t0 = time.perf_counter()
response = requests.post(
    "http://127.0.0.1:8003/prices", json={"symbols": symbols, "venue": "US"}, timeout=180)
elapsed = (time.perf_counter() - t0) * 1000
payload = response.json()

print(f"POST /prices  {response.status_code}  {elapsed:.0f} ms")
print(f"  route          : {payload.get('route')}")
print(f"  requested      : {payload.get('requested')}")
print(f"  count          : {payload.get('count')}")
print(f"  missing        : {len(payload.get('missing') or [])}")

stats_after = requests.get("http://127.0.0.1:8003/stats", timeout=30).json()
units_after = stats_after["vendor"]["units"]
after = quota()

print(f"\n  service units this request : {units_after - units_before}")
print(f"  vendor counter before/after: {before} / {after}  (delta {after - before})")
print(f"  per-symbol would have cost : {len(symbols)}")

assert payload.get("route") == "bulk", "expected the bulk route for 150 symbols"
print("\nPASS: 150 symbols routed through one whole-market call")
