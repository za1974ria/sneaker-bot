"""Tier definitions for multi-source collection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.scrapers.core import SCRAPERS as CORE_SCRAPERS
from app.scrapers.extension import SCRAPERS as EXTENSION_SCRAPERS
from app.scrapers.long_tail import SCRAPERS as LONG_TAIL_SCRAPERS

ScraperFn = Callable[[], list[dict[str, Any]]]

TIERS: dict[str, dict[str, ScraperFn]] = {
    "core": CORE_SCRAPERS,
    "extension": EXTENSION_SCRAPERS,
    "long_tail": LONG_TAIL_SCRAPERS,
}

