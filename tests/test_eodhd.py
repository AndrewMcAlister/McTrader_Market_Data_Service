"""Symbol and venue handling: the part every caller gets wrong once.

EODHD identifies an instrument as ``ROOT.VENUE`` (``AAPL.US``), but callers type ``AAPL``,
``aapl``, ``AAPL.US``, ``NASDAQ.AAPL`` and ``ASX:CBA`` and mean the same things. These tests
pin the three behaviours that matter: a bare symbol gets the venue it was asked for, an
already-suffixed symbol is not double-suffixed, and an unknown venue is refused rather than
silently priced off the US feed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.eodhd import (
    MAX_RATE_LIMIT_RETRIES,
    EodhdClient,
    EodhdError,
    bare_symbol,
    normalise_venue,
    render_symbol,
)


# ---------------------------------------------------------------------------
# venue
# ---------------------------------------------------------------------------

def test_venue_aliases_resolve_to_eodhd_codes():
    assert normalise_venue("US") == "US"
    assert normalise_venue("NASDAQ") == "US"
    assert normalise_venue("ASX") == "AU"
    assert normalise_venue("AU") == "AU"
    assert normalise_venue("LSE") == "L"
    assert normalise_venue("UK") == "L"
    assert normalise_venue("TSX") == "TO"
    assert normalise_venue("XETRA") == "F"


def test_a_blank_venue_defaults_to_us():
    assert normalise_venue("") == "US"
    assert normalise_venue(None) == "US"


def test_an_unknown_venue_is_refused_rather_than_defaulted():
    """Pricing an Australian listing from the US feed yields a wrong number that looks right."""
    with pytest.raises(EodhdError) as exc:
        normalise_venue("MARS")
    assert exc.value.reason == "unknown-venue"


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------

def test_a_bare_symbol_gets_the_requested_venue():
    assert render_symbol("AAPL", "US") == "AAPL.US"
    assert render_symbol("cba", "AU") == "CBA.AU"


def test_an_already_suffixed_symbol_is_not_double_suffixed():
    assert render_symbol("AAPL.US", "US") == "AAPL.US"
    assert render_symbol("CBA.AU", "US") == "CBA.AU"


def test_a_dotted_ticker_is_not_mistaken_for_a_venue():
    """BRK.B is a share class, not a B-venue listing."""
    assert render_symbol("BRK.B", "US") == "BRK.B.US"


def test_a_blank_symbol_is_refused():
    with pytest.raises(EodhdError):
        render_symbol("", "US")


def test_bare_symbol_strips_one_known_suffix():
    assert bare_symbol("AAPL.US") == "AAPL"
    assert bare_symbol("brk.b.us") == "BRK"
    assert bare_symbol("AAPL") == "AAPL"
    assert bare_symbol("") == ""


def test_class_shares_keep_their_dot_after_the_venue_is_stripped():
    """BRK.B.US is BRK in the bulk feed's vocabulary; that is the vendor's convention, not ours."""
    assert render_symbol("BRK.B", "US").endswith(".US")


# ---------------------------------------------------------------------------
# intraday
# ---------------------------------------------------------------------------

class _Response:
    status_code = 200

    @staticmethod
    def json():
        return [{"timestamp": 1_758_029_400, "open": 1, "high": 2, "low": 0.5, "close": 1.5}]


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params), timeout))
        return _Response()


def test_intraday_asks_with_epoch_seconds_and_the_venue_suffix():
    """A date-string window is HTTP 422; epoch is the shape the endpoint accepts."""
    session = _Session()
    client = EodhdClient(api_key="k", session=session)
    start = 1_700_000_000
    client.intraday("BHP", "AU", start=start, end=start + 400 * 86400)

    url, params, _ = session.calls[0]
    assert url.endswith("/intraday/BHP.AU")
    assert params["interval"] == "5m"
    assert params["from"] == start
    assert params["to"] == start + 400 * 86400
    assert "api_token" in params
    assert client.units == 5


def test_intraday_refuses_a_window_wider_than_the_vendor_accepts():
    client = EodhdClient(api_key="k", session=_Session())
    with pytest.raises(EodhdError) as exc:
        client.intraday("BHP", "US", start=1, end=1 + 601 * 86400)
    assert exc.value.reason == "window-too-wide"


def test_intraday_refuses_a_window_without_a_start():
    client = EodhdClient(api_key="k", session=_Session())
    with pytest.raises(EodhdError) as exc:
        client.intraday("BHP", "US", start=0, end=1_700_000_000)
    assert exc.value.reason == "empty-window"


# ---------------------------------------------------------------------------
# billing: 429 is a refusal, 404 is a billed answer
# ---------------------------------------------------------------------------

class _NotFoundResponse:
    status_code = 404
    headers = {}
    text = "not found"

    @staticmethod
    def json():
        return {}


class _RateLimitedResponse:
    status_code = 429
    headers = {"Retry-After": "0"}
    text = "429 Too Many Requests"

    @staticmethod
    def json():
        return {}


class _SequenceSession:
    """A session that answers with a fixed sequence, so retries are observable."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params), timeout))
        return self.responses.pop(0)


