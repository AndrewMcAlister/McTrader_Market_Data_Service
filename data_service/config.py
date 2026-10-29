"""Configuration for McTrader_Market_Data_Service.

Everything here is overridable by environment variable, because the two things most likely
to change are the ones that must not require a code edit:

* the **database** -- `Market_Data` (renamed from `Back_Test` on 2026-10-29), so `MDS_DATABASE`
  decides which it connects to. Nothing in the code or the SQL names a database, so the
  rename is a config change and not a migration.
* the **EODHD key** -- taken from the OS keyring by default, the same entry McTrader and
  Mini-McTrader already use, so all three books provably share one key and one quota.

Defaults are deliberately the safe ones: bind to localhost only, short price TTL, and ATR
computed with gap-inclusive true range because that is what every existing implementation
in the estate does (see atr.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

#: The OS keyring entry the EODHD key lives in. Shared with McTrader deliberately: two
#: keys would mean two quotas, two bills and no single place to measure usage.
KEYRING_SERVICE = "market_data:eodhd:global"
KEYRING_USERNAME = "eodhd:api_key"

EODHD_BASE_URL = "https://eodhd.com/api"

#: One price is reused for this long. Five minutes is the number the estate already uses
#: (McTrader's QuoteCache) and it is short enough that a price is never more than one
#: cycle stale at a 15-minute cadence.
DEFAULT_PRICE_TTL_SECONDS = 300.0

#: Above this many symbols in one request, the whole-market call is cheaper. Measured
#: tariff: /real-time/ bills 1 per symbol, /real-time?ex=US bills 100 flat, so 100 is the
#: exact break-even. Below it, buying the whole market to price a handful is up to a
#: hundred times more expensive.
DEFAULT_BULK_BREAKEVEN = 100

#: ATR needs 14 completed sessions plus one earlier close to give the first bar a
#: previous close. Fifteen is the floor; a request for fewer cannot produce ATR(14).
DEFAULT_MIN_SESSIONS = 15

#: The periods the consumer set actually reads. 22 is McTrader's Chandelier window and its
#: preferred stop ATR; 26 is PortfolioAdvisor's MACD-V divisor. 20 and 50 are carried
#: because they were asked for, and are read by nothing today.
DEFAULT_ATR_PERIODS = (14, 20, 22, 26, 50)

#: 14 is the conventional stop period, and the one both stop books use.
DEFAULT_ATR_PERIOD = 14


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Resolved settings. Immutable so a running request cannot mutate the TTL."""

    # -- vendor ------------------------------------------------------------
    eodhd_base_url: str = EODHD_BASE_URL
    eodhd_api_key: str = ""
    #: Where the key was found: ``explicit``, ``environment``, ``keyring`` or ``none``. Never
    #: the key itself -- an operator needs to know which path worked, not the secret.
    eodhd_key_source: str = "none"
    eodhd_timeout_seconds: float = 30.0
    #: The whole-market response is ~2.3 MB; it gets a longer leash than a price.
    eodhd_bulk_timeout_seconds: float = 60.0

    # -- database ----------------------------------------------------------
    db_server: str = "localhost"
    db_database: str = "Market_Data"
    db_driver: str = "ODBC Driver 17 for SQL Server"
    db_trusted: bool = True
    db_trust_server_certificate: bool = True
    db_timeout_seconds: int = 30

    # -- behaviour ---------------------------------------------------------
    price_ttl_seconds: float = DEFAULT_PRICE_TTL_SECONDS
    bulk_breakeven: int = DEFAULT_BULK_BREAKEVEN
    min_sessions: int = DEFAULT_MIN_SESSIONS
    atr_periods: tuple = DEFAULT_ATR_PERIODS
    atr_period: int = DEFAULT_ATR_PERIOD
    #: Gap-inclusive true range. `high_low` is available and is NOT the default, because
    #: every implementation in the estate uses true range and switching silently would
    #: change every stop distance.
    atr_method: str = "tr"
    #: `simple` is the mean of the last N true ranges, which is what PaperTrader's
    #: atr_from_bars and Mini-McTrader's atr_from_daily both do. `wilder` is available for
    #: parity with technical_utils.wilder_atr_series.
    atr_smoothing: str = "simple"

    # -- server ------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8003
    service_name: str = "McTrader_Market_Data_Service"

    @property
    def connection_string(self) -> str:
        """The pyodbc connection string, assembled from the fields above."""
        parts = [
            f"DRIVER={{{self.db_driver}}}",
            f"SERVER={self.db_server}",
            f"DATABASE={self.db_database}",
        ]
        if self.db_trusted:
            parts.append("Trusted_Connection=yes")
        if self.db_trust_server_certificate:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts) + ";"


