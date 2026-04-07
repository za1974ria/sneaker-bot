"""
Scrapers Tier 1 supplémentaires (France) — format enrichi list[dict].

Chaque entrée : brand, model, price, source, url, currency.
Les prix sont extraits via la même heuristique HTML que FranceScraper (réutilisation du parseur).
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any
from urllib.parse import quote_plus

import requests
import urllib3
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper
from scrapers.hypermarches import HYPERMARCHE_SCRAPER_CLASSES_REGISTERED
from scrapers.utils.normalize import clean_text, parse_price_to_eur

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": BaseScraper.USER_AGENT,
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.fr/",
    "Connection": "keep-alive",
}

UA_POOL = (
    BaseScraper.USER_AGENT,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
)

_SESSION_TLS = threading.local()
_PARSE_SCRAPER: Any = None


def _session() -> requests.Session:
    """Session par thread (``requests.Session`` n’est pas thread-safe)."""
    s = getattr(_SESSION_TLS, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        proxy_url = os.getenv("PROXY_URL", "").strip()
        if proxy_url:
            s.proxies.update({"http": proxy_url, "https": proxy_url})
        _SESSION_TLS.session = s
    return s


def _parse_scraper():
    global _PARSE_SCRAPER
    if _PARSE_SCRAPER is None:
        from scrapers.scraper_fr import FranceScraper

        _PARSE_SCRAPER = FranceScraper()
    return _PARSE_SCRAPER


def tier1_random_sleep() -> None:
    time.sleep(random.uniform(0.25, 0.35))


def hits_to_prices(hits: list[dict]) -> list[float]:
    out: list[float] = []
    for h in hits:
        try:
            p = float(h["price"])
            if p > 0:
                out.append(p)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _build_hit(
    brand: str,
    model: str,
    price: float,
    source: str,
    url: str,
) -> dict[str, Any]:
    return {
        "brand": brand,
        "model": model,
        "price": round(float(price), 2),
        "source": source,
        "url": url,
        "currency": "EUR",
    }


def _extract_prices_from_page(html: str, brand: str, model: str) -> list[float]:
    """Réutilise l’extracteur FranceScraper (prix pertinents marque/modèle)."""
    try:
        return _parse_scraper()._extract_prices_from_html(html, brand, model)
    except Exception as e:  # noqa: BLE001
        logger.debug("tier1 extract: %s", e)
        return []


def _tier1_listing_price_fallback(html: str, brand: str, model: str) -> list[float]:
    """
    Pages de résultats : l’arbre DOM ne passe souvent pas is_relevant_product (prix et titre non liés
    sur 3 niveaux). On extrait les prix sans filtre DOM puis on applique filter_prices (même logique
    que le reste du bot), si la marque apparaît dans la page.
    """
    brand = (brand or "").strip()
    model = (model or "").strip()
    if not brand:
        return []
    brand_l = clean_text(brand).lower()
    low = (html or "").lower()
    if brand_l not in low:
        return []
    mtoks = [t for t in clean_text(model).lower().split() if len(t) >= 2]
    weak = {"low", "high", "mid", "og", "pro", "women", "men", "unisex", "premium"}
    strong = [t for t in mtoks if t not in weak][:4]
    if strong and not any(t in low for t in strong):
        return []
    try:
        scraper = _parse_scraper()
        raw = scraper._extract_prices_from_html(html, None, None)
        if not raw:
            return []
        filtered = scraper.filter_prices(brand, raw)
        return filtered[:12] if filtered else []
    except Exception as e:  # noqa: BLE001
        logger.debug("tier1 listing fallback: %s", e)
        return []


def _fallback_meta_prices(html: str, brand: str, model: str, max_n: int = 12) -> list[float]:
    """Fallback léger si l’extracteur principal ne trouve rien (meta / itemprop)."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[float] = set()
    out: list[float] = []
    blob = clean_text(f"{brand} {model}").lower()
    brand_l = clean_text(brand).lower()
    model_tokens = [t for t in model.lower().split() if len(t) > 2][:3]

    for tag in soup.select('[itemprop="price"], meta[property="product:price:amount"], [data-price]'):
        raw = tag.get("content") or tag.get("data-price") or tag.get_text(" ", strip=True)
        p = parse_price_to_eur(raw, currency_hint="EUR")
        if p is None or p in seen:
            continue
        parent = tag
        ctx = ""
        for _ in range(4):
            parent = getattr(parent, "parent", None)
            if parent is None:
                break
            try:
                ctx = parent.get_text(" ", strip=True).lower()
            except Exception:
                ctx = ""
            if ctx:
                break
        if brand_l and brand_l not in ctx:
            continue
        if model_tokens and not any(t in ctx for t in model_tokens):
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= max_n:
            break
    return out


def extract_search_result_prices(html: str, brand: str, model: str) -> list[float]:
    """
    Chaîne d’extraction pour une page de résultats (HTML brut) : strict → meta → filtre marque → fallback listing.
    Réutilisable par des scrapers Playwright (hors session requests).
    """
    brand = (brand or "").strip()
    model = (model or "").strip()
    if not brand or not model:
        return []
    prices = _extract_prices_from_page(html, brand, model)
    if not prices:
        prices = _fallback_meta_prices(html, brand, model)
    if prices:
        try:
            prices = _parse_scraper().filter_prices(brand, prices)
        except Exception:  # noqa: BLE001
            pass
    if not prices:
        prices = _tier1_listing_price_fallback(html, brand, model)
    return prices


