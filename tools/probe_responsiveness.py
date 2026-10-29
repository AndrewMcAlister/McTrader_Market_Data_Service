"""Prove the service stays responsive while the daily job is hammering the database.

Before the async->sync fix, a blocking DB call inside an `async def` endpoint serialised the
whole event loop: /healthz timed out at 90 s and the next request took 36 s. This fires
health checks concurrently with a real data call and fails if any of them is slow.
"""
import concurrent.futures
import statistics
import time

import requests

BASE = "http://127.0.0.1:8003"


def timed(path, method="GET", body=None, timeout=120):
    t0 = time.perf_counter()
    try:
        if method == "GET":
            response = requests.get(BASE + path, timeout=timeout)
        else:
            response = requests.post(BASE + path, json=body, timeout=timeout)
        return response.status_code, (time.perf_counter() - t0) * 1000
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}", (time.perf_counter() - t0) * 1000


print("=== /healthz alone x5 ===")
health = [timed("/healthz") for _ in range(5)]
for status, ms in health:
    print(f"  {status}  {ms:.0f} ms")
codes = [s for s, _ in health]
lat = [ms for _, ms in health]
print(f"  all 200: {all(c == 200 for c in codes)}   median {statistics.median(lat):.0f} ms")

print("\n=== 8 concurrent /healthz while a /bars call runs ===")
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    bars_future = pool.submit(timed, "/bars", "POST", {"symbols": ["AAPL"], "sessions": 15})
    health_futures = [pool.submit(timed, "/healthz") for _ in range(8)]
    bars_result = bars_future.result()
    health_results = [f.result() for f in health_futures]

print(f"  /bars      {bars_result[0]}  {bars_result[1]:.0f} ms")
for status, ms in health_results:
    print(f"  /healthz   {status}  {ms:.0f} ms")
worst = max(ms for _, ms in health_results)
print(f"  worst healthz: {worst:.0f} ms  -> {'OK' if worst < 5000 else 'TOO SLOW'}")

assert all(s == 200 for s, _ in health_results), "a health check did not return 200"
assert worst < 5000, f"health check blocked for {worst:.0f} ms"
print("\nPASS: health checks stayed responsive under load")
