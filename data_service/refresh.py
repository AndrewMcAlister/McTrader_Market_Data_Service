"""One bulk refresh of the daily bars, callable from the endpoint and from the daily job.

WHY THIS IS A SEPARATE MODULE

The pull already existed inside ``jobs/daily.py``, reachable only by running that script and having it
decide for itself what to do. An endpoint needs the same work with different decisions around it --
whether it is already fresh, whether another refresh is in flight, and what to report back -- so the
work and the policy are separated here rather than copied.

WHAT A REFRESH COSTS

Measured: 100 vendor units and about 10 seconds for a full-market session (44,026 rows written for
2026-10-28). That is one call to ``/eod-bulk-last-day``, flat-priced, which is why the endpoint checks
whether it has already happened today before spending again.

WHY A LOCK

Two clients asking at once would each spend 100 units and write the same rows. The lock is
non-blocking: the second caller is told the refresh is already running rather than queued behind it,
because a caller waiting 20 seconds for someone else's refresh is worse than being told to ask again.

WHY NOT TRIGGERED BY A DATA REQUEST

Deliberate. A single-symbol ``/bars`` call costs 1 unit through ``/eod/``; if a miss triggered the bulk
pull it would cost 100 -- a hundred times more than the request that caused it. It would also charge
those units to whichever client happened to ask first, and several clients hitting stale data at once
would pile in behind the lock. And it would hide the failure that matters: if the daily refresh did
not run, data requests should visibly degrade to per-symbol fetches, not silently repair themselves.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .sessions import latest_completed_session
from .store import Store

log = logging.getLogger(__name__)

#: One refresh at a time, process-wide.
_LOCK = threading.Lock()

#: The request type whose presence today means the bulk pull has already run.
BULK_REQUEST_TYPE = "/eod-bulk-last-day/US"


def is_fresh(store: Store) -> bool:
    """True when a bulk pull is already recorded for today, from the spend ledger.

    The ledger is the right source and MAX(SnapshotDate) is not: single-symbol rows written by
    ``/bars`` land in DailySnapshot with today's date, so the newest session can be today while the
    market-wide pull has not run for weeks.
    """
    try:
        return store.bulk_pull_count(BULK_REQUEST_TYPE) > 0
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger means "do not assume fresh"
        log.warning("could not read the ledger for freshness (%s); treating as stale", exc)
        return False


def refresh_daily_bars(
    *,
    store: Store,
    client: Any,
    settings: Any,
    market: str = "US",
    session_date: Optional[str] = None,
    force: bool = False,
    include_indicators: bool = True,
) -> Dict[str, Any]:
    """Pull the whole market into DailySnapshot, optionally rebuilding ATR.

    Returns a summary rather than raising for the ordinary outcomes: "already fresh" and "someone else
    is doing it" are answers, not errors.
    """
    from jobs.daily import pull_bulk, rebuild_indicators  # local: the job imports this module's siblings

    if client is None:
        # Every status carries `units`, including the ones that spend nothing: a caller summing the
        # field should not have to handle a response that omits it.
        return {"status": "no-vendor-key", "units": 0,
                "detail": "no EODHD key; store one with POST /secrets/eodhd"}

    # `force` and an explicit session both bypass the freshness check: the first is a person saying
    # "do it anyway", the second is asking for a session the ledger cannot vouch for.
    if not force and session_date is None and is_fresh(store):
        return {"status": "already-fresh",
                "detail": f"a {BULK_REQUEST_TYPE} pull is already recorded today; "
                          f"pass force=true to pull again",
                "units": 0}

    if not _LOCK.acquire(blocking=False):
        return {"status": "busy", "detail": "a refresh is already running", "units": 0}

    started = time.perf_counter()
    units_before = getattr(client, "units", 0)
    try:
        # What the venue's own clock says should be available. Reported next to what the pull
        # actually returned, because "the refresh ran" and "the refresh produced the session it
        # should have" are different claims, and only the second one is useful.
        try:
            expected = latest_completed_session(market).isoformat()
        except Exception:  # noqa: BLE001 - a calendar problem must not stop a pull
            expected = None

        pull = pull_bulk(store, client, market, [session_date] if session_date else None)
        sessions = pull.get("sessions", [])
        newest = sessions[-1].get("session") if sessions else None
        result: Dict[str, Any] = {
            "status": "refreshed",
            "market": market,
            "sessions": sessions,
            "rows_written": pull.get("rows_written", 0),
            "expected_session": expected,
            "newest_session": newest,
            # A vendor that lags, a venue holiday, or a pull that landed on the wrong day. Silent
            # either way, which is why it is reported.
            "behind_expected": bool(expected and newest and newest < expected),
        }
        if include_indicators:
            result["indicators"] = rebuild_indicators(store, settings)
        result["units"] = getattr(client, "units", 0) - units_before
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        log.info("bulk refresh: %s rows over %s session(s) in %sms, %s units",
                 result["rows_written"], len(sessions),
                 result["duration_ms"], result["units"])
        if result["behind_expected"]:
            log.warning("bulk refresh returned session %s but %s was expected -- vendor lag or a "
                        "venue holiday", newest, expected)
        return result
    finally:
        _LOCK.release()
