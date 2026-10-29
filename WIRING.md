# Wiring McTrader to McTrader_Market_Data_Service

A checklist for the next step. Read §1 first: it is the one instruction that is easy to get wrong
and expensive to get wrong.

---

## 1. Delete McTrader's cache. All of it.

**Caching belongs in this service.** Once McTrader's external leg points at `:8003`, every cache
left behind in McTrader is a *second* cache for the same data with its own TTL, and the two will
disagree at exactly the moment it matters.

| In McTrader | What happens to it |
|---|---|
| `services/quote_cache.py` (`QuoteCache`) | **delete** — its job is now `SymbolPriceCache` |
| `services/eodhd_price_service.py` (`EodhdPriceService`) | **delete** — its job is `POST /prices` |
| the 90-second bar memo in `market_data_service` | **delete** — `DailySnapshot` is keyed on the completed session, which is a better key than a clock |
| the 600-second negative price memo | **delete** — `missing` symbols are not cached here either, so a retry is already free |
| `BULK_PRICE_ENABLED`, `services/bulk_price_route.py` | **retire** — `POST /prices` picks bulk vs per-symbol itself from the list length |

What must stay in McTrader: `MARKET_DATA_PROVIDER_PRIORITY` (the broker-first policy), the broker
leg, and `EodhdUsage` *reads* (the ledger is still the accounting source).

Why this matters concretely: a 300-second TTL here and a 300-second TTL there do not expire
together. The first request after a restart gets one answer from each, and a stop computed from a
stale price is indistinguishable from a stop computed from a fresh one.

---

## 2. The adapter

McTrader cannot run an ABC over HTTP. The pattern is a **6-method proxy** implementing
`IMarketDataProvider`, which forwards to the four endpoints:

```python
class HttpMarketDataProvider(IMarketDataProvider):
    # get_current_price  -> GET  /price/{symbol}
    # get_latest_trade    -> GET  /price/{symbol}
    # get_latest_quote    -> GET  /price/{symbol}
    # get_bars            -> POST /bars
    # is_market_open      -> answered locally (no vendor call, no network hop)
    # get_market_clock    -> answered locally
```

`is_market_open` and `get_market_clock` are calendar logic — do **not** proxy them. Three of the
six methods are answerable without a round trip.

Slot it into `FallbackMarketDataAdapter` as the external leg, replacing the direct `EODHDClient`
calls. `MarketDataService`, `IBrokerAdapter` and `AlpacaMarketDataAdapter` are untouched.

---

## 3. Caller attribution — do not skip this

Every request should carry who is asking:

```
X-MarketData-Caller: mctrader
```

Without it the ledger cannot attribute spend, and R6 in `requirements.md` fails while looking
perfectly fine. The service reports unattributed spend as `mktdata.unattributed` and counts it, so
the gap is visible rather than absorbed — but visible is not the same as correct.

---

## 4. The four endpoints

### `GET /price/{symbol}?venue=US&asset_class=&max_age_seconds=`

```json
{ "symbol": "AAPL", "price": 343.83, "previous_close": 347.10, "change_pct": -0.9421,
  "bid": null, "ask": null,
  "provider": "eodhd", "fetched_at": "2026-10-29T04:34:16", "age_seconds": 0.4,
  "as_of": "2026-10-28T20:29:00", "as_of_age_seconds": 25424.3 }
```

404 `{"error": "unpriced", ...}` when nothing can price it.

### `POST /prices`

```json
{ "symbols": ["AAPL","MSFT"], "venue": "US", "asset_class": null, "max_age_seconds": 300 }
```

Answers with `quotes`, `fetched`, `missing`, `deferred`, `route`, `complete`.

**`route` is the important field.** The service chooses bulk or per-symbol from the list length
because the tariff breaks even at 100 symbols. A 2,757-name scan costs 100 units; six positions
cost 6. Neither caller has to know.

### `POST /atr`

```json
{ "symbols": ["AAPL"], "period": 14, "method": "tr", "smoothing": "simple",
  "venue": "US", "asset_class": null,
  "price": 343.83, "multiplier": 1.0, "direction": "long" }
```

Answers with `value`, `percent`, `reference_price`, `period`, `method`, `smoothing`,
`sessions_used`, `as_of_session`, `source`, and `stop_level` when price **and** multiplier are
supplied.

- **`multiplier` is never defaulted.** It comes from `STOP_RULES.ATR_STOP_MULTIPLIER` in the broker
  config, which currently reads `1.0` on both broker rows. Pass it explicitly.
- 400 `{"error": "incomplete-stop"}` if `price` is given without `multiplier`.
- `value: null` with a `reason` when there is too little history — never a guess.

### `POST /bars`

```json
{ "symbols": ["AAPL"], "sessions": 15, "venue": "US", "allow_fetch": true }
```

Answers with `as_of_session`, `sessions`, `adjusted`, `provider`, `rows[]`.

**The session in progress is already excluded.** Do not filter it again downstream; filtering twice
is harmless but it hides the fact that the guarantee lives in the service.

---

## 5. Error handling

| Status | Meaning | What to do |
|---|---|---|
| 400 | bad request (incomplete stop, bad method, no symbols) | fix the call — it will not succeed on retry |
| 404 | that symbol cannot be priced | mark it missing, move on |
| 502 | the vendor refused | retry later, or fall back to the broker |
| 503 `database-busy` | the daily load held a lock | **retry** — it is transient and short |

`503 database-busy` is the one worth wiring deliberately: a read that could not get a lock in 15
seconds returns immediately instead of hanging. Treat it as retryable rather than as an outage.

---

## 6. Gotchas that will bite

1. **The ATR method is `tr` + `simple`** — gap-inclusive true range, mean of the last N. That is
   the agreed standard and the service default; `method`/`smoothing` are echoed back on every
   response. Do not assume Wilder.
2. **`provider` may differ between two calls for the same symbol.** EODHD is primary, Yahoo is the
   fallback. A quote from Yahoo and a bar from EODHD is allowed, but it must be visible — that is
   what the field is for. Do not compute a change percentage by mixing providers.
3. **`as_of_age_seconds` is not `age_seconds`.** The first is when the vendor observed the price
   (measured hours behind for EODHD's US feed); the second is when the service fetched it. Gate
   freshness on `age_seconds`, report the other.
4. **`POST /prices` with very long lists returns a large payload** (~12,700 rows for the whole
   market, ~2 MB). If McTrader only needs the top N movers, sort locally rather than asking for a
   narrower list — a narrower list may drop below the 100-symbol break-even and become *more*
   expensive.
5. **Crypto uses `asset_class: "crypto"`.** A bare `venue` is meaningless there and is ignored.

---

## 7. Checklist

- [ ] `HttpMarketDataProvider` implements the 6 methods; calendar methods answered locally
- [ ] `X-MarketData-Caller: mctrader` on every request
- [ ] `QuoteCache` deleted
- [ ] `EodhdPriceService` deleted
- [ ] 90-second bar memo deleted
- [ ] `BulkPriceRoute` retired
- [ ] `ATR_STOP_MULTIPLIER` read from broker config and passed explicitly
- [ ] 503 `database-busy` treated as retryable
- [ ] Kill switch: `D:\market-data\NO-MKTDATA` present → direct vendor (per the `NO-BULK` precedent,
      which treats an *unreadable* switch as "off")
- [ ] Verified: a McTrader cycle prices and stops with `:8003` stopped and the kill switch set
