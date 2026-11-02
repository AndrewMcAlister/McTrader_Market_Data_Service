"""The store: three tables, and the queries the products need.

Nothing in this module names a database. It takes a connection string, so the
The ``Back_Test`` -> ``Market_Data`` rename was a configuration change (``MDS_DATABASE``) rather
than a migration.

Upserts use ``MERGE`` rather than update-then-insert because the daily job writes ~2,800
rows at a time and two round trips per row would turn a one-second load into a minute. All
writes are batched through ``executemany`` with ``fast_executemany`` on.

Both bar tables are keyed by venue as well as symbol. A bare ticker is not unique -- the same
code can be an ASX listing and a US one -- so a symbol-only key lets one venue's row
overwrite the other's without a word. The price cache has always carried ``Venue`` for that
reason; the bar tables carry ``Exchange``, which is the same market code under the name the
vendor and its feeds use for it (``US``, ``AU``, ...).

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
    ON target.SymbolCode = source.SymbolCode
   AND target.Exchange = source.Exchange
   AND target.SnapshotDate = source.SnapshotDate
WHEN MATCHED THEN UPDATE SET
    [Open] = source.[Open], [High] = source.[High], [Low] = source.[Low],
    [Close] = source.[Close],
    -- A source with no raw close (the BarHourly seed, or a replayed benchmark row) must not
    -- erase one already collected. COALESCE keeps the known value; a fresh pull overwrites it.
    [RawClose] = COALESCE(source.[RawClose], target.[RawClose]),
    Volume = source.Volume, Source = source.Source,
    IsAdjusted = source.IsAdjusted, LoadedUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
    (SymbolCode, Exchange, SnapshotDate, [Open], [High], [Low], [Close], [RawClose], Volume,
     Source, IsAdjusted)
VALUES
    (source.SymbolCode, source.Exchange, source.SnapshotDate, source.[Open],
     source.[High], source.[Low], source.[Close], source.[RawClose], source.Volume,
     source.Source, source.IsAdjusted);
"""

_CREATE_STAGE_DAILY = """
CREATE TABLE #stage (
    SymbolCode   varchar(24)   NOT NULL,
    Exchange     varchar(8)    NOT NULL,
    SnapshotDate date          NOT NULL,
    [Open]       decimal(18,6) NOT NULL,
    [High]       decimal(18,6) NOT NULL,
    [Low]        decimal(18,6) NOT NULL,
    [Close]      decimal(18,6) NOT NULL,
    [RawClose]   decimal(18,6) NULL,
    Volume       bigint        NOT NULL,
    Source       varchar(16)   NOT NULL,
    IsAdjusted   bit           NOT NULL
);
"""

def _exchange_key(value: Any, *, required: bool = False) -> str:
    """The Exchange (or Venue) column's value for a venue: an upper-case market code.

    A read defaults to US, which is what a caller that says nothing about its venue means
    and what the API itself defaults to. A write passes ``required=True``: a row whose venue
    was forgotten is not a US row, it is an unknown one -- and defaulting it is exactly how
    an ASX session came to be filed as US in the first place.

    The tables have no default for this column either, so a writer that bypasses this module
    is refused by the database rather than filed as US. The rule is the same at both layers:
    a venue is something a writer states, never something it inherits.
    """
    text = str(value or "").strip().upper()
    if text:
        return text
    if required:
        raise ValueError(
            "a row must state its venue (Exchange); refusing to file an unknown venue as US")
    return "US"


_MERGE_PRICE = """
MERGE dbo.SymbolPriceCache AS target
USING (SELECT ? AS SymbolCode, ? AS Venue, ? AS Price, ? AS Bid, ? AS Ask,
              ? AS PreviousClose, ? AS ChangePct, ? AS Volume, ? AS AsOfUtc,
              ? AS LastUpdatedUtc, ? AS Source) AS source
    ON target.SymbolCode = source.SymbolCode AND target.Venue = source.Venue
WHEN MATCHED THEN UPDATE SET
    Price = source.Price, Bid = source.Bid, Ask = source.Ask,
    PreviousClose = source.PreviousClose, ChangePct = source.ChangePct,
    Volume = source.Volume,
    AsOfUtc = source.AsOfUtc, LastUpdatedUtc = source.LastUpdatedUtc, Source = source.Source
WHEN NOT MATCHED THEN INSERT
    (SymbolCode, Venue, Price, Bid, Ask, PreviousClose, ChangePct, Volume, AsOfUtc,
     LastUpdatedUtc, Source)
VALUES
    (source.SymbolCode, source.Venue, source.Price, source.Bid, source.Ask,
     source.PreviousClose, source.ChangePct, source.Volume, source.AsOfUtc,
     source.LastUpdatedUtc, source.Source);
"""

