#!/usr/bin/env python3
"""Re-fetch every 5-minute series with an explicit venue, verify it, and load the new table.

WHY RE-FETCH RATHER THAN RELABEL

The cached series were requested by bare ticker. The classifier can prove which venue a
series came from by matching its daily high and low, but a relabel would still leave 400
days of data whose history ends where the cache ends. The request is for the full 600-day
window with the venue stated on the wire, so each series is fetched again through
``/intraday/{SYMBOL}.{VENUE}`` -- the suffix is applied by the client from the venue, never
by the vendor's default.

Every fetched series is verified before a row is written. Its bars are restricted to the
venue's regular session, aggregated by session date, and compared with that venue's daily
high and low from the fresh backfill (``Source='eodhd-backfill'``). The price gate depends
on how the venue was established:

  * ``exact`` / ``unverified-other`` / ``price-exact/session-mismatch`` -- the venue was
    proven by the daily High/Low match, so the fresh series must reproduce it: 95% of at
    least five sessions.
  * ``majority`` -- a dual-listed name whose cached series matched one venue's daily on
    80-95% of sessions and the other on at most 20%. The fresh series must clear 80%.
  * ``listing-only`` -- the authoritative symbol list has the name on one venue only, and
    every cached bar was in that venue's session. There is no rival exchange to fetch
    from; the daily comparison is recorded (``price_check``) but does not veto, because
    the daily feed is split/spin-off adjusted or single-priced on these names.

A series with no usable bars, or one that fails its gate, is reported rather than loaded:
a wrong series in the table is worse than a missing one.

Rows land in ``dbo.BarReal5Min_New`` keyed by ``(SymbolCode, Exchange, BarTimeUtc)``. The
live ``dbo.BarReal5Min`` is untouched until the new table has been verified.

    python tools/rebuild_bar5min.py --dry-run
    python tools/rebuild_bar5min.py --limit 5
    python tools/rebuild_bar5min.py --workers 8 --rate 8
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import classify_bar5min_exchange as match  # noqa: E402
from pace import RateLimiter  # noqa: E402
from data_service.config import load as load_settings  # noqa: E402
from data_service.eodhd import EodhdClient  # noqa: E402
from data_service.store import Store  # noqa: E402
from data_service.usage import Tariff, UsageLedger  # noqa: E402

WORK_DIR = ROOT / "tools" / ".cache"
CLASSIFICATION = WORK_DIR / "bar5min_exchange_report.json"
PROGRESS = WORK_DIR / "rebuild_bar5min_progress.json"
REPORT = WORK_DIR / "rebuild_bar5min_report.json"
REPORT_CSV = WORK_DIR / "rebuild_bar5min_report.csv"

TABLE = "BarReal5Min_New"
DAILY_SOURCE = "eodhd-backfill"
CALLER = "barreal5min.rebuild"
INSERT_BATCH = 5_000

#: The fresh series is fetched from an explicit venue, so this is a sanity gate rather than
#: an attribution: five exact high/low matches cannot come from the other listing, whose
#: prices differ by tens of percent. It is lower than the classifier's ten because a recent
#: listing can legitimately have fewer sessions in the window.
MIN_MATCHED_DAYS = 5
MIN_MATCH_RATE = 0.95

#: The price rate each classifier confidence must reproduce on the fresh series.
#: ``listing-only`` is absent deliberately: its venue is fixed by the authoritative list,
#: not by the daily feed, so it is handled separately in ``fetch``.
PRICE_GATES = {
    "exact": MIN_MATCH_RATE,
    "unverified-other": MIN_MATCH_RATE,
    "price-exact/session-mismatch": MIN_MATCH_RATE,
    "majority": match.MAJORITY_MATCH_RATE,
}

#: EODHD's shared plan allows 100,000 units/day across the estate. The guard is an
#: estimate, not a reservation: other projects spend concurrently.
DAILY_UNIT_LIMIT = 100_000
UNITS_PER_INTRADAY = 5

INSERT_SQL = (
    f"INSERT INTO dbo.{TABLE} "
    "(SymbolCode, Exchange, BarTimeUtc, [Open], [High], [Low], [Close], Volume) "
    "VALUES (?,?,?,?,?,?,?,?)"
)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _wanted_rows(report: Dict[str, Any], symbols: set) -> List[Dict[str, Any]]:
    """Classifier rows whose venue is settled enough to fetch.

    ``unverified-other`` means the chosen venue matched and the other listing had no
    overlapping daily bars; the fresh fetch is explicit, so the caveat does not apply to
    it. ``price-exact/session-mismatch`` means the venue is proven and the old timestamp
    was wrong, which the fresh fetch replaces. ``majority`` is a dual-listed name with
    one-sided price evidence; ``listing-only`` is a name the authoritative list has on a
    single venue, with every cached bar in that venue's session. AMBIGUOUS and UNKNOWN
    are not fetched.
    """
    wanted = []
    for row in report.get("symbols", []):
        if row.get("venue") not in ("US", "AU"):
            continue
        if row.get("confidence") not in ("exact", "unverified-other",
                                         "price-exact/session-mismatch",
                                         "majority", "listing-only"):
            continue
        if symbols and str(row.get("symbol", "")).upper() not in symbols:
            continue
        wanted.append(row)
    return wanted


def _normalise(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Vendor bars with a numeric timestamp and the price fields named as the matcher expects."""
    out: List[Dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        stamp = _number(bar.get("timestamp"))
        if stamp is None:
            continue
        out.append({
            "timestamp": stamp,
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "volume": bar.get("volume"),
        })
    return out


def _session_bars(bars: List[Dict[str, Any]], venue: str) -> List[Dict[str, Any]]:
    """Keep only the venue's regular session, inclusive of the closing print."""
    tz, open_at, close_at = match.VENUES[venue]
    kept: List[Dict[str, Any]] = []
    for bar in bars:
        stamp = bar["timestamp"]
        local = datetime.fromtimestamp(float(stamp), tz)
        if open_at <= local.time() <= close_at:
            kept.append(bar)
    return kept


def _sql_rows(symbol: str, venue: str, bars: List[Dict[str, Any]]) -> Tuple[
        List[Tuple[Any, ...]], Dict[str, int]]:
    """Bars as SQL rows, oldest first, with malformed rows counted rather than guessed."""
    seen: Dict[int, Tuple[Any, ...]] = {}
    bad = 0
    valid = 0
    for bar in bars:
        stamp = int(bar["timestamp"])
        open_ = _number(bar.get("open"))
        high = _number(bar.get("high"))
        low = _number(bar.get("low"))
        close = _number(bar.get("close"))
        volume = _number(bar.get("volume"))
        if None in (open_, high, low, close):
            bad += 1
            continue
        if (not all(math.isfinite(value) for value in (open_, high, low, close))
                or min(open_, high, low, close) <= 0):
            bad += 1
            continue
        # A bar whose high is below its own open or close, or whose low is above either,
        # is not a bar that can be trusted in a high/low verification.
        if high < max(open_, close, low) or low > min(open_, close, high):
            bad += 1
            continue
        valid += 1
        seen[stamp] = (
            symbol, venue,
            datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None),
            Decimal(str(open_)), Decimal(str(high)), Decimal(str(low)),
            Decimal(str(close)), max(0, int(volume or 0)),
        )
    rows = [seen[stamp] for stamp in sorted(seen)]
    return rows, {"bad": bad, "duplicates": max(0, valid - len(seen))}


