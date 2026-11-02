#!/usr/bin/env python3
"""Verify dbo.BarReal5Min_New against its daily reference before the swap.

The rebuild already verified each series as it fetched it, but that check ran one
symbol at a time and wrote the result to a JSON report. This is the independent
check on what actually landed in the table:

  * row and symbol counts, by Exchange, and the time range;
  * every bar inside the venue's regular session (09:30-16:00 ET, 10:00-16:00 Sydney);
  * the primary key actually present and covering (SymbolCode, Exchange, BarTimeUtc);
  * each symbol's daily High/Low rebuilt from the table and compared with
    DailySnapshot (Source='eodhd-backfill'), the same half-percent rule the
    classifier used;
  * symbols missing from the new table, or present there and absent from the old,
    and each symbol's row-count ratio against the old table.

The per-symbol daily check is annotated with the classifier's confidence, because a
``listing-only`` series can be correctly attributed while its daily feed is adjusted
for a corporate action or carries a single price. Those low rates are expected and do
not fail the run; a rate below the confidence's gate (95% for ``exact`` and its
siblings, 80% for ``majority``) is a failure.

    python tools/verify_bar5min_new.py
    python tools/verify_bar5min_new.py --table BarReal5Min_New
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_service.config import load as load_settings  # noqa: E402
from data_service.store import Store  # noqa: E402

WORK_DIR = ROOT / "tools" / ".cache"
CLASSIFICATION = WORK_DIR / "bar5min_exchange_report.json"
REPORT = WORK_DIR / "verify_bar5min_new.json"
REPORT_CSV = WORK_DIR / "verify_bar5min_new.csv"

OLD_TABLE = "BarReal5Min"
DAILY_SOURCE = "eodhd-backfill"

#: SQL Server's time-zone names, not the vendor's venue codes.
TIME_ZONES = {
    "US": "Eastern Standard Time",
    "AU": "AUS Eastern Standard Time",
}
SESSIONS = {
    "US": ("09:30:00", "16:00:00"),
    "AU": ("10:00:00", "16:00:00"),
}
RELATIVE_TOLERANCE = 0.005
PRICE_TOLERANCE = 1e-4

#: The rate each classifier confidence must reproduce, matching the loader's gates.
#: ``listing-only`` is absent deliberately: its venue is fixed by the authoritative
#: symbol list, and the daily feed is split-adjusted or single-priced on exactly those
#: ADR/OTC names, so a low rate there is expected and is not a veto.
GATES = {
    "exact": 0.95,
    "unverified-other": 0.95,
    "price-exact/session-mismatch": 0.95,
    "majority": 0.80,
}


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _local_date(venue: str) -> str:
    zone = TIME_ZONES.get(venue)
    if zone is None:
        raise SystemExit(f"  no session rule for Exchange={venue!r}")
    return (f"CAST(BarTimeUtc AT TIME ZONE 'UTC' AT TIME ZONE '{zone}' AS date)")


def _local_time(venue: str) -> str:
    zone = TIME_ZONES[venue]
    return f"CAST(BarTimeUtc AT TIME ZONE 'UTC' AT TIME ZONE '{zone}' AS time)"


def _same_sql(left: str, right: str) -> str:
    """The classifier's ``_same``: 1e-4 absolute or 0.5% of the larger value."""
    return (
        f"ABS({left} - {right}) <= {PRICE_TOLERANCE} + {RELATIVE_TOLERANCE} * "
        f"(CASE WHEN ABS({left}) > ABS({right}) THEN ABS({left}) ELSE ABS({right}) END)"
    )


def _primary_key(store: Store, table: str) -> List[Dict[str, Any]]:
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT i.name, c.name, ic.key_ordinal "
            "FROM sys.key_constraints k "
            "JOIN sys.indexes i ON i.object_id = k.parent_object_id "
            "AND i.index_id = k.unique_index_id "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id "
            "AND ic.index_id = i.index_id "
            "JOIN sys.columns c ON c.object_id = ic.object_id "
            "AND c.column_id = ic.column_id "
            "WHERE k.parent_object_id = OBJECT_ID(?) AND k.type = 'PK' "
            "ORDER BY ic.key_ordinal",
            f"dbo.{table}")
        return [{"name": name, "column": column, "ordinal": ordinal}
                for name, column, ordinal in cursor.fetchall()]
    finally:
        conn.close()


