# McTrader_Market_Data_Service — interface reference

Everything needed to call this service. No rationale, no history: request and response shapes,
error codes, and the traps that produce wrong code if ignored.

- **Base URL:** `http://127.0.0.1:8003`
- **Auth:** none. Localhost only.
- **Content type:** `application/json` on every POST.
- **OpenAPI:** `GET /docs`
- **Python package:** `data_service` (the project directory is `McTrader_Market_Data_Service`; the product
  name and the package name differ deliberately)

---

## 1. `GET /price/{symbol}`

Latest price for one symbol.

**Query parameters**

| Name | Type | Default | Notes |
|---|---|---|---|
| `venue` | string | `US` | Venue/country code. `US`, `AU`/`ASX`, `L`/`LSE`, `TO`, `HK`, `F`, `PA`. Ignored for crypto. |
| `asset_class` | string | `shares` | `shares`, `crypto`, `indices`, `commodities`, `rates`, `forex`. Chooses the provider. |
| `max_age_seconds` | float | `300` | **Lowers** the cache window; cannot raise it. |

**200 response**

```json
{
  "symbol": "AAPL",
  "price": 343.83,
  "previous_close": 347.10,
  "change_pct": -0.9421,
  "bid": null,
  "ask": null,
  "provider": "eodhd",
  "fetched_at": "2026-10-29T04:34:16.685844",
  "age_seconds": 0.381,
  "as_of": "2026-10-28T20:29:00",
  "as_of_age_seconds": 25424.31
}
```

**Errors**

| Status | Body `error` | Meaning |
|---|---|---|
| 404 | `unpriced` | nothing could price it |
| 502 | vendor reason | the vendor refused |
| 503 | `vendor-unavailable` | the service has no vendor client configured |
| 503 | `database-busy` | transient lock; **retry** |

`bid`/`ask` are always `null` — the vendor has no quote side.
`age_seconds` is our cache age; `as_of_age_seconds` is the vendor's observation age, which can be
hours. Gate freshness on `age_seconds`.

---

## 2. `POST /prices`

Latest prices for many symbols in one call. **This is also the whole-market call** — there is no
separate endpoint.

**Request**

```json
{ "symbols": ["AAPL", "MSFT"], "venue": "US", "asset_class": null, "max_age_seconds": 300 }
```

**200 response**

```json
{
  "quotes": {
    "AAPL": { "symbol": "AAPL", "price": 343.83, "previous_close": 347.10, "change_pct": -0.9421,
              "bid": null, "ask": null, "provider": "eodhd",
              "fetched_at": "2026-10-29T04:34:16", "age_seconds": 0.4,
              "as_of": "2026-10-28T20:29:00", "as_of_age_seconds": 25424.3 }
  },
  "fetched": ["AAPL"],
  "missing": [],
  "deferred": [],
  "complete": true,
  "route": "per_symbol",
  "requested": 2,
  "count": 2
}
```

| Field | Meaning |
|---|---|
| `route` | `per_symbol` or `bulk` — which vendor path was used, chosen from the list length |
| `missing` | symbols the vendor could not price. **Not cached**, so a later call retries them |
| `deferred` | symbols not reached before the deadline; ask again |
| `complete` | `false` when anything is missing or deferred |

**The route is automatic and it matters:** the vendor bills 1 unit per symbol, or 100 units flat for
the whole US market. Below 100 symbols per-symbol is cheaper; at 100 or more, bulk wins by up to 100×.
Send the list you need and let the service decide.

**Errors:** 400 `no-symbols`, 502 vendor, 503 `database-busy`.

---

## 3. `POST /atr`

Average True Range, from the precomputed `IndicatorCache` when it can answer (costs no vendor call),
else computed from daily bars.

**Request**

```json
{
  "symbols": ["AAPL"],
  "period": 14,
  "method": "tr",
  "smoothing": "simple",
  "venue": "US",
  "asset_class": null,
  "price": 343.83,
  "multiplier": 1.0,
  "direction": "long"
}
```

| Field | Default | Notes |
|---|---|---|
| `period` | `14` | |
| `method` | `tr` | `tr` = gap-inclusive true range (the standard); `high_low` = High−Low only |
| `smoothing` | `simple` | `simple` = mean of the last N ranges (the standard); `wilder` |
| `price` | none | supply **with** `multiplier` to also get `stop_level` |
| `multiplier` | **none** | **never defaulted** — the books use 1.0, 2.0 and 3.0 |
| `direction` | `long` | `long` places the stop below, `short` above |

