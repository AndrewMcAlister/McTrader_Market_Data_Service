# McTrader_Market_Data_Service

One price cache, one daily bar cache, one ATR definition, one EODHD key — behind four
endpoints, for Mini-McTrader, PaperTrader, PortfolioAdvisor and McTrader.

```
Mini-McTrader ─┐
PaperTrader   ─┤
PortfolioAdv. ─┼──▶  McTrader_Market_Data_Service :8003  ──▶  EODHD
McTrader      ─┘        │
                        ├── SymbolPriceCache   (5-minute prices)
                        ├── DailySnapshot      (daily bars, one row per symbol-session)
                        ├── IndicatorCache     (precomputed ATR)
                        └── Daily job ──▶ /eod-bulk-last-day ──▶ DailySnapshot
```

---

> **Three names, deliberately different.** The product and the repository are
> `McTrader_Market_Data_Service`. The Python package is `data_service` — it was `market_data_service`,
> which collided with McTrader's own `services/market_data_service.py`. So `python -m data_service` is
> the entry point while the thing it starts is called `McTrader_Market_Data_Service`.

---

## Caching is this service's job

Every cache that matters lives here:

| Cache | Where | Lifetime |
|---|---|---|
| prices | `SymbolPriceCache` + an in-memory whole-market snapshot | 300 s / one cycle |
| daily bars | `DailySnapshot` | keyed on the completed session, not a clock |
| ATR | `IndicatorCache` | rebuilt after each daily load |

**A consumer must not keep its own.** Two caches for one dataset with independent TTLs do not expire
together, and the first request after a restart gets one answer from each — a stop computed from a
stale price is indistinguishable from a stop computed from a fresh one. `WIRING.md` §1 lists exactly
what to delete in McTrader.

---

## The four endpoints

Every endpoint that takes a symbol also takes the market, because a ticker is not unique across
exchanges (`CBA` is an ASX listing *and* a US one). Say it with `country_code` (`US`, `AU`), with
`venue` (`US`, `AU`/`ASX`, `L`/`LSE`, `TO`, `HK`, `F`, `PA`) or in the symbol itself (`CBA.AU`).
A stated country/venue wins, then the suffix, then `US`; a contradiction is a 400
(`venue-conflict`), because serving either market silently is how two venues' prices get mixed.
Every answer states the market it used.

### `GET /price/{symbol}`

```
GET /price/AAPL?country_code=US
GET /price/CBA?country_code=AU        # or /price/CBA.AU
```
```json
{
  "symbol": "AAPL", "price": 231.4, "previous_close": 229.1, "change_pct": 1.004,
  "bid": null, "ask": null,
  "provider": "eodhd", "fetched_at": "2026-10-29T02:41:07", "age_seconds": 0.4,
  "as_of": "2026-10-29T02:40:31", "as_of_age_seconds": 36.2
}
```

`age_seconds` is when **we** fetched it; `as_of_age_seconds` is when the **vendor** observed
it. They answer different questions and both are returned, because EODHD's US real-time
stamp runs behind the fetch and gating a cache on it would refetch forever.

`bid`/`ask` are always `null`: the vendor's payload carries OHLC and previous close but no
quote side.

### `POST /prices`

```json
{ "symbols": ["AAPL", "MSFT"], "country_code": "US", "max_age_seconds": 300 }
```
```json
{
  "quotes": { "AAPL": { ... }, "MSFT": { ... } },
  "fetched": ["AAPL", "MSFT"], "missing": [], "deferred": [],
  "complete": true, "route": "per_symbol", "requested": 2, "count": 2
}
```

**There is no separate whole-market endpoint: this is it.** The service picks the route from
the list length, because the tariff has a hard break-even at 100 symbols:

| Symbols | Route | Cost |
|---:|---|---:|
| < 100 | `/real-time/{sym}` per symbol | 1 each |
| ≥ 100 | `/real-time/{ph}?ex=US`, whole market | **100 flat** |

So `POST /prices` with 6 symbols costs 6 units and with 2,757 costs 100. A single always-bulk
or always-per-symbol rule is wrong for one of those by up to 100×.

`missing` symbols are **not** cached — a symbol that could not be priced once may price next
minute, and caching the failure would hide a recovery.

### `POST /atr`

```json
{ "symbols": ["AAPL"], "period": 14, "method": "tr", "smoothing": "simple",
  "price": 231.4, "multiplier": 1.0, "direction": "long" }
```
```json
{
  "atr": {
    "AAPL": {
      "value": 5.82, "percent": 2.515, "reference_price": 231.4,
      "period": 14, "method": "tr", "smoothing": "simple",
      "sessions_used": 51, "as_of_session": "2026-10-14",
      "source": "indicator_cache", "stop_level": 225.58
    }
  },
  "period": 14, "method": "tr", "smoothing": "simple", "complete": true
}
```

**The definition is never implicit.** `method` and `smoothing` are echoed on every response,
because the estate currently has four different ATRs in it and a stop level without a stated
definition cannot be reproduced.

- `method`: `tr` (gap-inclusive true range, **default**) or `high_low`. See §ATR below.
- `smoothing`: `simple` (mean of the last N ranges, **default**) or `wilder`.
- `stop_level` is returned only when `price` **and** `multiplier` are both supplied. The
  multiplier is never defaulted: the four books use 1.0, 1.0, 2.0 and 3.0, so a default would
  silently impose one book's risk on another's.

**The ATR is one venue's ATR.** `IndicatorCache` is keyed by `(symbol, exchange)`, so an ASX
`CBA` is never served the US `CBA`'s volatility — which, with a symbol-only key, is exactly
what would have happened. `country_code` (or `venue`, or a `CBA.AU` suffix) is what picks the
series, and `venue` is echoed at the top of the response.

### `POST /bars`

