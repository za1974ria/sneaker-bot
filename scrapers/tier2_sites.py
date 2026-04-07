"""
Sources Tier 2 premium — format identique au Tier 1 : list[dict] par hit.

Chaque entrée : brand, model, price, source, url, currency (EUR).

User-Agent : ``BaseScraper.USER_AGENT`` (iPhone / Safari, aligné sur le projet).

Délais : 1,5–3 s entre requêtes. Aucune exception ne remonte au pipeline (retour ``[]``).
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any
from urllib.parse import quote_plus

import requests

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


def tier2_sleep() -> None:
    time.sleep(random.uniform(0.45, 0.55))


def _headers_json() -> dict[str, str]:
    return {
        "User-Agent": BaseScraper.USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Referer": "https://www.google.fr/",
    }


def _headers_html() -> dict[str, str]:
    return {
        "User-Agent": BaseScraper.USER_AGENT,
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.fr/",
        "Connection": "keep-alive",
    }


def _extract_hits(html: str, brand: str, model: str, source_name: str, url: str) -> list[dict[str, Any]]:
    from scrapers.tier1_sites import _build_hit, extract_search_result_prices

    prices = extract_search_result_prices(html, brand, model)
    if not prices:
        return []
    return [_build_hit(brand, model, p, source_name, url) for p in prices[:8]]


def _requests_then_playwright(
    source_name: str,
    brand: str,
    model: str,
    urls: list[str],
) -> list[dict[str, Any]]:
    from scrapers.hypermarches import _fetch_html_playwright

    brand = (brand or "").strip()
    model = (model or "").strip()
    if not brand or not model:
        return []

    for url in urls:
        try:
            tier2_sleep()
            r = requests.get(url, headers=_headers_html(), timeout=20, allow_redirects=True)
            if r.status_code >= 400:
                continue
            hits = _extract_hits(r.text, brand, model, source_name, str(r.url))
            if hits:
                return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] requests %s: %s", source_name, url, e)

    for url in urls:
        try:
            tier2_sleep()
            html = _fetch_html_playwright(url, source_name=source_name)
            if not html:
                continue
            hits = _extract_hits(html, brand, model, source_name, url)
            if hits:
                return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] Playwright %s: %s", source_name, url, e)
    return []


def _playwright_only(
    source_name: str,
    brand: str,
    model: str,
    urls: list[str],
) -> list[dict[str, Any]]:
    from scrapers.hypermarches import _fetch_html_playwright

    brand = (brand or "").strip()
    model = (model or "").strip()
    if not brand or not model:
        return []

    for url in urls:
        try:
            tier2_sleep()
            html = _fetch_html_playwright(url, source_name=source_name)
            if not html:
                continue
            hits = _extract_hits(html, brand, model, source_name, url)
            if hits:
                return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] Playwright %s: %s", source_name, url, e)
    return []


def _model_tokens(model: str, *, max_tok: int = 4) -> list[str]:
    weak = {"low", "high", "mid", "og", "pro", "women", "men", "unisex", "premium", "the"}
    return [t for t in re.split(r"\W+", (model or "").lower()) if len(t) >= 2 and t not in weak][:max_tok]


def _name_matches_model(name: str, brand: str, model: str, *, skip_brand_in_title: bool = False) -> bool:
    """Si ``skip_brand_in_title`` (PLP mono-marque), on ne vérifie que les tokens du modèle."""
    n = (name or "").lower()
    b = (brand or "").strip().lower()
    if not skip_brand_in_title and b and b not in n:
        return False
    toks = _model_tokens(model)
    if not toks:
        return True
    return any(t in n for t in toks)


class WeThenewScraper:
    SOURCE_NAME = "wethenew.com"
    BASE = "https://wethenew.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model:
            return []
        q = f"{brand} {model}".strip()
        urls = [
            f"{self.BASE}/search?query={quote_plus(q)}",
            f"{self.BASE}/search?query={quote_plus(model)}",
            f"{self.BASE}/search?query={quote_plus(brand)}",
        ]
        try:
            return _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class NikeFrScraper:
    SOURCE_NAME = "nike.com/fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model or brand.lower() != "nike":
            return []
        q = f"{brand} {model}".strip()
        urls = [
            f"https://www.nike.com/fr/w?q={quote_plus(q)}&vst={quote_plus(q)}",
            f"https://www.nike.com/fr/w?q={quote_plus(model)}&vst={quote_plus(model)}",
            f"https://www.nike.com/fr/w?q={quote_plus(brand)}&vst={quote_plus(model)}",
        ]
        try:
            hits = _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
            if hits:
                return hits
            from scrapers.anti_bot_diag import (
                detect_block_reason,
                dump_snapshot,
                fetch_playwright_stealth,
                fetch_via_scraperapi,
                fetch_with_rotating_headers,
            )
            from scrapers.tier1_sites import _build_hit, extract_search_result_prices

            for url in urls:
                html = fetch_with_rotating_headers(url)
                if html:
                    prices = extract_search_result_prices(html, brand, model)
                    for p in prices[:8]:
                        hits.append(_build_hit(brand, model, p, self.SOURCE_NAME, url))
                    if hits:
                        return hits
                reason = detect_block_reason(html)
                if reason or not html:
                    dump_snapshot(self.SOURCE_NAME, url, html, reason or "empty_html")

                html = fetch_playwright_stealth(url, source_name=self.SOURCE_NAME)
                if html:
                    prices = extract_search_result_prices(html, brand, model)
                    for p in prices[:8]:
                        hits.append(_build_hit(brand, model, p, self.SOURCE_NAME, url))
                    if hits:
                        return hits
                reason = detect_block_reason(html)
                if reason or not html:
                    dump_snapshot(self.SOURCE_NAME, url, html, reason or "empty_html")

                html = fetch_via_scraperapi(url)
                if html:
                    prices = extract_search_result_prices(html, brand, model)
                    for p in prices[:8]:
                        hits.append(_build_hit(brand, model, p, self.SOURCE_NAME, url))
                    if hits:
                        return hits
            return hits
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class AdidasFrScraper:
    SOURCE_NAME = "adidas.fr"
    API_URL = "https://www.adidas.fr/api/plp/content-engine/search"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model or brand.lower() != "adidas":
            return []
        try:
            tier2_sleep()
            r = requests.get(
                self.API_URL,
                params={"query": model, "start": 0, "count": 12},
                headers=_headers_json(),
                timeout=20,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            raw = data.get("raw") or data
            il = raw.get("itemList") or {}
            items = il.get("items") or []
            from scrapers.tier1_sites import _build_hit

            out: list[dict[str, Any]] = []
            base = "https://www.adidas.fr"
            for it in items[:8]:
                if not isinstance(it, dict):
                    continue
                name = str(it.get("displayName") or "")
                if not _name_matches_model(name, brand, model, skip_brand_in_title=True):
                    continue
                price = it.get("salePrice") if it.get("salePrice") is not None else it.get("price")
                if price is None:
                    continue
                try:
                    pf = float(str(price).replace(",", "."))
                except (TypeError, ValueError):
                    continue
                if not (25 <= pf <= 600):
                    continue
                link = str(it.get("link") or "")
                url = f"{base}{link}" if link.startswith("/") else f"{base}/{link}"
                out.append(_build_hit(brand, model, pf, self.SOURCE_NAME, url))
                if len(out) >= 5:
                    break
            return out
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class KlektScraper:
    SOURCE_NAME = "klekt.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model:
            return []
        q = f"{brand} {model}".strip()
        urls = [
            f"https://www.klekt.com/search?q={quote_plus(q)}",
            f"https://www.klekt.com/search?q={quote_plus(model)}",
            f"https://www.klekt.com/search?q={quote_plus(brand)}",
            f"https://www.klekt.com/en/search?q={quote_plus(q)}",
        ]
        try:
            hits = _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
            if hits:
                return hits
            from scrapers.anti_bot_diag import detect_block_reason, dump_snapshot
            from scrapers.hypermarches import _fetch_html_playwright

            for url in urls:
                html = _fetch_html_playwright(url, source_name=self.SOURCE_NAME)
                if not html:
                    dump_snapshot(self.SOURCE_NAME, url, "", "empty_html")
                    continue
                reason = detect_block_reason(html)
                if reason:
                    dump_snapshot(self.SOURCE_NAME, url, html, reason)
            return hits
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class NewBalanceFrScraper:
    SOURCE_NAME = "newbalance.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        bl = brand.strip().lower().replace("-", " ")
        if not brand or not model or bl not in ("new balance", "newbalance"):
            return []
        q = quote_plus(model)
        urls = [
            f"https://www.newbalance.fr/search?q={q}",
            f"https://www.newbalance.fr/fr/search?q={q}",
            f"https://www.newbalance.fr/search?text={q}",
        ]
        try:
            return _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class AsicsFrScraper:
    SOURCE_NAME = "asics.com/fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model or brand.lower() != "asics":
            return []
        q = quote_plus(model)
        urls = [
            f"https://www.asics.com/fr/fr-fr/search?q={q}",
            f"https://www.asics.com/fr/fr-fr/catalogsearch/result/?q={q}",
            f"https://www.asics.com/fr/fr-fr/search?query={q}",
            f"https://www.asics.com/fr/fr-fr/search?text={q}",
        ]
        try:
            hits = _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
            if hits:
                return hits
            from scrapers.anti_bot_diag import detect_block_reason, dump_snapshot
            from scrapers.hypermarches import _fetch_html_playwright

            for url in urls:
                html = _fetch_html_playwright(url, source_name=self.SOURCE_NAME)
                if not html:
                    dump_snapshot(self.SOURCE_NAME, url, "", "empty_html")
                    continue
                reason = detect_block_reason(html)
                if reason:
                    dump_snapshot(self.SOURCE_NAME, url, html, reason)
            return hits
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class PumaFrScraper:
    SOURCE_NAME = "puma.com/fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model or brand.lower() != "puma":
            return []
        q = quote_plus(model)
        urls = [
            f"https://www.puma.com/fr/fr/search?q={q}",
            f"https://eu.puma.com/fr/fr/search?q={q}",
            f"https://fr.puma.com/fr/fr/search?q={q}",
        ]
        try:
            return _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class ConverseFrScraper:
    SOURCE_NAME = "converse.com/fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model or brand.lower() != "converse":
            return []
        q = quote_plus(model)
        q_full = quote_plus(f"{brand} {model}")
        # search-show retourne 200 avec les données produit (chargement JS requis → Playwright)
        urls = [
            f"https://www.converse.com/fr/fr/search-show?q={q}",
            f"https://www.converse.com/fr/fr/search-show?q={q_full}",
            f"https://www.converse.com/fr/fr/search?q={q}",
        ]
        try:
            return _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class ReebokFrScraper:
    SOURCE_NAME = "reebok.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model or brand.lower() != "reebok":
            return []
        q = quote_plus(model)
        q_full = quote_plus(f"{brand} {model}")
        # Reebok.fr est hors service — on cible le site global avec Playwright pour les prix EU
        urls = [
            f"https://www.reebok.com/search?q={q_full}",
            f"https://www.reebok.com/search?q={q}",
        ]
        try:
            return _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class VansFrScraper:
    SOURCE_NAME = "vans.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model or brand.lower() != "vans":
            return []
        q = quote_plus(model)
        q_full = quote_plus(f"{brand} {model}")
        # vans.fr redirige vers vans.com/fr-fr — URL directe validée
        urls = [
            f"https://www.vans.com/fr-fr/search?q={q}",
            f"https://www.vans.com/fr-fr/search?q={q_full}",
        ]
        try:
            return _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


class SalomonFrScraper:
    SOURCE_NAME = "salomon.com/fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model or brand.lower() != "salomon":
            return []
        q = quote_plus(model)
        q_full = quote_plus(f"{brand} {model}")
        # salomon.com/fr-fr/s?q= est l'endpoint validé (200 + prix extraits directement)
        urls = [
            f"https://www.salomon.com/fr-fr/s?q={q}",
            f"https://www.salomon.com/fr-fr/s?q={q_full}",
        ]
        try:
            return _requests_then_playwright(self.SOURCE_NAME, brand, model, urls)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] %s %s: %s", self.SOURCE_NAME, brand, model, e)
            return []


TIER2_SCRAPER_CLASSES_REGISTERED: tuple[tuple[str, type], ...] = (
    ("Nike FR", NikeFrScraper),
)