def _verify(store: Store, symbol: str, venue: str,
            bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Price-match the fetched series against the venue's fresh daily bars."""
    by_date, in_session, total = match._aggregate(bars, venue)
    daily = match._daily(store, symbol, DAILY_SOURCE)
    reference = daily.get(venue, {})
    matched, compared, offset = match._match(by_date, reference)
    rate = matched / compared if compared else 0.0
    return {
        "matched_days": matched,
        "compared_days": compared,
        "match_rate": round(rate, 4),
        "offset_matched_days": offset,
        "daily_days": len(reference),
        "bar_days": len(by_date),
        "session_share": round(in_session / total, 4) if total else 0.0,
    }


def _today_spend(store: Store) -> int:
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ISNULL(SUM(CostTotal), 0) FROM dbo.EODHDUsage WHERE UsageDate = ?",
            date.today())
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def _load_pair(conn: Any, result: Dict[str, Any]) -> None:
    """Replace one symbol's rows in one transaction, so a crash leaves no half series."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"DELETE FROM dbo.{TABLE} WHERE SymbolCode = ? AND Exchange = ?",
            result["symbol"], result["venue"])
        rows = result["rows"]
        for start in range(0, len(rows), INSERT_BATCH):
            cursor.executemany(INSERT_SQL, rows[start:start + INSERT_BATCH])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    result["rows"] = []


def main() -> int:
    global TABLE, DAILY_SOURCE, INSERT_SQL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classification", type=Path, default=CLASSIFICATION)
    parser.add_argument("--days", type=int, default=600)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rate", type=float, default=8.0,
                        help="shared requests/second across workers; 0 disables the throttle")
    parser.add_argument("--limit", type=int, default=0, help="pairs, for a pilot")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--table", default=TABLE)
    parser.add_argument("--daily-source", default=DAILY_SOURCE)
    parser.add_argument("--force", action="store_true", help="ignore the daily unit guard")
    parser.add_argument("--log", type=Path, default=None,
                        help="append stdout/stderr here (required under pythonw, which has no console)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.log is not None:
        # pythonw has no console, so a scheduled run needs somewhere to write. Line-buffered
        # so a reader sees progress as it happens rather than at process exit.
        args.log.parent.mkdir(parents=True, exist_ok=True)
        handle = args.log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle

    TABLE = args.table
    DAILY_SOURCE = args.daily_source
    INSERT_SQL = (
        f"INSERT INTO dbo.{TABLE} "
        "(SymbolCode, Exchange, BarTimeUtc, [Open], [High], [Low], [Close], Volume) "
        "VALUES (?,?,?,?,?,?,?,?)"
    )

    report = _load_json(args.classification, {})
    if not report.get("symbols"):
        print(f"  no classification at {args.classification}")
        print("  run tools/classify_bar5min_exchange.py first")
        return 1

    wanted = _wanted_rows(report, {s.upper() for s in args.symbol})
    all_wanted = _wanted_rows(report, set())
    pairs = [(row["symbol"], row["venue"], row) for row in wanted]
    if args.limit:
        pairs = pairs[:args.limit]
    if not pairs:
        print("  no pairs selected")
        return 1

    by_venue = Counter(venue for _, venue, _ in pairs)
    by_confidence = Counter(row.get("confidence") for _, _, row in pairs)
    units = len(pairs) * UNITS_PER_INTRADAY
    print(f"  classified symbols   {len(report.get('symbols', [])):,}")
    print(f"  skipped by venue     {len(report.get('symbols', [])) - len(all_wanted):,}")
    print(f"  to fetch             {len(pairs):,}  {dict(by_venue)}")
    print(f"  confidence           {dict(by_confidence)}")
    print(f"  intraday /intraday/  {len(pairs):,} calls (~{units:,} units)")

    if args.dry_run:
        return 0

    settings = load_settings()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)

    progress = _load_json(PROGRESS, {})
    todo = [pair for pair in pairs if f"{pair[0]}.{pair[1]}" not in progress]
    print(f"  already loaded       {len(pairs) - len(todo):,}")
    print(f"  to do now            {len(todo):,}\n")
    if not todo:
        return 0

    spend = _today_spend(store)
    todo_units = len(todo) * UNITS_PER_INTRADAY
    print(f"  units already spent  {spend:,} today; this run would take it to "
          f"~{spend + todo_units:,}")
    if spend + todo_units > DAILY_UNIT_LIMIT and not args.force:
        print(f"  refusing: over the {DAILY_UNIT_LIMIT:,} daily unit limit; pass --force")
        return 1

    tariff = Tariff.from_rows(store.request_types())
    ledger = UsageLedger(store, tariff)
    record = ledger.recorder_for(CALLER)
    record_lock = threading.Lock()
    limiter = RateLimiter(args.rate)

    def recorder(request_type: str, symbols: int) -> int:
        with record_lock:
            return record(request_type, symbols)

    local = threading.local()

    def client() -> EodhdClient:
        if not hasattr(local, "client"):
            local.client = EodhdClient(
                api_key=settings.eodhd_api_key,
                base_url=settings.eodhd_base_url,
                timeout_seconds=settings.eodhd_timeout_seconds,
                bulk_timeout_seconds=settings.eodhd_bulk_timeout_seconds,
            )
            local.client.recorder = recorder
        return local.client

    window_end = int(datetime.now(timezone.utc).timestamp())
    window_start = window_end - args.days * 86400

    def fetch(pair: Tuple[str, str, Dict[str, Any]]) -> Dict[str, Any]:
        symbol, venue, row = pair
        result: Dict[str, Any] = {
            "symbol": symbol, "venue": venue, "confidence": row.get("confidence"),
            "price_check": None,
            "fetched": 0, "kept": 0, "bad_rows": 0, "duplicates": 0,
            "rows": [], "evidence": {}, "ok": False, "error": None,
        }
        try:
            limiter.wait()
            payload = client().intraday(
                symbol, venue, interval="5m", start=window_start, end=window_end)
            normalised = _normalise(payload)
            kept = _session_bars(normalised, venue)
            rows, stats = _sql_rows(symbol, venue, kept)
            # Verify over every fetched bar so the session share describes the wire,
            # not the already-filtered set.
            evidence = _verify(store, symbol, venue, normalised)
            result.update({
                "fetched": len(payload), "kept": len(kept), "rows": rows,
                "bad_rows": stats["bad"], "duplicates": stats["duplicates"],
                "evidence": evidence,
            })
            confidence = row.get("confidence")
            if confidence == "listing-only":
                # The venue is fixed by the authoritative list -- the name has no listing
                # on the other exchange. The daily feed is adjusted for splits/spin-offs,
                # or carries a single price, on exactly the ADR/OTC names that reach this
                # path, so its rate is reported and not used as a veto.
                result["ok"] = bool(rows)
                result["price_check"] = (
                    "passed" if (evidence["compared_days"] >= MIN_MATCHED_DAYS
                                 and evidence["match_rate"] >= MIN_MATCH_RATE)
                    else "not-required")
                if not result["ok"]:
                    result["error"] = "no bars in the venue's regular session"
            else:
                gate = PRICE_GATES.get(confidence, MIN_MATCH_RATE)
                result["ok"] = (
                    bool(rows)
                    and evidence["compared_days"] >= MIN_MATCHED_DAYS
                    and evidence["match_rate"] >= gate
                )
                result["price_check"] = (
                    "passed" if evidence["match_rate"] >= MIN_MATCH_RATE
                    else "accepted-majority" if result["ok"] else "failed")
                if not result["ok"]:
                    result["error"] = (
                        f"verification failed: {evidence['matched_days']}/"
                        f"{evidence['compared_days']} sessions match at "
                        f"{evidence['match_rate']:.1%}; need {gate:.0%} of "
                        f">={MIN_MATCHED_DAYS}")
        except Exception as exc:  # noqa: BLE001 - one bad series must not stop the load
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    report_rows: List[Dict[str, Any]] = []
    loaded = 0
    loaded_bars = 0
    errors = 0
    started = time.time()
    conn = store.connect()
    workers = max(1, args.workers)
    #: Completed results held in memory before the loader drains them. One fetched
    #: 600-day series is ~20 MB of bar dictionaries. Submitting every future at once (the
    #: first attempt at this run) therefore holds the whole universe in RAM -- 2,800
    #: series is ~50 GB -- while the loader replaces them one at a time. The window keeps
    #: the fetch pool full without ever parking more than two results per worker.
    window = max(workers * 2, 2)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            remaining = iter(todo)
            in_flight = {pool.submit(fetch, pair) for pair in islice(remaining, window)}
            index = 0
            while in_flight:
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    index += 1
                    result = future.result()
                    key = f"{result['symbol']}.{result['venue']}"
                    if result["ok"]:
                        try:
                            _load_pair(conn, result)
                        except Exception as exc:  # noqa: BLE001
                            result["ok"] = False
                            result["error"] = f"load failed: {type(exc).__name__}: {exc}"
                            # A broken connection would fail every later pair too; replace it.
                            try:
                                conn.close()
                            except Exception:  # noqa: BLE001
                                pass
                            try:
                                conn = store.connect()
                            except Exception:  # noqa: BLE001
                                pass
                    if result["ok"]:
                        loaded += 1
                        loaded_bars += result["kept"]
                        progress[key] = {
                            "bars": result["kept"],
                            "confidence": result["confidence"],
                            "price_check": result["price_check"],
                            "matched": result["evidence"]["matched_days"],
                            "compared": result["evidence"]["compared_days"],
                            "rate": result["evidence"]["match_rate"],
                            "loaded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        }
                        _save_json(PROGRESS, progress)
                    else:
                        errors += 1
                    summary_row = {
                        key: value for key, value in result.items() if key != "rows"
                    }
                    report_rows.append(summary_row)
                    # Refill only after the result has been loaded and released, so the
                    # window counts completed results, not submitted ones.
                    pair = next(remaining, None)
                    if pair is not None:
                        in_flight.add(pool.submit(fetch, pair))
                    if index % 50 == 0 or index == len(todo):
                        elapsed = time.time() - started
                        rate = index / max(elapsed, 0.01)
                        left = (len(todo) - index) / max(rate, 0.01)
                        print(f"    [{index:,}/{len(todo):,}] {loaded:,} loaded  "
                              f"{loaded_bars:,} bars  {errors} rejected  "
                              f"{rate:.1f} pairs/s  about {left / 60:.1f} min left", flush=True)
                        _save_json(REPORT, {
                            "symbols": report_rows,
                            "loaded": loaded,
                            "errors": errors,
                        })
    finally:
        conn.close()

    by_loaded_venue = Counter(row["venue"] for row in report_rows if row["ok"])
    by_price_check = Counter(row["price_check"] for row in report_rows if row["ok"])
    final = {
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": args.days,
        "pairs": len(todo),
        "loaded": loaded,
        "loaded_bars": loaded_bars,
        "loaded_by_venue": dict(by_loaded_venue),
        "loaded_by_price_check": dict(by_price_check),
        "rejected": errors,
        "units_estimated": len(todo) * UNITS_PER_INTRADAY,
        "symbols": report_rows,
    }
    _save_json(REPORT, final)

    fields = ["symbol", "venue", "confidence", "price_check", "ok", "fetched", "kept",
              "bad_rows", "duplicates", "error",
              "ev_matched_days", "ev_compared_days", "ev_match_rate",
              "ev_offset_matched_days", "ev_daily_days", "ev_bar_days",
              "ev_session_share"]
    with REPORT_CSV.open("w", newline="", encoding="utf-8") as handle:
        import csv
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in report_rows:
            flat = dict(row)
            evidence = flat.pop("evidence", {}) or {}
            flat.update({f"ev_{key}": value for key, value in evidence.items()})
            writer.writerow(flat)

    print(f"\n  loaded {loaded:,} pairs / {loaded_bars:,} bars  {dict(by_loaded_venue)}")
    print(f"  price check          {dict(by_price_check)}")
    print(f"  rejected {errors:,}")
    print(f"  report -> {REPORT}")
    print(f"  csv    -> {REPORT_CSV}")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