def _scrape_search_urls(
    source_name: str,
    brand: str,
    model: str,
    urls: list[str],
    *,
    verify_ssl: bool = True,
) -> list[dict]:
    brand = (brand or "").strip()
    model = (model or "").strip()
    if not brand or not model:
        return []
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning("[%s] Vérification SSL désactivée pour ces URLs (certificat / CDN)", source_name)
    brand_l = clean_text(brand).lower()
    sess = _session()
    hits: list[dict] = []
    tried_urls: list[str] = []
    for url in urls:
        try:
            tried_urls.append(url)
            tier1_random_sleep()
            sess.headers.update({"User-Agent": random.choice(UA_POOL)})
            r = sess.get(url, timeout=14, allow_redirects=True, verify=verify_ssl)
            if r.status_code >= 500:
                continue
            if r.status_code == 404:
                continue
            if r.status_code in (401, 403) and brand_l not in (r.text or "").lower():
                continue
            prices = extract_search_result_prices(r.text, brand, model)
            final_url = str(r.url)
            for p in prices[:8]:
                hits.append(_build_hit(brand, model, p, source_name, final_url))
            if hits:
                return hits
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] Erreur scraping %s %s: %s", source_name, brand, model, e)
    if hits:
        return hits
    # Fallback JS rendering for SPA/WAF-heavy shops.
    try:
        from scrapers.hypermarches import _fetch_html_playwright
    except Exception:
        return hits
    for url in tried_urls:
        try:
            tier1_random_sleep()
            html = _fetch_html_playwright(url, source_name=source_name)
            if not html:
                continue
            prices = extract_search_result_prices(html, brand, model)
            for p in prices[:8]:
                hits.append(_build_hit(brand, model, p, source_name, url))
            if hits:
                return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] Fallback Playwright %s %s: %s", source_name, brand, model, e)
    return hits


def _q(brand: str, model: str) -> str:
    return quote_plus(f"{brand} {model}")


# --- Classes Tier 1 (une par site manquant) ---------------------------------


class JdSportsScraper:
    SOURCE_NAME = "JD Sports"
    BASE_URL = "https://www.jdsports.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/search/{q}/",
            f"{self.BASE_URL}/catalogsearch/result/?q={q}",
            f"{self.BASE_URL}/search?q={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


class ChausportScraper:
    SOURCE_NAME = "Chausport"
    BASE_URL = "https://www.chausport.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/recherche?q={q}",
            f"{self.BASE_URL}/search?q={q}",
        ]
        return _scrape_search_urls(
            self.SOURCE_NAME, brand, model, urls, verify_ssl=False
        )


class SarenzaScraper:
    SOURCE_NAME = "Sarenza"
    BASE_URL = "https://www.sarenza.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/search?q={q}",
            f"{self.BASE_URL}/recherche?q={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


class SpartooScraper:
    SOURCE_NAME = "Spartoo"
    BASE_URL = "https://www.spartoo.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/mobile/search.php?search={q}",
            f"{self.BASE_URL}/Search.php?p=1&st={q}",
            f"{self.BASE_URL}/search.php?search={q}",
        ]
        hits = _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)
        if hits:
            return hits
        # Fallback ScrapingBee (Spartoo bloque les requests directes)
        bee_key = os.getenv("SCRAPINGBEE_API_KEY", "").strip()
        if not bee_key:
            return []
        bee_url = f"{self.BASE_URL}/search/?search={q}"
        try:
            r = requests.get(
                "https://app.scrapingbee.com/api/v1/",
                params={"api_key": bee_key, "url": bee_url, "render_js": "false", "country_code": "fr"},
                timeout=30,
            )
            if r.status_code == 200 and r.text:
                prices = extract_search_result_prices(r.text, brand, model)
                return [_build_hit(brand, model, p, self.SOURCE_NAME, bee_url) for p in prices[:8]]
        except Exception as e:
            logger.warning("[Spartoo] ScrapingBee fallback erreur: %s", e)
        return []


class ZalandoScraper:
    SOURCE_NAME = "Zalando"
    BASE_URL = "https://www.zalando.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/catalogue/?q={q}",
            f"{self.BASE_URL}/search/?q={q}",
        ]
        hits = _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)
        if hits:
            return hits
        # Targeted anti-bot fallback for Zalando only.
        try:
            from scrapers.anti_bot_diag import (
                detect_block_reason,
                dump_snapshot,
                fetch_playwright_stealth,
                fetch_via_scraperapi,
                fetch_with_rotating_headers,
            )

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
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] anti-bot fallback err: %s", self.SOURCE_NAME, e)
        return hits


