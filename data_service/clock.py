"""The market clock: which session is open, for which venue, right now.

PORTED FROM McTrader (``Utils/trading_session.py``), independently. The algorithm is
the dual-clock model and is reproduced exactly, because the whole point is that the two
books cannot disagree about whether a market is open:

    Clock A -- the exchange clock
        Exchanges.Timezone / Opens / Closes, in the exchange's own local time, DST
        resolved there. A session is open when
        ``open_utc <= now < close_utc``.

    Clock B -- the broker's synthetic 24h clock
        00:00 to 23:59 UTC, for instruments a broker quotes around the clock. Weekend=0
        skips Saturday and Sunday; the next open is then the following Monday 00:00 UTC.

    Which clock -- the broker-asset-class rule
        ExchangeHours=1 -> the exchange clock
        else Synthetic24=1 -> the synthetic clock
        else -> always open (nothing to state, so it does not pretend to a session)

WHAT IS INDEPENDENT, AND WHAT IS NOT

The **exchange** half of the reference is read from ``Market_Data.dbo.Exchanges``, a
copy of ``McTrader_Live.dbo.Exchanges`` made on 2026-11-04. That is the table the clock
answers from: ``Timezone``, ``Opens`` and ``Closes`` per exchange, in the exchange's own
local time. The broker table, the asset-class rules and the broker->exchange mapping
remain in ``data/clock.json``, which is also the **fallback** when Market_Data is
unavailable, so the service still starts and answers if the database is down.

Refreshing the exchange copy is a deliberate act:

    python tools\\copy_exchanges.py          # McTrader_Live -> Market_Data
    python -m data_service.clock --show      # what the clock holds now

The clock loads the table once at startup. Reload after a refresh with
``POST /admin/shutdown`` (the supervisor restarts the process) or restart the
service.

TIME ZONES

Windows-style abbreviations ("EST", "AEST") and country codes ("US", "AU") are mapped
to IANA names *before* lookup, exactly as McTrader does: ``zoneinfo`` happily accepts
"EST" and would then pin the venue to a fixed -05:00 for the whole year, which is wrong
by an hour for the eight months New York is on daylight time. The tables below are the
same tables McTrader carries, and the test suite compares this module against it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

UTC = timezone.utc
log = logging.getLogger(__name__)

#: Abbreviations that appear in the reference data -> the IANA zone that has DST.
TZ_ABBREV_TO_IANA: Dict[str, str] = {
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
    "ACST": "Australia/Adelaide",
    "ACDT": "Australia/Adelaide",
    "AWST": "Australia/Perth",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "GMT": "UTC",
    "BST": "Europe/London",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "EET": "Europe/Helsinki",
    "EEST": "Europe/Helsinki",
    "JST": "Asia/Tokyo",
    "HKT": "Asia/Hong_Kong",
    "SGT": "Asia/Singapore",
    "NZST": "Pacific/Auckland",
    "NZDT": "Pacific/Auckland",
}

#: Country codes and aliases that are not IANA names either.
COUNTRY_ALIAS_TO_IANA: Dict[str, str] = {
    "AU": "Australia/Sydney",
    "US": "America/New_York",
    "UK": "Europe/London",
    "GB": "Europe/London",
    "NZ": "Pacific/Auckland",
    "JP": "Asia/Tokyo",
    "HK": "Asia/Hong_Kong",
    "SG": "Asia/Singapore",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
}

#: The frozen copy. Overridable for tests and for a deployment that keeps it elsewhere.
REFERENCE_PATH = Path(__file__).resolve().parent.parent / "data" / "clock.json"


def resolve_time_zone(label: Any) -> tzinfo:
    """A timezone label -> a tzinfo that knows about daylight saving.

    Abbreviations and country codes are mapped FIRST. ``ZoneInfo("EST")`` succeeds and
    pins -05:00 all year, which is an hour wrong for New York from March to November --
    the reason McTrader does the same lookup in the same order.
    """
    text = str(label or "").strip()
    if not text:
        return UTC
    for table in (TZ_ABBREV_TO_IANA, COUNTRY_ALIAS_TO_IANA):
        iana = table.get(text.upper())
        if iana:
            try:
                return ZoneInfo(iana)
            except Exception:  # noqa: BLE001 - a broken tzdata is a UTC fallback, not a crash
                break
    try:
        return ZoneInfo(text)
    except Exception:  # noqa: BLE001
        return UTC


@dataclass
class ResolvedSession:
    """The answer for one clock, at one instant. Mirrors McTrader's dataclass."""

    is_open: bool
    open_utc: Optional[datetime] = None
    close_utc: Optional[datetime] = None
    next_open_utc: Optional[datetime] = None
    next_close_utc: Optional[datetime] = None
    current_time_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    clock_source: str = "unknown"          # exchange | synthetic24 | always_open
    exchange_code: Optional[str] = None
    exchange_timezone: Optional[str] = None
    broker_id: Optional[int] = None
    asset_class_id: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        def iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat() if value else None

        return {
            "is_open": self.is_open,
            "open_utc": iso(self.open_utc),
            "close_utc": iso(self.close_utc),
            "next_open_utc": iso(self.next_open_utc),
            "next_close_utc": iso(self.next_close_utc),
            "current_time_utc": iso(self.current_time_utc),
            "clock_source": self.clock_source,
            "exchange_code": self.exchange_code,
            "exchange_timezone": self.exchange_timezone,
            "broker_id": self.broker_id,
            "asset_class_id": self.asset_class_id,
        }


