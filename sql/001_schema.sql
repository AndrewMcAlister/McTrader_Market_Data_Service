/* McTrader_Market_Data_Service — operational tables.
 *
 * Target database is `Back_Test` today, to be renamed `Market_Data` — that rename is the
 * point: this database stops being only a backtest warehouse and becomes the market data
 * source of record, so its operational tables belong here rather than beside it.
 *
 * Consequence for this file: nothing in it names a database. It is run against whichever
 * database MDS_DATABASE points at, so the rename is a connection-string change and not a
 * migration. See README §"The Back_Test -> Market_Data rename".
 *
 * Three tables, as specified: SymbolPriceCache, DailySnapshot, IndicatorCache.
 *
 * Idempotent: safe to re-run. Everything uses CREATE ... IF NOT EXISTS style
 * guards because the daily job calls ensure_schema() on every start.
 *
 * WHY THESE SHAPES
 *
 * SymbolPriceCache holds the latest known price per symbol, keyed (symbol, venue).
 * The venue is in the key because AAPL.US and AAPL.AU are different instruments and
 * a bare-symbol key would let one overwrite the other.
 *
 * It carries PreviousClose and ChangePct beyond the five columns originally specified,
 * because the morning scan's whole question is "how far has this moved since the close",
 * and computing that from a stored price alone would need a second table. Storing the
 * provider's own previousClose beside the price keeps the pair atomic: they were
 * observed together and must not be joined from different sessions.
 *
 * Bid/Ask are kept from the original spec and will be NULL: EODHD's /real-time/ payload
 * carries open/high/low/close/previousClose/volume but no quote side. The columns cost
 * nothing and document that the absence is a vendor fact, not an omission.
 *
 * DailySnapshot is the daily bar series, one row per symbol per venue per session. It is
 * keyed (SymbolCode, Exchange, SnapshotDate) in that order so "the last N sessions for one
 * symbol on one venue" is a clustered seek -- the exact access path an ATR read needs. The
 * venue is in the key because a bare ticker is not unique: the same code can be an ASX
 * listing and a US one, and a symbol-only key lets one venue's row overwrite the other's
 * without a word.
 *
 * The venue has no default, on any of the three tables. A row filed under the wrong venue
 * is indistinguishable from a right one, so the database does not invent one: every writer
 * must state it, and a write that does not is refused rather than filed as US.
 *
 * IsAdjusted exists because the warehouse already carries both conventions and documents
 * the trap: Bar5Min is NOT split-adjusted while BarHourly is, and a split shows as a
 * jump (JDZG, 2026-03-23: $0.0928 against $20.925 the same day). An ATR mixing the two
 * is silently wrong, so every row states which it is.
 *
 * IndicatorCache is the precomputed ATR, so no trader recalculates it at 10:30. It holds
 * five periods: 14, 20 and 50 as specified, plus 22 and 26 because those are the two the
 * existing books actually read -- 22 is McTrader's Chandelier window and its preferred
 * stop ATR, and 26 is PortfolioAdvisor's MACD-V period. Atr20 and Atr50 are currently read
 * by nothing; they are here because they were asked for and cost a few microseconds.
 *
 * ReferencePrice and SessionsUsed travel with the values because percent is derived from
 * them: storing Atr22Pct as well would let it drift out of step with the price it was
 * measured against.
 */

SET NOCOUNT ON;

