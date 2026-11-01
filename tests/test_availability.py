"""todays-data-is-available: the rule behind the boolean.

    available  =  newest observed price for the venue  >=  that venue's most recent open

Every case below is one that would otherwise be mistaken for a bug: the open in the
future, the weekend, an out-of-hours price, the AU session spanning UTC midnight.

    python -m pytest tests/test_availability.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.availability import check, country_key  # noqa: E402
from data_service.clock import MarketClock  # noqa: E402

UTC = timezone.utc
clock = MarketClock()


def iso(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_the_country_code_is_case_insensitive_and_trimmed() -> None:
    assert country_key("us") == "US"
    assert country_key(" AU ") == "AU"
    assert country_key("uS") == "US"


def test_an_unknown_country_is_a_caller_error() -> None:
    try:
        country_key("NZ")
    except KeyError as exc:
        assert "US" in str(exc)
    else:  # pragma: no cover - the assertion above is the test
        raise AssertionError("NZ should not be a supported country code")


def test_us_true_when_a_price_arrived_after_the_open() -> None:
    """Monday 2 November: open 14:30Z, a price observed at 15:10Z is today's."""
    result = check(clock, iso(2026, 11, 2, 15, 10), "US", iso(2026, 11, 2, 15, 20))
    assert result.available is True
    assert result.session_open_utc == iso(2026, 11, 2, 14, 30)
    assert result.session_is_open is True


def test_us_false_before_the_open_even_with_fridays_prices() -> None:
    """Monday 12:00Z is before the 14:30Z open: Friday's data is not today's."""
    result = check(clock, iso(2026, 10, 30, 19, 31), "US", iso(2026, 11, 2, 12, 0))
    assert result.available is False


def test_us_false_on_a_weekend_because_there_is_no_session_yet() -> None:
    result = check(clock, iso(2026, 10, 30, 19, 31), "US", iso(2026, 10, 31, 12, 0))
    assert result.available is False
    assert result.session_is_open is False


def test_an_out_of_hours_price_cannot_make_it_true() -> None:
    """A quote taken at 02:00 on Monday is stamped BEFORE the 14:30Z open."""
    result = check(clock, iso(2026, 11, 2, 2, 0), "US", iso(2026, 11, 2, 12, 0))
    assert result.available is False


def test_au_true_inside_the_sydney_session() -> None:
    """ASX 10:00-16:00 Sydney in November is 23:00-05:00Z; 00:30Z is inside it."""
    result = check(clock, iso(2026, 11, 2, 0, 30), "AU", iso(2026, 11, 2, 1, 0))
    assert result.available is True
    assert result.session_open_utc == iso(2026, 11, 1, 23, 0)


def test_au_false_when_the_newest_price_predates_the_asx_open() -> None:
    """The AU close rolls past UTC midnight, so the comparison must be on instants."""
    result = check(clock, iso(2026, 11, 1, 22, 50), "AU", iso(2026, 11, 2, 1, 0))
    assert result.available is False


def test_no_stored_price_at_all_is_false_not_an_error() -> None:
    result = check(clock, None, "US", iso(2026, 11, 2, 15, 20))
    assert result.available is False
    assert result.latest_utc is None