def test_a_429_is_retried_and_not_billed():
    """The vendor refused before processing, so the retry is free and the call succeeds."""
    session = _SequenceSession([_RateLimitedResponse(), _Response()])
    client = EodhdClient(api_key="k", session=session)
    client.eod("AAPL", "US")

    assert len(session.calls) == 2
    assert client.units == 1


def test_a_persistent_429_stops_at_the_cap_and_is_still_unbilled():
    session = _SequenceSession([_RateLimitedResponse()] * (MAX_RATE_LIMIT_RETRIES + 1))
    client = EodhdClient(api_key="k", session=session)
    with pytest.raises(EodhdError) as exc:
        client.eod("AAPL", "US")

    assert exc.value.reason == "rate-limited"
    assert len(session.calls) == MAX_RATE_LIMIT_RETRIES + 1
    assert client.units == 0


def test_a_billed_404_is_not_retried_and_is_recorded():
    """A malformed request was processed and billed; retrying it pays twice for one mistake."""
    session = _SequenceSession([_NotFoundResponse()])
    client = EodhdClient(api_key="k", session=session)
    with pytest.raises(EodhdError) as exc:
        client.eod("AAPL", "US")

    assert exc.value.reason == "http-error"
    assert len(session.calls) == 1
    assert client.units == 1


# ---------------------------------------------------------------------------
# bulk billing: the ledger row names the market that was asked
# ---------------------------------------------------------------------------

class _BulkResponse:
    status_code = 200

    @staticmethod
    def json():
        return [{"code": "BHP.AU", "date": "2026-10-30", "open": 1, "high": 2,
                 "low": 0.5, "close": 1.5, "adjusted_close": 1.5, "volume": 10}]


def _bulk_client(responses):
    session = _SequenceSession(responses)
    recorded = []

    def recorder(request_type, symbols):
        recorded.append((request_type, symbols))
        return 100

    client = EodhdClient(api_key="k", session=session)
    client.recorder = recorder
    return client, session, recorded


def test_an_au_bulk_pull_is_recorded_under_the_au_market():
    """Filing every pull as /US is what made is_fresh("AU") permanently false, so each
    restart re-spent 100 units on a session the ledger said nothing about."""
    client, session, recorded = _bulk_client([_BulkResponse()])

    client.bulk_last_day("AU")

    assert session.calls[0][0].endswith("/eod-bulk-last-day/AU")
    assert recorded == [("/eod-bulk-last-day/AU", 0)]
    assert client.units == 100


def test_a_us_bulk_pull_keeps_its_own_ledger_row():
    client, session, recorded = _bulk_client([_BulkResponse()])

    client.bulk_last_day("US")

    assert session.calls[0][0].endswith("/eod-bulk-last-day/US")
    assert recorded == [("/eod-bulk-last-day/US", 0)]


def test_a_billed_bulk_failure_is_recorded_under_the_market_asked():
    """A rejected request is still billed, so it must land on the right market's row."""
    client, _, recorded = _bulk_client([_NotFoundResponse()])

    with pytest.raises(EodhdError):
        client.bulk_last_day("AU")

    assert recorded == [("/eod-bulk-last-day/AU", 0)]