/* ------------------------------------------------------------------ */
/* 1. SymbolPriceCache — latest known price per instrument              */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('dbo.SymbolPriceCache', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.SymbolPriceCache (
        SymbolCode     varchar(24)   NOT NULL,
        Venue          varchar(8)    NOT NULL,
        Price          decimal(18,6) NOT NULL,
        Bid            decimal(18,6) NULL,
        Ask            decimal(18,6) NULL,
        PreviousClose  decimal(18,6) NULL,
        ChangePct      decimal(10,4) NULL,
        Volume         bigint        NULL,
        AsOfUtc        datetime2     NULL,
        LastUpdatedUtc datetime2     NOT NULL,
        Source         varchar(16)   NOT NULL,
        CONSTRAINT PK_SymbolPriceCache PRIMARY KEY CLUSTERED (SymbolCode, Venue)
    );

    /* The scan asks "everything I priced in the last five minutes"; without this the
       answer is a full scan of a table that grows to the whole US market. */
    CREATE NONCLUSTERED INDEX IX_SymbolPriceCache_Updated
        ON dbo.SymbolPriceCache (LastUpdatedUtc) INCLUDE (Price, PreviousClose);
END
GO

/* Volume arrived with todays-big-movers (2026-11-01): the providers were already
   returning it and the cache was throwing it away, so a mover could be ranked by
   percent and not by how much was actually traded. Guarded, so an existing database
   gains the column on the next start and a fresh one gets it from the CREATE above. */
IF COL_LENGTH('dbo.SymbolPriceCache', 'Volume') IS NULL
    ALTER TABLE dbo.SymbolPriceCache ADD Volume bigint NULL;
GO

/* ------------------------------------------------------------------ */
/* 2. DailySnapshot — the daily bar series, one row per symbol-venue-session */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('dbo.DailySnapshot', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.DailySnapshot (
        SymbolCode   varchar(24)   NOT NULL,
        Exchange     varchar(8)    NOT NULL,
        SnapshotDate date          NOT NULL,
        [Open]       decimal(18,6) NOT NULL,
        [High]       decimal(18,6) NOT NULL,
        [Low]        decimal(18,6) NOT NULL,
        [Close]      decimal(18,6) NOT NULL,
        [RawClose]   decimal(18,6) NULL,
        Volume       bigint        NOT NULL CONSTRAINT DF_DailySnapshot_Volume DEFAULT (0),
        Source       varchar(16)   NOT NULL CONSTRAINT DF_DailySnapshot_Source DEFAULT ('eod-bulk'),
        IsAdjusted   bit           NOT NULL CONSTRAINT DF_DailySnapshot_IsAdjusted DEFAULT (1),
        LoadedUtc    datetime2     NOT NULL CONSTRAINT DF_DailySnapshot_LoadedUtc DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_DailySnapshot PRIMARY KEY CLUSTERED (SymbolCode, Exchange, SnapshotDate)
    );

    /* A dated whole-market read (the backfill, and the "who moved today" query) scans by
       date rather than by symbol. */
    CREATE NONCLUSTERED INDEX IX_DailySnapshot_Date
        ON dbo.DailySnapshot (SnapshotDate) INCLUDE ([Close], [High], [Low]);
END
GO

/* ------------------------------------------------------------------ */
/* 3. IndicatorCache — the precomputed ATR                            */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('dbo.IndicatorCache', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.IndicatorCache (
        SymbolCode     varchar(24)   NOT NULL,
        Exchange       varchar(8)    NOT NULL,
        Atr14          decimal(18,6) NULL,
        Atr20          decimal(18,6) NULL,
        Atr22          decimal(18,6) NULL,
        Atr26          decimal(18,6) NULL,
        Atr50          decimal(18,6) NULL,
        ReferencePrice decimal(18,6) NULL,
        SessionsUsed   int           NULL,
        AsOfSession    date          NULL,
        Method         varchar(12)   NOT NULL CONSTRAINT DF_IndicatorCache_Method DEFAULT ('tr'),
        Smoothing      varchar(12)   NOT NULL CONSTRAINT DF_IndicatorCache_Smoothing DEFAULT ('simple'),
        UpdatedUtc     datetime2     NOT NULL CONSTRAINT DF_IndicatorCache_UpdatedUtc DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_IndicatorCache PRIMARY KEY CLUSTERED (SymbolCode, Exchange)
    );
END
GO

/* ------------------------------------------------------------------ */
/* 4. Self-documentation, following this database's own convention     */
/* ------------------------------------------------------------------ */
/* Every other table in Back_Test carries a TableDoc row and ColumnDoc rows for the
   columns that are not self-explanatory. A new table without them is the one thing
   that would look out of place here. */

IF NOT EXISTS (SELECT 1 FROM dbo.TableDoc WHERE TableName = 'SymbolPriceCache')
    INSERT INTO dbo.TableDoc (TableName, Grain, Purpose, RowCountNote, DataSource)
    VALUES ('SymbolPriceCache',
            'one row per symbol per venue',
            'LATEST KNOWN PRICE, written by D:\McTrader_Market_Data_Service. Operational, not historical: a row is overwritten in place when the price is refreshed, so this table holds one price per instrument and no history. Read it when you want the current price; never join it to a past session, because the row carries no record of what it said yesterday. Source=''eodhd''; LastUpdatedUtc is when this service fetched it, AsOfUtc is when the vendor observed it and can be older.',
            'grows to the priced universe (~2.8k rows)', 'measured');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.TableDoc WHERE TableName = 'DailySnapshot')
    INSERT INTO dbo.TableDoc (TableName, Grain, Purpose, RowCountNote, DataSource)
    VALUES ('DailySnapshot',
            'one row per symbol per venue per session',
            'Daily OHLCV, appended once per session by D:\McTrader_Market_Data_Service from EODHD /eod-bulk-last-day. Close is adjusted_close where the vendor supplies it (IsAdjusted=1), matching BarHourly''s convention and never mixed with Bar5Min, which is unadjusted. RawClose is the unadjusted close and may be NULL where it is not known (the BarHourly seed). This is the series ATR is computed from; IndicatorCache holds the result.',
            'universe x sessions', 'measured');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.TableDoc WHERE TableName = 'IndicatorCache')
    INSERT INTO dbo.TableDoc (TableName, Grain, Purpose, RowCountNote, DataSource)
    VALUES ('IndicatorCache',
            'one row per symbol per venue',
            'PRECOMPUTED ATR, so no trader recalculates it at entry time. Method ''tr'' is gap-inclusive true range max(H-L, |H-prevClose|, |L-prevClose|); ''high_low'' is H-L only, which is NOT what any McTrader/PaperTrader implementation uses and narrows a stop. Smoothing ''simple'' is the mean of the last N true ranges (what the two stop books use); ''wilder'' is Wilder''s recursion. Percent is derived: Atr / ReferencePrice * 100.',
            'one row per symbol with enough history', 'derived');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'IndicatorCache' AND ColumnName = 'Method')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('IndicatorCache', 'Method', 'varchar(12)', 'enum', 
            'tr = gap-inclusive true range; high_low = H-L only.',
            'The two differ by a median 4.3% and up to 25.8% on gappy names, so a stored ATR without this is not reproducible.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'IndicatorCache' AND ColumnName = 'Smoothing')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('IndicatorCache', 'Smoothing', 'varchar(12)', 'enum',
            'simple = mean of the last N true ranges; wilder = Wilder recursion.',
            'simple is what PaperTrader and Mini-McTrader use for their stops. wilder is what technical_utils.wilder_atr_series and RegimeService use. Median difference 3.2%.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'IndicatorCache' AND ColumnName = 'ReferencePrice')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('IndicatorCache', 'ReferencePrice', 'decimal(18,6)', 'price',
            'The close the ATR was measured against.',
            'Percent is derived from this, not stored: Atr / ReferencePrice * 100. Storing the percent as well would let it drift out of step with the price.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'IndicatorCache' AND ColumnName = 'Atr22')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('IndicatorCache', 'Atr22', 'decimal(18,6)', 'price',
            'ATR over 22 sessions.',
            'The period McTrader actually consumes: Chandelier window, and the preferred stop ATR in PositionGuards.resolve_atr_percent. Atr20 and Atr50 are carried because they were asked for but are read by nothing.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'IndicatorCache' AND ColumnName = 'Atr26')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('IndicatorCache', 'Atr26', 'decimal(18,6)', 'price',
            'ATR over 26 sessions.',
            'PortfolioAdvisor''s MACD-V divisor. Deliberately 26 and not 14: indicators.py records that the conventional 14 is the obvious mistake there because the ATR should match the MACD slow EMA.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'DailySnapshot' AND ColumnName = 'IsAdjusted')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('DailySnapshot', 'IsAdjusted', 'bit', 'flag',
            '1 = Close is split-adjusted (EODHD adjusted_close); 0 = Close is the raw close.',
            'Never mix 1 and 0 rows in one ATR: a split reads as a catastrophic move. Matches BarHourly (adjusted), not Bar5Min (unadjusted). RawClose is independent of this flag and is always the traded close.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'DailySnapshot' AND ColumnName = 'RawClose')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('DailySnapshot', 'RawClose', 'decimal(18,6)', 'price',
            'The unadjusted close the market actually traded.',
            'Close answers "what is the adjusted series"; RawClose answers "what price printed", and candle bodies, structure and orders read the second. NULL means not known: the free BarHourly seed is split-adjusted only, and rows written before 2026-11-02 are filled by the service''s first on-demand re-pull for a symbol. Never treat NULL as zero or as Close without saying so at the call site.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'SymbolPriceCache' AND ColumnName = 'PreviousClose')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('SymbolPriceCache', 'PreviousClose', 'decimal(18,6)', 'price',
            'The vendor''s own previous close, stored with the price.',
            'Stored beside the price rather than joined from DailySnapshot so the pair is atomic: they were observed together. The morning scan''s question is ChangePct, and computing it from a price and a separately-fetched close is how two tapes get mixed.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'SymbolPriceCache' AND ColumnName = 'AsOfUtc')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('SymbolPriceCache', 'AsOfUtc', 'datetime2', 'UTC instant',
            'When the vendor observed the price.',
            'Not when this service fetched it -- that is LastUpdatedUtc. EODHD''s US real-time stamp was measured running well behind the fetch, so the two ages answer different questions and both are stored.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'SymbolPriceCache' AND ColumnName = 'Bid')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('SymbolPriceCache', 'Bid', 'decimal(18,6)', 'price',
            'Always NULL on this vendor.',
            'EODHD''s /real-time/ payload carries open/high/low/close/previousClose/volume but no quote side. The column exists so the absence is a recorded vendor fact rather than a missing feature.');
