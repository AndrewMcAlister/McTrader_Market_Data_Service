#!/usr/bin/env python3
"""Backfill DailySnapshot for the 5-minute bar universe, on every venue it trades on.

Why this exists
---------------

``BarReal5Min`` was loaded from bare tickers, without an exchange key. A bare ticker is
not unique: 253 of the 2,826 cached names also exist on the ASX, and the vendor resolves
a bare symbol by its own default. The 5-minute rows therefore cannot be attributed by
name alone. This tool fetches the *daily* bars for every (symbol, venue) candidate over
the same 600-day window, so the 5-minute series can be matched against them by price.

Each daily row is written to ``DailySnapshot`` with its venue, using the same convention
the daily service uses: raw Open/High/Low, adjusted Close when the vendor supplies one,
and ``IsAdjusted`` stating which close was stored. The raw Open/High/Low are what the
5-minute matcher compares, because ``BarReal5Min`` is not split-adjusted.

Cost: one unit per symbol per venue (``/eod/`` is flat, whatever the window).
2,822 US + 253 ASX = 3,075 calls, about 3,075 units.

    python tools/backfill_bar5min_daily.py --dry-run
    python tools/backfill_bar5min_daily.py --limit 5
    python tools/backfill_bar5min_daily.py --workers 8
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_service.config import load as load_settings  # noqa: E402
from data_service.eodhd import EodhdClient  # noqa: E402
from data_service.store import Store  # noqa: E402
from data_service.usage import Tariff, UsageLedger  # noqa: E402
from pace import RateLimiter  # noqa: E402

CACHE_DIR = Path(r"D:\PaperTrader\cache\minute5")
WORK_DIR = ROOT / "tools" / ".cache"
SYMBOLS_US = WORK_DIR / "symbols_US.json"
SYMBOLS_AU = WORK_DIR / "symbols_AU.json"
PROGRESS = WORK_DIR / "backfill_daily_progress.json"
REPORT = WORK_DIR / "backfill_daily_report.json"

CALLER = "barreal5min.backfill_daily"
BATCH_ROWS = 20_000


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _codes(path: Path) -> set:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {str(row.get("Code") or "").strip().upper() for row in rows if row.get("Code")}


def _universe(cache_dir: Path) -> set:
    return {path.stem.upper() for path in cache_dir.glob("*.json")}


def _candidates(symbols: set, us: set, au: set) -> List[Tuple[str, str]]:
    """Every (symbol, venue) the cached name could belong to, in a stable order.

    A cache name not in either authoritative list is not guessed onto a venue: it is
    reported and left out. The four hyphenated preferred codes are that case.
    """
    out: List[Tuple[str, str]] = []
    for symbol in sorted(symbols):
        if symbol in us:
            out.append((symbol, "US"))
        if symbol in au:
            out.append((symbol, "AU"))
    return out


def _daily_rows(payload: Iterable[Dict[str, Any]], symbol: str,
                venue: str) -> List[Dict[str, Any]]:
    """EODHD daily rows as DailySnapshot rows, oldest first.

    Open/High/Low stay raw, matching DailySnapshot's convention and the unadjusted
    5-minute bars. Close prefers ``adjusted_close`` and says so in ``adjusted``.
    """
    rows: List[Dict[str, Any]] = []
    for raw in payload:
        text = str(raw.get("date") or "").strip()
        if not text:
            continue
        try:
            session = date.fromisoformat(text)
        except ValueError:
            continue
        adjusted = _number(raw.get("adjusted_close"))
        close = adjusted if adjusted is not None else _number(raw.get("close"))
        open_ = _number(raw.get("open"))
        high = _number(raw.get("high"))
        low = _number(raw.get("low"))
        if None in (open_, high, low, close) or close is None or close <= 0:
            continue
        if high <= 0 or low <= 0 or open_ <= 0:
            continue
        rows.append({
            "symbol": symbol,
            "exchange": venue,
            "session": session,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": int(_number(raw.get("volume")) or 0),
            "source": "eodhd-backfill",
            "adjusted": adjusted is not None,
        })
    rows.sort(key=lambda row: row["session"])
    return rows


def _load_progress() -> Dict[str, int]:
    if not PROGRESS.exists():
        return {}
    try:
        return {str(k): int(v) for k, v in json.loads(PROGRESS.read_text()).items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--us-list", type=Path, default=SYMBOLS_US)
    parser.add_argument("--asx-list", type=Path, default=SYMBOLS_AU)
    parser.add_argument("--days", type=int, default=600)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rate", type=float, default=8.0,
                        help="shared requests/second across workers; 0 disables the throttle")
    parser.add_argument("--limit", type=int, default=0, help="candidate pairs, for a pilot")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.us_list.exists() or not args.asx_list.exists():
        print(f"  missing symbol lists: {args.us_list} / {args.asx_list}")
        print("  copy the EODHD exchange-symbol-list JSON for US and AU there first.")
        return 1

    cache_symbols = _universe(args.cache_dir)
    us_codes = _codes(args.us_list)
    au_codes = _codes(args.asx_list)
    candidates = _candidates(cache_symbols, us_codes, au_codes)

    skipped = sorted(cache_symbols - us_codes - au_codes)
    print(f"  cache symbols        {len(cache_symbols):,}")
    print(f"  US candidates        {sum(1 for _, v in candidates if v == 'US'):,}")
    print(f"  ASX candidates       {sum(1 for _, v in candidates if v == 'AU'):,}")
    print(f"  not on either list   {len(skipped):,} {skipped}")
    print(f"  daily /eod/ calls    {len(candidates):,}  (~{len(candidates):,} units)")

    if args.dry_run:
        return 0

    progress = _load_progress()
    remaining = [pair for pair in candidates if progress.get(f"{pair[0]}.{pair[1]}", 0) <= 0]
    todo = remaining[:args.limit] if args.limit else remaining
    print(f"  already recorded     {len(candidates) - len(remaining):,}")
    print(f"  to fetch             {len(todo):,}\n")
    if not todo:
        return 0

    settings = load_settings()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)
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

    to_date = date.today().isoformat()

    def fetch(pair: Tuple[str, str]) -> Tuple[str, str, List[Dict[str, Any]], int, Optional[str]]:
        symbol, venue = pair
        try:
            limiter.wait()
            payload = client().eod(symbol, venue, days=args.days, to_date=to_date)
            rows = _daily_rows(payload, symbol, venue)
            return symbol, venue, rows, len(payload), None
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the load
            return symbol, venue, [], 0, f"{type(exc).__name__}: {exc}"

    pending_rows: List[Dict[str, Any]] = []
    pending_done: List[str] = []
    errors: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    started = time.time()
    completed = 0

    def flush() -> None:
        nonlocal pending_rows, pending_done
        if not pending_rows:
            return
        store.upsert_daily(pending_rows)
        for key in pending_done:
            counts[key] = counts.get(key, 0) + 1
            progress[key] = counts[key]
        _save_json(PROGRESS, progress)
        pending_rows = []
        pending_done = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch, pair): pair for pair in todo}
        for future in as_completed(futures):
            symbol, venue, rows, raw_count, error = future.result()
            completed += 1
            key = f"{symbol}.{venue}"
            if error:
                errors.append({"symbol": symbol, "venue": venue, "error": error})
            elif not rows:
                errors.append({"symbol": symbol, "venue": venue,
                                "error": f"no usable rows from {raw_count} vendor rows"})
            else:
                pending_rows.extend(rows)
                pending_done.append(key)
            if len(pending_rows) >= BATCH_ROWS:
                flush()
            if completed % 100 == 0 or completed == len(todo):
                elapsed = time.time() - started
                rate = completed / max(elapsed, 0.01)
                left = (len(todo) - completed) / max(rate, 0.01)
                print(f"    [{completed:,}/{len(todo):,}] pairs  "
                      f"{len(progress):,} recorded  {len(errors)} errors  "
                      f"{rate:.1f}/s  about {left / 60:.1f} min left", flush=True)
    flush()

    by_venue: Dict[str, int] = {}
    for key in progress:
        venue = key.rsplit(".", 1)[-1]
        by_venue[venue] = by_venue.get(venue, 0) + 1
    report = {
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": args.days,
        "to_date": to_date,
        "candidates": len(candidates),
        "recorded_pairs": len(progress),
        "recorded_by_venue": by_venue,
        "errors": errors,
        "skipped_not_listed": sorted(skipped),
    }
    _save_json(REPORT, report)
    print(f"\n  recorded {len(progress):,} pairs {by_venue}")
    print(f"  errors   {len(errors):,}  -> {REPORT}")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
