"""The collection schedule: open + 45, one retry, then a person.

The times are computed from the market clock, so these tests double as a check that a
daylight-saving change moves the fire time with the session rather than away from it.

    python -m pytest tests/test_schedule.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.clock import MarketClock  # noqa: E402
from data_service.schedule import MARKETS, RefreshScheduler, SessionSchedule  # noqa: E402

UTC = timezone.utc
US, AU = MARKETS
clock = MarketClock()
schedule = SessionSchedule(clock, offset_minutes=45, retry_minutes=15)


def test_the_us_fire_time_is_the_open_plus_forty_five_minutes() -> None:
    """NYSE 09:30 New York, in daylight time, is 13:30Z -> fire 14:15Z."""
    fire = schedule.fire_time(US, datetime(2026, 10, 30, 15, 0, tzinfo=UTC))
    assert fire == datetime(2026, 10, 30, 14, 15, tzinfo=UTC)


def test_the_fire_time_moves_with_the_clock_change() -> None:
    """From 1 November New York is EST: 09:30 -> 14:30Z -> fire 15:15Z."""
    fire = schedule.fire_time(US, datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
    assert fire == datetime(2026, 11, 2, 15, 15, tzinfo=UTC)


def test_the_asx_fire_time_is_its_own_open_not_the_us_one() -> None:
    """ASX 10:00 Sydney in November (AEDT) is 23:00Z the previous day -> fire 23:45Z."""
    fire = schedule.fire_time(AU, datetime(2026, 11, 4, 1, 0, tzinfo=UTC))
    assert fire == datetime(2026, 11, 3, 23, 45, tzinfo=UTC)


def test_a_closed_venue_is_not_in_session_and_nothing_is_collected() -> None:
    saturday = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert schedule.in_session(US, saturday) is False

    calls = []
    scheduler = RefreshScheduler(schedule=schedule, run_market=lambda market: calls.append(market) or {"ok": True})
    assert scheduler.tick(saturday) == []
    assert calls == []


def test_the_first_attempt_lands_on_the_fire_time() -> None:
    calls = []
    scheduler = RefreshScheduler(schedule=schedule,
                                  run_market=lambda market: calls.append(market) or {"ok": True})
    # 14:10Z is before 14:15Z: nothing yet.
    assert scheduler.tick(datetime(2026, 10, 30, 14, 10, tzinfo=UTC)) == []
    assert calls == []
    actions = scheduler.tick(datetime(2026, 10, 30, 14, 15, tzinfo=UTC))
    assert [a["market"] for a in actions] == ["US"]
    assert calls == ["US"]


def test_a_failure_is_retried_once_fifteen_minutes_later_and_then_left_alone() -> None:
    calls = []

    def failing(market: str):
        calls.append(market)
        return {"ok": False, "status": "error", "detail": "vendor 500"}

    scheduler = RefreshScheduler(schedule=schedule, run_market=failing)
    scheduler.tick(datetime(2026, 10, 30, 14, 15, tzinfo=UTC))   # attempt 1
    assert scheduler.tick(datetime(2026, 10, 30, 14, 20, tzinfo=UTC)) == []   # too early
    actions = scheduler.tick(datetime(2026, 10, 30, 14, 30, tzinfo=UTC))      # attempt 2
    assert [a["attempt"] for a in actions] == [2]
    assert scheduler.tick(datetime(2026, 10, 30, 14, 45, tzinfo=UTC)) == []   # manual from here
    assert scheduler.tick(datetime(2026, 10, 30, 15, 0, tzinfo=UTC)) == []
    assert calls == ["US", "US"]
    status = scheduler.status()["markets"][0]
    assert status["state"] == "manual-intervention"
    assert len(status["attempts"]) == 2


def test_a_success_stops_the_retry() -> None:
    calls = []
    scheduler = RefreshScheduler(schedule=schedule,
                                  run_market=lambda market: calls.append(market) or {"ok": True})
    scheduler.tick(datetime(2026, 10, 30, 14, 15, tzinfo=UTC))
    scheduler.tick(datetime(2026, 10, 30, 14, 30, tzinfo=UTC))
    assert calls == ["US"]
    assert scheduler.status()["markets"][0]["state"] == "done"


def test_both_markets_are_scheduled_even_though_only_one_is_open() -> None:
    """At 23:45Z the ASX is opening and New York is closed -- one fires, one does not."""
    calls = []
    scheduler = RefreshScheduler(schedule=schedule,
                                  run_market=lambda market: calls.append(market) or {"ok": True})
    actions = scheduler.tick(datetime(2026, 11, 3, 23, 45, tzinfo=UTC))
    assert [a["market"] for a in actions] == ["AU"]
    assert calls == ["AU"]


def test_the_next_fire_rolls_to_monday_over_a_weekend() -> None:
    saturday = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert schedule.next_fire(US, saturday) == datetime(2026, 11, 2, 15, 15, tzinfo=UTC)


def test_a_new_session_starts_with_clean_state() -> None:
    scheduler = RefreshScheduler(schedule=schedule, run_market=lambda market: {"ok": True})
    scheduler.tick(datetime(2026, 10, 30, 14, 15, tzinfo=UTC))
    assert scheduler.status()["markets"][0]["state"] == "done"
    # Monday is a different session, so it waits again rather than inheriting "done".
    scheduler.tick(datetime(2026, 11, 2, 15, 15, tzinfo=UTC))
    assert len(scheduler.status()["markets"][0]["attempts"]) == 1
