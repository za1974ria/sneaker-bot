"""Run all CORE scrapers with per-source fault tolerance."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from scrapers.core.nike import scrape_nike

logger = logging.getLogger(__name__)

CORE_SCRAPERS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "nike": scrape_nike,
}


def run_all_core() -> list[dict[str, Any]]:
    """
    Execute all core scrapers independently.
    Never crashes globally if one source fails.
    """
    rows: list[dict[str, Any]] = []
    for source_name, fn in CORE_SCRAPERS.items():
        try:
            batch = fn()
            if not isinstance(batch, list):
                logger.warning("source=%s invalid payload type", source_name)
                continue
            rows.extend(batch)
            logger.info("source=%s ok rows=%d", source_name, len(batch))
        except Exception as e:  # noqa: BLE001
            logger.exception("source=%s failed: %s", source_name, e)
            continue
    return rows

