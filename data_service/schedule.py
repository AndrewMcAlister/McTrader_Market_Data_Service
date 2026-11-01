"""The refresh schedule: bulk data at session open + 45 minutes, once, with one retry.

WHAT THIS IS FOR

The book ranks on a whole-market pull, and the pull has to be taken while the venue is
open. A price collected at 02:00 on a Sunday is either a stale echo or a synthetic print,
and both are the kind of quote a manipulator can leave lying around for a screener to
find. So collection is tied to the session, not to a wall clock:

    US   NYSE opens 09:30 New York  -> fire at 10:15 New York
    AU   ASX  opens 10:00 Sydney    -> fire at 10:45 Sydney

Both are computed from the market clock (``data_service.clock``), so a daylight-saving
change moves the fire time with the session instead of leaving it an hour out -- the class
of bug that made McTrader's next-open an hour late across a transition.

WHAT HAPPENS WHEN IT FAILS

One attempt, then exactly one retry fifteen minutes later, then nothing. No backoff, no
third try, no "wait for the next tick": after the retry a failed session is a person's
problem, and ``GET /schedule`` says so rather than retrying quietly forever. A retry at
open+60 is still inside the session, which is the only window the data is worth having.

WHAT IT DOES NOT DO

It does not backfill. If the service was down all session, the day stays missing and the
next day's pull carries only its own session; ``jobs.daily --since`` is how a gap is
closed by hand. And it never fires while the venue is closed: ``tick`` refuses, and the
collection itself re-checks ``in_session`` before it writes anything, because a guard on
the schedule alone can be defeated by a manual run.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

UTC = timezone.utc


@dataclass(frozen=True)
class MarketWindow:
    """One venue whose bulk data is collected on a schedule."""

    market: str        # vendor market code, as /eod-bulk-last-day/{market}
    exchange: str      # exchange code in the clock reference
    label: str


#: The two markets this service collects for. The clock resolves each one's own hours.
MARKETS = (
    MarketWindow("US", "NYSE", "US equities"),
    MarketWindow("AU", "ASX", "Australian equities"),
)


@dataclass
class Attempt:
    at: datetime
    ok: bool
    status: str
    detail: str = ""


@dataclass
class MarketState:
    session: Optional[str] = None          # the venue's date this state belongs to
    attempts: List[Attempt] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return any(attempt.ok for attempt in self.attempts)

    @property
    def exhausted(self) -> bool:
        return len(self.attempts) >= 2 and not self.done


class SessionSchedule:
    """When to fire, derived from the clock rather than from a fixed hour."""

    def __init__(self, clock: Any, offset_minutes: int = 45, retry_minutes: int = 15) -> None:
        self.clock = clock
        self.offset = timedelta(minutes=offset_minutes)
        self.retry = timedelta(minutes=retry_minutes)

    def _exchange(self, window: MarketWindow) -> Dict[str, Any]:
        for row in self.clock.exchanges.values():
            if row["code"] == window.exchange:
                return row
        raise KeyError(f"{window.exchange} is not in the clock reference")

    def session(self, window: MarketWindow, ref: Optional[datetime] = None):
        return self.clock.exchange_session(self._exchange(window), ref or datetime.now(UTC))

    def in_session(self, window: MarketWindow, ref: Optional[datetime] = None) -> bool:
        return bool(self.session(window, ref).is_open)

    def fire_time(self, window: MarketWindow, ref: datetime) -> datetime:
        """Today's fire instant for this venue: the session open plus the offset.

        Read off the resolved session, so it is already in UTC and already DST-correct.
        On a day the venue is shut, the session resolves to the *next* open, which is what
        the caller wants: the fire time for the day it will actually trade.
        """
        session = self.session(window, ref)
        return session.open_utc + self.offset

    def next_fire(self, window: MarketWindow, ref: Optional[datetime] = None) -> datetime:
        """The first fire instant strictly after ``ref`` -- for reporting, not for firing."""
        ref = (ref or datetime.now(UTC)).astimezone(UTC)
        candidate = self.fire_time(window, ref)
        if candidate <= ref:
            # Today's already gone; ask the clock again from just after the close.
            candidate = self.fire_time(window, ref + timedelta(days=1))
        return candidate


class RefreshScheduler:
    """One attempt, one retry, then a person. Never outside the session."""

    def __init__(
        self,
        *,
        schedule: SessionSchedule,
        run_market: Callable[[str], Dict[str, Any]],
        markets: tuple = MARKETS,
        retry_minutes: int = 15,
        offset_minutes: int = 45,
    ) -> None:
        self.schedule = schedule
        self.run_market = run_market
        self.markets = markets
        self.offset = timedelta(minutes=offset_minutes)
        self.retry = timedelta(minutes=retry_minutes)
        self.states: Dict[str, MarketState] = {w.market: MarketState() for w in markets}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_tick: Optional[datetime] = None
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ state

    def _date_key(self, window: MarketWindow, ref: datetime) -> str:
        """The venue's own calendar date, so a new session starts with clean state."""
        session = self.schedule.session(window, ref)
        moment = session.open_utc or ref
        return moment.date().isoformat()

    # ------------------------------------------------------------------- tick

    def tick(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """One scheduling pass. Returns what it did, for the log and for tests."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        actions: List[Dict[str, Any]] = []
        with self._lock:
            self.last_tick = now
            for window in self.markets:
                state = self.states.setdefault(window.market, MarketState())
                session = self.schedule.session(window, now)

                if not session.is_open:
                    # Closed: nothing is collected, and nothing is recorded as a failure --
                    # a closed venue is not an outage.
                    continue

                key = self._date_key(window, now)
                if state.session != key:
                    state.session = key
                    state.attempts = []

                fire = session.open_utc + self.offset
                first_due = now >= fire
                retry_due = now >= fire + self.retry

                if state.done or state.exhausted:
                    continue
                if not state.attempts and not first_due:
                    continue
                if state.attempts and not retry_due:
                    continue

                try:
                    result = self.run_market(window.market) or {}
                    ok = bool(result.get("ok"))
                    status = str(result.get("status") or ("ok" if ok else "failed"))
                    detail = str(result.get("detail") or "")
                except Exception as exc:  # noqa: BLE001 - one bad run must not stop the clock
                    ok, status, detail = False, "error", f"{type(exc).__name__}: {exc}"
                    self.last_error = detail

                attempt = Attempt(at=now, ok=ok, status=status, detail=detail)
                state.attempts.append(attempt)
                actions.append({"market": window.market, "attempt": len(state.attempts),
                                "ok": ok, "status": status, "detail": detail,
                                "session": key, "at": now.isoformat()})
                log.info("schedule: %s attempt %d at %s -> %s (%s)", window.market,
                         len(state.attempts), now.strftime("%H:%M:%SZ"), status,
                         detail or "no detail")
                if not ok and len(state.attempts) >= 2:
                    log.warning("schedule: %s failed twice (%s, %s); manual intervention "
                                "required for session %s", window.market,
                                state.attempts[0].status, status, key)
        return actions

    # ----------------------------------------------------------------- thread

    def start(self, tick_seconds: int = 20) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(tick_seconds):
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - the thread must outlive one bad tick
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("schedule: tick failed")

        self._thread = threading.Thread(target=loop, name="mds-schedule", daemon=True)
        self._thread.start()
        log.info("schedule: started, every %ss", tick_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ---------------------------------------------------------------- status

    def status(self) -> Dict[str, Any]:
        rows = []
        for window in self.markets:
            state = self.states.get(window.market, MarketState())
            session = self.schedule.session(window)
            rows.append({
                "market": window.market,
                "label": window.label,
                "exchange": window.exchange,
                "is_open": session.is_open,
                "session_open_utc": session.open_utc.isoformat() if session.open_utc else None,
                "session_close_utc": session.close_utc.isoformat() if session.close_utc else None,
                "fire_at_utc": (session.open_utc + self.offset).isoformat() if session.open_utc else None,
                "next_fire_utc": self.schedule.next_fire(window).isoformat(),
                "session": state.session,
                "attempts": [{"at": a.at.isoformat(), "ok": a.ok, "status": a.status,
                              "detail": a.detail} for a in state.attempts],
                "state": ("done" if state.done else
                          "manual-intervention" if state.exhausted else
                          "waiting"),
            })
        return {
            "enabled": self._thread is not None and self._thread.is_alive(),
            "last_tick_utc": self.last_tick.isoformat() if self.last_tick else None,
            "last_error": self.last_error,
            "markets": rows,
        }