```json
{ "symbols": ["AAPL"], "sessions": 15, "country_code": "US", "allow_fetch": true }
```
```json
{
  "bars": {
    "AAPL": {
      "as_of_session": "2026-10-14", "sessions": 15, "adjusted": true,
      "provider": "cache", "source": "cache",
      "rows": [ { "d": "2026-09-24", "o": 250.1, "h": 254.9, "l": 249.2, "c": 253.7, "v": 51200000 } ]
    }
  },
  "fetched": [], "short_history": [], "complete": true, "requested_sessions": 15
}
```

`country_code`/`venue` selects which series is read, because `DailySnapshot` is keyed by
exchange: `AAPL` at `AU` is a different instrument from `AAPL` at `US`, not a misspelling to
fall back from. A venue whose series is short answers `short_history`; it does not borrow the
other venue's bars. A suffixed symbol (`CBA.AU`) states the same thing when no country is sent.

The session in progress is **dropped here**, in the service, not by each caller — a partial
session has a partial range and using it shrinks a stop exactly when the morning is quiet.
`allow_fetch: false` answers only from `DailySnapshot` and never calls the vendor.

### Infrastructure, keys and stopping

- `GET /healthz` — status, database reachable, vendor configured, and `key_source` (`keyring`,
  `environment` or `none`) so a key problem is visible without reading a log.
- `GET /stats` — row counts, cache hit/fetch counters, vendor units spent this process.
- `GET /docs` — OpenAPI UI.
- `POST` / `GET` / `DELETE /secrets/eodhd` — store, inspect (masked) and remove the vendor key in this
  account's keyring. See "Storing the vendor key at runtime" below.
- `POST /admin/shutdown` — stop this process from inside it, which is what `stop.bat` calls; nothing in
  an interactive shell can terminate a session-0 process.

---

## Providers: three sources, routed per asset class

```
                    ┌── EODHD    (metered, adjusted, whole-market feeds)
POST /prices  ──────┼── Yahoo    (free; indices ^IXIC, futures GC=F, DX-Y.NYB)
POST /atr     ──────└── Binance  (free; crypto pairs, natively quoted)
POST /bars
GET /price/{symbol}
```

The order is per asset class, not one global list:

| `asset_class` | order | why |
|---|---|---|
| `shares` (default) | EODHD → Yahoo | EODHD is adjusted and consistent; Yahoo is the free fallback |
| `indices` / `commodities` / `rates` / `forex` | EODHD → Yahoo | EODHD renders these as `.INDX` / US ETF proxies; Yahoo has real `^IXIC`, `GC=F` |
| `crypto` | **Binance** → EODHD → Yahoo | the pair is quoted natively rather than rendered as `BTC.CC` |

A provider that cannot serve an asset class is never asked. Every quote and bar carries the
`provider` that answered — **that field is load-bearing**, not decoration: this estate has
already been bitten by a silent tape switch (`FALLBACK to Alpaca IEX prices - a different
tape`), and a stop computed on one tape while the level was chosen on another is a silent
error. Combining providers is allowed; doing it invisibly is not.

Two provider quirks are handled deliberately and are worth knowing:

- **Yahoo's `chartPreviousClose` is not the previous close.** On `^IXIC` it returned 14,258
  against a 26,550 price — an 86% "move". The previous close is now read as the second-newest
  close in a short daily series, with a guard that reports *no* reference rather than a false
  one if the ratio is implausible.
- **`BTCUSD` is not universally listed on Binance spot**; `BTCUSDT` is. The rendered pair is
  tried first, then the `USDT` variant, and the pair that answered is in `raw_ticker`.

### What moved and what did not

The provider layer is a port, not an import. McTrader's `YFinanceClient` and
`BinanceMarketDataAdapter` depend on `models.domain.*` (`Quote`, `Bar`, `TradeDTO`,
`MarketClock`) and `LogService`, so vendoring them would drag half of McTrader into a service
whose availability is part of why it exists. What was ported:

| From McTrader | To the service | Notes |
|---|---|---|
| `market_data/provider_symbols.py` | `providers/symbols.py` | dependency-free, so copied faithfully; `tests/test_providers.py` pins the conventions |
| `YFinanceClient` | `providers/yahoo_provider.py` | same chart endpoint, same range-ladder lesson, no pandas |
| `BinanceMarketDataAdapter` | `providers/binance_provider.py` | `ticker/24hr` + `klines`, no domain DTOs |
| `EODHDClient` | `providers/eodhd_provider.py` | wraps the existing client; the two whole-market feeds stay EODHD-only |

---

## MCP wrapper

The same four operations are exposed over MCP, built the way McTrader's own MCP server is built:
**a thin proxy in front of the REST API, with no logic of its own.**

That is deliberate. If a tool reimplemented a cache policy or the ATR definition, the two surfaces
could disagree, and an MCP client would be answered by a different implementation than a REST
client. A proxy cannot drift.

| MCP tool | Proxies to |
|---|---|
| `get_price` | `GET /price/{symbol}` |
| `get_prices` | `POST /prices` |
| `get_atr` | `POST /atr` |
| `get_bars` | `POST /bars` |
| `get_service_status` | `GET /stats` |

```powershell
python -m data_service.mcp_server                       # stdio, for a local client
$env:MDS_MCP_TRANSPORT = 'streamable-http'                     # for a remote one
python -m data_service.mcp_server
```

The REST service must already be running — this is a client of it, not a second copy.
`MDS_MCP_BASE_URL` (default `http://127.0.0.1:8003`) points it elsewhere.

`mcp` is pinned below 2.x: 2.0 renamed `FastMCP` to `MCPServer` and changed other APIs, and this
wrapper mirrors McTrader's, which is written against v1.

---

## ATR: the agreed standard

**The agreed method**, and the service's default:

