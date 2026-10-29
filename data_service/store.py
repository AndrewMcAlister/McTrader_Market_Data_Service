"""The store: three tables, and the queries the products need.

Nothing in this module names a database. It takes a connection string, so the
The ``Back_Test`` -> ``Market_Data`` rename was a configuration change (``MDS_DATABASE``) rather
than a migration.

Upserts use ``MERGE`` rather than update-then-insert because the daily job writes ~2,800
rows at a time and two round trips per row would turn a one-second load into a minute. All
writes are batched through ``executemany`` with ``fast_executemany`` on.

Decimals arrive from pyodbc as ``decimal.Decimal`` and are converted to ``float`` at the
boundary: every consumer downstream (ATR, JSON) works in floats, and mixing the two is how
a rounding rule gets invented by accident.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pyodbc

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "001_schema.sql"

_MERGE_DAILY_FROM_STAGE = """
MERGE dbo.DailySnapshot AS target
USING #stage AS source
    ON target.SymbolCode = source.SymbolCode AND target.SnapshotDate = source.SnapshotDate
WHEN MATCHED THEN UPDATE SET
    [Open] = source.[Open], [High] = source.[High], [Low] = source.[Low],
    [Close] = source.[Close], Volume = source.Volume, Source = source.Source,
    IsAdjusted = source.IsAdjusted, LoadedUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
    (SymbolCode, SnapshotDate, [Open], [High], [Low], [Close], Volume, Source, IsAdjusted)
VALUES
    (source.SymbolCode, source.SnapshotDate, source.[Open], source.[High], source.[Low],
     source.[Close], source.Volume, source.Source, source.IsAdjusted);
"""

_CREATE_STAGE_DAILY = """
CREATE TABLE #stage (
    SymbolCode   varchar(24)   NOT NULL,
    SnapshotDate date          NOT NULL,
    [Open]       decimal(18,6) NOT NULL,
    [High]       decimal(18,6) NOT NULL,
    [Low]        decimal(18,6) NOT NULL,
    [Close]      decimal(18,6) NOT NULL,
    Volume       bigint        NOT NULL,
    Source       varchar(16)   NOT NULL,
    IsAdjusted   bit           NOT NULL
);
"""

_MERGE_PRICE = """
MERGE dbo.SymbolPriceCache AS target
USING (SELECT ? AS SymbolCode, ? AS Venue, ? AS Price, ? AS Bid, ? AS Ask,
              ? AS PreviousClose, ? AS ChangePct, ? AS AsOfUtc, ? AS LastUpdatedUtc,
              ? AS Source) AS source
    ON target.SymbolCode = source.SymbolCode AND target.Venue = source.Venue
WHEN MATCHED THEN UPDATE SET
    Price = source.Price, Bid = source.Bid, Ask = source.Ask,
    PreviousClose = source.PreviousClose, ChangePct = source.ChangePct,
    AsOfUtc = source.AsOfUtc, LastUpdatedUtc = source.LastUpdatedUtc, Source = source.Source
WHEN NOT MATCHED THEN INSERT
    (SymbolCode, Venue, Price, Bid, Ask, PreviousClose, ChangePct, AsOfUtc, LastUpdatedUtc, Source)
VALUES
    (source.SymbolCode, source.Venue, source.Price, source.Bid, source.Ask,
     source.PreviousClose, source.ChangePct, source.AsOfUtc, source.LastUpdatedUtc, source.Source);
"""

_MERGE_DAILY = """
MERGE dbo.DailySnapshot AS target
USING (SELECT ? AS SymbolCode, ? AS SnapshotDate, ? AS [Open], ? AS [High], ? AS [Low],
              ? AS [Close], ? AS Volume, ? AS Source, ? AS IsAdjusted) AS source
    ON target.SymbolCode = source.SymbolCode AND target.SnapshotDate = source.SnapshotDate
WHEN MATCHED THEN UPDATE SET
    [Open] = source.[Open], [High] = source.[High], [Low] = source.[Low],
    [Close] = source.[Close], Volume = source.Volume, Source = source.Source,
    IsAdjusted = source.IsAdjusted, LoadedUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
    (SymbolCode, SnapshotDate, [Open], [High], [Low], [Close], Volume, Source, IsAdjusted)
