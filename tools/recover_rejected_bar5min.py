#!/usr/bin/env python3
"""Re-examine the series the rebuild's price gate rejected, and load the ones the evidence clears.

WHY THESE SERIES ARE NOT AUTOMATICALLY LOST

The rebuild refuses a series whose fresh daily High/Low match falls below its gate. On a
correct series that refusal can still fire, for reasons that are about the daily feed and
not about the venue:

  * the daily feed is split- or dividend-adjusted on some names (SONY, SCCO, CSAN) while
    the 5-minute feed is raw, so the two disagree by a factor the price rule cannot see;
  * a handful of vendor 5-minute bars carry a bad high or low, which moves one day's
    aggregated High/Low out of tolerance;
  * an illiquid name returns mostly empty 5-minute slots (OHLC all null), so only a few
    days have any usable bar and the "at least five days" sample-size floor is not met at
    all.

A low daily match is therefore evidence that the daily comparison did not reproduce, not
proof that the venue is wrong. What settles the venue is the series itself: the old table
holds the very bars the classifier proved against the daily feed at 95-100%. If a fresh
fetch from the explicit venue reproduces those bars at the same timestamps, the fresh
series is the same instrument from the same venue -- an independent confirmation that does
not depend on the daily feed at all.

The tool re-fetches each rejected pair and reports three pieces of evidence:

  daily      the same comparison the rebuild made
  old-table  how many overlapping timestamps carry the same OHLC as the old series
  session    the share of fetched bars inside the venue's regular session

With ``--load`` it writes the series whose evidence clears the rule below; without it,
it only reports. Nothing is accepted on the classifier's say-so alone.

    python tools/recover_rejected_bar5min.py
    python tools/recover_rejected_bar5min.py --load
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import rebuild_bar5min as rb  # noqa: E402
from data_service.config import load as load_settings  # noqa: E402
from data_service.eodhd import EodhdClient  # noqa: E402
from data_service.store import Store  # noqa: E402

WORK_DIR = ROOT / "tools" / ".cache"
REPORT = WORK_DIR / "recover_rejected_bar5min.json"
REPORT_CSV = WORK_DIR / "recover_rejected_bar5min.csv"
REBUILD_REPORT = WORK_DIR / "rebuild_bar5min_report.json"
PROGRESS = WORK_DIR / "rebuild_bar5min_progress.json"

#: A fresh series must be this close to the old, already-classified series at overlapping
#: timestamps. The old table is the classifier's evidence; reproducing it is the point.
OLD_SERIES_RATE = 0.95
OLD_SERIES_MIN_OVERLAP = 50
#: The rule the rebuild already applies, restated here so a recovered pair passes it too.
STRICT_DAYS = 5
STRICT_RATE = 0.95
#: A perfect but short daily sample (HACQ 4/4) still needs corroboration from the old series.
SHORT_DAYS_MIN_OVERLAP = 10
SHORT_DAYS_OLD_RATE = 0.90


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _predicate(price_a: float, price_b: float) -> bool:
    """The classifier's ``_same``: 1e-4 or 0.5% of the larger value, whichever is wider."""
    return abs(price_a - price_b) <= max(
        rb.match.PRICE_TOLERANCE,
        rb.match.RELATIVE_TOLERANCE * max(abs(price_a), abs(price_b)))


def _old_series(store: Store, table: str, symbol: str) -> Dict[datetime, Tuple[float, float, float, float]]:
    """The old table's bars for one symbol, keyed by the UTC timestamp.

    The old table predates the Exchange column, so a symbol is the whole key. Every row
    in it was classified as US, which is what this recovery is checking.
    """
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT BarTimeUtc, [Open], [High], [Low], [Close] "
            f"FROM dbo.{table} WHERE SymbolCode = ?", symbol)
        return {stamp: (float(o), float(h), float(l), float(c))
                for stamp, o, h, l, c in cursor.fetchall()}
    finally:
        conn.close()


