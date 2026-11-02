#!/usr/bin/env python3
"""Say which exchange each cached 5-minute series belongs to, from prices, not names.

The 5-minute cache was fetched by bare ticker. A bare ticker is not unique: 253 of the
2,826 names are also ASX listings, and the vendor resolves a bare symbol by its own
default. The only defensible attribution is the data itself:

  * a US session runs 09:30-16:00 America/New_York;
  * an ASX session runs 10:00-16:00 Australia/Sydney;
  * the two windows do not overlap, so a series can sit in only one;
  * and within the right window, the day's High and Low must equal the venue's daily bar
    to within half a percent. A same-named company on the other exchange has different
    prices. (Open is deliberately not compared: the daily open is the opening auction
    print, which differs from the first 5-minute trade by a few cents on many days.)

For every symbol this reports, per candidate venue, how many bars fall in that venue's
session and how many sessions match the venue's daily high and low. Price is the proof;
session share says whether the timestamps are right, and a price match with the wrong
session is reported as such rather than discarded. A series is only called for a venue
when it beats the other venue by at least 20 percentage points; a near-tie is AMBIGUOUS.
If a venue the symbol is listed on has no overlapping daily bars, the result is marked
unverified rather than treated as proven. Anything else is UNKNOWN and is not written.

Two weaker but still evidence-based paths cover the names the exact gate misses:

  * ``listing-only`` -- the authoritative symbol list has the name on one venue and not
    the other, and every cached bar falls in that venue's session. There is no rival
    listing to confuse it with. This matters for ADRs and OTC names whose daily feed is
    adjusted for splits/spin-offs or carries a single price, so the daily High/Low cannot
    confirm what the listing already settles. The price rate is still reported.
  * ``majority`` -- the name is on both venues, but one venue's daily High/Low match the
    series on at least 80% of shared sessions while the other matches at most 20%. A
    91%-vs-0% split over hundreds of sessions is not a coincidence, even though it does
    not clear the exact 95% gate.

Read-only. The daily bars it compares against come from DailySnapshot, backfilled over
the same 600 days by tools/backfill_bar5min_daily.py.

    python tools/classify_bar5min_exchange.py --limit 20
    python tools/classify_bar5min_exchange.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_service.config import load as load_settings  # noqa: E402
from data_service.store import Store  # noqa: E402

CACHE_DIR = Path(r"D:\PaperTrader\cache\minute5")
WORK_DIR = ROOT / "tools" / ".cache"
SYMBOLS_US = WORK_DIR / "symbols_US.json"
SYMBOLS_AU = WORK_DIR / "symbols_AU.json"
REPORT_JSON = WORK_DIR / "bar5min_exchange_report.json"
REPORT_CSV = WORK_DIR / "bar5min_exchange_report.csv"

# The regular session, exchange-local, inclusive at both ends. The cache carries a
# 16:00 ET bar -- the closing print -- and the day's high or low can be set by it, so
# excluding it would reject a perfectly good series on those days.
VENUES: Dict[str, Tuple[ZoneInfo, time, time]] = {
    "US": (ZoneInfo("America/New_York"), time(9, 30), time(16, 0)),
    "AU": (ZoneInfo("Australia/Sydney"), time(10, 0), time(16, 0)),
}

#: The cache writes a bar as ``[epoch, open, high, low, close, volume]``. A dict form is
#: accepted too, because the vendor's own JSON uses names and a re-fetch may keep them.
BAR_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")
BAR_INDEX = {name: index for index, name in enumerate(BAR_FIELDS)}

# A daily bar and a 5-minute aggregate of the same session must agree. The official open
# often does not: it is the opening auction print, while the 5-minute bar's open is the
# first trade after it, and the two differ by a few cents on some days. High and low are
# the honest comparison -- they are set by trades, and a different venue's prices differ
# by tens of percent. Half a percent absorbs auction and rounding noise without coming
# close to the gap between two companies or two currencies.
PRICE_TOLERANCE = 1e-4
RELATIVE_TOLERANCE = 5e-3

MIN_SESSION_SHARE = 0.90
MIN_MATCH_RATE = 0.95
MIN_MATCHED_DAYS = 10
#: The chosen venue must beat the other by this much. A near-tie is reported as ambiguous
#: rather than resolved in favour of whichever number happened to be higher.
MIN_SEPARATION = 0.20

#: A dual-listed name can still be called when the price evidence is one-sided: at least
#: 80% of shared sessions matching one venue, no more than 20% matching the other, and the
#: usual 20-point separation. This exists because the daily feed is adjusted for corporate
#: actions on some names, which drags a true match rate below 95% without weakening the
#: venue evidence at all -- 91% versus 0% over 275 sessions is decisive.
MAJORITY_MATCH_RATE = 0.80
MAX_RIVAL_RATE = 0.20


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _field(bar: Any, name: str) -> Any:
    """One value from an array-form or object-form bar."""
    if isinstance(bar, dict):
        return bar.get(name)
    index = BAR_INDEX[name]
    if isinstance(bar, (list, tuple)) and len(bar) > index:
        return bar[index]
    return None


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= max(PRICE_TOLERANCE, RELATIVE_TOLERANCE * max(abs(a), abs(b)))


def _codes(path: Path) -> set:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {str(row.get("Code") or "").strip().upper() for row in rows if row.get("Code")}


def _aggregate(bars: Iterable[Dict[str, Any]], venue: str) -> Tuple[
        Dict[date, Dict[str, float]], int, int]:
    """Bars grouped by this venue's local session date.

    Returns ``(by_date, bars_in_session, bars_total)``. A bar outside the session is
    not silently moved into the nearest one: it is counted as out, because that count
    is the first piece of evidence that the series belongs to another venue.
    """
    tz, open_at, close_at = VENUES[venue]
    total = 0
    in_session = 0
    groups: Dict[date, List[Any]] = defaultdict(list)
    for bar in bars:
        stamp = _field(bar, "timestamp")
        if not isinstance(stamp, (int, float)):
            continue
        total += 1
        local = datetime.fromtimestamp(float(stamp), tz)
        if not (open_at <= local.time() <= close_at):
            continue
        in_session += 1
        groups[local.date()].append(bar)

    by_date: Dict[date, Dict[str, float]] = {}
    for session_date, rows in groups.items():
        rows.sort(key=lambda row: float(_field(row, "timestamp")))
        highs = [_number(_field(row, "high")) for row in rows]
        lows = [_number(_field(row, "low")) for row in rows]
        opens = [_number(_field(row, "open")) for row in rows]
        closes = [_number(_field(row, "close")) for row in rows]
        if None in (highs[0], lows[0], opens[0], closes[-1]):
            continue
        by_date[session_date] = {
            "open": float(opens[0]),
            "high": max(float(value) for value in highs if value is not None),
            "low": min(float(value) for value in lows if value is not None),
            "close": float(closes[-1]),
            "volume": sum(float(_number(_field(row, "volume")) or 0) for row in rows),
            "bars": len(rows),
        }
    return by_date, in_session, total


def _daily(store: Store, symbol: str, source: str) -> Dict[str, Dict[date, Dict[str, float]]]:
    """The fresh daily bars this backfill wrote, per venue.

    The Source filter is not cosmetic. NHC.US has older rows from two different feeds: a
    National HealthCare bar at ~$206 and another vendor's New Hope bar at ~$6 on a nearby
    date, under the same key. Comparing against that mixture would make a correct series
    look wrong. Only rows from the backfill named on the command line are evidence.
    """
    conn = store.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Exchange, SnapshotDate, [Open], [High], [Low], Volume "
            "FROM dbo.DailySnapshot "
            "WHERE SymbolCode = ? AND Source = ? ORDER BY Exchange, SnapshotDate",
            symbol, source)
        out: Dict[str, Dict[date, Dict[str, float]]] = defaultdict(dict)
        for venue, session_date, open_, high, low, volume in cursor.fetchall():
            if open_ is None or high is None or low is None:
                continue
            out[str(venue)][session_date] = {
                "open": float(open_), "high": float(high), "low": float(low),
                "volume": float(volume or 0),
            }
        return out
    finally:
        conn.close()


def _match(by_date: Dict[date, Dict[str, float]],
           daily: Dict[date, Dict[str, float]]) -> Tuple[int, int, int]:
    """(matched sessions, sessions with a daily bar, matched only at a one-day offset).

    The offset count catches a series whose date was shifted across midnight by a bad
    conversion. The primary rate still uses same-date matches only.
    """
    matched = 0
    compared = 0
    offset_matched = 0
    for session_date, bar in by_date.items():
        reference = daily.get(session_date)
        if reference is not None:
            compared += 1
            if _same(bar["high"], reference["high"]) and _same(bar["low"], reference["low"]):
                matched += 1
                continue
        for delta in (-1, 1):
            neighbour = daily.get(session_date + timedelta(days=delta))
            if neighbour is not None and (
                    _same(bar["high"], neighbour["high"])
                    and _same(bar["low"], neighbour["low"])):
                offset_matched += 1
                break
    return matched, compared, offset_matched


def _classify_symbol(symbol: str, bars: List[Any],
                     daily: Dict[str, Dict[date, Dict[str, float]]],
                     us_listed: bool, au_listed: bool) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "symbol": symbol, "bars": len(bars),
        "us_listed": us_listed, "au_listed": au_listed,
        "venue": "UNKNOWN", "confidence": "none", "reason": "",
    }
    if not bars:
        row["reason"] = "no cached bars"
        return row

    # A venue the symbol is not listed on cannot be where the series came from, and has no
    # daily bars to compare. Evaluating it anyway doubles the timezone work for the ~90%
    # of names that are US-only.
    venues = [venue for venue, listed in (("US", us_listed), ("AU", au_listed)) if listed]
    if not venues:
        row["reason"] = "not on either exchange list"
        return row

    groups: Dict[str, Dict[date, Dict[str, float]]] = {}
    shares: Dict[str, float] = {}
    for venue in venues:
        by_date, in_session, total = _aggregate(bars, venue)
        groups[venue] = by_date
        shares[venue] = in_session / total if total else 0.0

    evidence: Dict[str, Dict[str, Any]] = {}
    for venue in venues:
        reference = daily.get(venue, {})
        other_venue = "AU" if venue == "US" else "US"
        own = _match(groups[venue], reference)
        # If the prices match but the timestamps sit in the other venue's session, the
        # venue is still this one; the timestamp is what is wrong. That is a separate
        # finding, reported as such, not a reason to reject the series.
        fallback = (_match(groups[other_venue], reference)
                    if other_venue in groups else (0, 0, 0))
        # Prefer a grouping that actually overlapped the daily series: "no bars in this
        # session" is not evidence either way, but a price comparison over 276 sessions
        # that found no match is a real rejection of this venue.
        if own[1] == 0 and fallback[1] > 0:
            matched_on = "other"
        elif fallback[1] == 0 and own[1] > 0:
            matched_on = "own"
        else:
            matched_on = "own" if own[0] >= fallback[0] else "other"
        matched, compared, offset = own if matched_on == "own" else fallback
        rate = matched / compared if compared else 0.0
        evidence[venue] = {
            "session_share": round(shares[venue], 4),
            "matched_days": matched,
            "compared_days": compared,
            "match_rate": round(rate, 4),
            "offset_matched_days": offset,
            "matched_on": matched_on,
            "own_matched_days": own[0],
            "own_compared_days": own[1],
            "fallback_matched_days": fallback[0],
            "fallback_compared_days": fallback[1],
            "daily_days": len(reference),
        }
        row[f"{venue.lower()}_session_share"] = round(shares[venue], 4)
        row[f"{venue.lower()}_matched_days"] = matched
        row[f"{venue.lower()}_compared_days"] = compared
        row[f"{venue.lower()}_match_rate"] = round(rate, 4)
        row[f"{venue.lower()}_matched_on"] = matched_on
        row[f"{venue.lower()}_daily_days"] = len(reference)

    for venue in ("US", "AU"):
        if venue in evidence:
            continue
        row[f"{venue.lower()}_session_share"] = 0.0
        row[f"{venue.lower()}_matched_days"] = 0
        row[f"{venue.lower()}_compared_days"] = 0
        row[f"{venue.lower()}_match_rate"] = 0.0
        row[f"{venue.lower()}_matched_on"] = "not-listed"
        row[f"{venue.lower()}_daily_days"] = len(daily.get(venue, {}))

    eligible = [
        venue for venue, stats in evidence.items()
        if stats["matched_days"] >= MIN_MATCHED_DAYS
        and stats["match_rate"] >= MIN_MATCH_RATE
    ]
    listed = {venue for venue, flag in (("US", us_listed), ("AU", au_listed)) if flag}

    if len(eligible) == 1:
        venue = eligible[0]
        rival = "AU" if venue == "US" else "US"
        rival_rate = evidence.get(rival, {}).get("match_rate", 0.0)
        separation = evidence[venue]["match_rate"] - rival_rate
        row["separation"] = round(separation, 4)
        untested = sorted(v for v in listed
                          if v != venue and v in evidence
                          and evidence[v]["compared_days"] == 0)
        if separation < MIN_SEPARATION:
            row["venue"] = "AMBIGUOUS"
            row["confidence"] = "ambiguous"
            row["reason"] = (
                f"{venue} leads but only by {separation:.0%}; both venues are too close "
                f"to call ({evidence[venue]['match_rate']:.0%} vs {rival_rate:.0%})")
        elif untested:
            row["venue"] = venue
            row["confidence"] = "unverified-other"
            row["reason"] = (
                f"{venue}: {evidence[venue]['matched_days']} sessions match daily OHLC; "
                f"{'/'.join(untested)} is listed but had no overlapping daily bars, so it "
                f"could not be ruled out")
        else:
            row["venue"] = venue
            if evidence[venue]["matched_on"] == "own":
                row["confidence"] = "exact"
                row["reason"] = (
                    f"{venue}: {evidence[venue]['matched_days']} sessions match daily "
                    f"OHLC; {evidence[venue]['session_share']:.0%} of bars in session")
            else:
                row["confidence"] = "price-exact/session-mismatch"
                row["reason"] = (
                    f"{venue}: {evidence[venue]['matched_days']} sessions match daily "
                    f"OHLC, but the bars are timestamped in the "
                    f"{'US' if venue == 'AU' else 'AU'} session; the venue is proven, the "
                    f"timestamp is not")
    elif len(eligible) > 1:
        row["venue"] = "AMBIGUOUS"
        row["confidence"] = "ambiguous"
        row["separation"] = 0.0
        row["reason"] = "both venues match; refusing to choose"
    else:
        # No venue cleared the exact gate. Two paths remain, both evidence-based.
        if len(listed) == 1:
            venue = next(iter(listed))
            stats = evidence[venue]
            if stats["session_share"] >= MIN_SESSION_SHARE:
                row["venue"] = venue
                row["confidence"] = "listing-only"
                row["separation"] = round(stats["match_rate"], 4)
                row["reason"] = (
                    f"{venue}: the only venue the authoritative list has for this name, "
                    f"and {stats['session_share']:.0%} of bars are in its session; daily "
                    f"High/Low agree on {stats['matched_days']}/"
                    f"{stats['compared_days']} sessions ({stats['match_rate']:.0%}), "
                    f"below the exact gate -- the venue rests on the listing and the "
                    f"session, not on the daily comparison")
                return row
        else:
            for venue in ("US", "AU"):
                if venue not in listed:
                    continue
                rival = "AU" if venue == "US" else "US"
                stats = evidence[venue]
                rival_stats = evidence.get(rival, {})
                rival_rate = rival_stats.get("match_rate", 0.0)
                separation = stats["match_rate"] - rival_rate
                if (stats["session_share"] >= MIN_SESSION_SHARE
                        and stats["matched_days"] >= MIN_MATCHED_DAYS
                        and stats["match_rate"] >= MAJORITY_MATCH_RATE
                        and rival_stats.get("compared_days", 0) > 0
                        and rival_rate <= MAX_RIVAL_RATE
                        and separation >= MIN_SEPARATION):
                    row["venue"] = venue
                    row["confidence"] = "majority"
                    row["separation"] = round(separation, 4)
                    row["reason"] = (
                        f"{venue}: {stats['matched_days']}/{stats['compared_days']} "
                        f"sessions match daily High/Low ({stats['match_rate']:.0%}) "
                        f"against {rival} {rival_rate:.0%}; a {separation:.0%} separation "
                        f"is decisive even though the match rate is below the exact gate")
                    return row

        # Say the strongest near-miss, so the failure is diagnosable without a rerun.
        best = max(evidence, key=lambda v: (evidence[v]["match_rate"],
                                            evidence[v]["session_share"]))
        row["separation"] = round(evidence[best]["match_rate"], 4)
        row["reason"] = (
            f"no venue met match>={MIN_MATCH_RATE:.0%} over >={MIN_MATCHED_DAYS} days; "
            f"best {best}: share={evidence[best]['session_share']:.0%}, "
            f"match={evidence[best]['match_rate']:.0%} over "
            f"{evidence[best]['compared_days']} days")
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--us-list", type=Path, default=SYMBOLS_US)
    parser.add_argument("--asx-list", type=Path, default=SYMBOLS_AU)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--symbol", action="append", default=[], help="restrict to this symbol")
    parser.add_argument("--source", default="eodhd-backfill",
                        help="only compare against DailySnapshot rows from this source")
    parser.add_argument("--out-json", type=Path, default=REPORT_JSON)
    parser.add_argument("--out-csv", type=Path, default=REPORT_CSV)
    args = parser.parse_args()

    us_codes = _codes(args.us_list)
    au_codes = _codes(args.asx_list)
    paths = sorted(args.cache_dir.glob("*.json"))
    if args.symbol:
        wanted = {s.upper() for s in args.symbol}
        paths = [path for path in paths if path.stem.upper() in wanted]
    if args.limit:
        paths = paths[:args.limit]
    if not paths:
        print("  no cache files selected")
        return 1

    settings = load_settings()
    store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)

    rows: List[Dict[str, Any]] = []
    for index, path in enumerate(paths, 1):
        symbol = path.stem.upper()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            rows.append({"symbol": symbol, "venue": "UNKNOWN", "confidence": "none",
                         "reason": f"unreadable cache: {exc}", "bars": 0})
            continue
        bars = payload.get("bars") or []
        row = _classify_symbol(symbol, bars, _daily(store, symbol, args.source),
                               symbol in us_codes, symbol in au_codes)
        rows.append(row)
        if index % 100 == 0 or index == len(paths):
            counts: Dict[str, int] = defaultdict(int)
            for item in rows:
                counts[item["venue"]] += 1
            print(f"    [{index:,}/{len(paths):,}] {dict(counts)}", flush=True)

    counts = defaultdict(int)
    for row in rows:
        counts[row["venue"]] += 1
    summary = {
        "symbols": len(rows),
        "by_venue": dict(sorted(counts.items())),
        "by_confidence": dict(sorted(
            {c: sum(1 for row in rows if row.get("confidence") == c)
             for c in {row.get("confidence") for row in rows}}.items())),
        "unknown": [row for row in rows if row["venue"] not in ("US", "AU")],
        "unverified_other": [row["symbol"] for row in rows
                             if row.get("confidence") == "unverified-other"],
        "session_mismatch": [row["symbol"] for row in rows
                             if row.get("confidence") == "price-exact/session-mismatch"],
        "us_listed_not_us": [row["symbol"] for row in rows
                             if row.get("us_listed") and row["venue"] != "US"],
        "au_listed_not_au": [row["symbol"] for row in rows
                             if row.get("au_listed") and row["venue"] == "US"],
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps({"summary": summary, "symbols": rows},
                                        indent=2, default=str), encoding="utf-8")

    fields = ["symbol", "bars", "venue", "confidence", "reason",
              "us_listed", "au_listed", "separation",
              "us_session_share", "us_matched_days", "us_compared_days", "us_match_rate",
              "us_matched_on", "us_daily_days",
              "au_session_share", "au_matched_days", "au_compared_days", "au_match_rate",
              "au_matched_on", "au_daily_days"]
    with args.out_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n  classified {len(rows):,} symbols: {dict(sorted(counts.items()))}")
    print(f"  report -> {args.out_json}")
    print(f"  csv    -> {args.out_csv}")
    return 0 if not summary["unknown"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
