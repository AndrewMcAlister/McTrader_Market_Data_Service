"""The bulk refresh: freshness, single-flight, and the outcomes that are answers rather than errors.

Database-free. The pull itself is faked; what is being pinned is the policy around it -- when it
spends 100 units and when it refuses to.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import refresh


class FakeStore:
    def __init__(self, pulls: int = 0, fail: bool = False) -> None:
        self.pulls = pulls
        self.fail = fail

    def bulk_pull_count(self, request_type, usage_date=None) -> int:
        if self.fail:
            raise RuntimeError("ledger unavailable")
        return self.pulls


class FakeClient:
    def __init__(self) -> None:
        self.units = 0


@pytest.fixture(autouse=True)
def _release_lock():
    """A leaked lock would make every later test report 'busy'."""
    yield
    if refresh._LOCK.locked():
        refresh._LOCK.release()


# ---------------------------------------------------------------------------
# freshness: the ledger is the signal, not MAX(SnapshotDate)
# ---------------------------------------------------------------------------

def test_fresh_when_the_ledger_records_a_pull_today():
    assert refresh.is_fresh(FakeStore(pulls=1)) is True


def test_stale_when_the_ledger_records_none():
    assert refresh.is_fresh(FakeStore(pulls=0)) is False


def test_an_unreadable_ledger_is_treated_as_stale():
    """Guessing 'fresh' would silently stop refreshing; guessing 'stale' costs one pull."""
    assert refresh.is_fresh(FakeStore(fail=True)) is False


def test_the_request_type_is_the_flat_priced_bulk_call():
    assert refresh.BULK_REQUEST_TYPE == "/eod-bulk-last-day/US"


# ---------------------------------------------------------------------------
# the outcomes
# ---------------------------------------------------------------------------

def test_no_vendor_key_is_reported_not_raised():
    result = refresh.refresh_daily_bars(
        store=FakeStore(), client=None, settings=object())
    assert result["status"] == "no-vendor-key"
    assert result["units"] == 0


def test_already_fresh_spends_nothing(monkeypatch):
    called = []
    monkeypatch.setattr("jobs.daily.pull_bulk", lambda *a, **k: called.append("pull") or {})

    result = refresh.refresh_daily_bars(
        store=FakeStore(pulls=1), client=FakeClient(), settings=object())

    assert result["status"] == "already-fresh"
    assert result["units"] == 0
    assert called == [], "the vendor was called despite being fresh"


def test_force_pulls_anyway(monkeypatch):
    monkeypatch.setattr("jobs.daily.pull_bulk",
                        lambda *a, **k: {"sessions": [{"session": "2026-10-28"}],
                                         "rows_written": 10})
    monkeypatch.setattr("jobs.daily.rebuild_indicators",
                        lambda *a, **k: {"symbols_considered": 1})

    result = refresh.refresh_daily_bars(
        store=FakeStore(pulls=1), client=FakeClient(), settings=object(), force=True)

    assert result["status"] == "refreshed"
    assert result["rows_written"] == 10


def test_an_explicit_session_bypasses_the_freshness_check(monkeypatch):
    """A dated backfill cannot be vouched for by today's ledger entry."""
    monkeypatch.setattr("jobs.daily.pull_bulk",
                        lambda *a, **k: {"sessions": [{"session": "2026-10-01"}],
                                         "rows_written": 5})
    monkeypatch.setattr("jobs.daily.rebuild_indicators", lambda *a, **k: {})

    result = refresh.refresh_daily_bars(
        store=FakeStore(pulls=1), client=FakeClient(), settings=object(),
        session_date="2026-10-01")

    assert result["status"] == "refreshed"


def test_busy_when_a_refresh_is_already_running(monkeypatch):
    """The second caller is told, not queued: waiting 30s for someone else's pull is worse."""
    refresh._LOCK.acquire()
    monkeypatch.setattr("jobs.daily.pull_bulk", lambda *a, **k: {})

    result = refresh.refresh_daily_bars(
        store=FakeStore(), client=FakeClient(), settings=object(), force=True)

    assert result["status"] == "busy"
    assert result["units"] == 0


def test_the_lock_is_released_after_a_successful_refresh(monkeypatch):
    monkeypatch.setattr("jobs.daily.pull_bulk",
                        lambda *a, **k: {"sessions": [], "rows_written": 0})
    monkeypatch.setattr("jobs.daily.rebuild_indicators", lambda *a, **k: {})

    refresh.refresh_daily_bars(store=FakeStore(), client=FakeClient(), settings=object(),
                               force=True)

    assert not refresh._LOCK.locked()


def test_units_are_measured_from_the_client(monkeypatch):
    """Reported cost must come from the client's own counter, not from an assumption."""
    client = FakeClient()

    def pull(store, client_, market, dates):
        client_.units += 100
        return {"sessions": [{"session": "2026-10-28"}], "rows_written": 3}

    monkeypatch.setattr("jobs.daily.pull_bulk", pull)
    monkeypatch.setattr("jobs.daily.rebuild_indicators", lambda *a, **k: {})

    result = refresh.refresh_daily_bars(store=FakeStore(), client=client, settings=object(),
                                        force=True)

    assert result["units"] == 100


# ---------------------------------------------------------------------------
# did it produce the session it should have?
# ---------------------------------------------------------------------------

def test_a_lagging_pull_is_flagged(monkeypatch):
    """"The refresh ran" and "the refresh produced the right session" are different claims."""
    monkeypatch.setattr("jobs.daily.pull_bulk",
                        lambda *a, **k: {"sessions": [{"session": "2020-01-01"}],
                                         "rows_written": 1})
    monkeypatch.setattr("jobs.daily.rebuild_indicators", lambda *a, **k: {})
    monkeypatch.setattr(refresh, "latest_completed_session",
                        lambda market: __import__("datetime").date(2026, 10, 28))

    result = refresh.refresh_daily_bars(store=FakeStore(), client=FakeClient(),
                                        settings=object(), force=True)

    assert result["behind_expected"] is True
    assert result["expected_session"] == "2026-10-28"
    assert result["newest_session"] == "2020-01-01"


def test_a_matching_pull_is_not_flagged(monkeypatch):
    monkeypatch.setattr("jobs.daily.pull_bulk",
                        lambda *a, **k: {"sessions": [{"session": "2026-10-28"}],
                                         "rows_written": 1})
    monkeypatch.setattr("jobs.daily.rebuild_indicators", lambda *a, **k: {})
    monkeypatch.setattr(refresh, "latest_completed_session",
                        lambda market: __import__("datetime").date(2026, 10, 28))

    result = refresh.refresh_daily_bars(store=FakeStore(), client=FakeClient(),
                                        settings=object(), force=True)

    assert result["behind_expected"] is False


def test_a_broken_calendar_does_not_stop_the_pull(monkeypatch):
    monkeypatch.setattr("jobs.daily.pull_bulk",
                        lambda *a, **k: {"sessions": [{"session": "2026-10-28"}],
                                         "rows_written": 1})
    monkeypatch.setattr("jobs.daily.rebuild_indicators", lambda *a, **k: {})

    def explode(market):
        raise RuntimeError("no tz database")

    monkeypatch.setattr(refresh, "latest_completed_session", explode)

    result = refresh.refresh_daily_bars(store=FakeStore(), client=FakeClient(),
                                        settings=object(), force=True)

    assert result["status"] == "refreshed"
    assert result["expected_session"] is None
    assert result["behind_expected"] is False