def _exchange_counts(store: Store, table: str) -> Dict[str, Dict[str, Any]]:
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT Exchange, COUNT_BIG(*), COUNT(DISTINCT SymbolCode), "
            f"MIN(BarTimeUtc), MAX(BarTimeUtc) FROM dbo.{table} GROUP BY Exchange")
        out: Dict[str, Dict[str, Any]] = {}
        for venue, rows, symbols, first, last in cursor.fetchall():
            out[str(venue)] = {
                "rows": int(rows), "symbols": int(symbols),
                "first_bar_utc": first, "last_bar_utc": last,
            }
        return out
    finally:
        conn.close()


def _session_violations(store: Store, table: str, venue: str) -> int:
    open_at, close_at = SESSIONS[venue]
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT COUNT_BIG(*) FROM dbo.{table} WHERE Exchange = ? AND "
            f"({_local_time(venue)} < ? OR {_local_time(venue)} > ?)",
            venue, open_at, close_at)
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def _daily_match(store: Store, table: str, venue: str,
                 source: str) -> List[Dict[str, Any]]:
    """Each symbol's daily High/Low rebuilt from the table, against DailySnapshot."""
    expression = _local_date(venue)
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            WITH bars AS (
                SELECT SymbolCode, Exchange, {expression} AS SessionDate,
                       MAX([High]) AS BarHigh, MIN([Low]) AS BarLow,
                       COUNT_BIG(*) AS Bars
                FROM dbo.{table}
                WHERE Exchange = ?
                GROUP BY SymbolCode, Exchange, {expression}
            )
            SELECT b.SymbolCode, b.Exchange,
                   COUNT_BIG(*) AS ComparedDays,
                   SUM(CASE WHEN {_same_sql('b.BarHigh', 'd.[High]')}
                             AND {_same_sql('b.BarLow', 'd.[Low]')}
                        THEN 1 ELSE 0 END) AS MatchedDays,
                   SUM(b.Bars) AS Bars
            FROM bars b
            JOIN dbo.DailySnapshot d
              ON d.SymbolCode = b.SymbolCode
             AND d.Exchange = b.Exchange
             AND d.SnapshotDate = b.SessionDate
             AND d.Source = ?
            GROUP BY b.SymbolCode, b.Exchange
            """,
            venue, source)
        out = []
        for symbol, exchange, compared, matched, bars in cursor.fetchall():
            compared = int(compared)
            matched = int(matched or 0)
            out.append({
                "symbol": symbol, "exchange": exchange,
                "compared_days": compared, "matched_days": matched,
                "match_rate": round(matched / compared, 4) if compared else 0.0,
                "bars": int(bars or 0),
            })
        return out
    finally:
        conn.close()


def _symbol_rows(store: Store, table: str) -> Dict[tuple, int]:
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT SymbolCode, Exchange, COUNT_BIG(*) FROM dbo.{table} "
            f"GROUP BY SymbolCode, Exchange")
        return {(str(symbol), str(exchange)): int(rows)
                for symbol, exchange, rows in cursor.fetchall()}
    finally:
        conn.close()


def _old_symbol_rows(store: Store, table: str) -> Dict[str, int]:
    """The old table has no Exchange column; one row count per SymbolCode."""
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT SymbolCode, COUNT_BIG(*) FROM dbo.{table} GROUP BY SymbolCode")
        return {str(symbol): int(rows) for symbol, rows in cursor.fetchall()}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="BarReal5Min_New")
    parser.add_argument("--old-table", default=OLD_TABLE)
    parser.add_argument("--daily-source", default=DAILY_SOURCE)
    parser.add_argument("--classification", type=Path, default=CLASSIFICATION)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--csv", type=Path, default=REPORT_CSV)
    args = parser.parse_args()

    settings = load_settings()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)

    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT OBJECT_ID(?, 'U')", f"dbo.{args.table}")
        if cursor.fetchone()[0] is None:
            print(f"  {args.table} does not exist")
            return 1
        cursor.execute(f"SELECT COUNT_BIG(*) FROM dbo.{args.table}")
        total_rows = int(cursor.fetchone()[0])
    finally:
        conn.close()
    if not total_rows:
        print(f"  {args.table} is empty")
        return 1

    pk = _primary_key(store, args.table)
    by_exchange = _exchange_counts(store, args.table)
    print(f"  {args.table}: {total_rows:,} rows  {list(by_exchange)}")
    print(f"  primary key: {[(item['column'], item['ordinal']) for item in pk]}")

    classification = _load_json(args.classification, {})
    by_symbol = {str(row.get("symbol", "")).upper(): row
                 for row in classification.get("symbols", [])}

    details: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {
        "table": args.table,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_rows": total_rows,
        "total_pairs": sum(item["symbols"] for item in by_exchange.values()),
        "by_exchange": by_exchange,
        "primary_key": pk,
        "daily_source": args.daily_source,
    }

    rows = _symbol_rows(store, args.table)
    old_rows = _old_symbol_rows(store, args.old_table)

    for venue in sorted(by_exchange):
        violations = _session_violations(store, args.table, venue)
        daily = {item["symbol"]: item for item in
                 _daily_match(store, args.table, venue, args.daily_source)}
        for (symbol, exchange), count in sorted(rows.items()):
            if exchange != venue:
                continue
            reference = daily.get(symbol, {})
            classifier = by_symbol.get(symbol.upper(), {})
            compared = int(reference.get("compared_days", 0))
            matched = int(reference.get("matched_days", 0))
            details.append({
                "symbol": symbol, "exchange": exchange, "rows": count,
                "old_rows": old_rows.get(symbol, 0),
                "compared_days": compared, "matched_days": matched,
                "match_rate": round(matched / compared, 4) if compared else 0.0,
                "confidence": classifier.get("confidence", "not-classified"),
                "classifier_venue": classifier.get("venue"),
                "reason": classifier.get("reason", ""),
            })
        summary[f"session_violations_{venue}"] = violations
        print(f"  {venue}: {by_exchange[venue]['symbols']:,} symbols, "
              f"{violations:,} bars outside session")

    by_confidence: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"symbols": 0, "below_95": 0, "below_gate": 0, "zero_compared": 0})
    low_rate = []
    for row in details:
        bucket = by_confidence[row["confidence"]]
        bucket["symbols"] += 1
        if row["compared_days"] == 0:
            bucket["zero_compared"] += 1
        if row["match_rate"] < 0.95:
            bucket["below_95"] += 1
        gate = GATES.get(row["confidence"])
        if gate is not None and row["match_rate"] < gate:
            bucket["below_gate"] += 1
            low_rate.append(row)

    old_symbols = set(old_rows)
    new_symbols = {(row["symbol"], row["exchange"]) for row in details}
    new_names = {symbol for symbol, _ in new_symbols}
    missing = sorted(old_symbols - new_names)
    extra = sorted(pair for pair in new_symbols if pair[0] not in old_symbols)
    non_us = sorted(pair for pair in new_symbols if pair[1] != "US")

    summary["daily_match_by_confidence"] = dict(by_confidence)
    summary["gated_low_rate"] = low_rate
    summary["missing_from_new"] = missing
    summary["extra_in_new"] = extra
    summary["new_non_us_pairs"] = non_us
    summary["row_ratio"] = {
        "min": min((row["rows"] / row["old_rows"] for row in details if row["old_rows"]), default=None),
        "max": max((row["rows"] / row["old_rows"] for row in details if row["old_rows"]), default=None),
    }

    _save_json(args.report, {"summary": summary, "symbols": details})

    fields = ["symbol", "exchange", "rows", "old_rows", "compared_days",
              "matched_days", "match_rate", "confidence", "classifier_venue", "reason"]
    with args.csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(details)

    print(f"  daily match by confidence: {dict(by_confidence)}")
    print(f"  gated symbols below their confidence's gate: {len(low_rate)}")
    print(f"  old symbols missing here:  {len(missing)} {missing[:10]}")
    print(f"  new symbols not in old:    {len(extra)} {extra[:10]}")
    print(f"  new pairs not marked US:   {len(non_us)} {non_us[:10]}")
    print(f"  report -> {args.report}")
    print(f"  csv    -> {args.csv}")
    return 0 if not low_rate and not missing and not extra else 2


if __name__ == "__main__":
    raise SystemExit(main())