GO


/* ------------------------------------------------------------------ */
/* View: SymbolPriceCache in local time                                */
/* ------------------------------------------------------------------ */
/* The two timestamp columns record when the *vendor* observed the price and when this service fetched
 * it. Those are instants, and UTC is the honest frame for an instant -- which is why the caches stayed
 * UTC when the logging and usage tables moved to local time. What was missing was a way to read them
 * without doing the arithmetic by hand, and a view is the right size for that: no storage, cannot go
 * stale, and the stored values are untouched.
 *
 * The conversion labels the naive datetime2 as UTC and then converts it to the zone, applying the DST
 * rules in force on THAT ROW's date:
 *
 *     CAST((x AT TIME ZONE 'UTC') AT TIME ZONE 'AUS Eastern Standard Time' AS datetime2(7))
 *
 * A single SYSDATETIMEOFFSET() offset would be wrong for every row written in the other half of the
 * year. datetime2(7) rather than (3) so the conversion is exact and can be checked with an equality.
 *
 * The zone is hard-coded because SQL Server has no portable "this machine's Windows zone" function.
 * If this instance moves timezone, this view has to move with it.
 */
CREATE OR ALTER VIEW dbo.SymbolPriceCacheLocal
AS
SELECT
    SymbolCode,
    Venue,
    Price,
    Bid,
    Ask,
    PreviousClose,
    ChangePct,
    -- The stored instants, unchanged and still named for what they are.
    AsOfUtc,
    LastUpdatedUtc,
    -- The same instants as this site's wall clock. The names deliberately drop the "Utc" suffix:
    -- a column called AsOfUtc holding Sydney time is how a reader gets misled.
    CAST((AsOfUtc AT TIME ZONE 'UTC') AT TIME ZONE 'AUS Eastern Standard Time' AS datetime2(7))
        AS AsOfLocal,
    CAST((LastUpdatedUtc AT TIME ZONE 'UTC') AT TIME ZONE 'AUS Eastern Standard Time' AS datetime2(7))
        AS LastUpdatedLocal,
    -- For one hour a year local time is ambiguous; this says which side of the change a row is on.
    DATEPART(TZOFFSET, (LastUpdatedUtc AT TIME ZONE 'UTC') AT TIME ZONE 'AUS Eastern Standard Time')
        AS LastUpdatedOffsetMinutes,
    Source
