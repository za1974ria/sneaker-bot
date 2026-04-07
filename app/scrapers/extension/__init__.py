"""EXTENSION tier scraper registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _noop_scraper() -> list[dict[str, Any]]:
    return []


SCRAPERS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "adidas_extension": _noop_scraper,
    "new_balance_extension": _noop_scraper,
    "puma_extension": _noop_scraper,
    "asics_extension": _noop_scraper,
    "jordan_extension": _noop_scraper,
    "reebok_extension": _noop_scraper,
    "salomon_extension": _noop_scraper,
    "on_running_extension": _noop_scraper,
    "converse_extension": _noop_scraper,
    "courir_extension": _noop_scraper,
    "jd_sports_extension": _noop_scraper,
    "footlocker_extension": _noop_scraper,
    "snipes_extension": _noop_scraper,
    "bstn_extension": _noop_scraper,
    "size_extension": _noop_scraper,
    "offspring_extension": _noop_scraper,
    "kikikickz_extension": _noop_scraper,
    "zalando_extension": _noop_scraper,
    "spartoo_extension": _noop_scraper,
    "sarenza_extension": _noop_scraper,
}

