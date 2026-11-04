"""The four products, and every cache decision in the service.

    GET  /price/{symbol}   -> price()
    POST /prices           -> prices()
    POST /atr              -> atr_batch()
    POST /bars             -> bars()

THE CACHE POLICY, IN ONE PLACE

Three caches, each with the lifetime its data actually has:

* **prices** -- five minutes, held in ``SymbolPriceCache``. Short enough that a price is
  never more than one cycle stale; long enough that two books asking inside the same minute
  pay for one vendor call rather than two.
* **whole-market** -- one vendor call per cycle, held in memory. This is 100 units flat, so
  refetching it per request is the one thing the bulk path must never do.
* **daily bars** -- held in ``DailySnapshot`` and keyed by the completed session, not by a
  clock. Yesterday's bar cannot change, so a TTL would be a worse model than "the session it
  belongs to". This is the 33% saving: today the estate re-buys daily bars every 90 seconds.

WHY ``prices()`` CHOOSES THE ROUTE AND THE CALLER DOES NOT

The tariff has a hard break-even at 100 symbols: ``/real-time/`` bills 1 per symbol, the
``ex=US`` whole-market form bills 100 flat. So a 6-symbol request and a 2,800-symbol request
want opposite answers, and the only thing that distinguishes them is the length of the list
-- which this layer knows and the caller should not have to. A single "always bulk" or
"always per-symbol" rule is wrong for one of the two cases by a factor of up to 100.

That is also why the API needs no fifth endpoint for the whole market: ``POST /prices`` with
a long list *is* the whole-market call.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import atr as atr_module
from . import config as config_module
from .eodhd import EodhdClient, EodhdError, bare_symbol
from .providers import (
    ProviderError, build_router, normalise_asset_class,
)
from .sessions import latest_completed_session
from .store import Store
from .usage import Tariff, UsageLedger
from .venues import resolve_venue

log = logging.getLogger(__name__)

#: How old a whole-market row may be before it is dropped as a price that no longer exists.
#: Four days: the feed carries delisted symbols holding their last-ever price, and a row
#: from 2022 would price a stop as if it were current.
DEFAULT_MAX_ROW_LAG_SECONDS = 345_600.0

#: The whole-market snapshot is reused for this long inside one process.
BULK_SNAPSHOT_TTL_SECONDS = 120.0


class MarketDataError(RuntimeError):
    """A product request that cannot be answered, with a reason to branch on."""

    def __init__(self, reason: str, detail: str = "", status: int = 400) -> None:
        self.reason = reason
        self.detail = detail
        self.status = int(status)
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _utc_naive() -> datetime:
    """UTC as a naive datetime, which is what SQL Server's datetime2 stores here."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch_to_utc(value: Any) -> Optional[datetime]:
    """A vendor epoch-second field as a naive UTC datetime, or ``None``."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


#: How long a refresh that could not reach the newest session is trusted before it is
#: tried again.  A vendor publishes a session some time after it closes, so the first
#: caller past the settle window often finds nothing new; without a retry that one early
#: answer would hold every later caller in the same session back until the next close.
#: An hour bounds the cost of a day the venue does not trade -- one call per symbol per
#: hour -- while still catching the publication an hour after it happens.
BARS_REFRESH_TTL_SECONDS = 3600.0

#: Calendar days a session depth spans, for counting how much history a symbol holds:
#: seven for five is the weekend, and forty days of margin covers the holidays in between.
def _history_window_days(sessions: int) -> int:
    return int(sessions * 7 / 5) + 40


def _number(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _wanted_symbols(symbols: Sequence[str], kind: str) -> List[str]:
    """The request's symbols, bare and de-duplicated, in the order they arrived.

    A suffix is stripped because the venue is served separately -- and the venue has already
    been read off that suffix by ``resolve_venue``, so dropping it here loses nothing. Crypto
    keeps its pair as sent: ``BTC-USD`` has no venue suffix to strip and ``BTC`` alone is a
    different (and usually unpriced) request.
    """
    wanted: List[str] = []
    for raw in symbols:
        text = str(raw).strip().upper() if kind == "crypto" else bare_symbol(str(raw))
        if text and text not in wanted:
            wanted.append(text)
    return wanted


class Products:
    """Price, prices, bars and ATR over one vendor client, one store and three caches."""

    def __init__(self, settings: config_module.Settings, store: Store,
                 client: Optional[EodhdClient] = None,
                 router: Optional[Any] = None) -> None:
        self.settings = settings
        self.store = store
        if client is not None:
            self.client = client
        elif settings.eodhd_api_key:
            self.client = EodhdClient(
                api_key=settings.eodhd_api_key,
                base_url=settings.eodhd_base_url,
                timeout_seconds=settings.eodhd_timeout_seconds,
                bulk_timeout_seconds=settings.eodhd_bulk_timeout_seconds,
            )
        else:
            # No vendor key: EODHD is simply not in the router. Yahoo and Binance need no key, so
            # indices, crypto and FX keep working instead of the whole service stopping for one
            # missing credential. A share then either falls through to Yahoo or reports `missing`,
            # and every answer still names the provider that served it.
            log.warning(
                "no EODHD API key: the vendor is not in the router. Daily bars and the "
                "whole-market feed are unavailable; Yahoo and Binance still answer.")
            self.client = None

        # Vendor spend: the tariff comes from dbo.RequestTypes, the record goes to dbo.EODHDUsage.
        # Neither may stop pricing, so the tariff falls back to empty and the ledger swallows its own
        # write failures (an unpriced call is recorded with cost 0 rather than not recorded).
        self.tariff = Tariff()
        try:
            self.tariff = Tariff.from_rows(store.request_types())
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read dbo.RequestTypes (%s); spend will be recorded unpriced", exc)
        self.ledger = UsageLedger(store, self.tariff)
        if self.client is not None:
            self.client.recorder = self.ledger.record

        # Routing is injectable so a test can supply fakes and stay offline. With no router
        # supplied, EODHD leads and Yahoo/Binance are the fallbacks (see providers.router).
        self.router = router if router is not None else build_router(
            self.client, timeout=settings.eodhd_timeout_seconds)
        self._bulk_lock = threading.Lock()
        self._bulk_snapshot: Dict[str, Dict[str, Any]] = {}
        self._bulk_fetched_at: Optional[datetime] = None
        self._bulk_rows_seen = 0
        self._bulk_rows_dropped = 0
        # (market, symbol) -> (the session a refresh was asked for, when it was asked).
        # A cached series that does not yet reach the newest completed session is refreshed,
        # and remembering the attempt keeps that to one call per symbol per retry window
        # rather than one per caller -- see BARS_REFRESH_TTL_SECONDS.
        self._bars_refreshed: Dict[Tuple[str, str], Tuple[date, datetime]] = {}
        # (market, symbol) -> how many sessions the last *deep* vendor pull returned. A
        # listing younger than the history window can never reach the target depth, so
        # without this it would be re-asked at full width on every daily refresh; once a
        # deep pull has returned everything the vendor has, the check stops trying.
        self._bars_deep: Dict[Tuple[str, str], int] = {}
        self.counters: Dict[str, int] = {
            "price_from_cache": 0, "price_from_vendor": 0,
            "bars_from_cache": 0, "bars_from_vendor": 0,
            "atr_from_cache": 0, "atr_computed": 0,
            "bulk_snapshot_fetches": 0, "per_symbol_fetches": 0,
        }

    # ------------------------------------------------------------------
    # price
    # ------------------------------------------------------------------

    def _fresh(self, fetched_at: Optional[datetime], max_age_seconds: Optional[float]) -> bool:
        if fetched_at is None:
            return False
        limit = self.settings.price_ttl_seconds if max_age_seconds is None else min(
            self.settings.price_ttl_seconds, float(max_age_seconds))
        if fetched_at.tzinfo is not None:
            fetched_at = fetched_at.astimezone(timezone.utc).replace(tzinfo=None)
        return (_utc_naive() - fetched_at).total_seconds() <= limit

    @staticmethod
    def _row_from_quote(quote: Any, venue: str,
                        fetched_at: datetime) -> Dict[str, Any]:
        """A provider quote as a SymbolPriceCache row.

        ``change_pct`` comes from ``ProviderQuote.change_pct`` so every provider is ranked
        with the same arithmetic -- the move is computed from the price and the previous
        close that were observed together, never read from a vendor's own field.
        """
        return {
            "symbol": quote.symbol,
            "venue": venue,
            "price": quote.price,
            "bid": None,
            "ask": None,
            "previous_close": quote.previous_close,
            "change_pct": quote.change_pct,
            # Volume is what makes a mover a mover: a name up 30% on 4,000 shares is a
            # different event from one up 30% on four million, and until 2026-11-01 the
            # cache threw the field away. getattr, because a quote from a caller's own
            # adapter may not carry it and an absent volume is 0, not an error.
            "volume": int(getattr(quote, "volume", 0) or 0),
            "as_of": quote.as_of,
            "fetched_at": fetched_at,
            # Which provider answered. Carried into the cache and out again so a caller can
            # see a tape switch rather than discover it in the P&L.
            "provider": quote.provider,
        }

    def _quote_payload(self, stored: Dict[str, Any],
                       max_age_seconds: Optional[float]) -> Dict[str, Any]:
        """The public quote shape, from a cache row."""
        fetched_at = stored.get("fetched_at")
        age = None
        if isinstance(fetched_at, datetime):
            if fetched_at.tzinfo is not None:
                fetched_at = fetched_at.astimezone(timezone.utc).replace(tzinfo=None)
            age = (_utc_naive() - fetched_at).total_seconds()
        as_of = stored.get("as_of")
        as_of_age = None
        if isinstance(as_of, datetime):
            if as_of.tzinfo is not None:
                as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)
            as_of_age = (_utc_naive() - as_of).total_seconds()
        return {
            "symbol": stored.get("symbol"),
            # Which market this price is for. ``CBA`` at AU and at US are two instruments,
            # and a caller that sent only a country code needs to see which one answered.
            "venue": stored.get("venue"),
            "price": stored.get("price"),
            "previous_close": stored.get("previous_close"),
            "change_pct": stored.get("change_pct"),
            "bid": stored.get("bid"),
            "ask": stored.get("ask"),
            "provider": stored.get("provider") or "eodhd",
            "fetched_at": fetched_at.isoformat() if isinstance(fetched_at, datetime) else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "as_of": as_of.isoformat() if isinstance(as_of, datetime) else None,
            "as_of_age_seconds": round(as_of_age, 3) if as_of_age is not None else None,
        }

    def price(self, symbol: str, venue: Optional[str] = None,
              max_age_seconds: Optional[float] = None,
              asset_class: Optional[str] = None,
              country_code: Optional[str] = None) -> Dict[str, Any]:
        """One symbol's latest price: cache first, then one vendor call."""
        batch = self.prices([symbol], venue=venue, max_age_seconds=max_age_seconds,
                            asset_class=asset_class, country_code=country_code)
        kind = normalise_asset_class(asset_class)
        key = bare_symbol(symbol) if kind != "crypto" else str(symbol).strip().upper()
        quotes = batch.get("quotes") or {}
        if key not in quotes:
            raise MarketDataError(
                "unpriced",
                f"{symbol!r} could not be priced at market {batch.get('venue')!r}",
                status=404)
        return quotes[key]

    def prices(self, symbols: Sequence[str], venue: Optional[str] = None,
               max_age_seconds: Optional[float] = None,
               asset_class: Optional[str] = None,
               country_code: Optional[str] = None) -> Dict[str, Any]:
        """Latest prices for a batch, choosing bulk or per-symbol by size.

        The break-even is the measured tariff: 100 units flat for the whole market against
        1 per symbol, so above ``bulk_breakeven`` symbols the bulk call is cheaper and below
        it the bulk call is up to a hundred times more expensive.

        ``venue`` or ``country_code`` names the market, because ``CBA`` at the ASX and ``CBA``
        in New York are two instruments; with neither, a symbol's own ``.AU`` suffix decides
        and bare symbols default to US. See :mod:`data_service.venues`.
        """
        kind = normalise_asset_class(asset_class)
        # One market for the whole batch: the cache, the vendor ticker and the rows written
        # back are all keyed by it, so a list spanning two venues is refused rather than
        # half-served (venues.resolve_venue).
        market = resolve_venue(venue, country_code, symbols, kind)
        wanted = _wanted_symbols(symbols, kind)
        if not wanted:
            raise MarketDataError("no-symbols", "supply at least one symbol")

        cached = self.store.get_prices(wanted, market)
        quotes: Dict[str, Dict[str, Any]] = {}
        stale: List[str] = []
        for symbol in wanted:
            row = cached.get(symbol)
            if row is not None and self._fresh(row.get("fetched_at"), max_age_seconds):
                quotes[symbol] = self._quote_payload(row, max_age_seconds)
                self.counters["price_from_cache"] += 1
            else:
                stale.append(symbol)

        fetched: List[str] = []
        missing: List[str] = []

        if stale:
            use_bulk = len(stale) >= self.settings.bulk_breakeven and market == "US"
            if use_bulk:
                banked = self._bulk_quotes(stale, market)
                for symbol, row in banked.items():
                    quotes[symbol] = self._quote_payload(row, max_age_seconds)
                    fetched.append(symbol)
                self.counters["bulk_snapshot_fetches"] += 1
            else:
                self.counters["per_symbol_fetches"] += 1
                rows: List[Dict[str, Any]] = []
                for symbol in stale:
                    try:
                        quote = self.router.quote(symbol, asset_class, market)
                    except ProviderError as exc:
                        log.warning("price %s failed: %s", symbol, exc)
                        quote = None
                    if quote is None:
                        missing.append(symbol)
                        continue
                    rows.append(self._row_from_quote(quote, market, _utc_naive()))
                    quotes[symbol] = rows[-1]
                    fetched.append(symbol)
                if rows:
                    self.store.upsert_prices(rows)
                    self.counters["price_from_vendor"] += len(rows)
                    # Re-shape the freshly written rows through the same formatter, so a
                    # cached and a fresh quote are indistinguishable to a caller.
                    for symbol in fetched:
                        stored = self.store.get_prices([symbol], market).get(symbol)
                        if stored is not None:
                            quotes[symbol] = self._quote_payload(stored, max_age_seconds)

            missing.extend(s for s in stale if s not in quotes and s not in missing)

        return {
            "quotes": quotes,
            # The market this batch was priced on. Echoed because it may have been derived
            # from a symbol suffix rather than stated, and a caller must be able to see which
            # instrument a quote belongs to.
            "venue": market,
            "fetched": fetched,
            "missing": missing,
            "deferred": [],
            "complete": not missing,
            "route": "bulk" if len(stale) >= self.settings.bulk_breakeven and market == "US"
                     else "per_symbol",
            "requested": len(wanted),
            "count": len(quotes),
        }

    # ------------------------------------------------------------------
    # the whole-market snapshot
    # ------------------------------------------------------------------

    def _bulk_quotes(self, symbols: Sequence[str], venue: str) -> Dict[str, Dict[str, Any]]:
        """The whole-market snapshot, reusing the in-memory copy inside its lifetime.

        Every row the response can price is written to SymbolPriceCache, not just the
        requested ones: one bulk call then warms the cache for the scanners *and* for
        everyone asking about a single symbol afterwards.
        """
        with self._bulk_lock:
            now = _utc_naive()
            fresh = (
                self._bulk_fetched_at is not None
                and (now - self._bulk_fetched_at).total_seconds() < BULK_SNAPSHOT_TTL_SECONDS
            )
            if not fresh:
                # The whole-market feed is an EODHD capability; the router exposes it rather
                # than pretending Yahoo or Binance have an equivalent.
                quotes = self.router.whole_market_quotes(venue)
                snapshot: Dict[str, Dict[str, Any]] = {}
                observed: List[datetime] = []
                dropped = 0
                for quote in quotes:
                    code = quote.symbol
                    if not code:
                        continue
                    if quote.as_of is not None:
                        observed.append(quote.as_of)
                    normalised = self._row_from_quote(quote, venue, now)
                    if not normalised.get("price") or normalised["price"] <= 0:
                        continue
                    snapshot[code] = normalised

                # Two freshness bounds, because one cannot express both facts: an absolute
                # age (the whole feed cannot be ancient) and a lag behind the newest row
                # (this symbol cannot have stopped trading while the market traded on).
                newest = max(observed) if observed else None
                kept: Dict[str, Dict[str, Any]] = {}
                for code, row in snapshot.items():
                    as_of = row.get("as_of")
                    if newest is not None and as_of is not None:
                        if (newest - as_of).total_seconds() > DEFAULT_MAX_ROW_LAG_SECONDS:
                            dropped += 1
                            continue
                    kept[code] = row

                self._bulk_snapshot = kept
                self._bulk_fetched_at = now
                self._bulk_rows_seen = len(quotes)
                self._bulk_rows_dropped = dropped
                if kept:
                    self.store.upsert_prices(list(kept.values()))
                    self.counters["price_from_vendor"] += len(kept)
                log.info(
                    "bulk snapshot: %d rows, %d usable, %d dropped as stale",
                    len(quotes), len(kept), dropped)

            return {s: self._bulk_snapshot[s] for s in symbols if s in self._bulk_snapshot}

    def refresh_snapshot(self, venue: str = "US") -> int:
        """Collect the whole-market snapshot now and cache every usable row.

        Called by the schedule at session open + 45. Unlike ``prices()`` this will not
        accept the in-memory copy: the point is a collection at a known instant, so the
        120-second snapshot is discarded first and the vendor is asked again.

        Returns the number of rows written to SymbolPriceCache -- zero when the venue has
        no whole-market feed (EODHD has one for the US only), which the caller reports.
        """
        with self._bulk_lock:
            self._bulk_fetched_at = None
        self._bulk_quotes([], venue)
        return len(self._bulk_snapshot)

    # ------------------------------------------------------------------
    # bars
    # ------------------------------------------------------------------

    def bars(self, symbols: Sequence[str], sessions: int = 15, venue: Optional[str] = None,
             allow_fetch: bool = True,
             asset_class: Optional[str] = None,
             country_code: Optional[str] = None) -> Dict[str, Any]:
        """Completed daily bars. Cache (DailySnapshot) first, then one /eod/ call each.

        ``sessions`` is floored at ``min_sessions`` for ATR purposes elsewhere, but this
        call returns what was asked for: a caller inspecting 5 sessions is not an error.

        A cache hit requires the series to reach the newest *completed* session, not merely
        to hold enough rows. Counting rows was the whole test, so a symbol whose stored
        series was one session behind was served from the cache forever -- nothing asked
        the vendor again, and an instrument traded on the ASX sat a session stale while the
        venue's own clock said the day was over. The refresh it triggers is remembered by
        the session it was asked for, so a holiday costs one call per symbol, not one per
        request. The answer's ``as_of_session`` states which session was actually served.

        A cache hit also requires a raw close on every served row: ``Close`` is the adjusted
        series and ``RawClose`` is the traded one, and a window that carries only the first
        cannot answer a candle-body question on a single price scale. A missing raw close
        triggers one vendor refresh, so pre-existing rows are backfilled for the symbols
        that are actually requested rather than for the whole market.

        The same refresh also fills *depth*. ``/eod/`` bills one unit whatever the width of
        the window, so a symbol whose stored series is still short of ``history_sessions``
        (two years by default) is asked for that whole horizon; one that already reaches it
        is refreshed for a handful of sessions and costs the same. That is what makes the
        history accumulate as symbols are requested, instead of every symbol being pinned
        at the fifteen sessions the window used to keep.

        ``venue``/``country_code`` chooses the series, and so does a suffixed symbol when
        neither is stated: ``CBA.AU`` is ASX data, not a US listing that happens to share the
        ticker (see :mod:`data_service.venues`).
        """
        kind = normalise_asset_class(asset_class)
        market = resolve_venue(venue, country_code, symbols, kind)
        wanted = _wanted_symbols(symbols, kind)
        if not wanted:
            raise MarketDataError("no-symbols", "supply at least one symbol")
        if sessions < 1:
            raise MarketDataError("bad-sessions", "sessions must be at least 1")

        latest = latest_completed_session(market)
        banked = self.store.get_daily_bars_many(wanted, sessions, market)
        target = max(1, int(self.settings.history_sessions))
        horizon = latest - timedelta(days=_history_window_days(target))
        # Asked for lazily: a request answered wholly from the cache must not pay for a
        # count it will not use.
        depths: Optional[Dict[str, int]] = None

        out: Dict[str, Any] = {}
        fetched: List[str] = []
        short: List[str] = []

        for symbol in wanted:
            rows = [r for r in banked.get(symbol, []) if r["session"] <= latest]
            current = bool(rows) and rows[-1]["session"] >= latest
            served = rows[-sessions:]
            # A served window is only ready when every row states its raw close. The two
            # closes are two price scales, and a series half adjusted and half raw is worse
            # than one that is wholly adjusted. Rows written before RawClose existed, and
            # BarHourly seeds, are unknown; the vendor refresh below fills them once for the
            # symbols actually asked for, which is the backfill.
            raw_ready = bool(served) and all(r.get("raw_close") is not None for r in served)
            if len(rows) >= sessions and current and raw_ready:
                self.counters["bars_from_cache"] += 1
                out[symbol] = self._bars_payload(symbol, served, "cache", market)
                continue

            keep = (market, symbol)
            asked = self._bars_refreshed.get(keep)
            if not allow_fetch or (
                asked is not None
                and asked[0] == latest
                and (_utc_naive() - asked[1]).total_seconds() < BARS_REFRESH_TTL_SECONDS
            ):
                if len(rows) < sessions:
                    short.append(symbol)
                if rows:
                    out[symbol] = self._bars_payload(symbol, rows, "cache", market)
                continue

            # Enough sessions to answer, and one vendor call whatever the window. While the
            # symbol's stored history is short of the horizon, ask for the whole horizon:
            # the call is being made anyway and bills the same, so the depth is free, and
            # it is the difference between a support level drawn on two years and one drawn
            # on the three weeks this used to keep.
            need = max(sessions, self.settings.min_sessions)
            if target > need:
                if depths is None:
                    depths = self.store.daily_bar_depth(wanted, market, since=horizon)
                held = depths.get(symbol, 0)
                achieved = self._bars_deep.get(keep, 0)
                if held < target and not (achieved and held >= achieved):
                    need = target
            self._bars_refreshed[keep] = (latest, _utc_naive())
            try:
                provider_rows, provider_name = self.router.bars(
                    symbol, kind, sessions=need, venue=market)
            except ProviderError as exc:
                log.warning("bars %s failed: %s", symbol, exc)
                provider_rows, provider_name = [], None

            if not provider_rows:
                short.append(symbol)
                if rows:
                    out[symbol] = self._bars_payload(symbol, rows, "cache", market)
                continue

            fresh_rows = self._write_provider_bars(symbol, provider_rows, provider_name,
                                                   market)
            if need >= target:
                self._bars_deep[keep] = len(provider_rows)
            fetched.append(symbol)
            self.counters["bars_from_vendor"] += 1
            fresh_rows = [r for r in fresh_rows if r["session"] <= latest]
            if fresh_rows:
                out[symbol] = self._bars_payload(
                    symbol, fresh_rows[-sessions:], "vendor", market, provider_name)
            else:
                short.append(symbol)

        return {
            "bars": out,
            "fetched": fetched,
            "short_history": short,
            "complete": not short,
            "requested_sessions": sessions,
        }

    def _write_provider_bars(self, symbol: str, bars: Any,
                             provider_name: Optional[str],
                             exchange: str) -> List[Dict[str, Any]]:
        """Upsert provider bars into DailySnapshot and return them in cache-row shape.

        ``Source`` records which provider answered, so a series built partly from EODHD and
        partly from Yahoo is visible rather than silent. ``IsAdjusted`` travels with each bar
        because an ATR mixing adjusted and raw closes reads a split as a catastrophic move.
        ``exchange`` is required rather than defaulted: the row cannot be filed without the
        venue it belongs to.
        """
        rows: List[Dict[str, Any]] = []
        for bar in bars:
            rows.append({
                "symbol": symbol,
                # The venue travels with the bar: the same ticker can be an ASX listing
                # and a US one, and without it the two overwrite each other.
                "exchange": exchange,
                "session": bar.session,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "raw_close": getattr(bar, "raw_close", None),
                "volume": int(bar.volume or 0),
                "source": provider_name or "unknown",
                "adjusted": bool(getattr(bar, "adjusted", True)),
            })
        if rows:
            rows.sort(key=lambda r: r["session"])
            self.store.upsert_daily(rows)
        return rows

    @staticmethod
    def _bars_payload(symbol: str, rows: List[Dict[str, Any]], source: str,
                      venue: str, provider: Optional[str] = None) -> Dict[str, Any]:
        adjusted = all(r.get("adjusted", True) for r in rows) if rows else True
        return {
            "symbol": symbol,
            "venue": venue,
            "as_of_session": rows[-1]["session"].isoformat() if rows else None,
            "sessions": len(rows),
            "adjusted": adjusted,
            # Which provider answered. Hardcoding this was a real bug: Binance crypto bars
            # were being reported as eodhd, which is precisely the silent tape-mix this
            # field exists to prevent.
            "provider": provider or ("eodhd" if source == "vendor" else "cache"),
            "source": source,
            "rows": [
                {
                    "d": r["session"].isoformat(),
                    "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"],
                    # The unadjusted close. ``c`` is adjusted when the series says so, and a
                    # candle body must not mix the two scales; null means not known.
                    "cr": r.get("raw_close"),
                    "v": r.get("volume"),
                }
                for r in rows
            ],
        }

    # ------------------------------------------------------------------
    # ATR
    # ------------------------------------------------------------------

    def atr_batch(
        self,
        symbols: Sequence[str],
        period: Optional[int] = None,
        method: Optional[str] = None,
        smoothing: Optional[str] = None,
        venue: Optional[str] = None,
        price: Optional[float] = None,
        multiplier: Optional[float] = None,
        direction: str = "long",
        allow_fetch: bool = True,
        asset_class: Optional[str] = None,
        country_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """ATR per symbol, from IndicatorCache when it can answer, else computed.

        The cache only answers for the configured method and smoothing, because it has one
        ``Method`` column and one ``Smoothing`` column: a request for a different definition
        is computed on the fly and not cached. That keeps the table honest -- a cached value
        can always be read back with the definition that produced it.

        The market comes from ``venue``, ``country_code`` or the symbols' own suffix (see
        :mod:`data_service.venues`), and it is the cache key: an ASX ``CBA`` and a US ``CBA``
        have separate ATRs and neither is served for the other.
        """
        period = int(period or self.settings.atr_period)
        method = (method or self.settings.atr_method).lower()
        smoothing = (smoothing or self.settings.atr_smoothing).lower()

        if method not in atr_module.METHODS:
            raise MarketDataError(
                "bad-method",
                f"{method!r} is not an ATR method; use one of {', '.join(atr_module.METHODS)}")
        if smoothing not in atr_module.SMOOTHINGS:
            raise MarketDataError(
                "bad-smoothing",
                f"{smoothing!r} is not a smoothing; use one of "
                f"{', '.join(atr_module.SMOOTHINGS)}")
        if period < 1:
            raise MarketDataError("bad-period", "period must be at least 1")

        cache_eligible = (
            method == self.settings.atr_method
            and smoothing == self.settings.atr_smoothing
        )

        wanted: List[str] = []
        for raw in symbols:
            text = bare_symbol(str(raw))
            if text and text not in wanted:
                wanted.append(text)
        if not wanted:
            raise MarketDataError("no-symbols", "supply at least one symbol")

        # One normalised market for the whole call: the ATR cache is keyed by venue, so the
        # read has to ask with the same code the bars were written under ("ASX" and "AU" are
        # one venue, and AU is what is stored).
        market = resolve_venue(venue, country_code, symbols, asset_class)
        cached = self.store.get_indicators(wanted, market) if cache_eligible else {}
        latest = latest_completed_session(market)

        results: Dict[str, Any] = {}
        to_compute: List[str] = []
        for symbol in wanted:
            hit = cached.get(symbol)
            if (
                hit is not None
                and hit.get("method") == method
                and hit.get("smoothing") == smoothing
                and hit.get("as_of_session") is not None
                and hit["as_of_session"] <= latest
                and (hit.get("values") or {}).get(period) is not None
            ):
                value = hit["values"][period]
                reference = hit.get("reference_price")
                results[symbol] = {
                    "symbol": symbol,
                    "value": round(value, 6),
                    "percent": round(atr_module.atr_percent(value, reference), 4)
                               if reference else None,
                    "reference_price": reference,
                    "period": period,
                    "method": hit.get("method"),
                    "smoothing": hit.get("smoothing"),
                    "sessions_used": hit.get("sessions_used"),
                    "as_of_session": hit["as_of_session"].isoformat(),
                    "source": "indicator_cache",
                }
                self.counters["atr_from_cache"] += 1
            else:
                to_compute.append(symbol)

        if to_compute:
            # Ask for enough sessions that the longest configured period is defined.
            longest = max(list(self.settings.atr_periods) + [period])
            need = max(self.settings.min_sessions, longest + 1)
            bar_payload = self.bars(
                to_compute, sessions=need, venue=market, allow_fetch=allow_fetch,
                asset_class=asset_class, country_code=country_code)

            fresh_for_cache: List[Dict[str, Any]] = []
            for symbol in to_compute:
                entry = (bar_payload.get("bars") or {}).get(symbol)
                rows = (entry or {}).get("rows") or []
                if not rows:
                    results[symbol] = {
                        "symbol": symbol, "value": None, "percent": None,
                        "reference_price": None, "period": period, "method": method,
                        "smoothing": smoothing, "sessions_used": len(rows),
                        "as_of_session": None, "source": "unavailable",
                        "reason": "no completed daily bars",
                    }
                    continue

                highs = [r["h"] for r in rows]
                lows = [r["l"] for r in rows]
                closes = [r["c"] for r in rows]
                reference = closes[-1] if closes else None

                values: Dict[int, Optional[float]] = {}
                for configured in self.settings.atr_periods:
                    values[configured] = atr_module.average_true_range(
                        highs, lows, closes, configured, method, smoothing)
                if period not in values:
                    values[period] = atr_module.average_true_range(
                        highs, lows, closes, period, method, smoothing)

                value = values.get(period)
                results[symbol] = {
                    "symbol": symbol,
                    "value": round(value, 6) if value is not None else None,
                    "percent": round(atr_module.atr_percent(value, reference), 4)
                               if value is not None else None,
                    "reference_price": reference,
                    "period": period,
                    "method": method,
                    "smoothing": smoothing,
                    "sessions_used": len(rows),
                    "as_of_session": (entry or {}).get("as_of_session"),
                    "source": "computed",
                    "reason": None if value is not None else
                              f"{len(rows)} session(s) is fewer than the {period + 1} "
                              f"ATR({period}) needs",
                }
                self.counters["atr_computed"] += 1

                if cache_eligible and value is not None:
                    fresh_for_cache.append({
                        "symbol": symbol,
                        # The venue the bars came from. Without it the row lands as US, and
                        # the next AU request reads a US ATR out of the cache.
                        "exchange": market,
                        "values": values,
                        "reference_price": reference,
                        "sessions_used": len(rows),
                        "as_of_session": (entry or {}).get("as_of_session"),
                        "method": method,
                        "smoothing": smoothing,
                    })

            if fresh_for_cache:
                # as_of_session arrives as an ISO string from _bars_payload; the store wants
                # a date. Convert here rather than widening the store's contract.
                for row in fresh_for_cache:
                    if isinstance(row.get("as_of_session"), str):
                        row["as_of_session"] = date.fromisoformat(row["as_of_session"])
                self.store.upsert_indicators(fresh_for_cache)

        # The stop level is pure arithmetic over a value already computed, so it is attached
        # here rather than being a fifth endpoint. The multiplier is never defaulted: the
        # books use 1.0, 1.0, 2.0 and 3.0, and a default would impose one book's risk on
        # another's.
        if price is not None or multiplier is not None:
            if price is None or multiplier is None:
                raise MarketDataError(
                    "incomplete-stop",
                    "stop_level needs both price and multiplier (the multiplier is never "
                    "defaulted: the books use 1.0, 1.0, 2.0 and 3.0)")
            # Validated here so a bad stop is a 400 with a reason, not the AtrError the
            # arithmetic raises deeper down -- which reaches the client as a 500 and reads
            # like a broken service. A zero is the common one: it arrives whenever a caller
            # sends every field of the schema rather than only the two that are set.
            if float(multiplier) <= 0:
                raise MarketDataError(
                    "bad-multiplier",
                    f"multiplier must be positive, got {multiplier!r}; it is never defaulted")
            side = str(direction or "long").strip().lower()
            if side not in ("long", "short"):
                raise MarketDataError(
                    "bad-direction",
                    f"{direction!r} is not a direction; use 'long' or 'short'")
            for symbol, reading in results.items():
                reading["stop_level"] = atr_module.stop_level(
                    float(price), reading.get("value"), float(multiplier), side)

        return {
            "atr": results,
            # The market the readings belong to: derived from a country code or a symbol
            # suffix as often as it is stated, so it is stated back.
            "venue": market,
            "period": period,
            "method": method,
            "smoothing": smoothing,
            "complete": all(r.get("value") is not None for r in results.values()),
        }

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "vendor": self.client.stats() if self.client is not None else None,
            "router": self.router.stats(),
            "bulk_snapshot": {
                "rows_seen": self._bulk_rows_seen,
                "rows_usable": len(self._bulk_snapshot),
                "rows_dropped": self._bulk_rows_dropped,
                "fetched_at": self._bulk_fetched_at.isoformat()
                              if self._bulk_fetched_at else None,
            },
            "settings": {
                "price_ttl_seconds": self.settings.price_ttl_seconds,
                "bulk_breakeven": self.settings.bulk_breakeven,
                "min_sessions": self.settings.min_sessions,
                "history_sessions": self.settings.history_sessions,
                "atr_periods": list(self.settings.atr_periods),
                "atr_method": self.settings.atr_method,
                "atr_smoothing": self.settings.atr_smoothing,
                "database": self.settings.db_database,
            },
        }