FROM dbo.SymbolPriceCache
GO

/* ------------------------------------------------------------------ */
/* 5. Exchange on the bar tables (2026-11-01)                          */
/* ------------------------------------------------------------------ */
/* A bare ticker is not unique: the same code can be an ASX listing and a US one, and with
   a symbol-only key the two overwrite each other -- the US pull silently replacing the AU
   row for the same date. Both bar tables are keyed with the venue from here on, and every
   writer states it: a write that omits the exchange is refused rather than filed as US,
   because "unknown" and "US" are not the same claim.

   Guarded twice over: the column is added only when absent, and the primary key is rebuilt
   only when it does not already include Exchange. The backfill runs in the same batch as the
   key rebuild, so it happens once rather than on every start. It moves a row only when the
   price cache knows that symbol as AU and knows it as nothing else -- a symbol known on both
   venues is left as US rather than guessed at. On the live database it found 18,363 rows
   that had been AU all along and were indistinguishable from US ones. */

IF COL_LENGTH('dbo.DailySnapshot', 'Exchange') IS NULL
    ALTER TABLE dbo.DailySnapshot ADD Exchange varchar(8) NOT NULL
        CONSTRAINT DF_DailySnapshot_Exchange DEFAULT ('US');
GO

IF (SELECT COUNT(*) FROM sys.index_columns ic
      JOIN sys.indexes i ON i.object_id = ic.object_id AND i.index_id = ic.index_id
     WHERE i.name = 'PK_DailySnapshot' AND ic.is_included_column = 0) = 2