```
TR  = max(High - Low, |High - PreviousClose|, |Low - PreviousClose|)
ATR(14) = sum(TR for the last 14 bars) / 14
```

Gap-inclusive true range, simple mean over the period. `tests/test_atr.py` asserts this formula
independently of the implementation, so "the standard method" cannot quietly become something else.

Before it was settled, the requirement text said "average high to low". Every implementation in the
estate does something else, and the backtests were measured with it:

| Implementation | Formula |
|---|---|
| `McTrader/Utils/technical_utils.py` `wilder_atr_series` | `max(H-L, \|H-prevClose\|, \|L-prevClose\|)` |
| `PaperTrader/strategies.py` `atr_from_bars` | same |
| `Mini-McTrader/slot1_trader.py` `atr_from_daily` | same |

Measured over 2,850 symbols in `Back_Test.BarHourly`, the gap term makes a 14-session ATR:

| | p05 | p25 | **median** | p75 | p95 |
|---|---:|---:|---:|---:|---:|
| true range ÷ (H−L) | 1.001 | 1.018 | **1.043** | 1.085 | **1.258** |

So `high_low` is supported and is **not** the default: switching to it silently would narrow
every 1×ATR stop by a median 4.3% and by up to 25.8% on exactly the gappy names a movers
screen selects.

Smoothing is the same story: `simple` is what both stop books use (so it is the default),
`wilder` is what `technical_utils` uses. They differ by a median 3.2%.

---

## Data model

Three tables the service owns, created by `sql/001_schema.sql`, in the database
`MDS_DATABASE` points at (`Market_Data` today), plus the clock's exchange reference.
The script names no database, so a rename is config, not migration.

| Table | Grain | Holds |
|---|---|---|
| `SymbolPriceCache` | symbol × venue | latest known price, overwritten in place |
| `DailySnapshot` | symbol × exchange × session | daily OHLCV, appended once per session per venue |
| `IndicatorCache` | symbol × exchange | precomputed ATR 14/20/22/26/50 for one venue's series |
| `Exchanges` | one row per exchange | `Code`, `Timezone`, `Opens`, `Closes` in the exchange's own local time; copied from `McTrader_Live.dbo.Exchanges` by `tools/copy_exchanges.py` and read by the market clock |

The market clock reads the exchange hours from `dbo.Exchanges`, not from
`data/clock.json`. The JSON keeps the broker table, the asset-class rules and the
broker-to-exchange mapping, and is the fallback if the database cannot be read —
so a database outage freezes the clock at the last copied exchange list rather
than taking it down. Refresh the copy deliberately:

```
.venv\Scripts\python.exe tools\copy_exchanges.py
```

The clock loads the table once at startup. Reload after a refresh with
`POST /admin/shutdown` (NSSM restarts the process within ~5s), or restart the
service. The clock's `reference_source` names the database table when this is the
source in use, and `data/clock.json` when it has fallen back.

Notes that are load-bearing:

- **The bar tables are keyed by exchange as well as symbol.** A bare ticker is not unique —
  `CBA` is an ASX listing and a US one — and a symbol-only key lets one venue's row overwrite
  the other's without a word. `DailySnapshot` is `(SymbolCode, Exchange, SnapshotDate)` and
  `IndicatorCache` is `(SymbolCode, Exchange)`, so every read and write states the venue.
  `SymbolPriceCache` calls the same code `Venue` because it got there first; both columns hold
  the vendor's exchange code (`US`, `AU`, ...).
- **A write always states its venue.** The three columns are `NOT NULL` and carry no default,
  at either layer: the store raises before it builds a row, and SQL Server refuses one that
  arrives without a value. A row is never filed as US by omission. The only place US is
  assumed is a request that does not name a venue, which is the API's documented default.
- **Rows written before 2026-11-01 were backfilled** from the price cache: US, unless that
  symbol was known on the ASX and nowhere else. That reassigned 18,363 rows that had been AU
  all along and were indistinguishable from US ones.
- **The ATR cache was cleared once**, when the venue key was added: every row in it had been
  computed under a symbol-only key, so it was either labelled with the wrong venue or had both
  venues' bars mixed into one number. It refills from the next daily refresh, or from `/atr`
  on demand.
- **`IndicatorCache` carries 22 and 26 as well as the specified 14/20/50.** 22 is McTrader's
  Chandelier window and its preferred stop ATR; 26 is PortfolioAdvisor's MACD-V divisor. 20
  and 50 are read by nothing today.
- **Percent is derived, not stored** (`Atr / ReferencePrice × 100`). Storing it too would let
  it drift from the price it was measured against.
- **`DailySnapshot.IsAdjusted`** records whether `Close` is split-adjusted. `Close` stores
  `adjusted_close` — matching `BarHourly`, and *not* `Bar5Min`, which is unadjusted and
  documented as such (`JDZG, 2026-03-23: $0.0928 against $20.925 the same day`).
  **`RawClose`** stores the unadjusted close beside it: `Close` keeps a series continuous
  across distributions, `RawClose` is the price that printed, and a candle body must read
  both sides on one scale. It is `NULL` where not known (the `BarHourly` seed, or a symbol
  the 5-minute tape does not carry), and the `/bars` cache-hit rule re-pulls a requested
  window from the vendor to fill it. `jobs.daily --fill-raw-close` fills it in bulk from
  `Bar5Min` at no vendor cost.
- **A `/bars` refresh keeps `MDS_HISTORY_SESSIONS` (560, ~2 years), not just the window the
  caller asked for.** `/eod/` bills one unit whatever its width, so the depth is already
  paid for; asking for fifteen sessions and keeping fifteen is how every symbol in the book
  ended up averaging 55 rows against a five-year span. A symbol whose stored series already
  reaches that horizon is refreshed for the requested sessions instead, so the fill is a
  one-off per symbol rather than a rewrite on every daily refresh. The horizon is what makes
  support and resistance levels computable from one call.