def resolve_api_key_with_source(explicit: Optional[str] = None) -> "tuple[str, str]":
    """The EODHD key and where it came from: ``(key, source)``.

    ``source`` is one of ``explicit``, ``environment``, ``keyring`` or ``none``. It exists because
    the keyring is **per user**: a service running as ``NT AUTHORITY\\SYSTEM`` cannot see the entry
    stored under an interactive account, and without this a service that found no key would log
    exactly like one that did -- right up to the first vendor call.

    Order is deliberate. The keyring is last-resort rather than first so a one-off run or a service
    can be told the key without touching stored credentials, and so a test can never accidentally
    spend real quota by inheriting one.
    """
    if explicit and explicit.strip():
        return explicit.strip(), "explicit"
    env = os.environ.get("EODHD_API_KEY", "").strip()
    if env:
        return env, "environment"
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if stored and stored.strip():
            return stored.strip(), "keyring"
    except Exception:  # noqa: BLE001 - no keyring is a missing key, not a crash
        pass
    return "", "none"


def resolve_api_key(explicit: Optional[str] = None) -> str:
    """The EODHD key alone; see :func:`resolve_api_key_with_source` for where it came from."""
    return resolve_api_key_with_source(explicit)[0]


def load(api_key: Optional[str] = None) -> Settings:
    """Build Settings from the environment, resolving the key last."""
    periods_raw = _env("MDS_ATR_PERIODS", "")
    periods: List[int] = []
    for chunk in periods_raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            periods.append(int(chunk))

    resolved_key, key_source = resolve_api_key_with_source(api_key)

    return Settings(
        eodhd_base_url=_env("MDS_EODHD_BASE_URL", EODHD_BASE_URL).rstrip("/"),
        eodhd_api_key=resolved_key,
        eodhd_key_source=key_source,
        eodhd_timeout_seconds=_env_float("MDS_EODHD_TIMEOUT", 30.0),
        eodhd_bulk_timeout_seconds=_env_float("MDS_EODHD_BULK_TIMEOUT", 60.0),
        db_server=_env("MDS_DB_SERVER", "localhost"),
        db_database=_env("MDS_DATABASE", "Market_Data"),
        db_driver=_env("MDS_DB_DRIVER", "ODBC Driver 17 for SQL Server"),
        db_trusted=_env("MDS_DB_TRUSTED", "1") not in ("0", "false", "False"),
        db_trust_server_certificate=_env("MDS_DB_TRUST_CERT", "1") not in ("0", "false", "False"),
        db_timeout_seconds=_env_int("MDS_DB_TIMEOUT", 30),
        price_ttl_seconds=_env_float("MDS_PRICE_TTL_SECONDS", DEFAULT_PRICE_TTL_SECONDS),
        bulk_breakeven=_env_int("MDS_BULK_BREAKEVEN", DEFAULT_BULK_BREAKEVEN),
        min_sessions=_env_int("MDS_MIN_SESSIONS", DEFAULT_MIN_SESSIONS),
        atr_periods=tuple(periods) if periods else DEFAULT_ATR_PERIODS,
        atr_period=_env_int("MDS_ATR_PERIOD", DEFAULT_ATR_PERIOD),
        atr_method=_env("MDS_ATR_METHOD", "tr").lower(),
        atr_smoothing=_env("MDS_ATR_SMOOTHING", "simple").lower(),
        host=_env("MDS_HOST", "127.0.0.1"),
        port=_env_int("MDS_PORT", 8003),
    )
