"""The daily job: one bulk pull into DailySnapshot, then rebuild IndicatorCache.

    python -m jobs.daily                     # the latest session (what the scheduler runs)
    python -m jobs.daily --date 2026-10-30   # a specific session
    python -m jobs.daily --seed-from-barhourly --days 90
                                             # FREE history seed from Back_Test.BarHourly
    python -m jobs.daily --rebuild-indicators # ATR only, no vendor call

WHAT THIS COSTS

The bulk pull is ``/eod-bulk-last-day/US``: **100 units flat, ~27,714 rows**, carrying both
``close`` and ``adjusted_close``. Adjusted is stored, because it matches ``BarHourly``'s
convention and avoiding it is how an ATR learns to read a split as a catastrophic move
(JDZG, 2026-03-23: $0.0928 against $20.925 the same day).

``--seed-from-barhourly`` spends **nothing**: it aggregates the 13.4M rows already in
``dbo.BarHourly`` into sessions. That is the right way to give ATR its first 51 sessions,
because the data is already paid for and already split-adjusted.

THE DELISTED-LISTINGS TRAP

A *dated* pull returns ~50,270 codes including listings that have since stopped trading,
against ~27,714 for the current session. So a dated pull is filtered to codes that exist in
the current session; without that, a backfill imports dead tickers that then hold stale
prices and drag market-wide averages around.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import atr as atr_module
from data_service import config as config_module
from data_service.eodhd import EodhdClient, EodhdError, bare_symbol
from data_service.usage import Tariff, UsageLedger, set_caller
from data_service.store import Store, _float

log = logging.getLogger("data_service.daily")


# ---------------------------------------------------------------------------
# vendor pull
# ---------------------------------------------------------------------------

def _row_from_bulk(raw: Dict[str, Any], session_date: date,
                   symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One bulk row as a DailySnapshot row, preferring adjusted_close."""
    code = symbol or bare_symbol(str(raw.get("code") or ""))
    if not code:
        return None

    adjusted_close = _float(raw.get("adjusted_close"))
    close = adjusted_close if adjusted_close is not None else _float(raw.get("close"))
    high = _float(raw.get("high"))
    low = _float(raw.get("low"))
    open_ = _float(raw.get("open"))
    if None in (close, high, low, open_):
        return None
    if close <= 0 or high <= 0 or low <= 0:
        return None

    return {
        "symbol": code,
        "session": session_date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": int(_float(raw.get("volume")) or 0),
        "source": "eod-bulk",
        # True only when adjusted_close was present: claiming adjustment for a raw close
        # would let a split through as a real move.
        "adjusted": adjusted_close is not None,
    }


def pull_bulk(store: Store, client: EodhdClient, market: str = "US",
              dates: Optional[List[str]] = None) -> Dict[str, Any]:
    """Pull one or more sessions of whole-market daily bars into DailySnapshot.

    NOTHING IS FILTERED OUT, and that is deliberate. A dated pull answers with ~50,270 codes
    against ~27,714 for the current session, because it still lists instruments that have
    since stopped trading. The first version of this function intersected dated pulls with
    the current listing set to drop them -- which was wrong twice over: a delisted
    instrument's bars *are* real history for the sessions it traded, and the intersection
    was computed from a dated pull anyway, so it excluded nothing and wrote 49,600 symbols
    per session instead of 2,800.

    The trap the intersection was meant to avoid is a different one, and it belongs to the
    reader, not the loader: never compute a market-wide statistic from a dated pull without
    intersecting with a live listing set. Storing history is not that. A symbol that stopped
    trading simply stops gaining rows, and its last session is visible in
    DailySnapshot.SnapshotDate, so a stale ATR is detectable rather than silently wrong.
    """
    session_dates = dates or [None]
    total = 0
    per_session: List[Dict[str, Any]] = []

    for wanted in session_dates:
        rows = client.bulk_last_day(market, session_date=wanted)
        if not rows:
            log.warning("bulk pull for %s returned nothing", wanted or "latest")
            continue

        # The session date is always taken from the rows, never from the request. A bulk
        # call for a date the market was shut answers with the previous session, and filing
        # that under the requested date would silently invent a duplicate bar.
        raw_dates = [str(r.get("date") or "")[:10] for r in rows]
        raw_dates = [d for d in raw_dates if d]
        if not raw_dates:
            log.warning("bulk pull for %s carried no dates", wanted or "latest")
            continue
        resolved = max(raw_dates)
        if wanted and wanted != resolved:
            log.warning("asked for %s, vendor answered with %s (not a trading day)",
                        wanted, resolved)

        session_date = date.fromisoformat(resolved)
        payload: List[Dict[str, Any]] = []
        unadjusted = 0
        for raw in rows:
            symbol = bare_symbol(str(raw.get("code") or ""))
            row = _row_from_bulk(raw, session_date, symbol)
            if row is None:
                continue
            if not row["adjusted"]:
                unadjusted += 1
            payload.append(row)

        written = store.upsert_daily(payload)
        total += written
        per_session.append({
            "session": resolved,
            "received": len(rows),
            "written": written,
            "rows_without_adjusted_close": unadjusted,
        })
        log.info("session %s: %d received, %d written, %d without adjusted_close",
                 resolved, len(rows), written, unadjusted)

    return {"sessions": per_session, "rows_written": total}