- `TableDoc` / `ColumnDoc` rows are written for all three tables, following this database's
  own convention.

### Indexes, and the weekly rebuild

Three indexes carry the reads: `PK_DailySnapshot` (SymbolCode, Exchange, SnapshotDate) for one
symbol's series, `PK_IndicatorCache` (SymbolCode, Exchange) and `PK_SymbolPriceCache`
(SymbolCode, Venue). The one gap was the *session-wide* read — every symbol's bar for one date,
which is what movers and the close-to-close comparisons do. `sql/002_indexes.sql` replaces
`IX_DailySnapshot_Date` with `IX_DailySnapshot_Session (SnapshotDate, Exchange) INCLUDE (Open,
High, Low, Close, Volume, IsAdjusted)`. Measured before that change: 11,986 scans of the date
index against 9,595 key lookups into the 223 MB clustered index — the signature of a
non-covering index, confirmed by three separate missing-index hints.

`sql/003_index_maintenance.sql` creates `dbo.usp_MMDS_MaintainIndexes`, which reads
fragmentation in LIMITED mode, rebuilds at 30% and reorganises at 10%, updates statistics, and
records every index it considered in `dbo.IndexMaintenanceLog` — including the ones it skipped,
so "ran and did nothing" is distinguishable from "did not run".

```sql
EXEC dbo.usp_MMDS_MaintainIndexes @WhatIf = 1;   -- the plan, changing nothing
EXEC dbo.usp_MMDS_MaintainIndexes;               -- do it (offline: this is Standard edition)
```

Run it after a bulk change and weekly, outside the two collection windows (10:45 Sydney for the
ASX, 02:15 for the US). `@IncludeWarehouse = 1` extends it to `BarHourly` / `Bar5Min`,
which are otherwise left alone — a fullscan of 77 million rows is not a maintenance task.

### Expect ~27,700 rows per session, not ~2,800

`/eod-bulk-last-day/US` returns the **whole US market** including OTC and pink sheets —
~27,714 codes for the current session. Your liquidity-filtered trading universe is ~2,860.
Storing everything is deliberate: a newly-listed mover would otherwise have no ATR history,
and one call costs the same either way.

---

### Reading the price cache in local time

`SymbolPriceCache` is UTC, deliberately: `AsOfUtc` is when the vendor observed the price and
`LastUpdatedUtc` is when this service fetched it, and those are instants — a local timestamp cannot be
compared across timezones, and one hour a year is ambiguous.

For reading them at a Sydney desk there is a view. It keeps the UTC columns and adds the local ones
beside them, so the conversion is checkable rather than trusted:

```sql
SELECT SymbolCode, Price, LastUpdatedUtc, LastUpdatedLocal, LastUpdatedOffsetMinutes
FROM dbo.SymbolPriceCacheLocal
ORDER BY LastUpdatedUtc DESC;
```

| Column | Meaning |
|---|---|
| `AsOfLocal`, `LastUpdatedLocal` | the same instants as local wall clock, DST applied per row |
| `LastUpdatedOffsetMinutes` | 660 (AEDT) or 600 (AEST) — which side of the clock change that row is on |

Verified over all 12,053 rows: every one converts to exactly `UTC + 600` or `UTC + 660` minutes, with
precision preserved to the microsecond.

## What it costs, and who spent it

Two tables, both yours to read:

| Table | Holds |
|---|---|
| `dbo.RequestTypes` | the **measured tariff**: one row per vendor endpoint, with `CostBasis` (`flat`, `per_call`, `per_symbol`, `free`), `CostEach`, and the measurement evidence |
| `dbo.EODHDUsage` | the **ledger**: one row per `(UsageDate, Caller, RequestType)` with `Calls`, `Symbols`, `CostTotal`, `FirstSeen`, `LastSeen` |

### The tariff is the authority, not the code

Per-call costs used to be literals at each call site (`units=1`, `units=100`). They are now read from
`dbo.RequestTypes` at startup, so a re-measured price is a row update rather than a release — and the
number a reader checks is the number the code spends.

The four bases are not interchangeable, and getting one wrong is silent:

| Basis | Meaning | Measured |
|---|---|---|
| `per_symbol` | multipled by symbols in the request | 3 tickers in one call billed **3**, so batching saves wall-clock and nothing else |
| `flat` | once per call, whatever the size | the whole-market feed is **100** whether it returns 12,737 rows or 527 |
| `per_call` | once per call, whatever the window | 5-day and 30-day `/eod/` both cost **1** |
| `free` | zero | `/user` costs nothing, which is why reconciliation is free |

An unpriced row (`CostEach IS NULL`) is recorded with cost 0 **and a warning**: unpriced is not free,
and a call that vanished would be the accounting hole these tables exist to close.

### Who spent it

Every data endpoint attributes its spend to the `X-MarketData-Caller` header, falling back to the path:

```powershell
Invoke-RestMethod http://127.0.0.1:8003/price/AAPL -Headers @{ 'X-MarketData-Caller' = 'minictrader' }
```

```
caller        request type   calls  symbols  cost
mctrader      /real-time/        1        1     1
minictrader   /real-time/        1        1     1
http:/price   /real-time/        1        1     1     <- no header, attributed by path
```

Without a header the row still lands, attributed to the path. That is deliberate: an unattributed call
is a question worth asking, and a call that vanished would answer nothing.

### Failed calls cost money too

A rejected request **is billed** — measured: a 404 from `/eod/` cost 1, a 404 from `/intraday/` cost 5.
So a 4xx is recorded before the error is raised. This closed a real gap: the client used to count only
successes, so a run of rejections was invisible in the accounting while still spending quota. A
transport failure (the request never landed) is the one case that costs nothing and is not recorded.

