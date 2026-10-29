"""Average True Range — the one place in this service that defines it.

WHY TRUE RANGE AND NOT HIGH MINUS LOW

The obvious reading of "ATR" is the average of each session's high minus its low. That is
not what any implementation in the estate does, and it is not what the backtests were
measured with:

  * ``McTrader/Utils/technical_utils.py`` — ``wilder_atr_series``, gap-inclusive
  * ``PaperTrader/strategies.py`` — ``atr_from_bars``, gap-inclusive
  * ``Mini-McTrader/slot1_trader.py`` — ``atr_from_daily``, gap-inclusive

All three use ``max(H-L, |H-prevClose|, |L-prevClose|)``. Measured over 2,850 symbols in
Back_Test.BarHourly, the gap term widens a 14-session ATR by a median 4.3% and by up to
25.8% at p95 — and a movers screen selects precisely the gappy names in that tail. So
"average high to low" is supported here as ``method="high_low"`` and is not the default:
switching to it silently would narrow every stop relative to everything already backtested.

SMOOTHING

Two conventions exist in the estate and they differ by a median 3.2%:

  * ``simple`` — the mean of the last ``period`` true ranges. What both stop books use, and
    therefore the default here, because their ``atr_stop_multiple`` values were calibrated
    against it.
  * ``wilder`` — Wilder's recursion, seeded with the mean of the first ``period`` true
    ranges. What ``technical_utils.wilder_atr_series`` and RegimeService use.

Everything is pure: no network, no database, no clock unless one is passed in. That is what
makes the arithmetic testable against known-exact series.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

#: The two accepted definitions of "range".
METHOD_TRUE_RANGE = "tr"
METHOD_HIGH_LOW = "high_low"
METHODS = (METHOD_TRUE_RANGE, METHOD_HIGH_LOW)

#: The two accepted smoothings.
SMOOTHING_SIMPLE = "simple"
SMOOTHING_WILDER = "wilder"
SMOOTHINGS = (SMOOTHING_SIMPLE, SMOOTHING_WILDER)


class AtrError(ValueError):
    """An ATR request the service refuses, naming what was wrong."""


def true_ranges(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    method: str = METHOD_TRUE_RANGE,
) -> List[float]:
    """One range value per session.

    The first session has no previous close, so its range is ``H-L`` under both methods --
    there is nothing to gap against. Callers must not read that as a defect: it is the same
    choice ``wilder_atr_series`` makes, and it is why ``period`` true ranges need
    ``period + 1`` sessions.

    Under ``high_low`` every session is simply ``H-L``; the parameter exists so the two
    definitions can be compared on real data rather than argued about.
    """
    if method not in METHODS:
        raise AtrError(f"{method!r} is not an ATR method; use one of {', '.join(METHODS)}")

    n = min(len(highs), len(lows), len(closes))
    if n == 0:
        return []

    out: List[float] = [float(highs[0]) - float(lows[0])]
    for i in range(1, n):
        high = float(highs[i])
        low = float(lows[i])
        if method == METHOD_HIGH_LOW:
            out.append(high - low)
            continue
        previous = float(closes[i - 1])
        out.append(max(high - low, abs(high - previous), abs(low - previous)))
    return out


def average_true_range(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
    method: str = METHOD_TRUE_RANGE,
    smoothing: str = SMOOTHING_SIMPLE,
) -> Optional[float]:
    """The ATR over the last *period* ranges, or ``None`` when there is too little history.

    ``None`` rather than a guess: a stop placed from four sessions is not the stop the
    study measured, and both stop books already refuse that case rather than approximating
    it.

    Under ``simple`` the answer is the mean of the last ``period`` ranges, so extra history
    is harmless and only the tail is read. Under ``wilder`` every earlier range feeds the
    recursion, so the whole series is used and the seed is the mean of the first
    ``period`` ranges -- the same seed ``technical_utils.wilder_atr_series`` uses, which is
    what makes the two agree exactly on identical input.
    """
    if period < 1:
        raise AtrError(f"period must be positive, got {period!r}")
    if smoothing not in SMOOTHINGS:
        raise AtrError(
            f"{smoothing!r} is not a smoothing; use one of {', '.join(SMOOTHINGS)}")

    ranges = true_ranges(highs, lows, closes, method)
    if len(ranges) < period:
        return None

    if smoothing == SMOOTHING_SIMPLE:
        window = ranges[-period:]
        return sum(window) / float(len(window))

    running = sum(ranges[:period]) / float(period)
    for value in ranges[period:]:
        running = (running * (period - 1) + value) / float(period)
    return running


def atr_percent(value: Optional[float], reference_price: Optional[float]) -> Optional[float]:
    """ATR as percent points of price (2.51 means 2.51%), or ``None``.

    Percent points and not a fraction, because that is the unit ``McTrader``'s
    ``atr_14_pct`` / ``atr_22_pct`` already carry and the unit every caller divides by 100.
    A zero or missing price yields ``None`` rather than an infinity.
    """
    if not value or not reference_price or reference_price <= 0:
        return None
    return float(value) / float(reference_price) * 100.0


def stop_level(
    price: float,
    atr_value: Optional[float],
    multiplier: Optional[float] = None,
    direction: str = "long",
) -> Optional[float]:
    """Where a resting stop sits, or ``None`` when it cannot be computed.

    The multiplier is **required**. The four books in the estate use 1.0, 1.0, 2.0 and 3.0,
    so a default here would silently impose one book's risk on another's. ``None`` is
    returned rather than a defaulted width.

    A long's stop sits below the price, a short's above. A level that would be zero or
    negative is refused: it is not a stop, it is a sign that the ATR is larger than the
    instrument's price.
    """
    if atr_value is None or atr_value <= 0:
        return None
    if multiplier is None or multiplier <= 0:
        raise AtrError("multiplier is required and must be positive")
    if price is None or price <= 0:
        return None

    side = str(direction or "long").strip().lower()
    if side not in ("long", "short"):
        raise AtrError(f"{direction!r} is not a direction; use 'long' or 'short'")

    offset = float(multiplier) * float(atr_value)
    level = float(price) - offset if side == "long" else float(price) + offset
    return round(level, 6) if level > 0 else None
