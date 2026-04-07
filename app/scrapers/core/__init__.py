"""CORE tier scraper registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.scrapers.core.adidas import scrape as scrape_adidas
from app.scrapers.core.new_balance import scrape as scrape_new_balance
from app.scrapers.core.nike import scrape as scrape_nike
from app.scrapers.core.puma import scrape as scrape_puma


def _noop_scraper() -> list[dict[str, Any]]:
    return []


SCRAPERS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "nike_core": scrape_nike,
    "adidas_core": scrape_adidas,
    "new_balance_core": scrape_new_balance,
    "puma_core": scrape_puma,
    "asics_core": _noop_scraper,
    "jordan_core": _noop_scraper,
    "reebok_core": _noop_scraper,
    "salomon_core": _noop_scraper,
    "on_running_core": _noop_scraper,
    "converse_core": _noop_scraper,
}