### Where to see it

`GET /stats` reports `usage_today` from the ledger — totals, by request type, and by caller:

```json
"usage_today": {
  "date": "2026-10-29", "calls": 261, "symbols": 261, "cost": 733,
  "by_request_type": [{"request_type": "/screener", "calls": 108, "cost": 540}, ...],
  "by_caller": [{"caller": "mctrader", "calls": 2, "cost": 2}, ...]
}
```

The process counters (`vendor.units`) are still there but reset on restart; the ledger does not. When
the two disagree, the ledger is right.

### Reconciliation is free

`/user` returns the vendor's own counter and costs nothing, so the ledger can be checked against the
vendor at any time:

```
apiRequests 2798 / dailyRateLimit 100000
```

### The day boundary is local

`UsageDate`, `FirstSeen` and `LastSeen` are **local wall-clock**, matching the logging tables and the
desk clock.

It was UTC, which filed a Sydney morning's spend under the previous day: a "today" query at 09:00 read
yesterday's row, and the ledger disagreed with every other clock in the estate.

Two consequences, stated once:

- a date here cannot be compared with a date from another timezone without knowing this one;
- the vendor's own quota (`/user`) resets on UTC, so the ledger's day and the vendor's day are offset
  by the number of hours this site is ahead. A reconciliation against `/user` therefore agrees on the
  total but not on where the midnight boundary falls.

Rows written before this change keep the dates they were recorded under — they are not rewritten,
because converting a historical `UsageDate` would move spend between days on a guess.

## Bulk refresh on demand

`POST /refresh` pulls the whole market's daily bars into `DailySnapshot`. Measured:

| | |
|---|---|
| vendor units | **100**, one `/eod-bulk-last-day` call (flat-priced) |
| duration | **~30 s** with the ATR rebuild (2,866 symbols), **~10 s** pull-only |
| rows written | ~44,000 for a full session |

That is short enough to be a synchronous HTTP call, and long enough that the service does not do it
behind a data request.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8003/refresh -ContentType 'application/json' -Body '{}'
```

```json
{"status":"refreshed","newest_session":"2026-10-28","expected_session":"2026-10-28",
 "behind_expected":false,"rows_written":44026,"units":100,"duration_ms":29791,
 "sessions":[{"session":"2026-10-28","received":44134,"written":44026}],
 "indicators":{"symbols_considered":2866,"indicators_written":2866}}
