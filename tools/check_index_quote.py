"""Force a cache bypass to check the Yahoo previous-close fix on indices."""
import requests

for symbol, asset_class in (("IXIC", "indices"), ("GSPC", "indices"), ("GLD", "commodities")):
    response = requests.get(
        f"http://127.0.0.1:8003/price/{symbol}",
        params={"asset_class": asset_class, "max_age_seconds": 0.001},
        timeout=90)
    try:
        payload = response.json()
    except ValueError:
        print(f"  {symbol}: non-JSON {response.text[:120]}")
        continue
    print(f"  {symbol:6} [{response.status_code}] provider={payload.get('provider')} "
          f"price={payload.get('price')} prev={payload.get('previous_close')} "
          f"chg={payload.get('change_pct')}")
    # A move > 50% on an index is a data fault, not a move.
    change = payload.get("change_pct")
    if change is not None and abs(change) > 50:
        print(f"         ^ STILL WRONG: {change:.2f}% is not a plausible index move")
