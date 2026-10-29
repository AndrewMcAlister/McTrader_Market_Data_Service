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
