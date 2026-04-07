"""LONG TAIL tier scraper registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _noop_scraper() -> list[dict[str, Any]]:
    return []


SCRAPERS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "goat_long_tail": _noop_scraper,
    "stockx_long_tail": _noop_scraper,
    "ebay_long_tail": _noop_scraper,
    "vinted_long_tail": _noop_scraper,
    "klekt_long_tail": _noop_scraper,
    "laced_long_tail": _noop_scraper,
    "restocks_long_tail": _noop_scraper,
    "snkrs_long_tail": _noop_scraper,
    "slamjam_long_tail": _noop_scraper,
    "end_clothing_long_tail": _noop_scraper,
    "sivasdescalzo_long_tail": _noop_scraper,
    "sneakersnstuff_long_tail": _noop_scraper,
    "solebox_long_tail": _noop_scraper,
    "extrabutter_long_tail": _noop_scraper,
    "packers_long_tail": _noop_scraper,
    "atmos_long_tail": _noop_scraper,
    "bodega_long_tail": _noop_scraper,
    "apb_long_tail": _noop_scraper,
    "hanon_long_tail": _noop_scraper,
    "overkill_long_tail": _noop_scraper,
    "afew_long_tail": _noop_scraper,
}

