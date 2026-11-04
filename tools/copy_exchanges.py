"""Copy McTrader_Live.dbo.Exchanges into the service's own database.

The market clock reads ``dbo.Exchanges`` from ``MDS_DATABASE`` (Market_Data); this
tool refreshes that copy from McTrader's live reference. It is idempotent: create
the table if it is missing, MERGE the source rows, then record it in ``TableDoc``.

    .venv\\Scripts\\python.exe tools\\copy_exchanges.py
    MDS_EXCHANGE_SOURCE=McTrader_Paper .venv\\Scripts\\python.exe tools\\copy_exchanges.py

The clock falls back to ``data/clock.json`` if the table cannot be read, so a
failed refresh does not take the market clock down -- it only freezes it at the
last copy until this tool succeeds.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyodbc  # noqa: E402

from data_service.config import load as load_settings  # noqa: E402

CREATE = """
IF OBJECT_ID('dbo.Exchanges', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Exchanges (
        Id       tinyint       NOT NULL CONSTRAINT PK_Exchanges PRIMARY KEY,
        Code     varchar(20)   NOT NULL CONSTRAINT UQ_Exchanges_Code UNIQUE,
        Name     nvarchar(200) NULL,
        Country  varchar(100)  NULL,
        Currency char(3)       NULL,
        Timezone varchar(50)   NULL,
        Opens    varchar(5)    NULL,
        Closes   varchar(5)    NULL
    );
END
"""

PURPOSE = (
    "Exchange session definitions copied from {source}.dbo.Exchanges: Code, Timezone, "
    "Opens and Closes in the exchange's own local time. Drives the MMDS market clock "
    "(/market/clock, /market/session, /market/exchanges) instead of the frozen "
    "data/clock.json exchange list."
)


def main() -> int:
    settings = load_settings()
    source = os.environ.get("MDS_EXCHANGE_SOURCE", "McTrader_Live").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source):
        print(f"refusing source database {source!r}: not a plain identifier", file=sys.stderr)
        return 2

    conn = pyodbc.connect(settings.connection_string, timeout=settings.db_timeout_seconds)
    try:
        cursor = conn.cursor()
        cursor.execute(CREATE)
        cursor.execute(f"""
            MERGE dbo.Exchanges AS t
            USING (
                SELECT Id, Code, Name, Country, Currency, Timezone, Opens, Closes
                FROM {source}.dbo.Exchanges
            ) AS s
            ON t.Id = s.Id
            WHEN MATCHED THEN UPDATE SET
                Code = s.Code, Name = s.Name, Country = s.Country, Currency = s.Currency,
                Timezone = s.Timezone, Opens = s.Opens, Closes = s.Closes
            WHEN NOT MATCHED BY TARGET THEN
                INSERT (Id, Code, Name, Country, Currency, Timezone, Opens, Closes)
                VALUES (s.Id, s.Code, s.Name, s.Country, s.Currency, s.Timezone, s.Opens, s.Closes)
            WHEN NOT MATCHED BY SOURCE THEN DELETE;
        """)
        purpose = PURPOSE.format(source=source)
        cursor.execute("""
            IF EXISTS (SELECT 1 FROM dbo.TableDoc WHERE TableName = 'Exchanges')
                UPDATE dbo.TableDoc
                   SET Grain = 'one row per exchange', Purpose = ?, RowCountNote = ?,
                       DataSource = ?
                 WHERE TableName = 'Exchanges'
            ELSE
                INSERT INTO dbo.TableDoc (TableName, Grain, Purpose, RowCountNote, DataSource)
                VALUES ('Exchanges', 'one row per exchange', ?, ?, ?);
        """, purpose, "13 rows", source, purpose, "13 rows", source)
        conn.commit()

        print(f"{settings.db_database}.dbo.Exchanges <- {source}.dbo.Exchanges")
        for row in cursor.execute(
                "SELECT Id, Code, Timezone, Opens, Closes FROM dbo.Exchanges ORDER BY Id"):
            print(f"  {row.Id:2} {row.Code:8} {row.Timezone:5} {row.Opens}-{row.Closes}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