BEGIN
    ALTER TABLE dbo.DailySnapshot DROP CONSTRAINT PK_DailySnapshot;
    ALTER TABLE dbo.DailySnapshot ADD CONSTRAINT PK_DailySnapshot
        PRIMARY KEY CLUSTERED (SymbolCode, Exchange, SnapshotDate);

    UPDATE d
       SET Exchange = 'AU'
      FROM dbo.DailySnapshot d
     WHERE EXISTS (SELECT 1 FROM dbo.SymbolPriceCache p
                    WHERE p.SymbolCode = d.SymbolCode AND p.Venue = 'AU')
       AND NOT EXISTS (SELECT 1 FROM dbo.SymbolPriceCache p
                        WHERE p.SymbolCode = d.SymbolCode AND p.Venue = 'US');
END
GO

IF COL_LENGTH('dbo.IndicatorCache', 'Exchange') IS NULL
    ALTER TABLE dbo.IndicatorCache ADD Exchange varchar(8) NOT NULL
        CONSTRAINT DF_IndicatorCache_Exchange DEFAULT ('US');
GO

IF (SELECT COUNT(*) FROM sys.index_columns ic
      JOIN sys.indexes i ON i.object_id = ic.object_id AND i.index_id = ic.index_id
     WHERE i.name = 'PK_IndicatorCache' AND ic.is_included_column = 0) = 1
BEGIN
    ALTER TABLE dbo.IndicatorCache DROP CONSTRAINT PK_IndicatorCache;
    ALTER TABLE dbo.IndicatorCache ADD CONSTRAINT PK_IndicatorCache
        PRIMARY KEY CLUSTERED (SymbolCode, Exchange);
END
GO

/* The column is added with a temporary default because SQL Server needs one to fill the
   rows that already exist. It does not stay. A default turns "this writer forgot the venue"
   into "this row is US" -- a claim the writer never made, and the exact silent mislabel the
   key exists to prevent. After this, a write without a venue fails instead. */

IF EXISTS (SELECT 1 FROM sys.default_constraints
            WHERE name = 'DF_DailySnapshot_Exchange'
              AND parent_object_id = OBJECT_ID('dbo.DailySnapshot'))
    ALTER TABLE dbo.DailySnapshot DROP CONSTRAINT DF_DailySnapshot_Exchange;
GO

IF EXISTS (SELECT 1 FROM sys.default_constraints
            WHERE name = 'DF_IndicatorCache_Exchange'
              AND parent_object_id = OBJECT_ID('dbo.IndicatorCache'))
    ALTER TABLE dbo.IndicatorCache DROP CONSTRAINT DF_IndicatorCache_Exchange;
GO

IF EXISTS (SELECT 1 FROM sys.default_constraints
            WHERE name = 'DF_SymbolPriceCache_Venue'
              AND parent_object_id = OBJECT_ID('dbo.SymbolPriceCache'))
    ALTER TABLE dbo.SymbolPriceCache DROP CONSTRAINT DF_SymbolPriceCache_Venue;
GO

/* The documentation follows the change. A grain that changed has to say so, or the next
   reader trusts a sentence that is no longer true -- and the doc rows are written after the
   migration, so they cannot describe a column that is not there. */

UPDATE dbo.TableDoc SET Grain = 'one row per symbol per venue per session'
 WHERE TableName = 'DailySnapshot' AND Grain = 'one row per symbol per session';
GO

UPDATE dbo.TableDoc SET Grain = 'one row per symbol per venue'
 WHERE TableName = 'IndicatorCache' AND Grain = 'one row per symbol';
GO

