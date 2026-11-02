#!/usr/bin/env python3
"""Create dbo.BarReal5Min_New: the 5-minute table with the venue in its key.

The existing table keys a bar by ``(SymbolCode, BarTimeUtc)``. A ticker is not unique
across exchanges -- BHP is both an NYSE listing and an ASX listing, and 253 of the cached
names are in the same position -- so that key cannot say which series a row belongs to.
The new table adds ``Exchange`` and keys on ``(SymbolCode, Exchange, BarTimeUtc)``.

This creates the table only. It never drops a table that holds rows unless both
``--recreate`` and ``--force`` are given, because the rows in a half-built replacement are
still the only copy of several hours of vendor calls.

    python tools/create_bar5min_new.py
    python tools/create_bar5min_new.py --recreate --force   # deliberately start over
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_service.config import load as load_settings  # noqa: E402
from data_service.store import Store  # noqa: E402

TABLE = "BarReal5Min_New"

DDL = f"""
CREATE TABLE dbo.{TABLE} (
    SymbolCode  varchar(24)   NOT NULL,
    Exchange    varchar(8)    NOT NULL,
    BarTimeUtc  datetime2(0)  NOT NULL,
    [Open]      decimal(18,6) NOT NULL,
    [High]      decimal(18,6) NOT NULL,
    [Low]       decimal(18,6) NOT NULL,
    [Close]     decimal(18,6) NOT NULL,
    Volume      bigint        NOT NULL,
    CONSTRAINT PK_{TABLE} PRIMARY KEY CLUSTERED (SymbolCode, Exchange, BarTimeUtc)
);
"""

INDEX = (f"CREATE INDEX IX_{TABLE}_Time ON dbo.{TABLE} (Exchange, BarTimeUtc);")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recreate", action="store_true", help="drop an existing table")
    parser.add_argument("--force", action="store_true",
                        help="allow --recreate to drop a table that holds rows")
    args = parser.parse_args()

    settings = load_settings()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT OBJECT_ID(?, 'U')", f"dbo.{TABLE}")
        exists = cursor.fetchone()[0] is not None
        if exists:
            cursor.execute(f"SELECT COUNT_BIG(*) FROM dbo.{TABLE}")
            rows = int(cursor.fetchone()[0])
            print(f"  {TABLE} exists with {rows:,} rows")
            if not args.recreate:
                print("  leaving it alone; pass --recreate to replace it")
                return 0
            if rows and not args.force:
                print(f"  refusing to drop {rows:,} rows without --force")
                return 1
            cursor.execute(f"DROP TABLE dbo.{TABLE}")
            conn.commit()
            print(f"  dropped {TABLE}")

        cursor.execute(DDL)
        cursor.execute(INDEX)
        conn.commit()
        print(f"  created dbo.{TABLE} with PK (SymbolCode, Exchange, BarTimeUtc)")
        print(f"  created IX_{TABLE}_Time on (Exchange, BarTimeUtc)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
