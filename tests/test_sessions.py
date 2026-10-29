"""Which session has finished: the guard that keeps a partial bar out of a stop.

The defect these tests exist for: the filter compared the session date against the
**UTC** date. That is right for the US, whose close at 16:00 ET is 21:00 UTC, and
wrong for the ASX, whose close at 16:00 Sydney is 05:00-06:00 UTC -- the UTC date
stays on the same day for another 18 hours, so a session that closed in the
afternoon was refused as "in progress" until local midnight. Every evening scan in
Sydney therefore read a daily series one session stale.

Each case states the wall-clock time at the venue rather than a UTC hour, because
the rule is about the venue's clock and a DST change must move the session rather
than the rule.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import sessions
from data_service.sessions import (
    SETTLEMENT_HOURS,
    latest_completed_session,
    market_code,
)


def _utc(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# the ASX case: a closed session must not be refused as in progress
# ---------------------------------------------------------------------------

def test_an_asx_session_closed_in_the_afternoon_is_complete_that_evening():
    """16:00 Sydney on the 29th; asked at 20:00 Sydney, still the 29th in UTC."""
    answered = latest_completed_session("AU", _utc(2026, 10, 29, 9))

    assert answered == date(2026, 10, 29)


def test_an_asx_session_is_not_complete_the_moment_it_closes():
    """The settlement margin is the point: a just-closed bar is not yet a bar."""
    just_after_close = _utc(2026, 10, 29, 6, 30)  # 17:30 Sydney

    assert latest_completed_session("AU", just_after_close) == date(2026, 10, 28)


def test_the_boundary_follows_the_settlement_constant():
    """One constant, one boundary: a minute before it, the session is not a session."""
    close = datetime(2026, 10, 29, 16, 0, tzinfo=ZoneInfo("Australia/Sydney"))
    margin = timedelta(hours=SETTLEMENT_HOURS)
    just_before = (close + margin - timedelta(minutes=1)).astimezone(timezone.utc)
    settled = (close + margin).astimezone(timezone.utc)

    assert latest_completed_session("AU", just_before) == date(2026, 10, 28)
    assert latest_completed_session("AU", settled) == date(2026, 10, 29)


# ---------------------------------------------------------------------------
# the US case: the behaviour that already worked must not change
# ---------------------------------------------------------------------------

def test_the_us_session_is_not_complete_at_the_open():
    """09:00 UTC is 05:00 in New York: nothing has even opened yet."""
    assert latest_completed_session("US", _utc(2026, 10, 29, 9)) == date(2026, 10, 28)


def test_the_us_session_is_complete_by_the_old_utc_midnight_boundary():
    """20:00 ET is where the UTC-date rule flipped; the new rule must not be later."""
    assert latest_completed_session("US", _utc(2026, 10, 30, 0)) == date(2026, 10, 29)


def test_a_dst_shift_moves_the_session_not_the_rule():
    """Winter close is still 16:00 local; only the UTC hour changes.

    2026-01-15, New York on EST: 22:00 UTC is 17:00 local, one hour past the
    close and still settling, so the 15th is not yet a completed session.  00:00
    UTC on the 16th is 19:00 local -- close + 3h -- so it is.
    """
    assert latest_completed_session("US", _utc(2026, 1, 15, 22)) == date(2026, 1, 14)
    assert latest_completed_session("US", _utc(2026, 1, 16, 0)) == date(2026, 1, 15)


# ---------------------------------------------------------------------------
# everything else stays conservative
# ---------------------------------------------------------------------------

def test_a_venue_with_no_known_close_answers_with_the_previous_utc_date():
    """An unknown venue must never be served a bar it might still be trading in.

    The caller compares ``session <= answer``, so answering with today's UTC date
    would admit the session still running; the old code compared ``<`` against
    today and this must reproduce it exactly.
    """
    assert latest_completed_session("ZZ", _utc(2026, 10, 29, 9)) == date(2026, 10, 28)


def test_crypto_never_returns_the_day_in_progress():
    """Crypto has no close, so the newest completed day is yesterday in UTC."""
    assert latest_completed_session("CRYPTO", _utc(2026, 10, 29, 23, 59)) == date(2026, 10, 28)


# ---------------------------------------------------------------------------
# a host with no time-zone database must degrade, not refuse every bar read
# ---------------------------------------------------------------------------

def test_a_host_without_a_zone_database_answers_conservatively(monkeypatch):
    """The defect: ZoneInfo raised inside every /bars call and the endpoint 500ed.

    Windows ships no IANA database and reads the PyPI ``tzdata`` package instead.
    One missing package then made every bar read in the estate fail -- worse than
    a stale answer, because the caller cannot tell it from "no data".
    """
    def _no_database(key):
        raise ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(sessions, "ZoneInfo", _no_database)

    assert latest_completed_session("AU", _utc(2026, 10, 29, 9)) == date(2026, 10, 28)


def test_the_zone_complaint_is_made_once(monkeypatch, caplog):
    """A per-call error log would bury the real one under a cycle's bar reads."""
    times = []

    def _no_database(key):
        times.append(key)
        raise ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(sessions, "ZoneInfo", _no_database)
    monkeypatch.setattr(sessions, "_unresolvable", set())

    with caplog.at_level("ERROR"):
        for _ in range(3):
            latest_completed_session("AU", _utc(2026, 10, 29, 9))

    assert len([r for r in caplog.records if "tzdata" in r.getMessage()]) == 1


def test_a_naive_clock_is_read_as_utc_rather_than_raising():
    answered = latest_completed_session("AU", datetime(2026, 10, 29, 9))

    assert answered == date(2026, 10, 29)


# ---------------------------------------------------------------------------
# the market code the rule is asked about
# ---------------------------------------------------------------------------

def test_the_market_code_resolves_a_venue_name():
    assert market_code("ASX") == "AU"
    assert market_code("NASDAQ") == "US"


def test_crypto_is_its_own_market_whatever_venue_was_passed():
    assert market_code("US", "crypto") == "CRYPTO"


def test_an_unknown_venue_yields_no_market_rather_than_raising():
    """Refusing an unknown venue is the price path's job, not this guard's."""
    assert market_code("NOWHERE") == ""
