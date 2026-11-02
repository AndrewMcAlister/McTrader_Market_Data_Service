/* 002 — DailySnapshot: a session-wide read should not touch the clustered index.

 * WHAT THE SERVICE ASKS FOR
 *
 *   the last N sessions for one symbol at one venue   -> PK (SymbolCode, Exchange, SnapshotDate)
 *   one session, every symbol (movers, comparisons)   -> IX_DailySnapshot_Date
 *   one session at one venue                          -> IX_DailySnapshot_Date, plus a filter
 *   rebuild_indicators: COUNT(*) per symbol+venue     -> a scan of the clustered PK
 *
 * The first is a clustered seek and needs nothing. The third is the one that was wrong: the
 * date index carried only (Close, High, Low), so any query that also wanted Volume, Open or
 * IsAdjusted did a key lookup per row into the 223 MB clustered index. Measured on this
 * database at 2026-11-02 11:11, the index had 11,986 scans against 9,595 key lookups -- more
 * lookups than seeks, which is the signature of a non-covering index.
 *
 * The missing-index DMV said the same thing three ways:
 *   [SnapshotDate] + [Close] INCLUDE [Volume]            impact 91.8
 *   [SnapshotDate], [Exchange] INCLUDE [Close], [Volume] impact 99.5
 *   [Exchange] INCLUDE [Open], [High], [Low], [Close]    impact 62.7
 *
 * So the date index becomes a covering index with Exchange as the second key: one session for
 * all symbols is still a seek on the leading column, one session at one venue is a seek on
 * both, and every column a session-wide read has asked for is in the index.
 *
 * WHY NOTHING ELSE
 *
 * - SymbolPriceCache is 12,086 rows and 1.6 MB. Its per-venue MAX reads that whole table in
 *   under a millisecond; an index would cost more in writes than it saves in reads.
 * - IndicatorCache is 3,207 rows; its PK is already (SymbolCode, Exchange).
 * - EodhdUsage is 258 rows with PK (UsageDate, Caller, RequestType); its lookups are seeks.
 * - A narrow (SymbolCode, Exchange) index for the rebuild's GROUP BY would scan 36 MB instead
 *   of the clustered 223 MB -- about one second, once per bulk load. Not worth maintaining a
 *   second copy of every row for that.
 *
 * APPLY WHEN THE LOAD IS QUIET. This builds ~120 MB and drops ~144 MB; on Standard edition
 * the rebuild is offline, so the table is unavailable for the duration.
 */
SET NOCOUNT ON;
GO

/* Only swap when the old index is there and the new one is not: re-running is a no-op. */
IF EXISTS (SELECT 1 FROM sys.indexes
            WHERE object_id = OBJECT_ID('dbo.DailySnapshot') AND name = 'IX_DailySnapshot_Date')
   AND NOT EXISTS (SELECT 1 FROM sys.indexes
                    WHERE object_id = OBJECT_ID('dbo.DailySnapshot') AND name = 'IX_DailySnapshot_Session')
BEGIN
    CREATE NONCLUSTERED INDEX IX_DailySnapshot_Session
        ON dbo.DailySnapshot (SnapshotDate, Exchange)
        INCLUDE ([Open], [High], [Low], [Close], Volume, IsAdjusted)
        WITH (FILLFACTOR = 90, SORT_IN_TEMPDB = ON);

    DROP INDEX IX_DailySnapshot_Date ON dbo.DailySnapshot;
END
GO

/* If a fresh database is created from 001 after this file has been applied, 001 still creates
   the old index. Both are kept in step by running 001 then 002, which is the documented order
   for a rebuild. */
