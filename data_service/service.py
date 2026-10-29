"""Service entry point: the same app, run in a way the Service Control Manager can manage.

WHY THIS EXISTS SEPARATELY FROM ``__main__``

``python -m data_service`` is for a developer at a console. A Windows service has **no console**:
anything written to stdout/stderr goes nowhere, so a service that logs only to the stream appears
to run silently whether it is healthy or broken. This module therefore logs to a rotating file
before it starts serving.

WHAT A SERVICE NEEDS THAT A CONSOLE RUN DOES NOT

* **A log file.** ``logs/data_service.log``, rotated at 10 MB, kept for 5 files.
* **A non-zero exit code on fatal startup failure.** NSSM restarts on a non-zero exit; a service
  that exits 0 after failing to bind its port looks like a clean stop and is never retried.
* **Graceful shutdown on stop.** uvicorn handles SIGINT/SIGTERM; NSSM's console stop method sends
  Ctrl+C, so in-flight requests finish instead of being killed.
* **``log_config=None``.** uvicorn normally reconfigures the root logger on startup, which would
  discard the file handler configured above.

The database must already be reachable when this starts: the app calls ``ensure_schema()`` during
startup, so the service is ordered after SQL Server via NSSM's ``DependOnService``. It never
creates the database itself.

Run it directly to check it works before installing:
    .\\.venv\\Scripts\\python.exe -m data_service.service
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import uvicorn

from .config import load

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s  %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging(log_dir: Path) -> Path:
    """Attach a rotating file handler and a stream handler to the root logger."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "data_service.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in list(root.handlers):
        if getattr(handler, "_mctrader", False):
            return log_path

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._mctrader = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler._mctrader = True  # type: ignore[attr-defined]
    root.addHandler(stream_handler)

    return log_path


def main() -> int:
    settings = load()
    log_path = configure_logging(Path(__file__).resolve().parent.parent / "logs")
    log = logging.getLogger("data_service.service")

    log.info("starting McTrader_Market_Data_Service on %s:%s (database=%s, pid=%d)",
             settings.host, settings.port, settings.db_database, os.getpid())
    log.info("log file: %s", log_path)
    # Which path found the key, never the key. A service running as LocalSystem cannot read the
    # per-user keyring, so "source=none" here is the first and clearest symptom.
    log.info("EODHD key source: %s%s", settings.eodhd_key_source,
             "" if settings.eodhd_key_source != "none" else
             " -- no key found: set EODHD_API_KEY in the service environment "
             "(install-service.ps1 -EodhdApiKey). The keyring is per user and a LocalSystem "
             "service cannot see it.")

    try:
        uvicorn.run(
            "data_service.api:app",
            host=settings.host,
            port=settings.port,
            log_level="info",
            log_config=None,
            # One worker, deliberately: the price cache holds an in-memory whole-market snapshot
            # and a per-request lock, so a second worker would mean two caches and two 100-unit
            # bulk pulls for one market.
            workers=1,
        )
    except Exception:  # noqa: BLE001 - a fatal start must exit non-zero so SCM retries
        log.exception("fatal error; exiting non-zero so the service manager restarts us")
        return 1

    log.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