def _parse_hhmm(text: Any) -> Optional[time]:
    """'HH:MM' or 'HH:MM:SS' -> time, or None when it is not a time."""
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(str(text).strip(), fmt).time()
        except ValueError:
            continue
    return None


def _skip_weekend(moment: datetime) -> datetime:
    """Advance a Saturday or Sunday to the following Monday, same time of day."""
    while moment.weekday() >= 5:
        moment += timedelta(days=1)
    return moment


def load_exchanges_from_db() -> Optional[List[Dict[str, Any]]]:
    """The exchange rows from ``Market_Data.dbo.Exchanges``, or None when unavailable.

    The table is the copy of ``McTrader_Live.dbo.Exchanges`` made on 2026-11-04. It is
    the clock's primary source; ``data/clock.json`` keeps the broker and asset-class
    rules and is the fallback, so a database outage degrades to the last frozen copy
    rather than to no clock at all.
    """
    try:
        import pyodbc

        from .config import load as load_settings

        settings = load_settings()
        conn = pyodbc.connect(settings.connection_string,
                              timeout=settings.db_timeout_seconds)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT Id, Code, Name, Country, Currency, Timezone, Opens, Closes "
                "FROM dbo.Exchanges ORDER BY Id")
            rows = [
                {"id": int(row.Id), "code": row.Code, "name": row.Name,
                 "country": row.Country,
                 "currency": (row.Currency or "").strip(),
                 "timezone": row.Timezone,
                 "opens": row.Opens, "closes": row.Closes}
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - falling back to the JSON is the point
        log.warning("market clock: Market_Data.dbo.Exchanges unavailable (%s); "
                    "falling back to %s", exc, REFERENCE_PATH.name)
        return None
    if not rows:
        log.warning("market clock: Market_Data.dbo.Exchanges is empty; falling back to %s",
                    REFERENCE_PATH.name)
        return None
    return rows


class MarketClock:
    """Resolve sessions from the database's exchange table and the rule reference."""

    def __init__(self, reference: Optional[Dict[str, Any]] = None,
                 path: Optional[Path] = None) -> None:
        self.source = "unknown"
        if reference is None:
            source_path = Path(path or REFERENCE_PATH)
            reference = json.loads(source_path.read_text(encoding="utf-8"))
            # An explicit path is a fixture (tests, a deliberately frozen copy) and
            # must not be overridden by whatever is in the database today.
            db_rows = load_exchanges_from_db() if path is None else None
            if db_rows:
                reference = {**reference, "exchanges": db_rows}
                self.source = (f"Market_Data.dbo.Exchanges ({len(db_rows)} rows) + "
                               f"{source_path.name} (brokers/rules)")
            else:
                self.source = str(reference.get("source") or source_path.name)
        else:
            self.source = str(reference.get("source") or "injected")
        self.brokers = {int(b["id"]): b for b in reference.get("brokers", [])}
        self.exchanges = {int(e["id"]): e for e in reference.get("exchanges", [])}
        self.asset_classes = {int(a["id"]): a for a in reference.get("asset_classes", [])}
        self.broker_exchanges: Dict[int, List[int]] = {}
        for row in reference.get("broker_exchanges", []):
            if row.get("active", True):
                self.broker_exchanges.setdefault(int(row["broker_id"]), []).append(int(row["exchange_id"]))
        self.broker_asset_classes: Dict[tuple, Dict[str, Any]] = {}
        for row in reference.get("broker_asset_classes", []):
            self.broker_asset_classes[(int(row["broker_id"]), int(row["asset_class_id"]))] = row

    # ------------------------------------------------------------------ clocks

    def exchange_session(self, exchange: Dict[str, Any], ref_utc: datetime,
                         weekend: bool = False) -> ResolvedSession:
        """Clock A: the venue's own hours, in the venue's own timezone."""
        ref_utc = ref_utc.astimezone(UTC)
        tz = resolve_time_zone(exchange.get("timezone"))
        opens = _parse_hhmm(exchange.get("opens"))
        closes = _parse_hhmm(exchange.get("closes"))
        code = exchange.get("code")
        label = exchange.get("timezone")

        if opens is None or closes is None:
            # No stated hours is not an open market; McTrader treats it as always open,
            # and the answer is kept identical so the two books cannot disagree.
            return ResolvedSession(is_open=True, clock_source="always_open",
                                   current_time_utc=ref_utc, exchange_code=code,
                                   exchange_timezone=label)

        local_now = ref_utc.astimezone(tz)

        if not weekend and local_now.weekday() >= 5:
            days_until_monday = 7 - local_now.weekday()
            next_open_local = local_now.replace(hour=opens.hour, minute=opens.minute,
                                                second=0, microsecond=0) + timedelta(days=days_until_monday)
            next_close_local = next_open_local.replace(hour=closes.hour, minute=closes.minute,
                                                       second=0, microsecond=0)
            if next_close_local <= next_open_local:
                next_close_local += timedelta(days=1)
            return ResolvedSession(
                is_open=False,
                open_utc=next_open_local.astimezone(UTC),
                close_utc=next_close_local.astimezone(UTC),
                next_open_utc=next_open_local.astimezone(UTC),
                next_close_utc=next_close_local.astimezone(UTC),
                current_time_utc=ref_utc, clock_source="exchange",
                exchange_code=code, exchange_timezone=label)

        today_open_local = local_now.replace(hour=opens.hour, minute=opens.minute, second=0, microsecond=0)
        today_close_local = local_now.replace(hour=closes.hour, minute=closes.minute, second=0, microsecond=0)
        if today_close_local <= today_open_local:
            today_close_local += timedelta(days=1)

        today_open_utc = today_open_local.astimezone(UTC)
        today_close_utc = today_close_local.astimezone(UTC)
        is_open = today_open_utc <= ref_utc < today_close_utc

        if ref_utc >= today_close_utc:
            next_open_local = _skip_weekend(today_open_local + timedelta(days=1))
            next_close_local = next_open_local.replace(hour=closes.hour, minute=closes.minute,
                                                       second=0, microsecond=0)
            if next_close_local <= next_open_local:
                next_close_local += timedelta(days=1)
            next_open_utc = next_open_local.astimezone(UTC)
            next_close_utc = next_close_local.astimezone(UTC)
        elif ref_utc < today_open_utc:
            next_open_utc, next_close_utc = today_open_utc, today_close_utc
        else:
            next_close_utc = today_close_utc
            next_open_utc = _skip_weekend(today_open_local + timedelta(days=1)).astimezone(UTC)

        return ResolvedSession(
            is_open=is_open, open_utc=today_open_utc, close_utc=today_close_utc,
            next_open_utc=next_open_utc, next_close_utc=next_close_utc,
            current_time_utc=ref_utc, clock_source="exchange",
            exchange_code=code, exchange_timezone=label)

    def synthetic_session(self, ref_utc: datetime, weekend: bool = False,
                          broker_id: Optional[int] = None,
                          asset_class_id: Optional[int] = None) -> ResolvedSession:
        """Clock B: the broker's 00:00-23:59 UTC clock, optionally weekends off."""
        ref_utc = ref_utc.astimezone(UTC)
        if not weekend and ref_utc.weekday() >= 5:
            next_open = _skip_weekend(ref_utc.replace(hour=0, minute=0, second=0, microsecond=0))
            return ResolvedSession(
                is_open=False, open_utc=next_open, close_utc=next_open + timedelta(days=1) - timedelta(microseconds=1),
                next_open_utc=next_open, next_close_utc=next_open + timedelta(days=1) - timedelta(microseconds=1),
                current_time_utc=ref_utc, clock_source="synthetic24",
                broker_id=broker_id, asset_class_id=asset_class_id)

        open_utc = ref_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        close_utc = open_utc + timedelta(days=1) - timedelta(microseconds=1)
        next_open = open_utc + timedelta(days=1)
        if not weekend:
            next_open = _skip_weekend(next_open)
        return ResolvedSession(
            is_open=True, open_utc=open_utc, close_utc=close_utc,
            next_open_utc=next_open, next_close_utc=next_open + timedelta(days=1) - timedelta(microseconds=1),
            current_time_utc=ref_utc, clock_source="synthetic24",
            broker_id=broker_id, asset_class_id=asset_class_id)

    # ------------------------------------------------------------- the dispatch

    def resolve(self, broker_id: int, asset_class_id: int, ref_utc: datetime) -> ResolvedSession:
        """One broker-asset-class pair, through the same rule McTrader applies.

        When the exchange clock applies and the broker maps several exchanges, the first
        active one is used. They are the same venue class in practice (Alpaca: NYSE,
        NASDAQ, ARCA, AMEX, IEX, all 09:30-16:00 New York; IBKR: ASX and NSX, both
        10:00-16:00 Sydney), and ``exchanges_ambiguous`` says when they are not.
        """
        ref_utc = (ref_utc or datetime.now(UTC)).astimezone(UTC)
        rule = self.broker_asset_classes.get((broker_id, asset_class_id))
        if rule is None:
            return ResolvedSession(is_open=False, clock_source="unknown",
                                   current_time_utc=ref_utc, broker_id=broker_id,
                                   asset_class_id=asset_class_id)

        weekend = bool(rule.get("weekend"))
        if rule.get("exchange_hours"):
            codes = self.broker_exchanges.get(broker_id, [])
            if codes:
                session = self.exchange_session(self.exchanges[codes[0]], ref_utc, weekend)
                session.broker_id = broker_id
                session.asset_class_id = asset_class_id
                return session
            return ResolvedSession(is_open=True, clock_source="always_open",
                                   current_time_utc=ref_utc, broker_id=broker_id,
                                   asset_class_id=asset_class_id)
        if rule.get("synthetic_24"):
            return self.synthetic_session(ref_utc, weekend, broker_id, asset_class_id)
        return ResolvedSession(is_open=True, clock_source="always_open",
                               current_time_utc=ref_utc, broker_id=broker_id,
                               asset_class_id=asset_class_id)

    # ------------------------------------------------------------------ payloads

    def broker_clock(self, broker_id: int, ref_utc: Optional[datetime] = None) -> Dict[str, Any]:
        """The payload McTrader's GET /trading_sessions returns, so the swap is one URL."""
        ref_utc = (ref_utc or datetime.now(UTC)).astimezone(UTC)
        broker = self.brokers.get(broker_id)
        if broker is None:
            raise KeyError(f"broker {broker_id} is not in the clock reference")
        broker_tz = resolve_time_zone(broker.get("timezone"))

        exchanges = []
        for exchange_id in self.broker_exchanges.get(broker_id, []):
            exchange = self.exchanges[exchange_id]
            session = self.exchange_session(exchange, ref_utc)
            opens_utc, closes_utc = self._exchange_hours_utc(exchange, ref_utc)
            exchanges.append({
                "id": exchange_id,
                "code": exchange.get("code"),
                "name": exchange.get("name"),
                "timezone": exchange.get("timezone"),
                "opens": exchange.get("opens"),
                "closes": exchange.get("closes"),
                "opens_utc": opens_utc,
                "closes_utc": closes_utc,
                "is_open": session.is_open,
                "clock_source": session.clock_source,
                "next_open_utc": session.next_open_utc.isoformat() if session.next_open_utc else None,
                "next_close_utc": session.next_close_utc.isoformat() if session.next_close_utc else None,
            })

        return {
            "success": True,
            "broker": {"id": broker_id, "name": broker.get("name"),
                       "timezone": broker.get("timezone"), "is_active": broker.get("active")},
            "reference_time_utc": ref_utc.isoformat(),
            "reference_time": ref_utc.strftime("%H:%M"),
            "reference_time_broker": ref_utc.astimezone(broker_tz).strftime("%H:%M"),
            "reference_time_broker_tz": str(broker.get("timezone")),
            "timestamp": datetime.now(UTC).isoformat(),
            "exchanges": exchanges,
            "reference_source": self.source,
        }

    def _exchange_hours_utc(self, exchange: Dict[str, Any],
                            ref_utc: datetime) -> tuple[Optional[str], Optional[str]]:
        """(opens, closes) as UTC 'HH:MM' for the reference day, close rolled if needed."""
        opens = _parse_hhmm(exchange.get("opens"))
        closes = _parse_hhmm(exchange.get("closes"))
        tz = resolve_time_zone(exchange.get("timezone"))
        if opens is None or closes is None:
            return None, None
        local = ref_utc.astimezone(tz)
        open_local = local.replace(hour=opens.hour, minute=opens.minute, second=0, microsecond=0)
        close_local = local.replace(hour=closes.hour, minute=closes.minute, second=0, microsecond=0)
        if close_local <= open_local:
            close_local += timedelta(days=1)
        return (open_local.astimezone(UTC).strftime("%H:%M"),
                close_local.astimezone(UTC).strftime("%H:%M"))

    # ------------------------------------------------------------------- extras

    def asset_class_id(self, name: str) -> Optional[int]:
        wanted = str(name or "").strip().lower()
        for identifier, row in self.asset_classes.items():
            if str(row.get("name", "")).lower() == wanted:
                return identifier
        return None

    def describe(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "brokers": len(self.brokers),
            "exchanges": len(self.exchanges),
            "asset_classes": len(self.asset_classes),
            "broker_exchanges": {k: len(v) for k, v in self.broker_exchanges.items()},
            "rules": len(self.broker_asset_classes),
        }


_CLOCK: Optional[MarketClock] = None


def clock() -> MarketClock:
    """The process-wide clock. The reference is small and read-only once loaded."""
    global _CLOCK
    if _CLOCK is None:
        _CLOCK = MarketClock()
    return _CLOCK


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the market clock reference")
    parser.add_argument("--show", action="store_true", help="print what the copy holds")
    parser.add_argument("--broker", type=int, help="print this broker's clocks now")
    args = parser.parse_args(argv)

    instance = clock()
    if args.broker:
        print(json.dumps(instance.broker_clock(args.broker), indent=2))
        return 0
    print(json.dumps(instance.describe(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