**200 response**

```json
{
  "atr": {
    "AAPL": {
      "symbol": "AAPL", "value": 6.18285, "percent": 1.7982, "reference_price": 343.83,
      "period": 14, "method": "tr", "smoothing": "simple",
      "sessions_used": 51, "as_of_session": "2026-10-28",
      "source": "indicator_cache", "reason": null,
      "stop_level": 337.64715
    }
  },
  "period": 14, "method": "tr", "smoothing": "simple", "complete": true
}
```

| Field | Meaning |
|---|---|
| `value` | ATR in **price units**; `null` when history is insufficient |
| `percent` | `value / reference_price * 100` — percent points (1.7982 means 1.80%) |
| `reference_price` | the close the ATR was measured against |
| `source` | `indicator_cache`, `computed`, or `unavailable` |
| `reason` | why `value` is `null` |
| `stop_level` | present only when `price` and `multiplier` were both supplied |

**The ATR definition, exactly:**

```
TR      = max(High - Low, |High - PreviousClose|, |Low - PreviousClose|)
ATR(n)  = sum(TR for the last n bars) / n
```

**Errors:** 400 `bad-method`, `bad-smoothing`, `bad-period`, `incomplete-stop` (price without
multiplier), `no-symbols`; 502 vendor.

---

## 4. `POST /bars`

Completed daily bars.

**Request**

```json
{ "symbols": ["AAPL"], "sessions": 15, "venue": "US", "asset_class": null, "allow_fetch": true }
```

**200 response**

```json
{
  "bars": {
    "AAPL": {
      "symbol": "AAPL", "venue": "US",
      "as_of_session": "2026-10-28",
      "sessions": 15,
      "adjusted": true,
      "provider": "cache",
      "source": "cache",
      "rows": [
        { "d": "2026-10-08", "o": 250.1, "h": 254.9, "l": 249.2, "c": 253.7, "v": 51200000 }
      ]
    }
  },
  "fetched": [], "short_history": [], "complete": true, "requested_sessions": 15
}
```

| Field | Meaning |
|---|---|
| `as_of_session` | the last completed session in `rows` |
| `adjusted` | whether the series is split-adjusted |
| `provider` | `cache` or the provider that answered (`eodhd`, `yahoo`, `binance`) |
| `short_history` | symbols with fewer sessions than asked |

`allow_fetch: false` answers only from the database and never calls a vendor.

**The session in progress is already excluded.** Do not filter it again downstream.

**Errors:** 400 `bad-sessions`, `no-symbols`; 502 vendor.

---

## 5a. `POST /refresh` — pull the whole market now

```json
{"market": "US", "session_date": null, "force": false, "include_indicators": true}
```

| Field | Default | Notes |
|---|---|---|
| `market` | `US` | only US is measured |
| `session_date` | `null` | `YYYY-MM-DD` for a dated backfill; bypasses the freshness check |
| `force` | `false` | pull even though a bulk pull is already recorded today |
| `include_indicators` | `true` | rebuild `IndicatorCache` afterwards |

| Status | HTTP | Meaning |
|---|---|---|
| `refreshed` | **202** | the pull ran; body carries sessions, rows, units, duration |
| `already-fresh` | 200 | a bulk pull is recorded today; **spent nothing**, returns in ms |
| `busy` | 409 | another refresh holds the lock; ask again later |
| `no-vendor-key` | 503 | no EODHD key configured |

**Cost: 100 units and about 30 seconds.** That is one flat-priced `/eod-bulk-last-day` call, plus the
ATR rebuild (~2,866 symbols). Pull-only is about 10 seconds.

**It is idempotent** — the pull upserts by (symbol, session), so running it twice is harmless — and it
**does not double-spend**: a non-blocking lock makes the second concurrent caller `409 busy`.

`expected_session` and `behind_expected` are the useful fields for monitoring. `expected_session` is
what `data_service.sessions` says the venue's clock allows; `behind_expected: true` means the vendor
returned an older session, i.e. lag or a holiday — visible rather than silent.