# ---------------------------------------------------------------------------
# free seed from BarHourly
# ---------------------------------------------------------------------------

BARHOURLY_AGGREGATE = """
WITH ranked AS (
    SELECT SymbolCode,
           CAST(BarTimeUtc AS DATE) AS SessionDate,
           BarTimeUtc, [Open], [High], [Low], [Close], Volume,
           ROW_NUMBER() OVER (PARTITION BY SymbolCode, CAST(BarTimeUtc AS DATE)
                              ORDER BY BarTimeUtc ASC)  AS rn_first,
           ROW_NUMBER() OVER (PARTITION BY SymbolCode, CAST(BarTimeUtc AS DATE)
                              ORDER BY BarTimeUtc DESC) AS rn_last
    FROM dbo.BarHourly
    WHERE BarTimeUtc >= ?
),
aggregated AS (
    SELECT SymbolCode, SessionDate,
           MAX(CASE WHEN rn_first = 1 THEN [Open] END) AS [Open],
           MAX([High]) AS [High],
           MIN([Low])  AS [Low],
           MAX(CASE WHEN rn_last = 1 THEN [Close] END) AS [Close],
           SUM(Volume) AS Volume
    FROM ranked
    GROUP BY SymbolCode, SessionDate
)
SELECT SymbolCode, SessionDate, [Open], [High], [Low], [Close], Volume
FROM aggregated
WHERE [Open] IS NOT NULL AND [Close] IS NOT NULL
ORDER BY SymbolCode, SessionDate
"""


