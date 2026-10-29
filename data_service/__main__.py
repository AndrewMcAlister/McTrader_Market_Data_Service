"""Run the service: ``python -m data_service``."""
from __future__ import annotations

import logging

import uvicorn

from .config import load


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = load()
    uvicorn.run(
        "data_service.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        # One worker, deliberately. The price cache holds an in-memory whole-market snapshot
        # and a per-request lock; a second worker would mean two caches and two bulk pulls
        # for one market. Scale out only after the cache moves to a shared store.
        workers=1,
    )


if __name__ == "__main__":
    main()
