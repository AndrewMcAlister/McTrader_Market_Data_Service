"""The market clock: the dual-clock model, ported from McTrader and checked against it.

The parity harness lives with the port under test (``verify_clock_parity.py`` in the
workspace) and compared 30,240 readings across four daylight-saving transitions; these
are the cases that harness found interesting, kept so a future edit cannot quietly
change one.

    python -m pytest tests/test_market_clock.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.clock import MarketClock, resolve_time_zone  # noqa: E402

UTC = timezone.utc
clock = MarketClock()


def exchange(code: str) -> dict:
    for row in clock.exchanges.values():
        if row["code"] == code:
            return row
    raise AssertionError(f"{code} is not in the reference")


def test_the_us_session_is_open_in_the_middle_of_the_day() -> None:
    session = clock.exchange_session(exchange("NYSE"), datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    assert session.is_open is True
    assert session.open_utc == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert session.close_utc == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    assert session.clock_source == "exchange"


def test_the_us_session_is_closed_before_the_open() -> None:
    session = clock.exchange_session(exchange("NYSE"), datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    assert session.is_open is False
    assert session.next_open_utc == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)


def test_the_session_shifts_an_hour_when_new_york_changes_clock() -> None:
    """30 Oct is EDT (13:30 UTC), 2 Nov is EST (14:30 UTC). The venue does not move."""
    before = clock.exchange_session(exchange("NYSE"), datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    after = clock.exchange_session(exchange("NYSE"), datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
    assert before.open_utc.hour == 13 and after.open_utc.hour == 14
    assert after.is_open is True


def test_a_projection_across_a_clock_change_is_not_an_hour_late() -> None:
    """The bug the port exposed in McTrader: pytz keeps the old offset across a timedelta.

    Asked on Friday 30 October (EDT), the next US open is Monday 2 November, which is EST:
    14:30 UTC, not 13:30.
    """
    session = clock.exchange_session(exchange("NYSE"), datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    assert session.next_open_utc == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def test_the_weekend_is_closed_and_the_next_open_is_monday() -> None:
    session = clock.exchange_session(exchange("NYSE"), datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    assert session.is_open is False
    assert session.next_open_utc == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def test_the_asx_close_rolls_into_the_next_utc_day() -> None:
    """10:00-16:00 Sydney in November (AEDT, +11) is 23:00-05:00 UTC."""
    session = clock.exchange_session(exchange("ASX"), datetime(2026, 11, 4, 1, 0, tzinfo=UTC))
    assert session.is_open is True
    assert session.open_utc == datetime(2026, 11, 3, 23, 0, tzinfo=UTC)
    assert session.close_utc == datetime(2026, 11, 4, 5, 0, tzinfo=UTC)


def test_the_synthetic_clock_skips_the_weekend_only_when_told_to() -> None:
    saturday = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert clock.synthetic_session(saturday, weekend=False).is_open is False
    assert clock.synthetic_session(saturday, weekend=True).is_open is True
    monday = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert clock.synthetic_session(monday, weekend=False).is_open is True


def test_a_broker_asset_class_resolves_through_the_same_rule_mctrader_uses() -> None:
    shares = clock.asset_class_id("shares")
    crypto = clock.asset_class_id("crypto")
    instant = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
    assert clock.resolve(1, shares, instant).clock_source == "exchange"
    # Alpaca's crypto row is ExchangeHours=0, Synthetic24=0 -> always open, as in McTrader.
    assert clock.resolve(1, crypto, instant).clock_source == "always_open"


def test_the_broker_payload_lists_the_exchanges_that_broker_answers_for() -> None:
    payload = clock.broker_clock(1, datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    assert payload["broker"]["name"] == "Alpaca"
    assert {row["code"] for row in payload["exchanges"]} == {
        "NYSE", "NASDAQ", "NYSEARCA", "AMEX", "IEX"}
    assert all(row["is_open"] for row in payload["exchanges"])
    assert payload["reference_source"].startswith("McTrader_Live")


def test_abbreviations_resolve_to_a_zone_that_knows_about_daylight_saving() -> None:
    """ZoneInfo("EST") would pin -05:00 all year, an hour wrong for eight months."""
    new_york = resolve_time_zone("EST")
    july = datetime(2026, 7, 15, 12, 0, tzinfo=UTC).astimezone(new_york)
    january = datetime(2026, 1, 15, 12, 0, tzinfo=UTC).astimezone(new_york)
    assert july.utcoffset().total_seconds() == -4 * 3600
    assert january.utcoffset().total_seconds() == -5 * 3600
