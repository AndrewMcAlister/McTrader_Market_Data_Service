/* 003 — index maintenance: dbo.usp_MMDS_MaintainIndexes, and the log it writes.

 * WHEN TO RUN IT
 *
 *   - after a bulk change (a backfill or a bulk load), and
 *   - weekly, outside the two collection windows (10:45 Sydney for the ASX, 02:15 for the US).
 *
 *   EXEC dbo.usp_MMDS_MaintainIndexes @WhatIf = 1;   -- what it would do, changing nothing
 *   EXEC dbo.usp_MMDS_MaintainIndexes;               -- AUTO: rebuild >= 30%, reorganise >= 10%
 *   EXEC dbo.usp_MMDS_MaintainIndexes @IncludeWarehouse = 1;  -- + BarHourly / Bar5Min
 *
 * WHAT IT DOES
 *
 *   Reads fragmentation from sys.dm_db_index_physical_stats in LIMITED mode (cheap: it reads
 *   the non-leaf levels, not every page), one named table at a time so the 77-million-row
 *   warehouse tables are not touched unless @IncludeWarehouse asks for them. Then, for each
 *   index above the page threshold:
 *
 *     >= @RebuildThresholdPercent   ALTER INDEX ... REBUILD
 *     >= @MinFragmentationPercent   ALTER INDEX ... REORGANIZE
 *     otherwise                     nothing, recorded as SKIP
 *
 *   then UPDATE STATISTICS on the tables involved (FULLSCAN below five million rows, RESAMPLE
 *   above it -- a fullscan of Bar5Min is not a maintenance task).
 *
 * WHY THE EDITION CHECK
 *
 *   ONLINE = ON is Enterprise-only. This instance reports EngineEdition 2 (Standard), so the
 *   procedure rebuilds offline and says so. An offline rebuild of DailySnapshot blocks readers
 *   for its duration -- which is why this is a scheduled, quiet-hours job and not something a
 *   data request can trigger.
 *
 * PERMISSIONS
 *
 *   The caller needs ALTER on the tables (db_owner / db_ddladmin) and VIEW DATABASE STATE for
 *   the fragmentation DMV. Without the latter the DMV returns no rows and the procedure would
 *   silently do nothing, so it checks first and refuses rather than pretending it ran.
 */
SET NOCOUNT ON;
GO

/* ------------------------------------------------------------------ */
/* 1. The log                                                          */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('dbo.IndexMaintenanceLog', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.IndexMaintenanceLog (
        LogId             int IDENTITY(1,1) NOT NULL,
        RunId             uniqueidentifier  NOT NULL,
        StartedUtc        datetime2(3)      NOT NULL CONSTRAINT DF_IndexMaintenanceLog_Started DEFAULT (SYSUTCDATETIME()),
        FinishedUtc       datetime2(3)      NULL,
        TableName         sysname           NOT NULL,
        IndexName         sysname           NOT NULL,
        Action            varchar(12)       NOT NULL,   -- REBUILD / REORGANIZE / SKIP / STATISTICS
        FragmentationPct  decimal(6,2)      NULL,
        PageCount         bigint            NULL,
        DurationMs        int               NULL,
        ErrorMessage      nvarchar(4000)    NULL,
        CONSTRAINT PK_IndexMaintenanceLog PRIMARY KEY CLUSTERED (LogId)
    );

    CREATE NONCLUSTERED INDEX IX_IndexMaintenanceLog_Run
        ON dbo.IndexMaintenanceLog (RunId, StartedUtc);
END
GO

IF NOT EXISTS (SELECT 1 FROM dbo.TableDoc WHERE TableName = 'IndexMaintenanceLog')
    INSERT INTO dbo.TableDoc (TableName, Grain, Purpose, RowCountNote, DataSource)
    VALUES ('IndexMaintenanceLog',
            'one row per index considered per run',
            'WHAT dbo.usp_MMDS_MaintainIndexes DID, including the indexes it left alone. A rebuild that failed is a row with ErrorMessage set, so a silent failure cannot pass for a clean run. A what-if run writes nothing: the log is a record of changes made, not of intentions.',
            'one row per index per run (~10 rows a week)', 'derived');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.ColumnDoc WHERE TableName = 'IndexMaintenanceLog' AND ColumnName = 'Action')
    INSERT INTO dbo.ColumnDoc (TableName, ColumnName, DataType, Unit, Meaning, Notes)
    VALUES ('IndexMaintenanceLog', 'Action', 'varchar(12)', 'enum',
            'REBUILD, REORGANIZE, SKIP or STATISTICS.',
            'SKIP is recorded on purpose: "the procedure ran and decided not to touch this index" is a different fact from "the procedure did not run".');
