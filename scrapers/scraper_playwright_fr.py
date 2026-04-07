"""
France market scraper via Playwright (Chromium headless).

Sites : courir.com, footlocker.fr, snipes.com/fr, sportsdirect.com
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import List
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright

from scrapers.utils.normalize import parse_price_to_eur

logger = logging.getLogger(__name__)

_RE_DECIMAL = re.compile(r"\d+[.,]\d{2}")
_RE_EUR_SUFFIX = re.compile(r"\d{2,3}\s*€")

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--mute-audio",
    "--no-first-run",
    "--js-flags=--max-old-space-size=256",
    "--renderer-process-limit=1",
]


class PlaywrightFranceScraper:
    """Scrape les prix FR avec rendu JavaScript (Playwright sync API)."""

    def _parse_prices_from_text(self, text: str) -> List[float]:
        out: List[float] = []
        seen: set[float] = set()
        for m in _RE_DECIMAL.finditer(text):
            p = parse_price_to_eur(m.group(0) + "\u00a0€", currency_hint="EUR")
            if p is None or p in seen or p < 15 or p > 2000:
                continue
            seen.add(p)
            out.append(p)
        for m in _RE_EUR_SUFFIX.finditer(text):
            p = parse_price_to_eur(m.group(0), currency_hint="EUR")
            if p is None or p in seen or p < 15 or p > 2000:
                continue
            seen.add(p)
            out.append(p)
        return out

    def _is_relevant_context(self, text: str, brand: str, model: str) -> bool:
        ctx = (text or "").lower()
        b = (brand or "").strip().lower()
        model_first = (model or "").strip().lower().split()[0] if (model or "").strip() else ""
        return (b and b in ctx) or (model_first and model_first in ctx)

    def _parse_prices_from_page_nodes(self, page, brand: str, model: str) -> List[float]:
        prices: List[float] = []
        seen: set[float] = set()
        selectors = '[class*="price"], [data-testid*="price"], [itemprop="price"], [data-price]'
        try:
            nodes = page.query_selector_all(selectors)
        except Exception:
            nodes = []
        for node in nodes:
            relevant = False
            cur = node
            for _ in range(3):
                try:
                    cur = cur.query_selector("xpath=..")
                except Exception:
                    cur = None
                if cur is None:
                    break
                try:
                    ctx = cur.inner_text()
                except Exception:
                    ctx = ""
                if self._is_relevant_context(ctx, brand, model):
                    relevant = True
                    break
            if not relevant:
                continue
            try:
                raw = cur.inner_text() if cur is not None else node.inner_text()
            except Exception:
                raw = ""
            if not raw:
                continue
            for p in self._parse_prices_from_text(raw):
                if p in seen:
                    continue
                seen.add(p)
                prices.append(p)
        return prices

    def _goto_parse(
        self,
        page,
        url: str,
        wait_selector: str,
        label: str,
        collected: List[float],
        brand: str,
        model: str,
        *,
        max_retries: int = 2,
    ) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=40_000)
                try:
                    page.wait_for_selector(wait_selector, timeout=6_000)
                except Exception:
                    logger.debug("%s: timeout sélecteur, extraction body quand même", label)
                collected.extend(self._parse_prices_from_page_nodes(page, brand, model))
                return
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.debug(
                        "Playwright %s tentative %d/%d échouée (%s) — retry dans %ds",
                        label, attempt, max_retries, e, wait,
                    )
                    time.sleep(wait)
        logger.debug("Playwright %s échec définitif: %s", label, last_exc)

    def _try_courir(self, page, brand: str, model: str, collected: List[float]) -> None:
        q = quote_plus(f"{brand} {model}")
        url = f"https://www.courir.com/fr/recherche/?q={q}"
        self._goto_parse(page, url, '[class*="price"]', "Courir", collected, brand, model)

    def _try_footlocker(self, page, brand: str, model: str, collected: List[float]) -> None:
        q = quote_plus(f"{brand} {model}")
        url = f"https://www.footlocker.fr/fr/search?query={q}"
        self._goto_parse(page, url, '[class*="price"]', "Footlocker", collected, brand, model)

    def _try_snipes(self, page, brand: str, model: str, collected: List[float]) -> None:
        q = quote_plus(f"{brand} {model}")
        url = f"https://www.snipes.com/fr/search?q={q}"
        self._goto_parse(page, url, '[class*="price"]', "Snipes", collected, brand, model)

    def _try_sportsdirect(self, page, brand: str, model: str, collected: List[float]) -> None:
        q = quote_plus(f"{brand} {model}")
        url = f"https://www.sportsdirect.com/search/?q={q}"
        self._goto_parse(page, url, '[class*="price"]', "Sportsdirect", collected, brand, model)

    def scrape_model(self, brand: str, model: str) -> List[float]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model:
            return []

        collected: List[float] = []
        browser = None
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(
                        headless=True,
                        args=_CHROMIUM_ARGS,
                    )
                    context = browser.new_context(
                        user_agent=IPHONE_UA,
                        viewport={"width": 390, "height": 844},
                    )
                    page = context.new_page()

                    steps = [
                        self._try_courir,
                        self._try_footlocker,
                        self._try_snipes,
                        self._try_sportsdirect,
                    ]
                    for j, step_fn in enumerate(steps):
                        step_fn(page, brand, model, collected)
                        if j < len(steps) - 1:
                            time.sleep(random.uniform(0.45, 0.55))
                finally:
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            logger.debug("browser.close() failed", exc_info=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("PlaywrightFranceScraper scrape_model failed: %s", e)

        seen: set[float] = set()
        unique: List[float] = []
        for x in collected:
            if x in seen:
                continue
            seen.add(x)
            unique.append(x)
        return unique