VALUES
    (source.SymbolCode, source.SnapshotDate, source.[Open], source.[High], source.[Low],
     source.[Close], source.Volume, source.Source, source.IsAdjusted);
"""

_MERGE_INDICATOR = """
MERGE dbo.IndicatorCache AS target
USING (SELECT ? AS SymbolCode, ? AS Atr14, ? AS Atr20, ? AS Atr22, ? AS Atr26,
              ? AS Atr50, ? AS ReferencePrice, ? AS SessionsUsed, ? AS AsOfSession,
              ? AS Method, ? AS Smoothing) AS source
    ON target.SymbolCode = source.SymbolCode
WHEN MATCHED THEN UPDATE SET
    Atr14 = source.Atr14, Atr20 = source.Atr20, Atr22 = source.Atr22, Atr26 = source.Atr26,
    Atr50 = source.Atr50, ReferencePrice = source.ReferencePrice,
    SessionsUsed = source.SessionsUsed, AsOfSession = source.AsOfSession,
    Method = source.Method, Smoothing = source.Smoothing, UpdatedUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
    (SymbolCode, Atr14, Atr20, Atr22, Atr26, Atr50, ReferencePrice, SessionsUsed,
     AsOfSession, Method, Smoothing)
VALUES
    (source.SymbolCode, source.Atr14, source.Atr20, source.Atr22, source.Atr26,
     source.Atr50, source.ReferencePrice, source.SessionsUsed, source.AsOfSession,
     source.Method, source.Smoothing);
"""


class StoreBusy(RuntimeError):
    """A read that could not get a lock in time.

    Raised instead of letting pyodbc hang: a read waiting 120 s for a writer is
    indistinguishable, from the caller's side, from a service that has died. This turns it
    into a named, fast, retryable condition.
    """


def _local_today() -> date:
    """The ledger's day boundary is LOCAL time, matching SYSDATETIME() and the desk clock.

    It was UTC, which meant a Sydney morning's spend was filed under the previous day: a "today"
    query at 09:00 read yesterday's row. Local is the right boundary for an estate that reads the
    table in one timezone.

    Rows written before this change keep the date they were recorded under; they are not rewritten,
    because converting a historical UsageDate would move spend between days on a guess.
    """
    return datetime.now().date()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _float(value: Any) -> Optional[float]:
    """A float from whatever the driver handed back, or ``None``."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _script_batches(sql_text: str) -> List[str]:
    """Split a T-SQL script on ``GO``, which pyodbc cannot execute itself."""
    batches: List[str] = []
    current: List[str] = []
    for line in sql_text.splitlines():
        if line.strip().upper() == "GO":
            body = "\n".join(current).strip()
            if body:
                batches.append(body)
            current = []
        else:
            current.append(line)
    body = "\n".join(current).strip()
    if body:
        batches.append(body)
    return batches


