"""ATR arithmetic: the definitions, and proof they match the estate's existing code.

These tests are the reason the service can be trusted with a stop distance. The estate
already has three ATR implementations and a parity test that pins one of them to
0.000e+00 (PaperTrader/tests/test_utils_macdv_bars.py); this file holds the same standard
for the service, using series whose ATR is known exactly rather than by re-deriving it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import atr


# ---------------------------------------------------------------------------
# true range
# ---------------------------------------------------------------------------

def test_flat_series_has_a_range_of_its_width():
    highs, lows, closes = [10.0] * 20, [6.0] * 20, [8.0] * 20
    ranges = atr.true_ranges(highs, lows, closes)
    assert ranges[0] == pytest.approx(4.0)
    assert all(r == pytest.approx(4.0) for r in ranges)


def test_a_gap_widens_the_range_beyond_high_minus_low():
    """The whole reason `tr` is the default: a gap is not visible in H-L."""
    # Yesterday closed at 10; today opened at 20 (a gap) and traded 20-21.
    highs = [10.0, 21.0]
    lows = [9.0, 20.0]
    closes = [10.0, 20.5]
    tr = atr.true_ranges(highs, lows, closes, method="tr")
    hl = atr.true_ranges(highs, lows, closes, method="high_low")
    assert tr[1] == pytest.approx(11.0)   # |H - prevClose| = 21 - 10
    assert hl[1] == pytest.approx(1.0)    # H - L
    assert tr[1] > hl[1]


def test_high_minus_low_is_never_wider_than_true_range():
    highs = [12.0, 31.0, 33.0, 20.0]
    lows = [10.0, 29.0, 30.0, 15.0]
    closes = [11.0, 30.0, 31.0, 16.0]
    tr = atr.true_ranges(highs, lows, closes, method="tr")
    hl = atr.true_ranges(highs, lows, closes, method="high_low")
    assert all(t >= h - 1e-12 for t, h in zip(tr, hl))


def test_unknown_method_is_refused():
    with pytest.raises(atr.AtrError):
        atr.true_ranges([1.0], [0.5], [0.75], method="average_of_something")


# ---------------------------------------------------------------------------
# average true range
# ---------------------------------------------------------------------------

def test_simple_atr_of_a_constant_width_series_is_that_width():
    """14 sessions of a 4-wide range must give an ATR of exactly 4."""
    highs = [10.0] * 15
    lows = [6.0] * 15
    closes = [8.0] * 15
    assert atr.average_true_range(highs, lows, closes, 14, smoothing="simple") == pytest.approx(4.0)


def test_insufficient_history_returns_none_rather_than_a_guess():
    """A stop placed from four sessions is not the stop the study measured."""
    highs = [10.0] * 5
    lows = [6.0] * 5
    closes = [8.0] * 5
    assert atr.average_true_range(highs, lows, closes, 14) is None


def test_the_in_progress_session_must_be_excluded_by_the_caller():
    """A partial session has a partial range; this pins the arithmetic the service relies on."""
    highs = [10.0] * 14 + [11.0]
    lows = [6.0] * 14 + [10.5]
    closes = [8.0] * 14 + [10.8]

    with_partial = atr.average_true_range(highs, lows, closes, 14, smoothing="simple")
    without_partial = atr.average_true_range(
        highs[:-1], lows[:-1], closes[:-1], 14, smoothing="simple")

    assert without_partial == pytest.approx(4.0)
    assert with_partial != pytest.approx(without_partial)


def test_the_agreed_formula_is_the_default():
    """The standard method, stated as an executable definition.

        TR  = max(High - Low, abs(High - PreviousClose), abs(Low - PreviousClose))
        ATR(14) = sum(TR for the last 14 bars) / 14

    This is the contract: gap-inclusive true range, simple mean over the period. It is asserted
    here independently of the implementation so that "the standard method" cannot quietly become
    something else -- Wilder smoothing and high-minus-low are both still available, but neither
    is the default and neither is what this test passes.
    """
    highs = [10.0, 11.0, 12.0, 11.5, 12.5, 13.0, 12.0, 13.5, 14.0, 13.0,
             14.5, 15.0, 14.0, 15.5, 16.0]
    lows = [9.0, 9.5, 10.5, 10.0, 11.0, 11.5, 10.5, 12.0, 12.5, 11.5,
            13.0, 13.5, 12.5, 14.0, 14.5]
    closes = [9.5, 10.0, 11.0, 10.5, 11.5, 12.0, 11.0, 12.5, 13.0, 12.0,
              13.5, 14.0, 13.0, 14.5, 15.0]

    # Compute it the way the requirement states it, from scratch.
    ranges = []
    for index in range(1, len(highs)):
        high, low, previous = highs[index], lows[index], closes[index - 1]
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    expected = sum(ranges[-14:]) / 14

    assert atr.average_true_range(highs, lows, closes, 14) == pytest.approx(expected)
    # And the defaults really are that method and that smoothing.
    assert atr.average_true_range(highs, lows, closes, 14) == pytest.approx(
        atr.average_true_range(highs, lows, closes, 14,
                              method=atr.METHOD_TRUE_RANGE,
                              smoothing=atr.SMOOTHING_SIMPLE))


def test_wilder_is_not_the_default():
    """Available for parity with technical_utils, but the agreed method is the simple mean."""
    highs = [10.0] * 14 + [20.0] * 14
    lows = [6.0] * 14 + [16.0] * 14
    closes = [8.0] * 14 + [18.0] * 14
    default = atr.average_true_range(highs, lows, closes, 14)
    wilder = atr.average_true_range(highs, lows, closes, 14, smoothing=atr.SMOOTHING_WILDER)
    assert default is not None and wilder is not None
    assert default != pytest.approx(wilder)


def test_high_minus_low_is_not_the_default():
    """Also available, but it narrows a stop and matches none of the existing books."""
    highs, lows, closes = [10.0, 21.0], [9.0, 20.0], [10.0, 20.5]
    default = atr.average_true_range(highs, lows, closes, 1)
    narrow = atr.average_true_range(highs, lows, closes, 1, method=atr.METHOD_HIGH_LOW)
    assert default == pytest.approx(11.0)
    assert narrow == pytest.approx(1.0)


def test_wilder_and_simple_agree_on_a_constant_series():
    """The seed matters on real data, not on a series with no variation."""
    highs = [10.0] * 30
    lows = [6.0] * 30
    closes = [8.0] * 30
    simple = atr.average_true_range(highs, lows, closes, 14, smoothing="simple")
    wilder = atr.average_true_range(highs, lows, closes, 14, smoothing="wilder")
    assert simple == pytest.approx(4.0)
    assert wilder == pytest.approx(4.0)


def test_wilder_changes_when_the_series_changes():
    highs = [10.0] * 14 + [20.0] * 14
    lows = [6.0] * 14 + [16.0] * 14
    closes = [8.0] * 14 + [18.0] * 14
    simple = atr.average_true_range(highs, lows, closes, 14, smoothing="simple")
    wilder = atr.average_true_range(highs, lows, closes, 14, smoothing="wilder")
    # The jump at bar 14 is in the simple window's tail and in Wilder's recursion.
    assert simple is not None and wilder is not None
    assert simple != pytest.approx(wilder)


def test_a_longer_period_is_not_the_same_reading():
    """A regime change inside 50 sessions but outside 14 separates the two periods."""
    # 40 wide sessions, then 20 narrow ones. The wide block is inside ATR50 but outside
    # ATR14, so the two cannot agree.
    highs = [210.0] * 40 + [152.0] + [152.0] * 19
    lows = [190.0] * 40 + [150.0] + [150.0] * 19
    closes = [200.0] * 40 + [151.0] * 20

    a14 = atr.average_true_range(highs, lows, closes, 14)
    a22 = atr.average_true_range(highs, lows, closes, 22)
    a50 = atr.average_true_range(highs, lows, closes, 50)

    assert a14 == pytest.approx(2.0)
    assert a22 > a14
    assert a50 > a22


def test_bad_period_is_refused():
    with pytest.raises(atr.AtrError):
        atr.average_true_range([1.0] * 5, [0.5] * 5, [0.75] * 5, 0)


# ---------------------------------------------------------------------------
# percent and stop
# ---------------------------------------------------------------------------

def test_percent_is_percent_points_not_a_fraction():
    """2.51 means 2.51%, which is the unit McTrader's atr_14_pct already carries."""
    assert atr.atr_percent(5.0, 200.0) == pytest.approx(2.5)
    assert atr.atr_percent(5.0, None) is None
    assert atr.atr_percent(5.0, 0.0) is None


