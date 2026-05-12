"""
Base scraper shared by market-specific scrapers.

Uses an iPhone-like user agent (camouflage) and a retry-enabled requests session.
"""

from __future__ import annotations

import os
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
    USER_AGENT_POOL = (
        USER_AGENT,
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    )
    REFERER_POOL = (
        "https://www.google.fr/",
        "https://www.bing.com/",
        "https://duckduckgo.com/",
    )

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
        proxy_url = os.getenv("PROXY_URL", "").strip()
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

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

    def _rotating_headers(self) -> dict[str, str]:
        """Headers stealth simples (rotation UA + referer) pour limiter les blocages basiques."""
        return {
            **self.DEFAULT_HEADERS,
            "User-Agent": random.choice(self.USER_AGENT_POOL),
            "Referer": random.choice(self.REFERER_POOL),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "DNT": "1",
        }

    def _fetch_with_exponential_backoff(self, url: str, *, timeout: float, attempts: int) -> requests.Response | None:
        """
        Retry intelligent: tentative courte + backoff exponentiel.
        Compat: renvoie Response ou None (même logique appelante).
        """
        from scrapers.utils.fetch_retry import fetch_with_retry

        last_resp: requests.Response | None = None
        total = max(1, int(attempts))
        for idx in range(total):
            # Rotation légère des headers à chaque tentative.
            self.session.headers.update(self._rotating_headers())
            resp = fetch_with_retry(
                self.session,
                url,
                timeout=float(timeout),
                attempts=1,
                allow_redirects=True,
            )
            if resp is not None:
                last_resp = resp
                if resp.status_code not in (401, 403, 429, 500, 502, 503, 504):
                    return resp
            if idx < total - 1:
                # 0.45, 0.90, 1.80... (cap 3.5s) pour rester rapide.
                sleep_s = min(3.5, 0.45 * (2 ** idx))
                time.sleep(sleep_s + random.uniform(0.0, 0.2))
        return last_resp

    def fetch_html(self, url: str, *, raise_for_status: bool = True) -> str:
        """
        Fetch raw HTML from a URL with retries, timeout and jitter delay.

        Certaines pages liste renvoient 404 avec du HTML partiellement exploitable :
        passer raise_for_status=False pour quand même parser le corps.
        """
        self._sleep_jitter()
        resp = self._fetch_with_exponential_backoff(
            url,
            timeout=float(self.TIMEOUT_SEC),
            attempts=3,
        )
        if resp is None:
            if raise_for_status:
                raise requests.HTTPError(f"GET failed after retries: {url}")
            return ""
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