def seed_from_barhourly(store: Store, days: int = 90, chunk: int = 20000) -> Dict[str, Any]:
    """Aggregate Back_Test.BarHourly into DailySnapshot. Costs no vendor units.

    BarHourly is split-adjusted and already paid for, so it is the correct source for the
    history ATR needs on day one. Rows are written with source='barhourly' and
    IsAdjusted=1; the daily job's own pulls then carry the series forward with
    source='eod-bulk'.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(BARHOURLY_AGGREGATE, since)
        rows = cursor.fetchall()
    finally:
        conn.close()

    log.info("BarHourly aggregated %d symbol-sessions since %s", len(rows), since)

    payload: List[Dict[str, Any]] = []
    for code, session, open_, high, low, close, volume in rows:
        open_f, high_f, low_f, close_f = _float(open_), _float(high), _float(low), _float(close)
        if None in (open_f, high_f, low_f, close_f) or close_f <= 0:
            continue
        payload.append({
            "symbol": str(code).strip().upper(),
            "session": session,
            "open": open_f, "high": high_f, "low": low_f, "close": close_f,
            "volume": int(volume or 0),
            "source": "barhourly",
            "adjusted": True,
        })

    written = 0
    for start in range(0, len(payload), chunk):
        written += store.upsert_daily(payload[start:start + chunk])
        log.info("  written %d/%d", written, len(payload))

    return {"rows_written": written, "since": since}


# ---------------------------------------------------------------------------
# indicator rebuild
# ---------------------------------------------------------------------------

def rebuild_indicators(store: Store, settings: config_module.Settings,
                       chunk: int = 200) -> Dict[str, Any]:
    """Recompute IndicatorCache for every symbol with enough daily history."""
    periods = list(settings.atr_periods)
    need = max(settings.min_sessions, max(periods) + 1)

    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT SymbolCode, COUNT(*) AS n
            FROM dbo.DailySnapshot
            GROUP BY SymbolCode
            HAVING COUNT(*) >= ?
        """, need)
        symbols = [str(r[0]).strip().upper() for r in cursor.fetchall()]
    finally:
        conn.close()

    log.info("%d symbol(s) have >= %d sessions for ATR(%d)", len(symbols), need, max(periods))
    written = 0
    for start in range(0, len(symbols), chunk):
        batch = symbols[start:start + chunk]
        bars_by_symbol = store.get_daily_bars_many(batch, max(periods) + 1)
        rows: List[Dict[str, Any]] = []
        for symbol, bars in bars_by_symbol.items():
            if len(bars) < need:
                continue
            highs = [b["high"] for b in bars]
            lows = [b["low"] for b in bars]
            closes = [b["close"] for b in bars]
            reference = closes[-1] if closes else None
            values: Dict[int, Optional[float]] = {}
            for period in periods:
                values[period] = atr_module.average_true_range(
                    highs, lows, closes, period,
                    settings.atr_method, settings.atr_smoothing)
            if values.get(periods[0]) is None:
                continue
            rows.append({
                "symbol": symbol,
                "values": values,
                "reference_price": reference,
                "sessions_used": len(bars),
                "as_of_session": bars[-1]["session"],
                "method": settings.atr_method,
                "smoothing": settings.atr_smoothing,
            })
        if rows:
            written += store.upsert_indicators(rows)
    log.info("IndicatorCache rebuilt for %d symbol(s)", written)
    return {"symbols_considered": len(symbols), "indicators_written": written}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="McTrader_Market_Data_Service daily job")
    parser.add_argument("--date", action="append",
                        help="A specific session date (repeatable). Default: the latest.")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="Pull every weekday from this date to today, to make the daily "
                             "series contiguous. One 100-unit call per weekday.")
    parser.add_argument("--market", default="US")
    parser.add_argument("--seed-from-barhourly", action="store_true",
                        help="Free history seed from Back_Test.BarHourly (no vendor units)")
    parser.add_argument("--days", type=int, default=90,
                        help="How far back to seed (default 90 calendar days)")
    parser.add_argument("--rebuild-indicators", action="store_true",
                        help="Recompute IndicatorCache and exit (no vendor call)")
    parser.add_argument("--skip-indicators", action="store_true",
                        help="Do not rebuild IndicatorCache after loading")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    settings = config_module.load()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)
    store.ensure_schema()
    log.info("database=%s", settings.db_database)

    # A gap in the daily series is worse than a missing session: ATR's true range would
    # span the gap and read it as one enormous move. --since exists to close such a gap.
    dates = list(args.date or [])
    if args.since:
        start = date.fromisoformat(args.since)
        cursor = start
        while cursor <= date.today():
            if cursor.weekday() < 5:
                dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        log.info("--since %s expands to %d weekday(s)", args.since, len(dates))

    started = time.perf_counter()
    summary: Dict[str, Any] = {}

    if args.seed_from_barhourly:
        summary["seed"] = seed_from_barhourly(store, days=args.days)

    if not args.rebuild_indicators:
        if settings.eodhd_api_key:
            client = EodhdClient(
                api_key=settings.eodhd_api_key,
                base_url=settings.eodhd_base_url,
                timeout_seconds=settings.eodhd_timeout_seconds,
                bulk_timeout_seconds=settings.eodhd_bulk_timeout_seconds,
            )
            # Spend from a job is attributed to the job, not to an HTTP caller, and priced from the
            # same tariff the API uses -- otherwise a daily bulk pull would be invisible.
            set_caller("jobs.daily")
            try:
                client.recorder = UsageLedger(
                    store, Tariff.from_rows(store.request_types())).record
            except Exception as exc:  # noqa: BLE001
                log.warning("no spend ledger for this run (%s)", exc)
            try:
                summary["pull"] = pull_bulk(store, client, args.market, dates or None)
            except EodhdError as exc:
                log.error("bulk pull failed: %s", exc)
                summary["pull_error"] = str(exc)
            log.info("vendor spend this run: %s units over %s call(s)",
                     client.units, sum(client.calls.values()))
        else:
            log.warning("no EODHD key: skipping the vendor pull")

    if not args.skip_indicators:
        summary["indicators"] = rebuild_indicators(store, settings)

    summary["database"] = store.stats()
    summary["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    summary["sessions"] = store.session_symbol_counts(5)

    log.info("done in %.1fs: %s", summary["elapsed_seconds"], summary.get("database"))
    for entry in summary.get("sessions", []):
        log.info("  session %s: %d symbols", entry["session"], entry["symbols"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