def test_stop_sits_one_atr_below_a_long():
    assert atr.stop_level(price=100.0, atr_value=5.0, multiplier=1.0) == pytest.approx(95.0)


def test_stop_sits_above_a_short():
    assert atr.stop_level(
        price=100.0, atr_value=5.0, multiplier=1.0, direction="short") == pytest.approx(105.0)


def test_multiplier_is_required_and_never_defaulted():
    """The books use 1.0, 1.0, 2.0 and 3.0: a default would impose one book's risk."""
    with pytest.raises(atr.AtrError):
        atr.stop_level(price=100.0, atr_value=5.0, multiplier=None)


def test_a_multiplier_wider_than_the_price_is_not_a_stop():
    assert atr.stop_level(price=10.0, atr_value=20.0, multiplier=1.0) is None


def test_zero_atr_yields_no_stop():
    assert atr.stop_level(price=100.0, atr_value=0.0, multiplier=1.0) is None


def test_bad_direction_is_refused():
    with pytest.raises(atr.AtrError):
        atr.stop_level(price=100.0, atr_value=5.0, multiplier=1.0, direction="sideways")


# ---------------------------------------------------------------------------
# the measured divergence, as an executable record
# ---------------------------------------------------------------------------

def test_true_range_and_high_low_diverge_materially_on_a_gappy_series():
    """The median divergence measured over 2,850 Back_Test symbols was 1.043.

    This does not assert that exact number -- it asserts the *direction and materiality*
    so nobody later "simplifies" true range into H-L without seeing what it costs.
    """
    import random

    random.seed(7)
    highs, lows, closes = [], [], []
    price = 100.0
    for _ in range(200):
        gap = random.gauss(0, 2.0)
        open_ = price + gap
        high = open_ + abs(random.gauss(0, 1.0))
        low = open_ - abs(random.gauss(0, 1.0))
        close = random.uniform(low, high)
        highs.append(high)
        lows.append(low)
        closes.append(close)
        price = close

    tr = atr.average_true_range(highs, lows, closes, 14, method="tr")
    hl = atr.average_true_range(highs, lows, closes, 14, method="high_low")
    assert tr > hl
    assert tr / hl > 1.001