_MERGE_INDICATOR = """
MERGE dbo.IndicatorCache AS target
USING (SELECT ? AS SymbolCode, ? AS Exchange, ? AS Atr14, ? AS Atr20, ? AS Atr22,
              ? AS Atr26, ? AS Atr50, ? AS ReferencePrice, ? AS SessionsUsed,
              ? AS AsOfSession, ? AS Method, ? AS Smoothing) AS source
    ON target.SymbolCode = source.SymbolCode AND target.Exchange = source.Exchange
WHEN MATCHED THEN UPDATE SET
    Atr14 = source.Atr14, Atr20 = source.Atr20, Atr22 = source.Atr22, Atr26 = source.Atr26,
    Atr50 = source.Atr50, ReferencePrice = source.ReferencePrice,
    SessionsUsed = source.SessionsUsed, AsOfSession = source.AsOfSession,
    Method = source.Method, Smoothing = source.Smoothing, UpdatedUtc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT
    (SymbolCode, Exchange, Atr14, Atr20, Atr22, Atr26, Atr50, ReferencePrice, SessionsUsed,
     AsOfSession, Method, Smoothing)
VALUES
    (source.SymbolCode, source.Exchange, source.Atr14, source.Atr20, source.Atr22,
     source.Atr26, source.Atr50, source.ReferencePrice, source.SessionsUsed,
     source.AsOfSession, source.Method, source.Smoothing);
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
        """Write price rows. Returns the count attempted.

        The venue is required, not defaulted: the key is (symbol, venue), so a row without
        one would overwrite another venue's price rather than create its own.
        """
        payload = [
            (
                r["symbol"], _exchange_key(r.get("venue"), required=True), r["price"],
                r.get("bid"), r.get("ask"),
                r.get("previous_close"), r.get("change_pct"), r.get("volume"),
                r.get("as_of"), r.get("fetched_at") or _now_utc(),
                r.get("provider") or "eodhd",
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

    def get_daily_bars(self, symbol: str, sessions: int,
                       exchange: str = "US") -> List[Dict[str, Any]]:
        """The last *sessions* completed sessions for one symbol, oldest first."""
        return self.get_daily_bars_many([symbol], sessions, exchange).get(
            symbol.strip().upper(), [])

    def get_daily_bars_many(
        self, symbols: Sequence[str], sessions: int, exchange: str = "US",
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
                           Volume, Source, IsAdjusted, [RawClose]
                    FROM (
                        SELECT SymbolCode, SnapshotDate, [Open], [High], [Low], [Close],
                               Volume, Source, IsAdjusted, [RawClose],
                               ROW_NUMBER() OVER (PARTITION BY SymbolCode
                                                  ORDER BY SnapshotDate DESC) AS rn
                        FROM dbo.DailySnapshot
                        WHERE Exchange = ? AND SymbolCode IN ({placeholders})
                    ) ranked
                    WHERE rn <= ?
                    ORDER BY SymbolCode, SnapshotDate
                    """,
                    _exchange_key(exchange), *chunk, int(sessions))
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
                        "raw_close": _float(row[9]),
                    })
        finally:
            conn.close()
        return out

    def daily_bar_depth(self, symbols: Sequence[str], exchange: str = "US",
                        since: Optional[date] = None) -> Dict[str, int]:
        """How many sessions each symbol holds in ``DailySnapshot`` on or after *since*.

        The bars path asks this before a vendor refresh: one ``/eod/`` call costs a unit
        whatever its width, so asking for two years costs nothing extra, but re-writing
        several hundred rows per symbol on every daily refresh is real write load. Counting
        first makes the deep fetch a fill rather than a nightly rewrite. One range on the
        clustered key per symbol, grouped into a single query.
        """
        wanted = [s.strip().upper() for s in symbols if s and s.strip()]
        if not wanted:
            return {}
        out: Dict[str, int] = {s: 0 for s in wanted}
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                sql = f"""
                    SELECT SymbolCode, COUNT(*)
                      FROM dbo.DailySnapshot
                     WHERE Exchange = ? AND SymbolCode IN ({placeholders})
                """
                params: List[Any] = [_exchange_key(exchange), *chunk]
                if since is not None:
                    sql += " AND SnapshotDate >= ?"
                    params.append(since)
                sql += " GROUP BY SymbolCode"
                self._execute(cursor, sql, *params)
                for row in cursor.fetchall():
                    out[str(row[0]).upper()] = int(row[1])
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
                r["symbol"], _exchange_key(r.get("exchange"), required=True), r["session"],
                r["open"], r["high"], r["low"], r["close"], r.get("raw_close"),
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
                    "INSERT INTO #stage (SymbolCode, Exchange, SnapshotDate, [Open], [High], "
                    "[Low], [Close], [RawClose], Volume, Source, IsAdjusted) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
        """Symbols landed per recent session, per exchange.

        Per exchange because a session date is not one load: the ASX and the US can both
        write 2026-10-30, and a single count would say "44,000 symbols" for what is two
        separate collections. The outer query is limited by *sessions*, not by rows, so
        ``limit=5`` is still five sessions even when both venues are present.
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT SnapshotDate, Exchange, COUNT(*) AS symbols
                FROM dbo.DailySnapshot
                WHERE SnapshotDate IN (
                    SELECT TOP (?) SnapshotDate FROM dbo.DailySnapshot
                    GROUP BY SnapshotDate ORDER BY SnapshotDate DESC)
                GROUP BY SnapshotDate, Exchange
                ORDER BY SnapshotDate DESC, Exchange
                """, int(limit))
            return [{"session": r[0], "exchange": r[1], "symbols": int(r[2])}
                    for r in cursor.fetchall()]
        finally:
            conn.close()

    # -- indicator cache ---------------------------------------------------

    def get_indicators(self, symbols: Sequence[str],
                       exchange: str = "US") -> Dict[str, Dict[str, Any]]:
        """The cached ATR for *symbols* at one exchange, keyed by bare symbol.

        The venue is part of the key, so it is part of the question: the same ticker can
        have a US series and an ASX one, and two different ATRs. Without the filter the
        answer for the second venue would be whichever row the database happened to return.
        """
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
                    WHERE Exchange = ? AND SymbolCode IN ({placeholders})
                    """, _exchange_key(exchange), *chunk)
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
                _exchange_key(r.get("exchange"), required=True),
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

    def big_movers(self, venue: str, limit: int = 25, min_price: float = 1.0,
                   min_dollar_volume: float = 0.0,
                   since: Optional[datetime] = None) -> Dict[str, List[Dict[str, Any]]]:
        """The venue's winners and losers by percent, from the price cache.

        Ranked by percent, floored by price and dollar volume, because a 300% move on
        4,000 shares is noise; a caller that wants pure percent passes zero floors.
        ``volume`` is the provider's latest cumulative volume, so a row collected at 10:15
        carries the volume traded up to 10:15 -- which is what "latest volume" means here.

        ``since`` is an observation floor: pass the session open and a row left behind by
        an earlier session is excluded rather than ranked as if it moved today. Without it
        the method answers from every row in the cache -- correct for "what is cached",
        wrong for "what is moving today".

        Returns ``{"winners": [...], "losers": [...]}``.
        """
        limit = max(0, int(limit))
        fresh = ""
        if since is not None:
            if since.tzinfo is not None:
                since = since.astimezone(timezone.utc).replace(tzinfo=None)
            fresh = "\n               AND COALESCE(AsOfUtc, LastUpdatedUtc) >= ?"
        sql = """
            SELECT TOP (?)
                   SymbolCode, Price, ChangePct, Volume, PreviousClose, AsOfUtc,
                   Source, CAST(Price * Volume AS decimal(38, 2)) AS DollarVolume
              FROM dbo.SymbolPriceCache
             WHERE Venue = ?
               AND Price >= ?
               AND ChangePct IS NOT NULL
               AND Volume IS NOT NULL AND Volume > 0
               AND Price * Volume >= ?
               AND ChangePct {direction} 0""" + fresh + """
          ORDER BY ChangePct {order}
        """
        params: List[Any] = [limit, venue, float(min_price), float(min_dollar_volume)]
        if since is not None:
            params.append(since)
        out: Dict[str, List[Dict[str, Any]]] = {"winners": [], "losers": []}
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for key, direction, order in (("winners", ">=", "DESC"),
                                          ("losers", "<", "ASC")):
                cursor.execute(sql.format(direction=direction, order=order), *params)
                for row in cursor.fetchall():
                    out[key].append({
                        "symbol": row[0],
                        "price": float(row[1]),
                        "change_pct": float(row[2]),
                        "volume": int(row[3]),
                        "previous_close": float(row[4]) if row[4] is not None else None,
                        "as_of_utc": row[5],
                        "source": row[6],
                        "dollar_volume": float(row[7]) if row[7] is not None else None,
                    })
        finally:
            conn.close()
        return out

    def latest_venue_price(self, venue: str) -> Optional[datetime]:
        """The observation time of the newest price stored for ``venue``.

        Observation, not write time, because "is today's data in?" is a question about the
        market's clock: a row written now from a quote stamped before the open is not
        today's data. ``LastUpdatedUtc`` is the fallback for a provider that sends no
        stamp -- it is the only evidence such a row carries.

        Venue-tagged, which is what makes a per-country answer possible at all:
        SymbolPriceCache carries a Venue column. DailySnapshot carries Exchange for the same
        reason but is deliberately not consulted here -- the question is about prices
        collected since the open, not about settled bars.

        Returns a NAIVE datetime: the columns are datetime2 and hold UTC that lost its
        label. Callers must attach UTC rather than localising it (availability._as_utc).
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT MAX(COALESCE(AsOfUtc, LastUpdatedUtc)) FROM dbo.SymbolPriceCache "
                "WHERE Venue = ?", venue)
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else None
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

    def top_symbols_by_dollar_volume(self, venue: str, limit: int = 100,
                                     min_breadth: int = 50) -> List[str]:
        """The venue's most-traded symbols by dollar volume on its newest broad session.

        ``min_breadth`` is what makes "newest" mean "newest session the market actually
        traded". A single-symbol ``/bars`` write lands in DailySnapshot carrying today's
        date while the market-wide pull is still on the previous session, so a plain
        MAX(SnapshotDate) would rank a handful of codes. A session counts once at least
        ``min_breadth`` symbols have a bar for it: the ASX's market-wide pulls carry ~2,300
        and the thinnest real ones measured ~300, while a stray single-symbol row cannot
        reach the floor.

        At an open + 45 run the newest broad session is the last settled one -- Friday on a
        Monday -- which is the right seed: today's bars are still in progress and cannot be
        ranked. Codes are bare symbols, the vocabulary ``pull_bulk`` writes.

        Returns ``[]`` when no session is broad enough or ``limit`` is not positive, so a
        caller can treat "no universe" as a named outcome instead of pricing nothing.
        """
        limit = max(0, int(limit))
        if limit == 0:
            return []
        sql = """
            WITH broad AS (
                SELECT SnapshotDate
                  FROM dbo.DailySnapshot
                 WHERE Exchange = ?
                 GROUP BY SnapshotDate
                HAVING COUNT(*) >= ?
            ), newest AS (
                SELECT TOP (1) SnapshotDate FROM broad ORDER BY SnapshotDate DESC
            )
            SELECT TOP (?) SymbolCode
              FROM dbo.DailySnapshot d
              JOIN newest n ON n.SnapshotDate = d.SnapshotDate
             WHERE d.Exchange = ?
               AND d.[Close] > 0
               AND d.Volume > 0
             ORDER BY d.[Close] * d.Volume DESC
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, venue, max(1, int(min_breadth)), limit, venue)
            return [row[0] for row in cursor.fetchall()]
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
        """Row counts, for /stats and the job's log line.

        ``daily_symbols`` counts (symbol, exchange) pairs, not bare tickers: one ticker on
        two venues is two series, and the count that matters is the number of series.
        """
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    (SELECT COUNT(*) FROM dbo.SymbolPriceCache),
                    (SELECT COUNT(*) FROM dbo.DailySnapshot),
                    (SELECT COUNT(*) FROM
                        (SELECT DISTINCT SymbolCode, Exchange FROM dbo.DailySnapshot) AS s),
                    (SELECT COUNT(*) FROM dbo.IndicatorCache),
                    (SELECT MAX(SnapshotDate) FROM dbo.DailySnapshot),
                    (SELECT MAX(LastUpdatedUtc) FROM dbo.SymbolPriceCache)
            """)
            row = cursor.fetchone()
            cursor.execute("""
                SELECT Exchange, COUNT(*) AS bars FROM dbo.DailySnapshot
                GROUP BY Exchange ORDER BY Exchange
            """)
            by_exchange = [{"exchange": r[0], "bars": int(r[1])} for r in cursor.fetchall()]
        finally:
            conn.close()
        return {
            "price_rows": int(row[0] or 0),
            "daily_rows": int(row[1] or 0),
            "daily_symbols": int(row[2] or 0),
            "indicator_rows": int(row[3] or 0),
            "latest_session": row[4].isoformat() if row[4] else None,
            "latest_price_at": row[5].isoformat() if row[5] else None,
            "daily_by_exchange": by_exchange,
        }

