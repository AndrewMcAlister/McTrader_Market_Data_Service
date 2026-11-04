"""Which market a symbol request means: the one rule behind ``venue``, ``country_code`` and ``.AU``.

The defect these tests exist for: ``CBA`` is an ASX listing **and** a US listing, and only
``venue`` could say which. A caller that sent ``country_code`` -- the vocabulary the
availability endpoints already speak -- had it silently ignored and was answered from the US
tape, which is a wrong number that looks right. The same happened to a suffixed symbol:
``CBA.AU`` was stripped to ``CBA`` and priced at the default venue, so the suffix named one
market and the answer came from another.

The order is the whole contract: a stated ``venue``/``country_code`` wins, then the suffix,
then US. Contradictions are refusals with a reason, never a guess -- because serving either
market silently is exactly how the two venues' prices get mixed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.products import MarketDataError
from data_service.venues import resolve_venue, suffix_venue, venue_for


# ---------------------------------------------------------------------------
# the two vocabularies, and the suffix
# ---------------------------------------------------------------------------

def test_a_country_code_and_a_venue_code_name_the_same_market():
    assert venue_for("au") == "AU"
    assert venue_for(" AU ") == "AU"
    assert venue_for("ASX") == "AU"
    assert venue_for("uk") == "L"
    assert venue_for("NYSE") == "US"


def test_an_unknown_market_is_refused_by_name_not_defaulted_to_us():
    with pytest.raises(MarketDataError) as exc:
        venue_for("NOWHERE")
    assert exc.value.reason == "unknown-venue"
    assert exc.value.status == 400


def test_a_blank_market_is_not_a_market():
    with pytest.raises(MarketDataError) as exc:
        venue_for("   ")
    assert exc.value.reason == "unknown-venue"


def test_a_suffix_names_a_market_and_a_class_share_does_not():
    assert suffix_venue("CBA.AU") == "AU"
    assert suffix_venue("CBA.AX") == "AU"
    assert suffix_venue("VOD.L") == "L"
    # A dotted US ticker is not a listing somewhere else: only the venue table decides.
    assert suffix_venue("BRK.B") is None
    assert suffix_venue("AAPL") is None


# ---------------------------------------------------------------------------
# the order of precedence
# ---------------------------------------------------------------------------

def test_nothing_stated_and_bare_symbols_means_us():
    assert resolve_venue(symbols=["AAPL", "MSFT"]) == "US"
    assert resolve_venue() == "US"


def test_a_suffixed_symbol_states_the_market_it_names():
    """The case that was silently answered from the wrong tape: no venue field, a .AU symbol."""
    assert resolve_venue(symbols=["CBA.AU", "BHP.AU"]) == "AU"


def test_a_country_code_picks_the_market_without_a_venue_field():
    assert resolve_venue(country_code="AU", symbols=["CBA"]) == "AU"
    assert resolve_venue(country_code="us", symbols=["CBA"]) == "US"


def test_a_stated_market_wins_over_a_bare_symbol():
    assert resolve_venue(venue="ASX", symbols=["CBA"]) == "AU"


def test_venue_and_country_code_may_agree():
    assert resolve_venue(venue="AU", country_code="au", symbols=["CBA"]) == "AU"


def test_venue_and_country_code_that_disagree_are_refused():
    with pytest.raises(MarketDataError) as exc:
        resolve_venue(venue="US", country_code="AU", symbols=["CBA"])
    assert exc.value.reason == "venue-conflict"
    assert "AU" in exc.value.detail and "US" in exc.value.detail


def test_a_suffix_that_contradicts_the_stated_market_is_refused():
    """venue=US with CBA.AU is a question with two answers; serving either one is a guess."""
    with pytest.raises(MarketDataError) as exc:
        resolve_venue(venue="US", symbols=["CBA.AU"])
    assert exc.value.reason == "venue-conflict"
    assert "CBA.AU" in exc.value.detail


def test_two_markets_in_one_batch_are_refused_with_the_offending_symbols():
    with pytest.raises(MarketDataError) as exc:
        resolve_venue(symbols=["CBA.AU", "VOD.L"])
    assert exc.value.reason == "mixed-venues"
    assert "CBA.AU" in exc.value.detail and "VOD.L" in exc.value.detail
    assert "split the list" in exc.value.detail


def test_a_bare_symbol_in_a_suffixed_batch_is_priced_at_that_market():
    """One request is one market, so a bare ticker joins the batch's venue rather than
    splitting it -- and if the ticker does not trade there it comes back as `missing`,
    which is visible, instead of being priced from another venue, which is not."""
    assert resolve_venue(symbols=["AAPL", "CBA.AU"]) == "AU"


def test_crypto_has_its_own_market_and_no_venue():
    """BTC-USD is the same instrument wherever it is cached, so a venue is noise, not a key."""
    assert resolve_venue(venue="AU", country_code="AU", symbols=["BTC-USD"],
                         asset_class="crypto") == "CRYPTO"