```

### It checks before it spends

`{"status":"already-fresh","units":0}` comes back in milliseconds when the spend ledger already records
a bulk pull for today. That check reads `EODHDUsage`, not `MAX(SnapshotDate)` — single-symbol rows
written by `/bars` carry today's date, so the newest session can be today while the market-wide pull has
not run for weeks. `force: true` skips the check; so does naming a `session_date`, since a dated
backfill cannot be vouched for by today's ledger entry.

### Two callers don't double-spend

A non-blocking lock makes the second caller report `409 busy` rather than queue behind the first. Waiting
30 s for someone else's pull is worse than being told to ask again, and two concurrent pulls would each
spend 100 units writing the same rows.

### It reports whether it produced the right session

`expected_session` comes from `data_service.sessions` — the venue's own clock, close plus a
three-hour settlement margin. `behind_expected` is true when the vendor returned an older session than
the venue says should exist, which is how a vendor lag or a holiday becomes visible instead of silent.
For scale: at 20:30 Sydney the answer is `US: 2026-10-28`, `AU: 2026-10-29` — the ASX has settled and
the US has not.

### Why a data request does not trigger it

A single-symbol `/bars` miss costs **1** unit through `/eod/`; triggering the bulk pull from it would
cost **100** — a hundred times the request that caused it. It would also charge those units to whichever
client happened to ask first, pile several clients up behind the lock, and hide the failure that
matters: if the daily refresh did not run, data requests should visibly degrade to per-symbol fetches,
not quietly repair themselves at 100 units a time.

### The task

`install-task.ps1 -Install` registers a third task, `<task>_Refresh`, running daily at **11:30 local**.
That time is chosen, not rounded: the US close is 16:00 ET (07:00 or 08:00 in Sydney) and a session
counts only after the three-hour settlement margin, so anything earlier would ask for a session the
service still considers open.

By hand:

```cmd
refresh.bat            :: skips if already fresh
refresh.bat /force     :: pulls again
```

History is appended to `logs\refresh-history.txt`, and the task's exit code is 0 for both "refreshed"
and "already-fresh", 1 for anything else — so `Get-ScheduledTaskInfo` distinguishes nothing-to-do from
went-wrong.

## The daily job

```
python -m jobs.daily                       # latest session: 1 bulk call, 100 units
python -m jobs.daily --since 2026-10-15    # every weekday since, to close a gap
python -m jobs.daily --rebuild-indicators  # ATR only, no vendor call
python -m jobs.daily --seed-from-barhourly --days 90   # FREE seed from BarHourly
python -m jobs.daily --fill-raw-close      # FREE RawClose fill from Bar5Min, no vendor call
```

**`--seed-from-barhourly` costs zero units** and is the right way to give ATR its first
sessions: `BarHourly` already holds 13.4M split-adjusted bars that aggregate
cleanly to sessions. Run it once before the first daily pull.

**`--fill-raw-close` also costs zero units**, and is where the raw close comes from when it
can: `Bar5Min` is the unadjusted 5-minute tape (81M bars, 2025-03-11 onward, 2,801 US
symbols), so the last traded bar of each session is the close that printed — measured equal
to `/eod/`'s `close` for AAPL and NVDA across fifteen sessions each. It fills only rows whose
`RawClose IS NULL`, commits per 500 symbols, and skips a database that has no `Bar5Min`
table. `BarHourly` cannot feed it: its `Close` is the split-adjusted series, which is what
`DailySnapshot.Close` already holds.

Two rules the job follows, both learned the hard way:

1. **The session date is taken from the rows, never from the request.** A bulk call for a
   date the market was shut answers with the previous session; filing that under the requested
   date would invent a duplicate bar.
2. **A dated pull returns ~50,270 codes** against ~27,714 for the current session, because it
   still lists instruments that have stopped trading. That is fine for storing history — a
   delisted instrument's bars are real. It is **not** fine for computing a market-wide
   statistic from a dated pull, which must be intersected with a live universe first.

A gap in the daily series is worse than a missing session: true range would span the gap and
read it as one enormous move. `--since` exists to close one.

---

## The `Back_Test` → `Market_Data` rename (done 2026-10-29)

`Back_Test` became `Market_Data`. The rename was the point: this database stops being only a
backtest warehouse and becomes the market data source of record.

It was a configuration change, as designed — the schema script names no database, the store takes a
connection string, and `/healthz` and `/stats` report which database is in use. What it needed,
in this order:

1. stop whichever process holds connections;
2. rename the database;
3. point every consumer at the new name.

**Step 3 is the one that bites**, and it caught the running service: after the rename it reported
`degraded` with `database_ok=false` and `/stats` returned 500, because its default was still
`Back_Test`. The default here is now `Market_Data`.

**PaperTrader also read this database** — `sql_store.py` defaulted to `BACKTEST_DB=Back_Test`, and
three of its analysis scripts named it in SQL. Those were updated in the same pass; anything that still
holds a connection or an old default needs a restart to pick the new name up.

Verified after the rename: `SymbolPriceCache`, `IndicatorCache`, `DailySnapshot`, `EODHDUsage` (237
rows), `RequestTypes` (11) and the 13.4M-row `BarHourly` all arrived intact.

---

## Configuration

All by environment variable; defaults are the safe ones.

| Variable | Default | Meaning |
|---|---|---|
| `MDS_DATABASE` | `Market_Data` | target database (renamed from `Back_Test` on 2026-10-29) |
| `MDS_DB_SERVER` | `localhost` | SQL Server |
| `MDS_PRICE_TTL_SECONDS` | `300` | price cache lifetime |
| `MDS_BULK_BREAKEVEN` | `100` | symbols at which bulk beats per-symbol |
| `MDS_MIN_SESSIONS` | `15` | sessions before ATR will answer |
| `MDS_HISTORY_SESSIONS` | `560` | depth a vendor refresh asks for and keeps (~2 years) |
| `MDS_ATR_PERIODS` | `14,20,22,26,50` | periods computed into IndicatorCache |
| `MDS_ATR_METHOD` | `tr` | `tr` (the agreed standard) or `high_low` |
| `MDS_ATR_SMOOTHING` | `simple` | `simple` or `wilder` |
| `MDS_PORT` | `8003` | port |
| `MDS_HOST` | `127.0.0.1` | bind address — localhost only |
| `EODHD_API_KEY` | *(keyring)* | overrides the OS keyring entry |

The key comes from `keyring.get_password("market_data:eodhd:global", "eodhd:api_key")` — the
same entry McTrader and Mini-McTrader already use, so all books provably share one key and
one quota.

---

## Running as a service

The deployment is a **Windows service**, installed with NSSM:

```
McTrader_Market_Data_Service   Running / Automatic
  D:\McTrader_Market_Data_Service\.venv\Scripts\python.exe -m data_service.service
  StartName: .\AndrewBot          AppExit: Restart (5s delay, 10s throttle)
  DependOnService: MSSQLSERVER
```

`.\install-service.ps1 -Install -TakeOver -AsUser "$env:USERDOMAIN\$env:USERNAME"` did it: it
installed the service, set the account, granted "Log on as a service", removed the old scheduled
tasks that would otherwise race it for port 8003, stopped the task-launched process, and started the
service. `-TakeOver` is what makes the switch clean; without it two launchers fight over the port
and the loser looks like it started and then died.

### Why your account rather than LocalSystem

The EODHD key lives in the Windows Credential Manager, which is **per user**. A LocalSystem service
has its own, empty vault: it starts, reports healthy, and then fails every vendor call. Running as
you means the keyring works with nothing else configured, and `/healthz` proves it:

```json
{"status": "ok", "vendor_configured": true, "key_source": "keyring",
 "database": "Market_Data", "database_ok": true}
