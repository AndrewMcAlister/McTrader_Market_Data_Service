"""The HTTP surface: four data endpoints, and nothing else worth calling.

    GET  /price/{symbol}
    POST /prices
    POST /atr
    POST /bars

Plus ``/healthz`` and ``/stats``, which are infrastructure rather than data: a service that
four books depend on needs to be able to say it is alive and what it has spent.

Design notes that are deliberate rather than incidental:

* **There is no whole-market endpoint.** ``POST /prices`` with a long list *is* the
  whole-market call -- the service picks the route from the list length, because the tariff
  break-even is 100 symbols and the caller should not have to know that.
* **ATR carries an optional stop level.** One round trip at entry time instead of two, and
  the multiplier is echoed back so a stored stop is reproducible.
* **Every response declares provenance** (``provider``, ``source``, ages) because the estate
  has already been bitten by a silent tape switch: Mini-McTrader logged
  ``FALLBACK to Alpaca IEX prices - a different tape`` with nothing flagging it.
* **A refusal is always a reason, never a zero.** A symbol that cannot be priced appears
  under ``missing``; it is not cached, so the next call retries it.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__, config as config_module
from .eodhd import EodhdError
from .products import MarketDataError, Products
from .secrets_service import EodhdKeyStore, KeyringUnavailable
from .refresh import refresh_daily_bars
from .store import Store, StoreBusy
from .usage import set_caller

log = logging.getLogger("data_service.api")


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------

class PricesRequest(BaseModel):
    symbols: List[str] = Field(..., min_length=1, description="Bare or suffixed symbols")
    venue: str = Field("US", description="Venue/country code, e.g. US, AU, L, TO")
    max_age_seconds: Optional[float] = Field(
        None, description="Lower the 5-minute cache window; cannot raise it")
    asset_class: Optional[str] = Field(
        None,
        description="shares (default), crypto, indices, commodities, rates, forex. "
                    "Chooses the provider order; crypto goes to Binance first.")


class AtrRequest(BaseModel):
    symbols: List[str] = Field(..., min_length=1)
    period: Optional[int] = Field(None, description="Default 14")
    method: Optional[str] = Field(
        None, description="'tr' (gap-inclusive, default) or 'high_low'")
    smoothing: Optional[str] = Field(
        None, description="'simple' (default, what the stop books use) or 'wilder'")
    venue: str = "US"
    price: Optional[float] = Field(
        None, description="Supply with multiplier to also get a stop_level")
    multiplier: Optional[float] = Field(
        None, description="ATR multiple for the stop. Required if price is given; never defaulted")
    direction: str = Field("long", description="'long' or 'short'")
    asset_class: Optional[str] = Field(None, description="shares (default) or crypto, ...")


class EodhdKeyRequest(BaseModel):
    """The vendor key, on its way in. Never logged, never echoed back."""

    api_key: str = Field(..., min_length=1, description="The EODHD API key")


class RefreshRequest(BaseModel):
    """Ask for a bulk refresh of the daily bars.

    ``force`` exists because the freshness check is deliberately conservative: if a bulk pull is
    already recorded for today, the endpoint does nothing and says so rather than spending another
    100 units. A person who wants it anyway says so.
    """

    market: str = Field("US", description="Market to pull; only US is measured")
    session_date: Optional[str] = Field(
        None, description="A specific session (YYYY-MM-DD); default is the latest completed one")
    force: bool = Field(False, description="Pull again even if one is already recorded today")
    include_indicators: bool = Field(
        True, description="Rebuild IndicatorCache afterwards -- a refresh that leaves ATR stale is "
                          "half a refresh")


class BarsRequest(BaseModel):
    symbols: List[str] = Field(..., min_length=1)
    sessions: int = Field(15, ge=1, description="Completed sessions to return")
    venue: str = "US"
    allow_fetch: bool = Field(
        True, description="False = answer only from DailySnapshot, never the vendor")
    asset_class: Optional[str] = Field(None, description="shares (default) or crypto, ...")


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

def _attribute(request: Request, fallback: str) -> None:
    """Record who caused any vendor spend during this request.

    The header is the point: a shared service's spend is only accountable if you can say which book
    spent it. Without a header the path stands in, so the ledger still shows *what* was asked even
    when the client did not identify itself -- and an unattributed row is a question worth asking.
    """
    set_caller(request.headers.get("X-MarketData-Caller") or fallback)


def _products(request: Request) -> Products:
    return request.app.state.products


def _error(exc: MarketDataError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.reason, "detail": exc.detail},
    )


def build_products(settings: config_module.Settings, store: Store) -> Any:
    """The product layer, rebuilt whenever the vendor key changes.

    Extracted so the secrets endpoints can rebuild it after a key is stored: without that, a key
    posted to a running service would sit in the keyring unused until the next restart, which is
    exactly the trap the endpoint exists to avoid.

    Returns ``None`` only when construction genuinely fails. The **absence of a key is not a
    failure**: the product layer is still built, with EODHD left out of the router, so the free
    providers keep answering rather than one missing credential stopping the service.
    """
    try:
        return Products(settings, store)
    except Exception as exc:  # noqa: BLE001 - a broken router must be visible, not fatal
        log.error("product layer unavailable: %s", exc)
        return None


def _build_schedule(app: FastAPI, settings: Any):
    """The collection schedule, wired to the same store and vendor client as the endpoints.

    Returns None when the product layer is unavailable: a service without a vendor client
    can still serve the clock and the cache, and a scheduler that cannot collect should not
    be started pretending it will.
    """
    from .clock import clock as market_clock
    from .refresh import refresh_daily_bars
    from .schedule import MARKETS, RefreshScheduler, SessionSchedule

    products = getattr(app.state, "products", None)
    if products is None:
        return None

    store = app.state.store
    windows = {window.market: window for window in MARKETS}
    session_schedule = SessionSchedule(
        market_clock(),
        offset_minutes=settings.schedule_offset_minutes,
        retry_minutes=settings.schedule_retry_minutes)

    def run_market(market: str) -> Dict[str, Any]:
        """Collect one venue's bulk data -- but only while that venue is open.

        The guard is here, at the point of collection, and not only on the schedule: a
        manual /schedule/run must obey the same rule or the rule is decorative. The bars
        this writes are the previous session's settled bar (a bulk daily endpoint cannot
        answer with a bar that has not closed); the snapshot it writes is live, which is
        exactly the data the session guard exists to protect.
        """
        window = windows.get(market)
        if window is None:
            return {"ok": False, "status": "unknown-market", "market": market,
                    "detail": f"market must be one of {sorted(windows)}"}
        if not session_schedule.in_session(window):
            return {"ok": False, "status": "closed", "market": market,
                    "detail": "the venue is not open; nothing was collected"}

        daily = refresh_daily_bars(
            store=store, client=products.client, settings=settings,
            market=market, force=False, include_indicators=True)
        result: Dict[str, Any] = {"market": market, "daily": daily,
                                  "status": str(daily.get("status") or "unknown")}
        result["ok"] = result["status"] in ("refreshed", "already-fresh")

        if market == "US":
            # The whole-market snapshot: the only endpoint EODHD publishes one for. Rows
            # land in SymbolPriceCache, which is what the books read a price from.
            try:
                result["snapshot_rows"] = products.refresh_snapshot("US")
            except Exception as exc:  # noqa: BLE001 - a snapshot failure is reported, not fatal
                result["snapshot_error"] = f"{type(exc).__name__}: {exc}"
        return result

    return RefreshScheduler(
        schedule=session_schedule,
        run_market=run_market,
        retry_minutes=settings.schedule_retry_minutes,
        offset_minutes=settings.schedule_offset_minutes)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the Products singleton and make sure the schema exists."""
    settings = config_module.load()
    if not settings.eodhd_api_key:
        # Not fatal: /healthz still works, and a caller gets a clear refusal rather than a
        # 500 from deep inside the vendor client.
        log.error("no EODHD API key resolved; price/bars/atr will refuse. "
                  "POST /secrets/eodhd to store one in this account's keyring.")
    app.state.settings = settings
    app.state.store = Store(settings.connection_string, timeout=settings.db_timeout_seconds)
    try:
        app.state.store.ensure_schema()
    except Exception as exc:  # noqa: BLE001 - a schema failure must be visible, not fatal
        log.error("schema ensure failed: %s", exc)
    app.state.products = build_products(settings, app.state.store)

    # The schedule: collect the whole market at each venue's open + 45, one retry, then a
    # person. Built here because it needs the same Products singleton the endpoints use,
    # and started as a daemon so a stuck tick cannot hold the process open.
    app.state.schedule = _build_schedule(app, settings)
    if app.state.schedule is not None and settings.schedule_enabled:
        app.state.schedule.start()
    log.info(
        "McTrader_Market_Data_Service %s ready: db=%s port=%s",
        __version__, settings.db_database, settings.port)
    yield
    if getattr(app.state, "schedule", None) is not None:
        app.state.schedule.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="McTrader_Market_Data_Service",
        version=__version__,
        description=(
            "One price cache, one daily bar cache, one ATR definition, one vendor key. "
            "Four data endpoints for Mini-McTrader, PaperTrader, PortfolioAdvisor and "
            "McTrader."
        ),
        lifespan=lifespan,
    )

    # -- infrastructure ----------------------------------------------------

    @app.exception_handler(StoreBusy)
    def _database_busy(request: Request, exc: StoreBusy) -> JSONResponse:
        """A lock wait became a fast, named, retryable refusal.

        Without this a read blocked behind the daily load looks exactly like a dead service
        to the caller -- it just hangs. 503 plus a reason lets a consumer decide to retry, or
        to fall back, instead of waiting out a timeout.
        """
        log.warning("database busy: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"error": "database-busy", "detail": str(exc), "retryable": True},
        )

    @app.get("/healthz", tags=["ops"])
    def healthz(request: Request) -> Dict[str, Any]:
        settings = request.app.state.settings
        store = request.app.state.store
        db_ok, db_error = True, None
        try:
            store.ping()
        except Exception as exc:  # noqa: BLE001
            db_ok, db_error = False, f"{type(exc).__name__}: {exc}"
        return {
            "status": "ok" if (db_ok and request.app.state.products) else "degraded",
            "version": __version__,
            "vendor_configured": bool(settings.eodhd_api_key),
            # Where the key came from, not the key. "keyring" under a LocalSystem service is a
            # warning sign: it means the service is running as the interactive user.
            "key_source": settings.eodhd_key_source,
            "database": settings.db_database,
            "database_ok": db_ok,
            "database_error": db_error,
        }

    @app.get("/stats", tags=["ops"])
    def stats(request: Request) -> Dict[str, Any]:
        settings = request.app.state.settings
        store = request.app.state.store
        payload: Dict[str, Any] = {"database": store.stats(), "settings": {
            "price_ttl_seconds": settings.price_ttl_seconds,
            "bulk_breakeven": settings.bulk_breakeven,
            "atr_periods": list(settings.atr_periods),
            "database": settings.db_database,
        }}
        products = request.app.state.products
        if products is not None:
            payload.update(products.stats())

        # Today's spend from dbo.EODHDUsage rather than from the process counters: the ledger
        # survives a restart and is attributable to a caller, which is what accounting needs.
        try:
            payload["usage_today"] = store.usage_summary()
        except Exception as exc:  # noqa: BLE001 - /stats must answer even if the ledger cannot
            payload["usage_today_error"] = f"{type(exc).__name__}: {exc}"
        return payload

    # -- the vendor key -----------------------------------------------------

    def _admin_guard(request: Request) -> Optional[JSONResponse]:
        """Refuse a secret write unless the caller proved it may.

        Writing the key is privileged: a local process that can overwrite it can point this service
        at an attacker's key (billing and data both), and once the service runs as LocalSystem it
        can make SYSTEM store a key the user could not write themselves.

        So: if ``MDS_ADMIN_TOKEN`` is set, it is required. If it is not, the call is allowed -- this
        binds to localhost on a single-user machine, and refusing by default would make the endpoint
        useless on first install -- but it is logged and reported, so the gap is never silent.
        """
        expected = os.environ.get("MDS_ADMIN_TOKEN", "").strip()
        if not expected:
            log.warning("secrets endpoint called with no MDS_ADMIN_TOKEN configured; "
                        "any local process can rewrite the vendor key")
            return None
        supplied = request.headers.get("X-Admin-Token", "").strip()
        if supplied != expected:
            return JSONResponse(status_code=401,
                                content={"error": "unauthorised",
                                         "detail": "X-Admin-Token is required and did not match"})
        return None

    @app.post("/secrets/eodhd", tags=["secrets"])
    def post_eodhd_secret(request: Request, body: "EodhdKeyRequest") -> Any:
        """Store the EODHD API key in this process account's keyring, and start using it.

        This is the supported way to give a LocalSystem service a key it can read: the service
        writes into its **own** vault, which is the only one it can see. It also replaces the
        environment-variable route, whose value would otherwise sit in plaintext in the registry.
        """
        denied = _admin_guard(request)
        if denied is not None:
            return denied

        settings = request.app.state.settings
        store = EodhdKeyStore()
        try:
            store.write(body.api_key)
        except KeyringUnavailable as exc:
            return JSONResponse(status_code=503,
                                content={"error": "keyring-unavailable", "detail": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=400,
                                content={"error": "invalid-key", "detail": str(exc)})

        # Take effect immediately rather than at the next restart.
        updated = dataclasses.replace(
            settings, eodhd_api_key=body.api_key.strip(), eodhd_key_source="keyring")
        request.app.state.settings = updated
        request.app.state.products = build_products(updated, request.app.state.store)

        return {
            "ok": True,
            "stored": True,
            "vendor_configured": request.app.state.products is not None,
            "status": store.status(),
        }

    @app.get("/secrets/eodhd", tags=["secrets"])
    def get_eodhd_secret(request: Request) -> Any:
        """Whether a key is stored and a masked form of it. **Never the key itself.**"""
        settings = request.app.state.settings
        status = EodhdKeyStore().status()
        return {
            **status,
            "in_use": bool(settings.eodhd_api_key),
            "in_use_source": settings.eodhd_key_source,
            "vendor_configured": request.app.state.products is not None,
        }

    @app.delete("/secrets/eodhd", tags=["secrets"])
    def delete_eodhd_secret(request: Request) -> Any:
        """Remove the stored key. The service keeps running and refuses vendor calls."""
        denied = _admin_guard(request)
        if denied is not None:
            return denied

        removed = EodhdKeyStore().delete()
        settings = request.app.state.settings
        # Only discard the live client if the key came from the keyring; an environment-supplied
        # key is still in force, and dropping it would be a surprise.
        if settings.eodhd_key_source == "keyring":
            keyless = dataclasses.replace(settings, eodhd_api_key="", eodhd_key_source="none")
            request.app.state.settings = keyless
            # Rebuild rather than null it: Yahoo and Binance need no key, so crypto, indices and FX
            # must keep answering. Setting None here stopped the whole service for one missing
            # credential -- found by testing exactly this path.
            request.app.state.products = build_products(keyless, request.app.state.store)
        return {"ok": True, "removed": removed,
                "in_use": bool(request.app.state.settings.eodhd_api_key)}

    # -- stopping ------------------------------------------------------------

    @app.post("/admin/shutdown", status_code=202, tags=["ops"])
    def shutdown(request: Request) -> Dict[str, Any]:
        """Stop this process, from inside this process.

        Session isolation means nothing in an interactive shell can terminate it: a service runs
        in session 0, and a session-2 process gets "Access is denied" at any run level. A process
        can always end itself, which is what makes this a usable last resort for a wedged child
        that cannot be stopped any other way.

        It does NOT stop the service. NSSM is installed with ``AppExit Default Restart``, so a
        process that exits -- even cleanly -- is started again five seconds later. ``stop.bat``
        stops the SERVICE, which is the only thing that means "stay down".
        """
        caller = request.client.host if request.client else "unknown"
        log.warning("shutdown requested via /admin/shutdown by %s", caller)
        logging.shutdown()  # flush handlers before the process goes
        threading.Timer(0.4, lambda: os._exit(0)).start()
        return {"ok": True, "stopping": True, "requested_by": caller,
                "note": "the supervisor restarts this within ~5s; use stop.bat to stop the service"}

    # -- bulk refresh ------------------------------------------------------

    @app.post("/refresh", tags=["market data"])
    def post_refresh(request: Request, body: Optional[RefreshRequest] = None) -> Any:
        """Pull the whole market's daily bars into DailySnapshot, on demand.

        Measured: 100 vendor units and about 30 seconds for a full session (~44,000 rows plus the
        ATR rebuild for 2,866 symbols); about 10 seconds without the rebuild. The freshness check is
        free -- it reads the spend ledger -- so calling this when nothing has changed costs nothing
        and answers in milliseconds.

        Idempotent: the pull upserts by (symbol, session), so running it twice is harmless. Two
        callers at once do not double-spend, because a non-blocking lock makes the second one report
        `busy` instead.
        """
        denied = _admin_guard(request)
        if denied is not None:
            return denied

        products = _products(request)
        if products is None:
            return JSONResponse(status_code=503, content={"error": "vendor-unavailable"})

        _attribute(request, "http:/refresh")
        options = body or RefreshRequest()
        result = refresh_daily_bars(
            store=request.app.state.store,
            client=products.client,
            settings=request.app.state.settings,
            market=options.market,
            session_date=options.session_date,
            force=options.force,
            include_indicators=options.include_indicators,
        )
        status = result.get("status")
        code = 202 if status == "refreshed" else 409 if status == "busy" else 200
        if status == "no-vendor-key":
            code = 503
        return JSONResponse(status_code=code, content=result)

    # -- the market clock ---------------------------------------------------
    #
    # The dual-clock model, ported from McTrader's Utils/trading_session.py and given the
    # service's own copy of the reference data (data/clock.json). GET /market/clock returns
    # the same payload McTrader's GET /trading_sessions returns, field for field, so the
    # books can switch by changing a URL rather than a response parser.

    @app.get("/schedule", tags=["market clock"])
    def get_schedule(request: Request) -> Any:
        """What the collection schedule is, and what it did today.

        Per market: whether the venue is open, the session's open/close, the fire time
        (open + 45), the next fire, and the attempts made this session. ``state`` is
        ``done``, ``waiting`` or ``manual-intervention`` -- the last one means two attempts
        failed and the service has stopped trying, by design.
        """
        _attribute(request, "http:/schedule")
        scheduler = getattr(request.app.state, "schedule", None)
        if scheduler is None:
            return JSONResponse(status_code=503, content={
                "error": "no schedule: the product layer is unavailable"})
        payload = scheduler.status()
        payload["offset_minutes"] = request.app.state.settings.schedule_offset_minutes
        payload["retry_minutes"] = request.app.state.settings.schedule_retry_minutes
        return payload

    @app.post("/schedule/run", tags=["market clock"])
    def post_schedule_run(
        request: Request,
        market: str = Query("US", description="US or AU"),
    ) -> Any:
        """Run one market's collection now -- still only while that venue is open.

        A person asking is not a reason to collect an out-of-hours price: this shares the
        session guard with the scheduler and answers 409 ``closed`` outside the session.
        It does not touch the attempt count, so a manual run does not consume the retry.
        """
        denied = _admin_guard(request)
        if denied is not None:
            return denied
        _attribute(request, "http:/schedule/run")
        scheduler = getattr(request.app.state, "schedule", None)
        if scheduler is None:
            return JSONResponse(status_code=503, content={
                "error": "no schedule: the product layer is unavailable"})
        result = scheduler.run_market(market.upper())
        return JSONResponse(status_code=200 if result.get("ok") else 409, content=result)

    @app.get("/market/clock", tags=["market clock"])
    def get_market_clock(
        request: Request,
        broker_id: int = Query(1, description="Broker Id from the clock reference"),
        time: Optional[str] = Query(
            None, description="Reference time HH:MM in UTC; omitted means now"),
        at: Optional[str] = Query(
            None, description="Full ISO-8601 reference instant; wins over time"),
    ) -> Any:
        _attribute(request, "http:/market/clock")
        from .clock import clock

        try:
            reference = _clock_reference_instant(time=time, at=at)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        try:
            return clock().broker_clock(broker_id, reference)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.get("/market/session", tags=["market clock"])
    def get_market_session(
        request: Request,
        broker_id: int = Query(1, description="Broker Id from the clock reference"),
        asset_class: str = Query("shares", description="shares, crypto, etfs, options, ..."),
        at: Optional[str] = Query(None, description="ISO-8601 instant; omitted means now"),
    ) -> Any:
        _attribute(request, "http:/market/session")
        from .clock import clock

        instance = clock()
        asset_class_id = instance.asset_class_id(asset_class)
        if asset_class_id is None:
            return JSONResponse(status_code=404,
                                content={"error": f"unknown asset class {asset_class!r}"})
        try:
            reference = _clock_reference_instant(at=at)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        session = instance.resolve(broker_id, asset_class_id, reference)
        payload = session.as_dict()
        payload["broker_id"] = broker_id
        payload["asset_class"] = asset_class
        payload["reference_source"] = instance.source
        return payload

    @app.get("/market/exchanges", tags=["market clock"])
    def get_market_exchanges(request: Request) -> Any:
        """The clock's own copy of the exchange table -- what it will answer from."""
        _attribute(request, "http:/market/exchanges")
        from .clock import clock

        instance = clock()
        return {
            "source": instance.source,
            "brokers": list(instance.brokers.values()),
            "exchanges": list(instance.exchanges.values()),
            "broker_exchanges": instance.broker_exchanges,
            "rules": [
                {"broker_id": key[0], "asset_class_id": key[1], "asset_class": instance.asset_classes.get(key[1], {}).get("name"), **rule}
                for key, rule in instance.broker_asset_classes.items()
            ],
        }

    @app.get("/trading_sessions", tags=["market clock"])
    def get_trading_sessions(
        request: Request,
        broker_id: int = Query(1, description="Broker ID to check sessions for"),
        time: Optional[str] = Query(
            None,
            description="Reference time in HH:MM. Read in your local timezone "
                        "(config clock_user_timezone, default Australia/Sydney) unless "
                        "use_broker_timezone=True. Omitted means now."),
        use_broker_timezone: bool = Query(
            False,
            description="If True, interpret `time` in the broker's local timezone."),
    ) -> Any:
        """The dual-clock sessions for a broker -- a drop-in for McTrader's endpoint.

        Same route, same parameters, same body: a client pointed at this service instead
        of McTrader's API gets the same answer without touching its parser. The one
        difference is the reference data: this answers from the service's own copy
        (data/clock.json), so it keeps answering when McTrader's database is unavailable.

        `time` is read in the OPERATOR's timezone by default, not UTC, because that is what
        McTrader's endpoint does -- "14:30" there means 14:30 Sydney. Sending a full ISO
        instant instead is what /market/clock?at= is for, and it is unambiguous.
        """
        _attribute(request, "http:/trading_sessions")
        from datetime import datetime, timezone

        from .clock import clock, resolve_time_zone

        settings = request.app.state.settings
        instance = clock()
        now_utc = datetime.now(timezone.utc)

        reference = now_utc
        if time is not None:
            parts = time.strip().split(":")
            try:
                if len(parts) != 2:
                    raise ValueError
                hour, minute = int(parts[0]), int(parts[1])
            except ValueError:
                return JSONResponse(status_code=400, content={
                    "error": f'Invalid time format. Use HH:MM (e.g. "09:30" or "14:00").',
                    "error_type": "validation_error",
                    "timestamp": now_utc.isoformat()})
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return JSONResponse(status_code=400, content={
                    "error": f'Invalid time: {time}. Hour must be 0-23, minute 0-59.',
                    "error_type": "validation_error",
                    "timestamp": now_utc.isoformat()})

            if use_broker_timezone:
                broker = instance.brokers.get(broker_id) or {}
                zone = resolve_time_zone(broker.get("timezone"))
            else:
                zone = resolve_time_zone(settings.clock_user_timezone)
            local = now_utc.astimezone(zone).replace(
                hour=hour, minute=minute, second=0, microsecond=0)
            reference = local.astimezone(timezone.utc)

        try:
            payload = instance.broker_clock(broker_id, reference)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={
                "error": str(exc), "error_type": "not_found",
                "timestamp": now_utc.isoformat()})
        payload["reference_time_interpreted_in"] = (
            "broker" if use_broker_timezone else settings.clock_user_timezone)
        return payload

    @app.get("/todays-data-is-available", tags=["market data"])
    def get_todays_data_is_available(
        request: Request,
        country_code: str = Query("US", description="us/US or au/AU"),
    ) -> Any:
        """Is there data for this country collected since its session opened today?

        The body is a bare boolean, because that is the question: a caller deciding whether
        to rank does not want to unpack an object to get one bit. The evidence that
        produced it rides in the response headers, so a person debugging still sees the two
        instants it compared:

            X-Dataset-Country, X-Dataset-Venue, X-Dataset-Latest-Price,
            X-Session-Open, X-Session-Close, X-Session-Is-Open

        False is not an error: before the open, on a weekend, or with only yesterday's
        prices stored, the answer is false and the status is 200. A country code that is
        not us/au is a 400 -- that one is a caller's mistake.
        """
        _attribute(request, "http:/todays-data-is-available")
        from .availability import COUNTRIES, check, country_key
        from .clock import clock

        try:
            country = country_key(country_code)
        except KeyError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

        store = request.app.state.store
        venue = COUNTRIES[country][0]
        try:
            latest = store.latest_venue_price(venue)
        except Exception as exc:  # noqa: BLE001 - an unreadable store cannot vouch for data
            log.warning("todays-data-is-available: could not read the price cache (%s)", exc)
            latest = None

        result = check(clock(), latest, country)
        return JSONResponse(status_code=200, content=result.available, headers={
            "X-Dataset-Country": result.country,
            "X-Dataset-Venue": result.venue,
            "X-Dataset-Latest-Price": result.latest_utc.isoformat() if result.latest_utc else "",
            "X-Session-Open": result.session_open_utc.isoformat() if result.session_open_utc else "",
            "X-Session-Close": result.session_close_utc.isoformat() if result.session_close_utc else "",
            "X-Session-Is-Open": "true" if result.session_is_open else "false",
        })

    @app.get("/todays-big-movers", tags=["market data"])
    def get_todays_big_movers(
        request: Request,
        country_code: str = Query("US", description="us/US or au/AU"),
        limit: int = Query(25, ge=1, le=200, description="how many winners and losers"),
        min_price: float = Query(1.0, ge=0.0, description="ignore anything cheaper"),
        min_dollar_volume: float = Query(
            0.0, ge=0.0,
            description="ignore anything that traded less than this (price x volume)"),
    ) -> Any:
        """Today's biggest winners and losers for a country, if today's data is in.

        Two lists, each entry shaped::

            {"symbol": "ADT", "price": 9.86, "change_pct": 6.31, "volume": 163754638,
             "dollar_volume": 1614620730.68, "previous_close": 9.27,
             "as_of_utc": "2026-10-30T19:31:00+00:00", "source": "eodhd",
             "market_cap": null}

        Gated on the same rule as ``GET /todays-data-is-available``, because a mover list
        built from yesterday's prices is the most misleading thing this service could
        serve -- it looks live. When the gate is false the answer is a 200 with
        ``available: false``, a reason, and empty lists: a caller branches on one field and
        never has to treat "no data yet" as an error.

        ``change_pct`` is the provider's own, computed against the previous close that was
        observed with the price, so a split or a stale previous close shows up as the
        vendor reports it rather than as this service's guess.

        MARKET CAP is not available at market scale from this vendor: EODHD's bulk feed
        carries no capitalisation, and per-symbol fundamentals cost 10 units each (about
        28,000 for one US scan). The field is present and null so a caller can rely on the
        shape today and gain the value later without a parser change.
        """
        _attribute(request, "http:/todays-big-movers")
        from .availability import COUNTRIES, _as_utc, check, country_key
        from .clock import clock

        try:
            country = country_key(country_code)
        except KeyError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

        store = request.app.state.store
        venue = COUNTRIES[country][0]
        try:
            latest = store.latest_venue_price(venue)
        except Exception as exc:  # noqa: BLE001 - an unreadable store cannot vouch for data
            log.warning("todays-big-movers: could not read the price cache (%s)", exc)
            latest = None

        availability = check(clock(), latest, country)
        session = {
            "open_utc": availability.session_open_utc.isoformat() if availability.session_open_utc else None,
            "close_utc": availability.session_close_utc.isoformat() if availability.session_close_utc else None,
            "is_open": availability.session_is_open,
        }
        if not availability.available:
            return JSONResponse(status_code=200, content={
                "available": False,
                "reason": ("no price for this venue was observed at or after its most "
                           "recent session open"),
                "country": country,
                "venue": venue,
                "session": session,
                "latest_price_utc": availability.latest_utc.isoformat() if availability.latest_utc else None,
                "winners": [],
                "losers": [],
            })

        try:
            movers = store.big_movers(venue, limit=limit, min_price=min_price,
                                      min_dollar_volume=min_dollar_volume)
        except Exception as exc:  # noqa: BLE001 - a broken query is a 503, not a 200 with no rows
            log.exception("todays-big-movers: query failed")
            return JSONResponse(status_code=503,
                                content={"error": "could not read the price cache",
                                         "detail": f"{type(exc).__name__}: {exc}"})

        def shape(rows: list) -> list:
            out = []
            for row in rows:
                observed = _as_utc(row.get("as_of_utc"))
                out.append({
                    "symbol": row["symbol"],
                    "price": row["price"],
                    "change_pct": row["change_pct"],
                    "volume": row["volume"],
                    "dollar_volume": row["dollar_volume"],
                    "previous_close": row["previous_close"],
                    "as_of_utc": observed.isoformat() if observed else None,
                    "source": row["source"],
                    "market_cap": None,
                })
            return out

        return JSONResponse(status_code=200, content={
            "available": True,
            "country": country,
            "venue": venue,
            "session": session,
            "latest_price_utc": availability.latest_utc.isoformat() if availability.latest_utc else None,
            "filters": {"limit": limit, "min_price": min_price,
                        "min_dollar_volume": min_dollar_volume},
            "counts": {"winners": len(movers["winners"]), "losers": len(movers["losers"])},
            "market_cap_source": None,
            "winners": shape(movers["winners"]),
            "losers": shape(movers["losers"]),
        })

    def _clock_reference_instant(time: Optional[str] = None, at: Optional[str] = None):
        """The instant to resolve against: an ISO stamp, an HH:MM today in UTC, or now."""
        from datetime import datetime, time as clock_time, timezone

        if at:
            stamp = at.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError as exc:
                raise ValueError(f"at must be ISO-8601, got {at!r}") from exc
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        if time:
            parts = time.strip().split(":")
            if len(parts) != 2:
                raise ValueError(f"time must be HH:MM, got {time!r}")
            try:
                hour, minute = int(parts[0]), int(parts[1])
            except ValueError as exc:
                raise ValueError(f"time must be HH:MM, got {time!r}") from exc
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"time out of range: {time!r}")
            now = datetime.now(timezone.utc)
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return None

    # -- the four data endpoints -------------------------------------------

    @app.get("/price/{symbol}", tags=["market data"])
    def get_price(
        request: Request,
        symbol: str,
        venue: str = Query("US", description="Venue/country code"),
        max_age_seconds: Optional[float] = Query(
            None, description="Lower the 5-minute window; cannot raise it"),
        asset_class: Optional[str] = Query(
            None, description="shares (default), crypto, indices, ..."),
    ) -> Any:
        _attribute(request, "http:/price")
        products = _products(request)
        if products is None:
            return JSONResponse(status_code=503,
                                content={"error": "vendor-unavailable"})
        try:
            return products.price(symbol, venue=venue, max_age_seconds=max_age_seconds,
                                  asset_class=asset_class)
        except MarketDataError as exc:
            return _error(exc)
        except EodhdError as exc:
            return JSONResponse(status_code=502,
                                content={"error": exc.reason, "detail": exc.detail})

    @app.post("/prices", tags=["market data"])
    def post_prices(request: Request, body: PricesRequest) -> Any:
        _attribute(request, "http:/prices")
        products = _products(request)
        if products is None:
            return JSONResponse(status_code=503,
                                content={"error": "vendor-unavailable"})
        try:
            return products.prices(
                body.symbols, venue=body.venue, max_age_seconds=body.max_age_seconds,
                asset_class=body.asset_class)
        except MarketDataError as exc:
            return _error(exc)
        except EodhdError as exc:
            return JSONResponse(status_code=502,
                                content={"error": exc.reason, "detail": exc.detail})

    @app.post("/atr", tags=["market data"])
    def post_atr(request: Request, body: AtrRequest) -> Any:
        _attribute(request, "http:/atr")
        products = _products(request)
        if products is None:
            return JSONResponse(status_code=503,
                                content={"error": "vendor-unavailable"})
        try:
            return products.atr_batch(
                body.symbols,
                period=body.period,
                method=body.method,
                smoothing=body.smoothing,
                venue=body.venue,
                price=body.price,
                multiplier=body.multiplier,
                direction=body.direction,
                asset_class=body.asset_class,
            )
        except MarketDataError as exc:
            return _error(exc)
        except EodhdError as exc:
            return JSONResponse(status_code=502,
                                content={"error": exc.reason, "detail": exc.detail})

    @app.post("/bars", tags=["market data"])
    def post_bars(request: Request, body: BarsRequest) -> Any:
        _attribute(request, "http:/bars")
        products = _products(request)
        if products is None:
            return JSONResponse(status_code=503,
                                content={"error": "vendor-unavailable"})
        try:
            return products.bars(
                body.symbols, sessions=body.sessions, venue=body.venue,
                allow_fetch=body.allow_fetch, asset_class=body.asset_class)
        except MarketDataError as exc:
            return _error(exc)
        except EodhdError as exc:
            return JSONResponse(status_code=502,
                                content={"error": exc.reason, "detail": exc.detail})

    return app


app = create_app()