class Store:
    """Reads and writes for SymbolPriceCache, DailySnapshot and IndicatorCache."""

    #: How long a read waits for a writer before giving up. The daily job's inserts take
    #: seconds now rather than minutes, so this only fires when something genuinely unusual
    #: is holding a lock -- and when it does, the caller gets a named refusal rather than a
    #: two-minute stall that looks like an outage.
    LOCK_TIMEOUT_MS = 15_000

    def __init__(self, connection_string: str, timeout: int = 30) -> None:
        self.connection_string = connection_string
        self.timeout = int(timeout)

    def connect(self) -> pyodbc.Connection:
        """A connection with fast executemany on, used as a context manager by callers."""
        conn = pyodbc.connect(
            self.connection_string, timeout=self.timeout, autocommit=False,
            fast_executemany=True)
        cursor = conn.cursor()
        try:
            cursor.execute(f"SET LOCK_TIMEOUT {int(self.LOCK_TIMEOUT_MS)}")
        finally:
            cursor.close()
        return conn

    @staticmethod
    def _execute(cursor: pyodbc.Cursor, sql: str, *params: Any) -> None:
        """Run a statement, translating a lock wait into :class:`StoreBusy`."""
        try:
            cursor.execute(sql, *params)
        except pyodbc.Error as exc:
            text = str(exc)
            if "1222" in text or "Lock request time out" in text:
                raise StoreBusy(
                    "the database is locked by a writer; retry shortly "
                    "(the daily load holds the lock only briefly)") from exc
            raise

    # -- schema ------------------------------------------------------------

    def ensure_schema(self) -> int:
        """Apply sql/001_schema.sql. Idempotent; returns the number of batches applied.

        The script guards every object with ``IF ... IS NULL`` / ``NOT EXISTS``, so running
        it on every start is safe and means a fresh database needs no manual step.
        """
        text = SCHEMA_PATH.read_text(encoding="utf-8")
        batches = _script_batches(text)
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for index, batch in enumerate(batches, start=1):
                try:
                    cursor.execute(batch)
                    # A batch with MERGE-free DDL can leave a result set open.
                    while cursor.nextset():
                        pass
                except pyodbc.Error as exc:
                    raise RuntimeError(
                        f"schema batch {index} failed: {exc}\n--- batch ---\n{batch[:800]}"
                    ) from exc
            conn.commit()
        finally:
            conn.close()
        log.info("schema ensured (%d batch(es))", len(batches))
        return len(batches)

    # -- prices ------------------------------------------------------------

    def get_prices(self, symbols: Sequence[str], venue: str) -> Dict[str, Dict[str, Any]]:
        """Cached prices for *symbols*, keyed by bare symbol. Missing symbols are absent."""
        wanted = [s.strip().upper() for s in symbols if s and s.strip()]
        if not wanted:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        conn = self.connect()
        try:
            cursor = conn.cursor()
            # Chunked so a 2,800-symbol scan does not hit SQL Server's 2,100 parameter cap.
            for start in range(0, len(wanted), 1000):
                chunk = wanted[start:start + 1000]
                placeholders = ",".join("?" * len(chunk))
                self._execute(
                    cursor,
                    f"""
                    SELECT SymbolCode, Venue, Price, Bid, Ask, PreviousClose, ChangePct,
                           AsOfUtc, LastUpdatedUtc, Source
                    FROM dbo.SymbolPriceCache
                    WHERE Venue = ? AND SymbolCode IN ({placeholders})
                    """,
                    venue, *chunk)
                for row in cursor.fetchall():
                    out[str(row[0]).upper()] = {
                        "symbol": str(row[0]).upper(),
                        "venue": row[1],
                        "price": _float(row[2]),
                        "bid": _float(row[3]),
                        "ask": _float(row[4]),
                        "previous_close": _float(row[5]),
                        "change_pct": _float(row[6]),
                        "as_of": row[7],
                        "fetched_at": row[8],
                        "provider": row[9],
                    }
        finally:
            conn.close()
        return out

    def upsert_prices(self, rows: Iterable[Dict[str, Any]]) -> int:
        """Write price rows. Returns the count attempted."""
        payload = [
            (
                r["symbol"], r["venue"], r["price"], r.get("bid"), r.get("ask"),
                r.get("previous_close"), r.get("change_pct"), r.get("as_of"),
                r.get("fetched_at") or _now_utc(), r.get("provider") or "eodhd",
            )
            for r in rows
        ]
        if not payload:
            return 0
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for start in range(0, len(payload), 1000):
                cursor.executemany(_MERGE_PRICE, payload[start:start + 1000])
            conn.commit()
        finally:
            conn.close()
        return len(payload)

    # -- daily bars --------------------------------------------------------

    def get_daily_bars(self, symbol: str, sessions: int) -> List[Dict[str, Any]]:
        """The last *sessions* completed sessions for one symbol, oldest first."""
        return self.get_daily_bars_many([symbol], sessions).get(symbol.strip().upper(), [])

    def get_daily_bars_many(
        self, symbols: Sequence[str], sessions: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """The last *sessions* completed sessions for each symbol, oldest first.

        One query for the whole batch: a per-symbol loop over 12 candidates is 12 round
        trips for a question that is one indexed seek each.
        """
        wanted = [s.strip().upper() for s in symbols if s and s.strip()]
        if not wanted:
            return {}
        out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in wanted}
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                self._execute(
                    cursor,
                    f"""
                    SELECT SymbolCode, SnapshotDate, [Open], [High], [Low], [Close],
                           Volume, Source, IsAdjusted
                    FROM (
                        SELECT SymbolCode, SnapshotDate, [Open], [High], [Low], [Close],
                               Volume, Source, IsAdjusted,
                               ROW_NUMBER() OVER (PARTITION BY SymbolCode
                                                  ORDER BY SnapshotDate DESC) AS rn
                        FROM dbo.DailySnapshot
                        WHERE SymbolCode IN ({placeholders})
                    ) ranked
                    WHERE rn <= ?
                    ORDER BY SymbolCode, SnapshotDate
                    """,
                    *chunk, int(sessions))
                for row in cursor.fetchall():
                    out[str(row[0]).upper()].append({
                        "session": row[1],
                        "open": _float(row[2]),
                        "high": _float(row[3]),
                        "low": _float(row[4]),
                        "close": _float(row[5]),
                        "volume": int(row[6]) if row[6] is not None else None,
                        "source": row[7],
                        "adjusted": bool(row[8]),
                    })
        finally:
            conn.close()
        return out

    def upsert_daily(self, rows: Iterable[Dict[str, Any]]) -> int:
        """Write daily bars through a staging table, in one short transaction.

        WHY NOT A ROW-AT-A-TIME MERGE

        A MERGE per row is one statement per row: ~50,000 statements for one backfilled
        session, which measured at roughly **3 minutes** -- and, worse, one transaction held
        open for all of it. SQL Server blocks readers behind an uncommitted writer, so while
        the daily job ran, ``/bars`` and ``/atr`` hung until they timed out. That is the
        "warehouse on the serving path" problem in a concrete form.

        Loading into a session-scoped temp table and merging once is set-based: the same
        50,000 rows land in a few seconds, so the write lock lives for seconds rather than
        minutes. The reader-side lock timeout (``LOCK_TIMEOUT_SECONDS``) is the second half
        of the fix -- a blocked read now fails fast with a named error instead of hanging.
        """
        payload = [
            (
                r["symbol"], r["session"], r["open"], r["high"], r["low"], r["close"],
                int(r.get("volume") or 0), r.get("source") or "eod-bulk",
                bool(r.get("adjusted", True)),
            )
            for r in rows
        ]
        if not payload:
            return 0

        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(_CREATE_STAGE_DAILY)
            for start in range(0, len(payload), 20000):
                cursor.executemany(
                    "INSERT INTO #stage (SymbolCode, SnapshotDate, [Open], [High], [Low], "
                    "[Close], Volume, Source, IsAdjusted) VALUES (?,?,?,?,?,?,?,?,?)",
                    payload[start:start + 20000])
            cursor.execute(_MERGE_DAILY_FROM_STAGE)
            conn.commit()
        finally:
            conn.close()
        return len(payload)

    def latest_session(self) -> Optional[date]:
        """The newest SnapshotDate in DailySnapshot, or ``None`` when empty."""
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(SnapshotDate) FROM dbo.DailySnapshot")
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def session_symbol_counts(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Symbols per recent session. Used by the job to prove a load actually landed."""
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT TOP (?) SnapshotDate, COUNT(*) AS symbols
                FROM dbo.DailySnapshot GROUP BY SnapshotDate ORDER BY SnapshotDate DESC
                """, int(limit))
            return [{"session": r[0], "symbols": int(r[1])} for r in cursor.fetchall()]
        finally:
            conn.close()

    # -- indicator cache ---------------------------------------------------

    def get_indicators(self, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        wanted = [s.strip().upper() for s in symbols if s and s.strip()]
        if not wanted:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for start in range(0, len(wanted), 1000):
                chunk = wanted[start:start + 1000]
                placeholders = ",".join("?" * len(chunk))
                self._execute(
                    cursor,
                    f"""
                    SELECT SymbolCode, Atr14, Atr20, Atr22, Atr26, Atr50, ReferencePrice,
                           SessionsUsed, AsOfSession, Method, Smoothing, UpdatedUtc
                    FROM dbo.IndicatorCache
                    WHERE SymbolCode IN ({placeholders})
                    """, *chunk)
                for row in cursor.fetchall():
                    out[str(row[0]).upper()] = {
                        "symbol": str(row[0]).upper(),
                        "values": {
                            14: _float(row[1]), 20: _float(row[2]), 22: _float(row[3]),
                            26: _float(row[4]), 50: _float(row[5]),
                        },
                        "reference_price": _float(row[6]),
                        "sessions_used": int(row[7]) if row[7] is not None else None,
                        "as_of_session": row[8],
                        "method": row[9],
                        "smoothing": row[10],
                        "updated_at": row[11],
                    }
        finally:
            conn.close()
        return out

    def upsert_indicators(self, rows: Iterable[Dict[str, Any]]) -> int:
        payload = [
            (
                r["symbol"],
                (r.get("values") or {}).get(14),
                (r.get("values") or {}).get(20),
                (r.get("values") or {}).get(22),
                (r.get("values") or {}).get(26),
                (r.get("values") or {}).get(50),
                r.get("reference_price"),
                r.get("sessions_used"),
                r.get("as_of_session"),
                r.get("method") or "tr",
                r.get("smoothing") or "simple",
            )
            for r in rows
        ]
        if not payload:
            return 0
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for start in range(0, len(payload), 1000):
                cursor.executemany(_MERGE_INDICATOR, payload[start:start + 1000])
            conn.commit()
        finally:
            conn.close()
        return len(payload)

    # -- vendor spend ------------------------------------------------------

    def request_types(self) -> List[Dict[str, Any]]:
        """The measured tariff. Read once at startup and cached by the caller."""
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT RequestType, CostBasis, CostEach, Endpoint, MeasuredAt, Is_Estimated "
                "FROM dbo.RequestTypes ORDER BY RequestType")
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [
            {
                "request_type": row[0],
                "cost_basis": row[1],
                "cost_each": int(row[2]) if row[2] is not None else None,
                "endpoint": row[3],
                "measured_at": row[4],
                "is_estimated": bool(row[5]),
            }
            for row in rows
        ]

    def record_usage(self, *, request_type: str, caller: str, calls: int = 1,
                     symbols: int = 0, cost: int = 0,
                     usage_date: Optional[date] = None) -> None:
        """Add one batch of calls to the daily ledger, merging by (date, caller, type).

        UPDATE-then-INSERT rather than MERGE: MERGE has a well-known race under concurrency, and the
        INSERT can lose a race here too, so a duplicate-key failure retries the UPDATE once. At this
        volume a collision is rare; losing the row because of one would be the accounting hole this
        table exists to close.

        ``FirstSeen`` keeps the earliest and ``LastSeen`` the latest, so a row read later still says
        when the spend happened rather than when it was last touched.
        """
        day = usage_date or _local_today()
        update = """
            UPDATE dbo.EODHDUsage
               SET Calls = Calls + ?,
                   Symbols = Symbols + ?,
                   CostTotal = CostTotal + ?,
                   FirstSeen = CASE WHEN SYSDATETIME() < FirstSeen
                                    THEN SYSDATETIME() ELSE FirstSeen END,
                   LastSeen = CASE WHEN SYSDATETIME() > LastSeen
                                   THEN SYSDATETIME() ELSE LastSeen END
             WHERE UsageDate = ? AND Caller = ? AND RequestType = ?
        """
        insert = """
            INSERT INTO dbo.EODHDUsage
                (UsageDate, Caller, RequestType, Calls, Symbols, CostTotal, FirstSeen, LastSeen)
            VALUES (?,?,?,?,?,?, SYSDATETIME(), SYSDATETIME())
        """
        params = (int(calls), int(symbols), int(cost), day, caller, request_type)

        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(update, *params)
            if cursor.rowcount == 0:
                try:
                    cursor.execute(insert, day, caller, request_type,
                                   int(calls), int(symbols), int(cost))
                except pyodbc.IntegrityError:
                    # Another thread inserted the same key between the UPDATE and the INSERT.
                    cursor.execute(update, *params)
            conn.commit()
        finally:
            conn.close()

    def bulk_pull_count(self, request_type: str,
                        usage_date: Optional[date] = None) -> int:
        """How many calls of *request_type* the ledger records for the day. Free to ask.

        Used as the freshness signal for the bulk refresh. Not MAX(SnapshotDate): single-symbol rows
        from /bars carry today's date, so the newest session can be today while the market-wide pull
        has not run.
        """
        day = usage_date or _local_today()
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ISNULL(SUM(Calls), 0) FROM dbo.EODHDUsage "
                "WHERE UsageDate = ? AND RequestType = ?", day, request_type)
            return int(cursor.fetchone()[0] or 0)
        finally:
            conn.close()

    def usage_summary(self, usage_date: Optional[date] = None) -> Dict[str, Any]:
        """The day's spend from the ledger: totals, and the breakdowns that explain them."""
        day = usage_date or _local_today()
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ISNULL(SUM(Calls),0), ISNULL(SUM(Symbols),0), ISNULL(SUM(CostTotal),0), "
                "       COUNT(*) "
                "FROM dbo.EODHDUsage WHERE UsageDate = ?", day)
            calls, symbols, cost, rowcount = cursor.fetchone()

            cursor.execute(
                "SELECT RequestType, SUM(Calls), SUM(Symbols), SUM(CostTotal) "
                "FROM dbo.EODHDUsage WHERE UsageDate = ? "
                "GROUP BY RequestType ORDER BY SUM(CostTotal) DESC, RequestType", day)
            by_type = [
                {"request_type": row[0], "calls": int(row[1] or 0),
                 "symbols": int(row[2] or 0), "cost": int(row[3] or 0)}
                for row in cursor.fetchall()
            ]

            cursor.execute(
                "SELECT Caller, SUM(Calls), SUM(CostTotal) "
                "FROM dbo.EODHDUsage WHERE UsageDate = ? "
                "GROUP BY Caller ORDER BY SUM(CostTotal) DESC, Caller", day)
            by_caller = [
                {"caller": row[0], "calls": int(row[1] or 0), "cost": int(row[2] or 0)}
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()
        return {
            "date": day.isoformat(),
            "calls": int(calls or 0),
            "symbols": int(symbols or 0),
            "cost": int(cost or 0),
            "rows": int(rowcount or 0),
            "by_request_type": by_type,
            "by_caller": by_caller,
        }

    def ping(self) -> None:
        """Cheapest possible liveness check: one round trip, no table touched.

        Deliberately not ``stats()``: health checks run often, and counting rows across
        DailySnapshot on every one of them both costs real time and turns a health check
        into a load source. The ``SELECT 1`` still proves the connection, the login and the
        database are all working.
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            conn.close()

    def stats(self) -> Dict[str, Any]:
        """Row counts, for /stats and the job's log line."""
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    (SELECT COUNT(*) FROM dbo.SymbolPriceCache),
                    (SELECT COUNT(*) FROM dbo.DailySnapshot),
                    (SELECT COUNT(DISTINCT SymbolCode) FROM dbo.DailySnapshot),
                    (SELECT COUNT(*) FROM dbo.IndicatorCache),
                    (SELECT MAX(SnapshotDate) FROM dbo.DailySnapshot),
                    (SELECT MAX(LastUpdatedUtc) FROM dbo.SymbolPriceCache)
            """)
            row = cursor.fetchone()
        finally:
            conn.close()
        return {
            "price_rows": int(row[0] or 0),
            "daily_rows": int(row[1] or 0),
            "daily_symbols": int(row[2] or 0),
            "indicator_rows": int(row[3] or 0),
            "latest_session": row[4].isoformat() if row[4] else None,
            "latest_price_at": row[5].isoformat() if row[5] else None,
        }