GO

/* ------------------------------------------------------------------ */
/* 2. The procedure                                                    */
/* ------------------------------------------------------------------ */
CREATE OR ALTER PROCEDURE dbo.usp_MMDS_MaintainIndexes
    @Mode                    varchar(12)  = 'AUTO',
    @MinFragmentationPercent decimal(5,2) = 10.0,
    @RebuildThresholdPercent decimal(5,2) = 30.0,
    @MinPageCount            int          = 64,
    @IncludeWarehouse        bit          = 0,
    @UpdateStatistics        bit          = 1,
    @WhatIf                  bit          = 0
AS
BEGIN
    SET NOCOUNT ON;

    IF @Mode NOT IN ('AUTO', 'REBUILD', 'REORGANIZE', 'STATS')
        THROW 50000, '@Mode must be AUTO, REBUILD, REORGANIZE or STATS.', 1;

    IF HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW DATABASE STATE') = 0
        THROW 50001, 'The caller needs VIEW DATABASE STATE: without it the fragmentation DMV returns no rows and this would silently do nothing.', 1;

    DECLARE @runId uniqueidentifier = NEWID();
    DECLARE @engineEdition int = CAST(SERVERPROPERTY('EngineEdition') AS int);
    DECLARE @onlineSupported bit = CASE WHEN @engineEdition IN (3, 5, 8) THEN 1 ELSE 0 END;

    PRINT CONCAT('Index maintenance ', @runId, '  mode=', @Mode,
                 '  ONLINE supported=', @onlineSupported,
                 CASE WHEN @WhatIf = 1 THEN '  (WHATIF: nothing will change)' ELSE '' END);

    /* -- the work list ------------------------------------------------ */
    DECLARE @work TABLE (
        SchemaName       sysname       NOT NULL,
        TableName        sysname       NOT NULL,
        IndexName        sysname       NOT NULL,
        PageCount        bigint        NOT NULL,
        FragmentationPct decimal(6,2)  NULL,
        Action           varchar(12)   NOT NULL
    );

    INSERT INTO @work (SchemaName, TableName, IndexName, PageCount, FragmentationPct, Action)
    SELECT sch.name, t.name, i.name, s.page_count,
           CAST(s.avg_fragmentation_in_percent AS decimal(6,2)),
           CASE
               WHEN @Mode = 'REBUILD'    THEN 'REBUILD'
               WHEN @Mode = 'REORGANIZE' THEN 'REORGANIZE'
               WHEN @Mode = 'STATS'      THEN 'SKIP'
               WHEN s.avg_fragmentation_in_percent >= @RebuildThresholdPercent THEN 'REBUILD'
               WHEN s.avg_fragmentation_in_percent >= @MinFragmentationPercent THEN 'REORGANIZE'
               ELSE 'SKIP'
           END
    FROM sys.tables AS t
    JOIN sys.schemas AS sch ON sch.schema_id = t.schema_id
    CROSS APPLY sys.dm_db_index_physical_stats(DB_ID(), t.object_id, NULL, NULL, 'LIMITED') AS s
    JOIN sys.indexes AS i ON i.object_id = s.object_id AND i.index_id = s.index_id
    WHERE (t.name IN ('DailySnapshot', 'SymbolPriceCache', 'IndicatorCache',
                      'EodhdUsage', 'RequestTypes', 'IndexMaintenanceLog')
           OR (@IncludeWarehouse = 1
               AND t.name IN ('BarHourly', 'Bar5Min')))
      AND i.type IN (1, 2)                 -- clustered and nonclustered; heaps and columnstore are not ours
      AND i.is_disabled = 0
      AND s.page_count >= @MinPageCount    -- under 64 pages (512 KB) a rebuild cannot pay for itself;

    /* -- rebuild / reorganise ----------------------------------------- */
    DECLARE @schemaName sysname, @tableName sysname, @indexName sysname,
            @pageCount bigint, @fragmentation decimal(6,2), @action varchar(12),
            @sql nvarchar(max), @logId int, @started datetime2(3), @error nvarchar(4000);

    DECLARE work CURSOR LOCAL FAST_FORWARD FOR
        SELECT SchemaName, TableName, IndexName, PageCount, FragmentationPct, Action
        FROM @work
        ORDER BY PageCount DESC;

    OPEN work;
    FETCH NEXT FROM work INTO @schemaName, @tableName, @indexName, @pageCount, @fragmentation, @action;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @started = SYSUTCDATETIME();
        SET @error = NULL;
        SET @sql = NULL;

        IF @action = 'REBUILD'
            SET @sql = 'ALTER INDEX ' + QUOTENAME(@indexName) + ' ON '
                     + QUOTENAME(@schemaName) + '.' + QUOTENAME(@tableName)
                     + ' REBUILD WITH (ONLINE = '
                     + CASE WHEN @onlineSupported = 1 THEN 'ON' ELSE 'OFF' END
                     + ', SORT_IN_TEMPDB = ON)';
        ELSE IF @action = 'REORGANIZE'
            SET @sql = 'ALTER INDEX ' + QUOTENAME(@indexName) + ' ON '
                     + QUOTENAME(@schemaName) + '.' + QUOTENAME(@tableName)
                     + ' REORGANIZE WITH (LOB_COMPACTION = ON)';

        IF @WhatIf = 1
        BEGIN
            IF @action <> 'SKIP'
                PRINT CONCAT('  would ', @action, ' ', @tableName, '.', @indexName,
                             '  pages=', @pageCount, '  frag=', @fragmentation, '%');
        END
        ELSE
        BEGIN
            INSERT INTO dbo.IndexMaintenanceLog
                (RunId, TableName, IndexName, Action, FragmentationPct, PageCount)
            VALUES (@runId, @tableName, @indexName, @action, @fragmentation, @pageCount);
            SET @logId = SCOPE_IDENTITY();

            IF @sql IS NOT NULL
            BEGIN
                BEGIN TRY
                    EXEC sys.sp_executesql @sql;
                END TRY
                BEGIN CATCH
                    SET @error = ERROR_MESSAGE();
                    UPDATE dbo.IndexMaintenanceLog
                       SET ErrorMessage = @error,
                           FinishedUtc = SYSUTCDATETIME(),
                           DurationMs = DATEDIFF(MILLISECOND, @started, SYSUTCDATETIME())
                     WHERE LogId = @logId;
                    PRINT CONCAT('  FAILED ', @tableName, '.', @indexName, ': ', @error);
                END CATCH
            END

            UPDATE dbo.IndexMaintenanceLog
               SET FinishedUtc = SYSUTCDATETIME(),
                   DurationMs = DATEDIFF(MILLISECOND, @started, SYSUTCDATETIME())
             WHERE LogId = @logId AND FinishedUtc IS NULL;

            IF @action <> 'SKIP'
                PRINT CONCAT('  ', @action, ' ', @tableName, '.', @indexName,
                             '  pages=', @pageCount, '  frag=', @fragmentation, '%');
        END

        FETCH NEXT FROM work INTO @schemaName, @tableName, @indexName, @pageCount, @fragmentation, @action;
    END
    CLOSE work;
    DEALLOCATE work;

    /* -- statistics --------------------------------------------------- */
    -- An index rebuild updates the statistics of that index; this covers the column statistics
    -- and the tables whose indexes were not touched.
    IF (@UpdateStatistics = 1 OR @Mode = 'STATS') AND @WhatIf = 0
    BEGIN
        DECLARE @statsTable sysname, @statsRows bigint, @statsSql nvarchar(max), @statsStarted datetime2(3);

        DECLARE stats CURSOR LOCAL FAST_FORWARD FOR
            SELECT t.name, SUM(p.rows)
            FROM sys.tables AS t
            JOIN sys.partitions AS p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            WHERE t.name IN ('DailySnapshot', 'SymbolPriceCache', 'IndicatorCache', 'EodhdUsage', 'RequestTypes')
               OR (@IncludeWarehouse = 1
                   AND t.name IN ('BarHourly', 'Bar5Min'))
            GROUP BY t.name;

        OPEN stats;
        FETCH NEXT FROM stats INTO @statsTable, @statsRows;
        WHILE @@FETCH_STATUS = 0
        BEGIN
            SET @statsStarted = SYSUTCDATETIME();
            SET @statsSql = 'UPDATE STATISTICS dbo.' + QUOTENAME(@statsTable)
                          + CASE WHEN @statsRows >= 5000000 THEN ' WITH RESAMPLE' ELSE ' WITH FULLSCAN' END;

            BEGIN TRY
                EXEC sys.sp_executesql @statsSql;
                INSERT INTO dbo.IndexMaintenanceLog
                    (RunId, TableName, IndexName, Action, PageCount, FinishedUtc, DurationMs)
                VALUES (@runId, @statsTable, '(all statistics)', 'STATISTICS', @statsRows,
                        SYSUTCDATETIME(), DATEDIFF(MILLISECOND, @statsStarted, SYSUTCDATETIME()));
            END TRY
            BEGIN CATCH
                INSERT INTO dbo.IndexMaintenanceLog
                    (RunId, TableName, IndexName, Action, PageCount, FinishedUtc, DurationMs, ErrorMessage)
                VALUES (@runId, @statsTable, '(all statistics)', 'STATISTICS', @statsRows,
                        SYSUTCDATETIME(), DATEDIFF(MILLISECOND, @statsStarted, SYSUTCDATETIME()),
                        ERROR_MESSAGE());
            END CATCH

            FETCH NEXT FROM stats INTO @statsTable, @statsRows;
        END
        CLOSE stats;
        DEALLOCATE stats;
    END

    /* -- what happened ------------------------------------------------ */
    IF @WhatIf = 1
        SELECT SchemaName, TableName, IndexName, PageCount, FragmentationPct, Action
        FROM @work ORDER BY PageCount DESC;
    ELSE
        SELECT RunId, TableName, IndexName, Action, FragmentationPct, PageCount, DurationMs, ErrorMessage
        FROM dbo.IndexMaintenanceLog
        WHERE RunId = @runId
        ORDER BY LogId;
END
GO

/* ------------------------------------------------------------------ */
/* 3. Scheduling it once a week                                        */
/* ------------------------------------------------------------------ */
/* SQL Server Agent is installed but stopped (Manual start) on this machine, so there is no job
   today. Once it is running, the job is a command, a schedule and a target:

     EXEC msdb.dbo.sp_add_job @job_name = N'MMDS index maintenance', @enabled = 1, ...
     -- step: EXEC Market_Data.dbo.usp_MMDS_MaintainIndexes @Mode = 'AUTO';
     -- schedule: weekly, Sunday 03:00 local

   The alternative, with no Agent at all, is the service's own scheduler (data_service.schedule):
   it already runs a thread, so a weekly maintenance tick there would keep one process
   responsible for everything the service does to the database. That is a code change rather
   than a SQL one, and is deliberately not made here.

   Either way: run it outside 10:45-11:00 Sydney (the ASX collection) and 02:15-03:00 (the US
   one), and never while a backfill is writing.
 */