def _compare_to_old(bars: List[Dict[str, Any]],
                    old: Dict[datetime, Tuple[float, float, float, float]]) -> Dict[str, Any]:
    """How many of the fresh bars reproduce the old series at the same timestamp."""
    overlap = 0
    matched = 0
    for bar in bars:
        stamp = datetime.fromtimestamp(int(bar["timestamp"]), timezone.utc).replace(tzinfo=None)
        reference = old.get(stamp)
        if reference is None:
            continue
        overlap += 1
        fresh = (bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close"))
        if None in fresh:
            continue
        if all(_predicate(float(a), b) for a, b in zip(fresh, reference)):
            matched += 1
    return {
        "overlap": overlap,
        "old_matched": matched,
        "old_rate": round(matched / overlap, 4) if overlap else 0.0,
    }


def _verdict(evidence: Dict[str, Any]) -> Tuple[bool, str]:
    """Accept only when the venue is established without the daily feed's help."""
    session_ok = evidence["session_share"] >= 0.99
    strict = (evidence["compared_days"] >= STRICT_DAYS
              and evidence["match_rate"] >= STRICT_RATE)
    reproduced = (evidence["overlap"] >= OLD_SERIES_MIN_OVERLAP
                  and evidence["old_rate"] >= OLD_SERIES_RATE)
    short_perfect = (evidence["compared_days"] >= 1
                     and evidence["match_rate"] == 1.0
                     and evidence["overlap"] >= SHORT_DAYS_MIN_OVERLAP
                     and evidence["old_rate"] >= SHORT_DAYS_OLD_RATE)
    if not session_ok:
        return False, "bars are not in the venue's regular session"
    if strict:
        return True, "daily gate met"
    if reproduced:
        return True, (f"fresh series reproduces the classified old series on "
                      f"{evidence['old_matched']}/{evidence['overlap']} timestamps")
    if short_perfect:
        return True, (f"every compared day matches ({evidence['matched_days']}/"
                      f"{evidence['compared_days']}) and the old series is reproduced")
    return False, (f"no independent confirmation: daily {evidence['matched_days']}/"
                   f"{evidence['compared_days']}, old series {evidence['old_matched']}/"
                   f"{evidence['overlap']}")


def _write_report(rows: List[Dict[str, Any]], report: Path, csv_path: Path) -> None:
    report.write_text(json.dumps({"symbols": rows}, indent=2, default=str), encoding="utf-8")
    fields = ["symbol", "venue", "confidence", "fetched", "kept", "bad_rows", "rows",
              "session_share", "matched_days", "compared_days", "match_rate",
              "overlap", "old_matched", "old_rate", "accepted", "reason", "loaded"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="BarReal5Min_New")
    parser.add_argument("--old-table", default="BarReal5Min")
    parser.add_argument("--daily-source", default="eodhd-backfill")
    parser.add_argument("--days", type=int, default=600)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--csv", type=Path, default=REPORT_CSV)
    parser.add_argument("--symbol", action="append", default=[],
                        help="re-examine this symbol instead of every rejected row")
    parser.add_argument("--load", action="store_true",
                        help="write the accepted series; without it, report only")
    parser.add_argument("--update-progress", action="store_true",
                        help="record loaded pairs in the rebuild's progress file")
    args = parser.parse_args()

    rb.TABLE = args.table
    rb.INSERT_SQL = (
        f"INSERT INTO dbo.{args.table} "
        "(SymbolCode, Exchange, BarTimeUtc, [Open], [High], [Low], [Close], Volume) "
        "VALUES (?,?,?,?,?,?,?,?)"
    )

    rebuild = _load_json(REBUILD_REPORT, {})
    if args.symbol:
        wanted = {s.upper() for s in args.symbol}
        rejected = [row for row in rebuild.get("symbols", [])
                    if row.get("symbol", "").upper() in wanted]
    else:
        rejected = [row for row in rebuild.get("symbols", []) if not row.get("ok")]
    if not rejected:
        print("  no rejected rows in the rebuild report")
        return 1

    settings = load_settings()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)
    client = EodhdClient(
        api_key=settings.eodhd_api_key, base_url=settings.eodhd_base_url,
        timeout_seconds=settings.eodhd_timeout_seconds,
        bulk_timeout_seconds=settings.eodhd_bulk_timeout_seconds)

    now = int(time.time())
    start = now - args.days * 86400
    conn = store.connect() if args.load else None
    rows: List[Dict[str, Any]] = []
    try:
        for rejected_row in rejected:
            symbol = rejected_row["symbol"]
            venue = rejected_row["venue"]
            payload = client.intraday(symbol, venue, interval="5m", start=start, end=now)
            normalised = rb._normalise(payload)
            kept = rb._session_bars(normalised, venue)
            sql_rows, stats = rb._sql_rows(symbol, venue, kept)
            daily = rb._verify(store, symbol, venue, normalised)
            old = _old_series(store, args.old_table, symbol)
            comparison = _compare_to_old(kept, old)
            evidence = {**daily, **comparison}
            accepted, reason = _verdict(evidence)
            row: Dict[str, Any] = {
                "symbol": symbol, "venue": venue,
                "confidence": rejected_row.get("confidence"),
                "fetched": len(payload), "kept": len(kept),
                "bad_rows": stats["bad"], "rows": len(sql_rows),
                "session_share": daily["session_share"],
                "matched_days": daily["matched_days"],
                "compared_days": daily["compared_days"],
                "match_rate": daily["match_rate"],
                "overlap": comparison["overlap"],
                "old_matched": comparison["old_matched"],
                "old_rate": comparison["old_rate"],
                "accepted": accepted, "reason": reason, "loaded": 0,
            }
            if accepted and args.load and sql_rows:
                result = {"symbol": symbol, "venue": venue, "rows": sql_rows}
                rb._load_pair(conn, result)
                row["loaded"] = len(sql_rows)
            rows.append(row)
            print(f"  {symbol:<6} {venue} rows={len(sql_rows):>6,}  "
                  f"daily {daily['matched_days']}/{daily['compared_days']} "
                  f"({daily['match_rate']:.1%})  old {comparison['old_matched']}/"
                  f"{comparison['overlap']} ({comparison['old_rate']:.1%})  "
                  f"{'ACCEPT' if accepted else 'REJECT'}  {reason}", flush=True)
    finally:
        if conn is not None:
            conn.close()

    _write_report(rows, args.report, args.csv)
    accepted = [row for row in rows if row["accepted"]]
    loaded = [row for row in rows if row["loaded"]]
    print(f"\n  examined {len(rows)}  accepted {len(accepted)}  loaded {len(loaded)}")
    if not args.load:
        print("  report only; pass --load to write the accepted series")
    print(f"  report -> {args.report}")
    print(f"  csv    -> {args.csv}")

    if args.update_progress and loaded:
        progress = _load_json(PROGRESS, {})
        for row in loaded:
            progress[f"{row['symbol']}.{row['venue']}"] = {
                "bars": row["kept"],
                "confidence": row["confidence"],
                "price_check": "recovered",
                "matched": row["matched_days"],
                "compared": row["compared_days"],
                "rate": row["match_rate"],
                "loaded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        PROGRESS.write_text(json.dumps(progress, indent=2, default=str), encoding="utf-8")
        print(f"  progress updated: {len(loaded)} pair(s)")
    return 0 if len(accepted) == len(rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