/* ------------------------------------------------------------------ */
/* 6. RawClose on DailySnapshot (2026-11-02)                          */
/* ------------------------------------------------------------------ */
/* Close stores EODHD's adjusted_close while Open/High/Low stay raw, so a candle body read
   from Close is on a different scale from its own wicks. RawClose carries the traded close
   beside it. Existing rows are left NULL and are filled by the service's first on-demand
   re-pull for a symbol (the /bars cache-hit rule refuses a window that is not raw-complete);
   a whole-market re-pull would cost 100 units per session for a column only requested
   symbols need.

   Nullable on purpose: Back_Test.BarHourly is split-adjusted only, so seed rows have no raw
   close to copy, and NULL says "not known" rather than inventing one. The MERGE preserves a
   known RawClose when a raw-less source rewrites the row. */

IF COL_LENGTH('dbo.DailySnapshot', 'RawClose') IS NULL
    ALTER TABLE dbo.DailySnapshot ADD [RawClose] decimal(18,6) NULL;
GO

UPDATE dbo.TableDoc
   SET Purpose = 'Daily OHLCV, appended once per session by D:\McTrader_Market_Data_Service from EODHD /eod-bulk-last-day. Close is adjusted_close where the vendor supplies it (IsAdjusted=1), matching BarHourly''s convention and never mixed with Bar5Min, which is unadjusted. RawClose is the unadjusted close and may be NULL where it is not known (the BarHourly seed). This is the series ATR is computed from; IndicatorCache holds the result.'
 WHERE TableName = 'DailySnapshot'
   AND Purpose NOT LIKE '%RawClose%';
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'DailySnapshot' AND ColumnName = 'Exchange')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('DailySnapshot', 'Exchange', 'varchar(8)', 'venue code',
            'Which listing the bar belongs to: US, AU, ... (the vendor''s exchange code).',
            'Part of the clustered key, because a bare ticker is not unique: the same code can be an ASX listing and a US one, and without the venue one pull overwrites the other. Rows written before 2026-11-01 were backfilled from the price cache, so a row dated before then can only be trusted for its venue when the symbol was known on one exchange.');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'IndicatorCache' AND ColumnName = 'Exchange')
BEGIN
    /* Every ATR in the cache predates the venue key: it was computed under a symbol-only
       key, so it is either labelled with the wrong venue or has both venues' bars mixed
       into one number. A derived value whose series is unknown is not worth keeping -- it
       rebuilds from the daily refresh, or from /atr on demand. The ColumnDoc row below is
       written in the same batch, so it is what marks this cleanup as done. */
    DELETE FROM dbo.IndicatorCache;

    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('IndicatorCache', 'Exchange', 'varchar(8)', 'venue code',
            'The venue whose daily series the ATR was measured on.',
            'Part of the primary key. An ATR is a property of one venue''s series: mixing an ASX bar into a US series reads the currency difference as a move, and a symbol-only key would serve whichever row the database returned first.');
END
GO

/* The ASX bulk pull gets its own tariff row.

   dbo.EODHDUsage is keyed by RequestType, and refresh.is_fresh("AU") reads the row named
   /eod-bulk-last-day/AU. Before this row existed the client filed every bulk pull as /US,
   so an AU refresh never looked fresh and every restart re-spent 100 units.

   The price is carried from the same endpoint measured flat at 100 across US 50,234, LSE
   7,082 and AS 527 rows -- a 95x spread in row count, all +100, so it is per call rather
   than per row. AU itself was not in that run, so Is_Estimated is 1 until one AU pull is
   reconciled against the vendor's free /user counter, at which point it flips to 0.

   The INSERT is dynamic because dbo.RequestTypes is an estate table this script does not
   create: naming it in the batch itself would fail to compile on a database without it,
   where the service otherwise runs (recording the call at cost 0, with a warning). */

IF OBJECT_ID('dbo.RequestTypes', 'U') IS NOT NULL
    EXEC sp_executesql N'
        IF NOT EXISTS (SELECT 1 FROM dbo.RequestTypes WHERE RequestType = N''/eod-bulk-last-day/AU'')
            INSERT INTO dbo.RequestTypes
                (RequestType, Endpoint, CostBasis, CostEach, MeasuredAt, Evidence, Note, Is_Estimated)
            VALUES
                (N''/eod-bulk-last-day/AU'', N''/eod-bulk-last-day/{market}'', N''flat'', 100,
                 ''2026-10-17'',
                 N''Carried from the same endpoint measured flat at 100 across US 50,234, LSE 7,082 and AS 527 rows; an AU pull itself was not in that run.'',
                 N''Separate from /eod-bulk-last-day/US so EODHDUsage names the market that was pulled and refresh.is_fresh(AU) reads the AU row rather than the US one.'',
                 1);';
GO