```

`key_source` is the field that matters. If it reads `none`, the service cannot see the key and every
vendor call will fail — see "The service account and SQL Server" and "Storing the vendor key at
runtime" below, which are the LocalSystem alternative.

### Restarts

NSSM is the supervisor: `AppExit Default Restart` with a 5-second delay and a 10-second throttle, so
a crash is retried without a tight loop, and `DependOnService MSSQLSERVER` keeps the start ordered
after the database (`ensure_schema()` runs at startup).

Verified on 2026-11-01 by stopping the child deliberately through `POST /admin/shutdown`:

```
17:25:57  POST /admin/shutdown 202
17:26:03  starting McTrader_Market_Data_Service on 127.0.0.1:8003 (pid=16852)
17:26:03  EODHD key source: keyring
17:26:03  McTrader_Market_Data_Service 0.1.0 ready: db=Market_Data port=8003
```

Six seconds, new pid, keyring still readable. That is the difference from the boot-only task era,
where a dead process stayed dead until the next reboot — the reason the daily collection silently
stopped for three days.

### Starting, stopping and restarting

```cmd
start.bat    :: start the service, then report state and health
stop.bat     :: stop the service
```

Both re-run themselves behind **one UAC prompt**, because controlling a Windows service needs
`SERVICE_START` / `SERVICE_STOP` and an ordinary shell's filtered token does not have them — `nssm`
itself answers `OpenService(): Access is denied`. The prompt is the price of SCM management.

**`stop.bat` no longer calls `/admin/shutdown`,** and that is deliberate: NSSM restarts a child that
exits, even a clean one, so asking the process to die would bring it back five seconds later. Only a
service stop means "stay down". `/admin/shutdown` survives for the one case SCM cannot cover — a
wedged child that will not serve HTTP — after which NSSM's restart is usually the fix.

`.\install-service.ps1 -Status|-Start|-Stop|-Restart|-Remove` are the scripted equivalents; all but
`-Status` need an elevated shell.

### Collection is a schedule inside the service, not a task

The service collects on its own clock: at each venue's **open + 45 minutes**, with **one retry
fifteen minutes later** and nothing after that.

| market | venue opens | fires at | UTC |
|---|---|---|---|
| US | 09:30 New York | 10:15 New York | 14:15Z on EDT, 15:15Z on EST |
| AU | 10:00 Sydney | 10:45 Sydney | 23:45Z the previous UTC day |

Both times come from the market clock, so a daylight-saving change moves the collection with the
session. The guard is at the point of collection, not only on the timer: nothing is written outside
the venue's session, including from a manual run — a price collected at 02:00 on a Sunday is a stale
echo or a synthetic print, and either is what a screener should not be ranking on.

What a run collects depends on the venue. The US gets the whole-market snapshot (one call, 100
units). The ASX has no whole-market real-time feed, so its run is the settled daily bulk **plus**
live prices for the top 100 listings by dollar volume on the last settled session
(`MDS_SCHEDULE_PRICE_UNIVERSE`, one unit a symbol). That second part is what writes an observation
after the open into `SymbolPriceCache` — the only table `GET /todays-data-is-available` reads — so
without it the AU answer could never be true.

```powershell
Invoke-RestMethod http://127.0.0.1:8003/schedule    # what it is, and what it did
Invoke-RestMethod -Method Post 'http://127.0.0.1:8003/schedule/run?market=AU'   # run one now
.\run.ps1 -Daily -Since 2026-10-27                  # close a gap by hand
```

`GET /schedule` reports `done`, `waiting` or **`manual-intervention`** — the last means two attempts
failed and the service has stopped trying, by design.

The external `McTrader_Market_Data_Service_Daily` task (11:30 local) was **retired on 2026-11-01**: it
pulled the same session the schedule had already collected, for 100 units a day. `run.ps1 -Daily` is
still there for a manual backfill; nothing calls it on a timer.

### The task-based stack it replaced

`install-task.ps1` still works and is kept for reference, but it is **superseded**: it registered a
boot-only keep-alive (no crash recovery), a stop task, and a refresh task at 11:30. Only the
keep-alive was ever actually registered here, which is why the whole-market pull stopped on 2026-10-29
with nothing to notice it. Do not run both — the service and a task share port 8003.

| Setting | Value | Why |
|---|---|---|
| Start | `SERVICE_AUTO_START` | on the critical path at 10:30 |
| Account | `.\AndrewBot` | the keyring is per user (LocalSystem: see the next two sections) |
| DependOnService | `MSSQLSERVER` | the app calls `ensure_schema()` on startup |
| AppExit | `Restart`, delay 5 s, throttle 10 s | a crash is retried, without a tight loop |
| AppStopMethodConsole | 20 s | a cycle mid-flight is not cut short by a restart |

### The service account and SQL Server

Only relevant to a **LocalSystem** install (`-Install` without `-AsUser`): SQL Server then sees
`NT AUTHORITY\SYSTEM`, not the account you are logged in as. SYSTEM has a login here but no database
users, so the service would install cleanly and fail on every start.

Fixed by grant, not by password:

```
Market_Data: db_datareader, db_datawriter, db_ddladmin
```

`db_ddladmin` because the app creates its three tables on startup if they are absent. Least
privilege, and no credential on disk.

### Storing the vendor key at runtime

`POST /secrets/eodhd` writes the key into **this process account's** keyring and starts using it
immediately — no restart.

That is how a LocalSystem service is given a key it can actually read: a service running as SYSTEM
cannot see a key stored under an interactive account, and only the service itself can write into
SYSTEM's vault. It is better than passing `EODHD_API_KEY` in the environment, because the credential
store is encrypted by Windows whereas an environment variable sits in plaintext in the registry.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8003/secrets/eodhd `
  -ContentType 'application/json' -Body '{"api_key":"<your-key>"}'
```

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/secrets/eodhd` | store the key, and rebuild the vendor client so it takes effect at once |
| `GET` | `/secrets/eodhd` | whether a key is stored, a **masked** form, and which source is in use |
| `DELETE` | `/secrets/eodhd` | remove the key; the service keeps running |

**The key is never returned.** `GET` gives `masked` (last four characters), `length`, the keyring
target and `backend` — enough to confirm *which* key is stored, useless to anyone intercepting it:

```json
{"stored": true, "masked": "*******************3836", "length": 23,
 "keyring_service": "market_data:eodhd:global", "keyring_username": "eodhd:api_key",
 "backend": "WinVaultKeyring", "keyring_available": true,
 "in_use": true, "in_use_source": "keyring", "vendor_configured": true}
```

#### No key does not stop the service

EODHD is left out of the router and Yahoo/Binance carry on, so crypto, indices and FX keep answering.
A share falls through to Yahoo — and the response says `provider: "yahoo"`, so the tape change is
declared rather than hidden. Verified with the key removed:

| Call | With no key |
|---|---|
| `/price/BTC-USD?asset_class=crypto` | `provider=binance` |
| `/price/IXIC?asset_class=indices` | `provider=yahoo` |
| `/price/AAPL` | `provider=yahoo` — degraded, and declared |
| `/prices` above 100 symbols | per-symbol, because the bulk feed is EODHD-only |

#### Protecting the write

