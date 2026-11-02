#!/usr/bin/env python3
"""Swap the verified 5-minute table into place, keeping the old one as a backup.

The rebuild writes ``dbo.BarReal5Min_New`` and never touches the live table. This
renames the live table to ``dbo.BarReal5Min_Old`` and the new table to
``dbo.BarReal5Min`` in one transaction, carrying the canonical constraint and index
names with it. The old rows are not dropped: they stay in ``BarReal5Min_Old`` until
someone decides they are no longer needed.

The rename order matters. Constraint and index names are schema-scoped, so the old
``PK_BarReal5Min`` and ``IX_BarReal5Min_Time`` must be moved out of the way before the
new table can take those names.

    python tools/swap_bar5min.py --dry-run
    python tools/swap_bar5min.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_service.config import load as load_settings  # noqa: E402
from data_service.store import Store  # noqa: E402

NEW_TABLE = "BarReal5Min_New"
LIVE_TABLE = "BarReal5Min"
BACKUP_TABLE = "BarReal5Min_Old"


def _one(cursor, sql: str, *params):
    cursor.execute(sql, *params)
    return cursor.fetchone()


def _table_exists(cursor, table: str) -> bool:
    return _one(cursor, "SELECT OBJECT_ID(?, 'U')", f"dbo.{table}")[0] is not None


def _count(cursor, table: str) -> int:
    return int(_one(cursor, f"SELECT COUNT_BIG(*) FROM dbo.{table}")[0])


def _primary_key(cursor, table: str):
    return _one(
        cursor,
        "SELECT k.name FROM sys.key_constraints k "
        "WHERE k.parent_object_id = OBJECT_ID(?) AND k.type = 'PK'",
        f"dbo.{table}")


def _index(cursor, table: str, primary: bool = False):
    return _one(
        cursor,
        "SELECT i.name FROM sys.indexes i "
        "WHERE i.object_id = OBJECT_ID(?) AND i.is_primary_key = ? "
        "AND i.name IS NOT NULL",
        f"dbo.{table}", 1 if primary else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new-table", default=NEW_TABLE)
    parser.add_argument("--live-table", default=LIVE_TABLE)
    parser.add_argument("--backup-table", default=BACKUP_TABLE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)
    conn = store.connect()
    try:
        cursor = conn.cursor()
        if not _table_exists(cursor, args.new_table):
            print(f"  {args.new_table} does not exist")
            return 1
        if not _table_exists(cursor, args.live_table):
            print(f"  {args.live_table} does not exist")
            return 1
        if _table_exists(cursor, args.backup_table):
            print(f"  {args.backup_table} already exists; refusing to overwrite it")
            return 1

        new_rows = _count(cursor, args.new_table)
        live_rows = _count(cursor, args.live_table)
        if not new_rows:
            print(f"  {args.new_table} is empty; refusing to swap")
            return 1
        if not live_rows:
            print(f"  warning: {args.live_table} is empty")

        new_pk = _primary_key(cursor, args.new_table)
        live_pk = _primary_key(cursor, args.live_table)
        new_index = _index(cursor, args.new_table)
        live_index = _index(cursor, args.live_table)
        if not all((new_pk, live_pk, new_index, live_index)):
            print("  a primary key or index is missing; refusing to guess its name")
            return 1

        print(f"  live: {args.live_table}: {live_rows:,} rows  PK={live_pk[0]}  "
              f"index={live_index[0]}")
        print(f"  new:  {args.new_table}: {new_rows:,} rows  PK={new_pk[0]}  "
              f"index={new_index[0]}")
        print(f"  backup after swap: {args.backup_table}")

        statements = [
            (f"EXEC sp_rename N'dbo.{args.live_table}', N'{args.backup_table}';", ()),
            (f"EXEC sp_rename N'dbo.{live_pk[0]}', N'{live_pk[0]}_Old', N'OBJECT';", ()),
            (f"EXEC sp_rename N'dbo.{args.backup_table}.{live_index[0]}', "
             f"N'{live_index[0]}_Old', N'INDEX';", ()),
            (f"EXEC sp_rename N'dbo.{args.new_table}', N'{args.live_table}';", ()),
            (f"EXEC sp_rename N'dbo.{new_pk[0]}', N'{live_pk[0]}', N'OBJECT';", ()),
            (f"EXEC sp_rename N'dbo.{args.live_table}.{new_index[0]}', "
             f"N'{live_index[0]}', N'INDEX';", ()),
        ]
        if args.dry_run:
            for sql, _ in statements:
                print(f"    {sql}")
            return 0

        cursor.execute("SET XACT_ABORT ON;")
        cursor.execute("BEGIN TRANSACTION;")
        try:
            for sql, _ in statements:
                cursor.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        cursor.execute(f"SELECT COUNT_BIG(*) FROM dbo.{args.live_table}")
        final_rows = int(cursor.fetchone()[0])
        cursor.execute(
            "SELECT k.name FROM sys.key_constraints k "
            "WHERE k.parent_object_id = OBJECT_ID(?) AND k.type = 'PK'",
            f"dbo.{args.live_table}")
        final_pk = cursor.fetchone()
        print(f"  swapped: {args.live_table} now holds {final_rows:,} rows, "
              f"PK={final_pk[0] if final_pk else 'MISSING'}")
        print(f"  backup remains as {args.backup_table}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
