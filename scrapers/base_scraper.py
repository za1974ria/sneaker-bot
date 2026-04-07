"""
Base scraper shared by market-specific scrapers.

Uses an iPhone-like user agent (camouflage) and a retry-enabled requests session.
"""

from __future__ import annotations

import random
import statistics
import time
from abc import ABC, abstractmethod
from typing import List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BaseScraper(ABC):
    """
    Base class for scraping sneaker prices from public e-commerce pages.
    """

    # Camouflage iPhone user-agent
    USER_AGENT = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    )

    DEFAULT_HEADERS = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "fr-FR",
        "Referer": "https://www.google.fr/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }

    MIN_DELAY_SEC = 2.5
    MAX_DELAY_SEC = 5.5
    TIMEOUT_SEC = 10
    RETRY_TOTAL = 3
    PRICE_RANGES = {
        "Nike": (55.0, 300.0),
        "Adidas": (45.0, 250.0),
        "New Balance": (55.0, 280.0),
        "Salomon": (90.0, 280.0),
        "Asics": (55.0, 250.0),
        "Puma": (40.0, 200.0),
        "Reebok": (40.0, 200.0),
        "Vans": (45.0, 180.0),
        "Converse": (40.0, 180.0),
        "On Running": (90.0, 300.0),
        "default": (35.0, 250.0),
    }
    # Backward compatibility for modules still using old constants.
    PRICE_FLOORS = {brand: bounds[0] for brand, bounds in PRICE_RANGES.items()}
    PRICE_CEIL = max(bounds[1] for bounds in PRICE_RANGES.values())

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)

        retry = Retry(
            total=self.RETRY_TOTAL,
            connect=self.RETRY_TOTAL,
            read=self.RETRY_TOTAL,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _sleep_jitter(self) -> None:
        """Random delay between requests to reduce bot detection."""
        time.sleep(random.uniform(self.MIN_DELAY_SEC, self.MAX_DELAY_SEC))

    def fetch_html(self, url: str, *, raise_for_status: bool = True) -> str:
        """
        Fetch raw HTML from a URL with retries, timeout and jitter delay.

        Certaines pages liste renvoient 404 avec du HTML partiellement exploitable :
        passer raise_for_status=False pour quand même parser le corps.
        """
        self._sleep_jitter()
        resp = self.session.get(url, timeout=self.TIMEOUT_SEC)
        if raise_for_status:
            resp.raise_for_status()
        return resp.text

    def is_price_in_range(self, brand: str, price: float) -> bool:
        brand_name = (brand or "").strip()
        min_price, max_price = self.PRICE_RANGES.get(brand_name, self.PRICE_RANGES["default"])
        return min_price <= price <= max_price

    def filter_prices(self, brand: str, prices: List[float]) -> List[float]:
        """Filter by brand range and median-based outlier bounds."""
        in_range = [price for price in prices if self.is_price_in_range(brand, price)]
        if not in_range:
            return []
        median_price = statistics.median(in_range)
        low_bound = median_price * 0.4
        high_bound = median_price * 2.2
        return [price for price in in_range if low_bound <= price <= high_bound]

    @abstractmethod
    def scrape_model(self, brand: str, model: str) -> List[float]:
        """
        Scrape prices for a given brand + model.

        Returns:
            List[float]: all detected prices for the product on the target market.
        """
        raise NotImplementedError