`POST` and `DELETE` are privileged: a local process that can overwrite the key can point this service
at an attacker's key, and once the service runs as LocalSystem it can make SYSTEM store a key the user
could not write themselves.

Set `MDS_ADMIN_TOKEN` in the service environment and the header becomes mandatory:

```powershell
# in the service's environment
$env:MDS_ADMIN_TOKEN = 'some-long-random-value'
# every write then needs:  -H "X-Admin-Token: some-long-random-value"
```

With no token configured the endpoints still work — this binds to localhost on a single-user machine,
and refusing by default would make the endpoint useless on first install — but every call logs a
warning, so the gap is never silent.

Verified both ways: without a header, or with a wrong one, `POST` and `DELETE` return **401 and change
nothing**; `GET` stays open because it reveals only a mask.

### The EODHD key and the service account

**A LocalSystem service cannot read the per-user keyring.** The key normally comes from the Windows
Credential Manager via `keyring`, and that store is **per user**. Verified: the entry
`market_data:eodhd:global` / `eodhd:api_key` lives under `AndrewBot`. `NT AUTHORITY\SYSTEM` has its
own separate vault, so a service installed without supplying the key starts, reports healthy, and
then fails every vendor call.

The resolution order in `config.py` already prefers the environment, so the key is handed over
there:

```cmd
install-service.cmd -Install -EodhdApiKey <your-key>
```

That puts it in the service's own registry key, readable only by administrators and SYSTEM — better
than a plaintext config file, and not visible to every user on the box.

**How to tell which path was used.** `GET /healthz` reports `key_source`, and the service logs it at
startup. Never the key itself, only where it came from:

| `key_source` | Meaning |
|---|---|
| `keyring` | **the expected state**: the task runs as your account, so the credential vault is readable |
| `environment` | a key was set in the environment (`-EodhdApiKey`); works, but sits in plaintext in the registry |
| `none` | no key: EODHD is out of the router, so only Yahoo and Binance answer |

Verified both ways by simulating the SYSTEM case: with no environment variable and an empty
keyring, resolution returns `none`; as the interactive user it returns `keyring`.

`-AsUser` does **not** need `-EodhdApiKey` — an interactive account can read its own vault.

### What a service has that a console run does not

`data_service/service.py` exists because a Windows service has **no console**: anything written to
stdout/stderr goes nowhere. It logs to `logs\data_service.log` (rotated 10 MB × 5) and passes
`log_config=None` to uvicorn so uvicorn's own lines reach that file too.

Run it by hand to check before installing:

```powershell
.\.venv\Scripts\python.exe -m data_service.service
```

**The service is only the API.** The daily load stays a scheduled job — `.
un.ps1 -Daily` — because
it is a once-a-day batch, not a long-running process.

---

## Running

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\run.ps1 -Schema     # create the three tables and their documentation rows
.\run.ps1 -Seed       # free 90-day history seed from BarHourly
.\run.ps1 -RawClose   # free RawClose fill from the Bar5Min tape
.\run.ps1 -Daily      # one bulk pull + ATR rebuild
.\run.ps1             # serve on 127.0.0.1:8003
.\run.ps1 -Test       # 47 tests, no vendor units
```

**One worker, deliberately.** The service holds an in-memory whole-market snapshot and a
per-request lock; a second worker would mean two caches and two 100-unit bulk pulls for one
market. Scale out only after the snapshot moves to a shared store.

---

## Scope, and the rule that keeps it thin

**In:** prices (one and many), daily bars, ATR, stop levels, the daily bulk load.

**Out:** screener, company profiles, fundamentals, news, crypto, forex, Yahoo fallback,
streaming, order-book depth, tick history.

> **Rule:** this project may not import from `McTrader/services/`, `McTrader/ai_traders/` or
> `McTrader/broker_adapters/`.

The service exists so that the data layer stays up when the trader does not. Its availability
*is* its justification, and every scope addition erodes it. If it grows a screener, profiles,
crypto and a scoring layer, it becomes a second trading engine and the reason to have it
disappears.

Sentiment deliberately stays in McTrader for now.

---

## Integrating McTrader

**`WIRING.md` is the checklist for this step** — endpoints, the adapter shape, caller attribution,
error handling, the gotchas, and what to delete. Summary below.

### Summary

`MarketDataService` (`services/data_service.py`) is a router with two legs, and only
one of them belongs here:

```
MarketDataService                      ← stays: broker-first router
├── IBrokerAdapter                     ← stays: the trading connection, its own credentials
├── AlpacaMarketDataAdapter            ← stays: broker-side prices
└── FallbackMarketDataAdapter          ← the EODHD leg moves behind an HTTP call to this service
```

The broker leg **must** stay in McTrader: it is the same connection that places orders,
`MarketDataService` reads `MARKET_DATA_PROVIDER_PRIORITY` out of broker config, and the stop
check is the most latency-sensitive read in the estate — routing it through a second process
adds a failure mode exactly where a failure costs money.

So the change is surgical: `FallbackMarketDataAdapter`'s `EODHDClient` calls become HTTP
calls to `:8003`. `IMarketDataProvider` is implemented by three adapters today (Binance,
Alpaca, Fallback); this service is a **fourth implementer of that seam over HTTP**, not a
replacement for the class that hosts them.

Two things to preserve when wiring it up:

- **Caller attribution.** Pass who is asking (`X-MarketData-Caller: mctrader`), or every unit
  in the ledger will be attributed to the service and per-app spend becomes unattributable.
  The `EodhdUsage.Caller` prefix is how that table was designed to work.
- **A kill switch.** `D:\market-data\NO-MKTDATA` present → McTrader goes direct to the vendor,
  so a service outage degrades to today's behaviour rather than to no data. Follow the
  existing `NO-BULK` precedent, which treats an unreadable switch as *off*.