**Send `X-MarketData-Caller`.** The 100 units land in the spend ledger against it, which is the only
way to tell an automatic refresh from a manual one later.

---

## 5. Operations

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | `{"status","version","vendor_configured","key_source","database","database_ok","database_error"}` |
| `GET /stats` | counters, process vendor units, row counts, and **`usage_today`** from the spend ledger |
| `POST /admin/shutdown` | stop the process from inside it — what `stop.bat` calls |
| `GET /docs` | OpenAPI UI |

### Tell it who you are: `X-MarketData-Caller`

Every data endpoint records the vendor spend it caused against this header, falling back to the path
when it is absent:

```powershell
Invoke-RestMethod http://127.0.0.1:8003/price/AAPL -Headers @{ 'X-MarketData-Caller' = 'minictrader' }
```

It costs nothing and makes the shared quota accountable. Send it:

```
caller        request type   calls  symbols  cost
mctrader      /real-time/        1        1     1
minictrader   /real-time/        1        1     1
http:/price   /real-time/        1        1     1     <- no header, attributed by path
```

Without it the row still lands, attributed to the path. That is deliberate — an unattributed call is
a question worth asking, whereas a call that vanished answers nothing.

**Spend is priced from `dbo.RequestTypes`, not from the code.** The four bases behave differently and
getting one wrong is silent: `per_symbol` multiplies by the batch (3 tickers = 3 units, so batching
saves time and not quota), while `flat` and `per_call` ignore it entirely (the whole-market feed is 100
whether it returns 527 rows or 12,737).

**A failed call is still billed.** A 4xx is recorded before the error is raised, because a rejected
request costs quota — measured: a 404 from `/eod/` cost 1 and a 404 from `/intraday/` cost 5. Only a
transport failure, where the request never landed, is free and unrecorded.

---

## 6. Provider routing — read this before interpreting `provider`

| `asset_class` | Order |
|---|---|
| `shares` (default) | EODHD → Yahoo |
| `indices`, `commodities`, `rates`, `forex` | EODHD → Yahoo |
| `crypto` | **Binance** → EODHD → Yahoo |

Every quote and bar declares the provider that answered. **Two calls for the same symbol can be
answered by different providers.** That is allowed, but it must be visible: never compute a change
percentage by mixing a price from one provider with a previous close from another.

A provider that cannot serve an asset class is never asked — Binance is never asked for an index.

---

## 7. Traps

1. **`multiplier` is never defaulted.** Omitting it with `price` is a 400, by design.
2. **`value: null` is not an error** — it means insufficient history. Read `reason`.
3. **`percent` is percent points**, not a fraction: `1.80`, not `0.018`. Do not multiply by 100 again.
4. **Do not keep your own cache.** Prices live 300 s here, bars are keyed on the completed session,
   ATR is precomputed. A second cache with its own TTL will disagree after any restart.
5. **`503 database-busy` is retryable.** It means the daily load held a lock; it clears in seconds.
6. **Symbol format:** bare (`AAPL`) with `venue`, or already-suffixed (`AAPL.US`). Crypto pairs are
   bare or dashed (`BTC-USD`); `venue` is ignored for crypto.
7. **Crypto needs `asset_class: "crypto"`**, or it is treated as a share and priced from the wrong feed.
8. **Whole-market responses are large** (~12,700 rows, ~2 MB). If you only need the top movers, sort
   locally rather than shortening the list — a shorter list can drop below the 100-symbol break-even
   and become *more* expensive.

---

## 8. Minimal Python client

```python
import requests

BASE = "http://127.0.0.1:8003"

def price(symbol, venue="US", asset_class="shares"):
    r = requests.get(f"{BASE}/price/{symbol}",
                     params={"venue": venue, "asset_class": asset_class}, timeout=30)
    r.raise_for_status()
    return r.json()

def atr_with_stop(symbol, price_value, multiplier=1.0, period=14):
    r = requests.post(f"{BASE}/atr", json={
        "symbols": [symbol], "period": period,
        "price": price_value, "multiplier": multiplier, "direction": "long",
    }, timeout=60)
    r.raise_for_status()
    return r.json()["atr"][symbol]        # -> {"value", "percent", "stop_level", ...}
```

MCP clients use the same four operations as tools: `get_price`, `get_prices`, `get_atr`, `get_bars`,
plus `get_service_status`.