class Basket4BallersScraper:
    SOURCE_NAME = "Basket4Ballers"
    BASE_URL = "https://www.basket4ballers.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/fr/recherche?search_query={q}",
            f"{self.BASE_URL}/fr/recherche?controller=search&search_query={q}",
            f"{self.BASE_URL}/recherche?controller=search&orderby=position&orderway=desc&search_query={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


class GoSportScraper:
    SOURCE_NAME = "Go Sport"
    BASE_URL = "https://www.go-sport.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/catalogsearch/result/?q={q}",
            f"{self.BASE_URL}/recherche?q={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


class IntersportScraper:
    SOURCE_NAME = "Intersport"
    BASE_URL = "https://www.intersport.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/sn-IS/?text={q}",
            f"{self.BASE_URL}/sn-IS/?q={q}",
            f"{self.BASE_URL}/recherche?q={q}",
            f"{self.BASE_URL}/search?q={q}",
            f"{self.BASE_URL}/recherche?text={q}",
        ]
        hits = _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)
        if len(hits) > 1:
            return hits
        # ScrapingBee render_js=true en priorité (Intersport SPA — Playwright timeout systématique)
        bee_key = os.getenv("SCRAPINGBEE_API_KEY", "").strip()
        if bee_key:
            bee_url = f"{self.BASE_URL}/sn-IS/?text={q}"
            try:
                r = requests.get(
                    "https://app.scrapingbee.com/api/v1/",
                    params={"api_key": bee_key, "url": bee_url, "render_js": "true", "country_code": "fr", "wait": "2000"},
                    timeout=45,
                )
                if r.status_code == 200 and r.text:
                    prices = extract_search_result_prices(r.text, brand, model)
                    if prices:
                        return [_build_hit(brand, model, p, self.SOURCE_NAME, bee_url) for p in prices[:8]]
            except Exception as e:  # noqa: BLE001
                logger.warning("[Intersport] ScrapingBee erreur: %s", e)
        return hits


class Sport2000Scraper:
    SOURCE_NAME = "Sport 2000"
    BASE_URL = "https://www.sport2000.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/recherche?search={q}",
            f"{self.BASE_URL}/search?q={q}",
            f"{self.BASE_URL}/recherche?q={q}",
        ]
        hits = _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)
        if hits:
            return hits
        try:
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
                prices = extract_search_result_prices(html, brand, model)
                for p in prices[:8]:
                    hits.append(_build_hit(brand, model, p, self.SOURCE_NAME, url))
                if hits:
                    return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] playwright fallback err: %s", self.SOURCE_NAME, e)
        return hits


class DecathlonScraper:
    SOURCE_NAME = "Decathlon"
    BASE_URL = "https://www.decathlon.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/tous-les-sports/q?search={q}",
            f"{self.BASE_URL}/recherche?q={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


class KickzFrScraper:
    SOURCE_NAME = "Kickz FR"
    BASE_URL = "https://www.kickz.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/fr/search?q={q}",
            f"{self.BASE_URL}/fr/catalogsearch/result/?q={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


class SnipesFrFrScraper:
    """Alias snipes.com/fr-fr (même logique que Snipes existant, URL alternative)."""

    SOURCE_NAME = "Snipes (fr-fr)"
    BASE_URL = "https://www.snipes.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/fr-fr/search?q={q}",
            f"{self.BASE_URL}/fr-fr/recherche?q={q}",
            f"{self.BASE_URL}/fr-fr/search?query={q}",
        ]
        hits = _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)
        if hits:
            return hits
        try:
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
                prices = extract_search_result_prices(html, brand, model)
                for p in prices[:8]:
                    hits.append(_build_hit(brand, model, p, self.SOURCE_NAME, url))
                if hits:
                    return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] playwright fallback err: %s", self.SOURCE_NAME, e)
        return hits


class FootshopScraper:
    SOURCE_NAME = "Footshop"
    BASE_URL = "https://www.footshop.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/search?q={q}",
            f"{self.BASE_URL}/catalogsearch/result/?q={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


# E-com Tier 1 (sans grande distribution — voir ``hypermarches``).
TIER1_ECOM_EXTRA_SCRAPER_CLASSES: tuple[tuple[str, type], ...] = (
    ("Zalando", ZalandoScraper),  # Réactivé 2026-04-07 — proxy IPRoyal injecté
    # ("JD Sports", JdSportsScraper),  # Désactivé 2026-04-07 — Playwright timeout 100%, bloqué même via proxy
    ("Intersport", IntersportScraper),
    ("Spartoo", SpartooScraper),
)


def _tier2_registered() -> tuple[tuple[str, type], ...]:
    from scrapers.tier2_sites import TIER2_SCRAPER_CLASSES_REGISTERED

    return TIER2_SCRAPER_CLASSES_REGISTERED


# Registre complet : e-com + hypers actifs + Tier 2 premium (``scrapers/tier2_sites.py``).
# Les classes Tier 1 désactivées restent dans ce fichier mais hors de ces tuples.
TIER1_EXTRA_SCRAPER_CLASSES: tuple[tuple[str, type], ...] = (
    TIER1_ECOM_EXTRA_SCRAPER_CLASSES
    + _tier2_registered()
    + HYPERMARCHE_SCRAPER_CLASSES_REGISTERED
)
