"""Which session has finished, for the venue that was asked about.

Every bar read is filtered so the session still in progress is never served: a
partial range would shrink a stop exactly when the morning is quiet.  The filter
used to compare the session date against the **UTC** date, which is right for the
one venue it was written for and wrong for every venue that closes earlier in UTC
terms.

The ASX is the case that bites.  Its session closes at 16:00 Sydney -- 05:00 or
06:00 UTC -- and the UTC date then stays on that same day for a further 18 hours,
so a session that has been closed all afternoon was refused as "in progress"
until local midnight.  An evening scan in Sydney therefore saw a daily series one
session stale, and the ATR built on it was stale with it.

So the rule is stated once, per venue: a session is complete once the clock at
that venue has passed its close plus a settlement margin.  Venues with no known
close keep the old answer -- the *previous* UTC date -- which can only ever be
conservative.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .eodhd import EodhdError, normalise_venue

log = logging.getLogger(__name__)

#: Venue -> (IANA zone, regular close).  Measured against the venue's own clock,
#: so a DST change moves the session rather than the rule.
VENUE_CLOSE: Dict[str, Tuple[str, time]] = {
    "US": ("America/New_York", time(16, 0)),
    "AU": ("Australia/Sydney", time(16, 0)),
    "L": ("Europe/London", time(16, 30)),
    "TO": ("America/Toronto", time(16, 0)),
    "HK": ("Asia/Hong_Kong", time(16, 0)),
    "F": ("Europe/Berlin", time(17, 30)),
    "PA": ("Europe/Paris", time(17, 30)),
}

#: How long after the close the day's bar is trusted.
#:
#: Three hours is not invented: it is the wait the UTC-date rule already imposed
#: on the US, where midnight UTC falls three hours after the 16:00 ET close in
#: summer and four in winter.  Writing it down keeps the US answer where it was
#: while letting every venue that closes earlier in UTC terms move to its own.
SETTLEMENT_HOURS = 3

#: Assets that have no venue and no close: their bars are UTC days, so the
#: in-progress day is the current UTC date.  The two spellings are the asset
#: class and the market label ``Products.bars`` builds from it.
_CRYPTO_CLASS = "crypto"
_CRYPTO_MARKET = "CRYPTO"


def market_code(venue: Optional[str], asset_class: Optional[object] = None) -> str:
    """The market *venue* names for session purposes, or ``""`` when unknown.

    An unrecognised venue is not an error here: it yields ``""`` and the caller
    falls back to the UTC-date rule.  Refusing the request is the venue
    normaliser's job, on the path that is about to price something.
    """
    kind = str(getattr(asset_class, "value", asset_class) or "").strip().lower()
    if kind == _CRYPTO_CLASS:
        return _CRYPTO_MARKET
    try:
        return normalise_venue(venue)
    except EodhdError:
        return ""


#: Zones this host could not resolve.  Remembered so the complaint is made once
#: rather than on every bar read, and so a host that later gains a database still
#: recovers without a restart.
_unresolvable: Set[str] = set()


def _venue_zone(zone_name: str) -> Optional[ZoneInfo]:
    """The IANA zone named *zone_name*, or ``None`` when this host has no database.

    Windows ships no IANA database; ``zoneinfo`` reads the PyPI ``tzdata`` package
    instead.  Without it the lookup raises, and because this runs inside every bar
    read that one missing package turned the whole ``/bars`` endpoint into a 500 --
    every symbol in the estate scored nothing, for a day, while the data itself was
    fine.  So the answer is "this host knows no venue", which is the situation the
    caller already handles conservatively, rather than a refusal to serve.
    """
    try:
        return ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        if zone_name not in _unresolvable:
            _unresolvable.add(zone_name)
            log.error(
                "No IANA time-zone database for %s -- install the 'tzdata' package. "
                "Falling back to the previous UTC date, so venues that close early in "
                "UTC terms report one session late until it is installed.",
                zone_name,
            )
        return None


def latest_completed_session(market: Optional[str], now: Optional[datetime] = None) -> date:
    """The newest session date that has finished and settled at *market*.

    A session dated *D* counts once the venue's clock has reached close +
    ``SETTLEMENT_HOURS`` on *D*.  Before that, the newest completed session is the
    previous local date -- a calendar day, so a holiday simply has no rows and
    costs nothing.

    A market with no known close answers with the *previous* UTC date, never
    today's: the caller compares ``session <= answer``, and today's UTC date is
    still trading somewhere.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    spec = VENUE_CLOSE.get(str(market or "").strip().upper())
    if spec is None:
        return moment.astimezone(timezone.utc).date() - timedelta(days=1)

    zone_name, close_at = spec
    zone = _venue_zone(zone_name)
    if zone is None:
        return moment.astimezone(timezone.utc).date() - timedelta(days=1)
    local = moment.astimezone(zone)
    settled_at = datetime.combine(local.date(), close_at, tzinfo=local.tzinfo) + timedelta(
        hours=SETTLEMENT_HOURS)
    if local >= settled_at:
        return local.date()
    return local.date() - timedelta(days=1)
