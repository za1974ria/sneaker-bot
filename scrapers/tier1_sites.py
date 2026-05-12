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
from scrapers.precision_engine import validate_price
from scrapers.hypermarches import HYPERMARCHE_SCRAPER_CLASSES_REGISTERED
from scrapers.utils.fetch_retry import fetch_url_get, fetch_with_retry
from scrapers.utils.normalize import clean_text, parse_price_to_eur

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": BaseScraper.USER_AGENT,
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.fr/",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

UA_POOL = (
    BaseScraper.USER_AGENT,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
)
REFERER_POOL = (
    "https://www.google.fr/",
    "https://www.bing.com/",
    "https://duckduckgo.com/",
)

_SESSION_TLS = threading.local()
_PARSE_SCRAPER: Any = None

# Bulk-pipeline fast mode: disables Playwright fallback (takes 30-60s) in _scrape_search_urls
# and in individual scrapers. Set per-thread by _scrape_tier1_site when self._fast_mode=True.
_PIPELINE_TLS = threading.local()


def _is_pipeline_fast_mode() -> bool:
    """True when the current thread is running in bulk pipeline fast mode (no Playwright)."""
    return bool(getattr(_PIPELINE_TLS, "fast_mode", False))


def _set_pipeline_fast_mode(active: bool) -> None:
    _PIPELINE_TLS.fast_mode = active


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


def _stability_headers(*, source_name: str) -> dict[str, str]:
    """Headers stealth simples + variations selon source."""
    lower = (source_name or "").lower()
    h = {
        **HEADERS,
        "User-Agent": random.choice(UA_POOL),
        "Referer": random.choice(REFERER_POOL),
        "DNT": "1",
    }
    if "foot" in lower and "locker" in lower:
        # Bypass léger 403/akamai.
        h.update(
            {
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    if "zalando" in lower:
        h.update({"Accept-Language": "fr-FR,fr;q=0.95,en;q=0.6"})
    return h


def _fetch_with_stability_backoff(
    sess: requests.Session,
    *,
    source_name: str,
    url: str,
    verify_ssl: bool,
    attempts: int = 4,
    timeout: float = 10.0,
) -> requests.Response | None:
    """
    Retry intelligent + backoff exponentiel + headers tournants.
    Ne remplace pas fetch_with_retry: le complète.
    """
    lower = (source_name or "").lower()
    for i in range(max(1, attempts)):
        sess.headers.update(_stability_headers(source_name=source_name))
        r = fetch_with_retry(
            sess,
            url,
            timeout=timeout,
            attempts=1,
            allow_redirects=True,
            verify=verify_ssl,
        )
        if r is not None:
            if r.status_code not in (401, 403, 429, 500, 502, 503, 504):
                return r
            # Foot Locker: second essai "léger" pour 403.
            if r.status_code == 403 and "foot" in lower and "locker" in lower:
                sess.headers.update(
                    {
                        **_stability_headers(source_name=source_name),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    }
                )
                r2 = fetch_with_retry(
                    sess,
                    url,
                    timeout=timeout + 1.5,
                    attempts=1,
                    allow_redirects=True,
                    verify=verify_ssl,
                )
                if r2 is not None and r2.status_code < 400:
                    return r2
                r = r2 or r
            if r.status_code == 404:
                return r
        if i < attempts - 1:
            time.sleep(min(3.0, 0.35 * (2 ** i)) + random.uniform(0.0, 0.2))
    return None


def _precision_accept_price(hit: dict[str, Any]) -> bool:
    """
    Filtre conservateur anti-prix suspects.
    Ne rejette qu’en cas de suspicion forte + faible fiabilité.
    """
    try:
        suspicious = bool(hit.get("precision_is_suspicious"))
        conf = float(hit.get("precision_confidence_score") or 0.0)
        rel = float(hit.get("precision_overall_reliability") or 0.0)
    except (TypeError, ValueError):
        return True
    return not (suspicious and conf <= 45.0 and rel <= 35.0)


def _build_hit(
    brand: str,
    model: str,
    price: float,
    source: str,
    url: str,
    *,
    avg_price: float | None = None,
    min_price: float | None = None,
    stock: str = "unknown",
) -> dict[str, Any]:
    price_v = round(float(price), 2)
    avg_v = float(avg_price) if avg_price is not None else price_v
    min_v = float(min_price) if min_price is not None else price_v
    # En pipeline fast mode (bulk scrape), skip validate_price : chaque appel lit 5000 lignes
    # SQLite + entraîne IsolationForest (~5s). Avec N prix × 4 threads parallèles, l'overhead
    # est de 100-200s/marque. Les precision_fields sont remplis lors du scrape individuel (non-bulk).
    if _is_pipeline_fast_mode():
        precision: dict[str, Any] = {
            "confidence_score": None,
            "is_suspicious": False,
            "risk_level": "UNKNOWN",
            "match_quality": "unknown",
            "overall_reliability": None,
            "explanation": "",
            "suggested_price": None,
            "reasoning": "",
        }
    else:
        precision = validate_price(
            {
                "brand": brand,
                "model": model,
                "title": f"{brand} {model}",
                "price": price_v,
                "current_price": price_v,
                "avg_price": avg_v,  # conserve pour compat/future features
                "min_price": min_v,  # conserve pour compat/future features
                "stock": stock,
                "site": source,
                "source": source,
                "market": "FR",
            }
        )
        if bool(precision.get("is_suspicious")):
            logger.warning(
                "[Tier1 Precision] suspicious %s %s source=%s price=%.2f conf=%s risk=%s overall_reliability=%s",
                brand,
                model,
                source,
                price_v,
                precision.get("confidence_score"),
                precision.get("risk_level"),
                precision.get("overall_reliability"),
            )
    return {
        "brand": brand,
        "model": model,
        "price": price_v,
        "source": source,
        "url": url,
        "currency": "EUR",
        "precision_confidence_score": precision.get("confidence_score"),
        "precision_is_suspicious": precision.get("is_suspicious"),
        "precision_risk_level": precision.get("risk_level"),
        "precision_match_quality": precision.get("match_quality"),
        "precision_overall_reliability": precision.get("overall_reliability"),
        "precision_explanation": precision.get("explanation"),
        "precision_suggested_price": precision.get("suggested_price"),
        "precision_reasoning": precision.get("reasoning"),
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
    playwright_fallback: bool = True,
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
    logged_ok = False
    # In bulk pipeline fast mode: 1 attempt + short timeout + max 2 URLs so each site stays
    # under twall even when queued behind other concurrent scrapers.
    # Full mode: 4 attempts with backoff for best quality, all URLs tried.
    _fast = _is_pipeline_fast_mode()
    _attempts = 1 if _fast else 4
    _timeout = 8.0 if _fast else 10.0
    _max_urls = 2 if _fast else len(urls)
    for url in urls[:_max_urls]:
        try:
            tried_urls.append(url)
            r = _fetch_with_stability_backoff(
                sess,
                source_name=source_name,
                url=url,
                verify_ssl=verify_ssl,
                attempts=_attempts,
                timeout=_timeout,
            )
            if r is None:
                continue
            if r.status_code >= 500:
                continue
            if r.status_code == 404:
                continue
            if r.status_code in (401, 403) and brand_l not in (r.text or "").lower():
                continue
            prices = extract_search_result_prices(r.text, brand, model)
            final_url = str(r.url)
            avg_page = (sum(prices) / len(prices)) if prices else None
            min_page = min(prices) if prices else None
            for p in prices[:8]:
                hit = _build_hit(
                    brand,
                    model,
                    p,
                    source_name,
                    final_url,
                    avg_price=avg_page,
                    min_price=min_page,
                )
                if _precision_accept_price(hit):
                    hits.append(hit)
                else:
                    logger.info(
                        "[SCRAPER STABILITY] %s → FILTERED suspicious (confidence=%s, reliability=%s)",
                        source_name,
                        hit.get("precision_confidence_score"),
                        hit.get("precision_overall_reliability"),
                    )
            if hits:
                if not logged_ok:
                    sample = hits[0]
                    logger.info(
                        "[SCRAPER STABILITY] %s → OK (confidence=%s, reliability=%s)",
                        source_name,
                        sample.get("precision_confidence_score"),
                        sample.get("precision_overall_reliability"),
                    )
                    logged_ok = True
                return hits
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] Erreur scraping %s %s: %s", source_name, brand, model, e)
    if hits:
        return hits
    # Fallback JS rendering for SPA/WAF-heavy shops.
    # Also skip in bulk pipeline fast mode (Playwright takes 30-60s, exceeds twall).
    if not playwright_fallback or _is_pipeline_fast_mode():
        return hits
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
            avg_page = (sum(prices) / len(prices)) if prices else None
            min_page = min(prices) if prices else None
            for p in prices[:8]:
                hit = _build_hit(
                    brand,
                    model,
                    p,
                    source_name,
                    url,
                    avg_price=avg_page,
                    min_price=min_page,
                )
                if _precision_accept_price(hit):
                    hits.append(hit)
            if hits:
                if not logged_ok:
                    sample = hits[0]
                    logger.info(
                        "[SCRAPER STABILITY] %s → OK (confidence=%s, reliability=%s)",
                        source_name,
                        sample.get("precision_confidence_score"),
                        sample.get("precision_overall_reliability"),
                    )
                    logged_ok = True
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
            self.SOURCE_NAME, brand, model, urls, verify_ssl=True
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
        # /Search.php?p=1&st= → 404 ; /search.php?search= → reCAPTCHA (2KB)
        # ScrapingBee contourne le reCAPTCHA en priorité.
        bee_key = os.getenv("SCRAPINGBEE_API_KEY", "").strip()
        if bee_key:
            bee_url = f"{self.BASE_URL}/search.php?search={q}"
            try:
                r = fetch_url_get(
                    "https://app.scrapingbee.com/api/v1/",
                    params={"api_key": bee_key, "url": bee_url, "render_js": "false", "country_code": "fr"},
                    timeout=30.0,
                    attempts=2,
                )
                if r is not None and r.status_code == 200 and r.text and len(r.text) > 10000:
                    prices = extract_search_result_prices(r.text, brand, model)
                    if prices:
                        avg_page = sum(prices) / len(prices)
                        min_page = min(prices)
                        return [
                            _build_hit(
                                brand,
                                model,
                                p,
                                self.SOURCE_NAME,
                                bee_url,
                                avg_price=avg_page,
                                min_price=min_page,
                            )
                            for p in prices[:8]
                        ]
            except Exception as e:
                logger.warning("[Spartoo] ScrapingBee erreur: %s", e)
        # Fallback requests direct (fonctionne si IP non bloquée)
        urls = [
            f"{self.BASE_URL}/mobile/search.php?search={q}",
            f"{self.BASE_URL}/search.php?search={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


class ZalandoScraper:
    SOURCE_NAME = "Zalando"
    BASE_URL = "https://www.zalando.fr"

    @staticmethod
    def _zalando_accept_hit(hit: dict[str, Any]) -> bool:
        """
        Filtre plus strict pour Zalando (Akamai/bruit).
        Conserve la compatibilité: accepte un hit propre ou suffisamment fiable.
        """
        try:
            suspicious = bool(hit.get("precision_is_suspicious"))
            conf = float(hit.get("precision_confidence_score") or 0.0)
            rel = float(hit.get("precision_overall_reliability") or 0.0)
            risk = str(hit.get("precision_risk_level") or "").upper().strip()
        except (TypeError, ValueError):
            return False
        # Réduit les faux positifs: on accepte plus facilement les prix cohérents.
        if not suspicious:
            return conf >= 50.0 or rel >= 48.0
        # Cas suspects: accepter si cohérence raisonnable (réduction faux positifs).
        if rel >= 90.0 or conf >= 92.0:
            return True
        if risk == "HIGH":
            return conf >= 88.0 and rel >= 82.0
        return (conf >= 72.0 and rel >= 66.0)

    @staticmethod
    def _zalando_header_profiles() -> list[dict[str, str]]:
        """Profils headers plus agressifs pour contourner Akamai sans casser les flows."""
        return [
            {
                "Accept-Language": "fr-FR,fr;q=0.95,en;q=0.6",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            {
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.5",
                "Sec-CH-UA-Mobile": "?0",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Cache-Control": "max-age=0",
            },
            {
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.4",
                "Priority": "u=0, i",
                "DNT": "1",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
            },
        ]

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        brand_slug = quote_plus((brand or "").strip().lower().replace(" ", "-"))
        urls = [
            f"{self.BASE_URL}/catalogue/?q={q}",
            f"{self.BASE_URL}/search/?q={q}",
            # Fallback Akamai-safe endpoint souvent plus permissif.
            f"{self.BASE_URL}/s/?q={q}",
            f"{self.BASE_URL}/women-home/?q={q}",
            f"{self.BASE_URL}/men-home/?q={q}",
            f"{self.BASE_URL}/outlet/?q={q}",
            f"{self.BASE_URL}/brands/{brand_slug}/?q={q}",
            f"{self.BASE_URL}/brands/{brand_slug}/",
            f"{self.BASE_URL}/chaussures/?q={q}",
            f"{self.BASE_URL}/sport/?q={q}",
            f"{self.BASE_URL}/mobile/?q={q}",
            f"https://m.zalando.fr/search/?q={q}",
            f"https://m.zalando.fr/catalogue/?q={q}",
        ]
        hits = _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)
        if hits:
            logger.info(
                "[ZALANDO OPTIMIZED] → OK (%d produits, conf=%s, rel=%s)",
                len(hits),
                hits[0].get("precision_confidence_score"),
                hits[0].get("precision_overall_reliability"),
            )
            return hits
        # Targeted anti-bot fallback for Zalando only.
        # Désactivé en mode bulk pipeline (fast mode) car Playwright prend 30-60s > twall 35s.
        if _is_pipeline_fast_mode():
            return hits
        blocked_seen = False
        partial_seen = False
        accepted_count = 0
        raw_detected_count = 0
        try:
            from scrapers.anti_bot_diag import (
                detect_block_reason,
                dump_snapshot,
                fetch_playwright_stealth,
                fetch_via_scraperapi,
                fetch_with_rotating_headers,
            )

            sitemap_like_urls = [
                f"{self.BASE_URL}/sitemap.xml",
                f"{self.BASE_URL}/sitemap_index.xml",
                f"{self.BASE_URL}/catalogue/sitemap.xml",
                f"{self.BASE_URL}/sitemap-products.xml",
                f"{self.BASE_URL}/sitemap-product.xml",
                f"{self.BASE_URL}/sitemap-products-1.xml",
                f"{self.BASE_URL}/sitemap-categories.xml",
                f"https://m.zalando.fr/sitemap.xml",
                f"{self.BASE_URL}/robots.txt",
            ]
            sess = _session()
            header_profiles = self._zalando_header_profiles()
            for url in urls + sitemap_like_urls:
                # Rotation plus agressive Akamai: headers + micro-jitter.
                sess.headers.update(_stability_headers(source_name=self.SOURCE_NAME))
                sess.headers.update(random.choice(header_profiles))
                time.sleep(random.uniform(0.12, 0.45))
                r0 = _fetch_with_stability_backoff(
                    sess,
                    source_name=self.SOURCE_NAME,
                    url=url,
                    verify_ssl=True,
                    attempts=3,
                    timeout=9.0,
                )
                html = r0.text if r0 is not None else ""
                if not html:
                    html = fetch_with_rotating_headers(url)
                if html:
                    prices = extract_search_result_prices(html, brand, model)
                    if prices:
                        raw_detected_count += len(prices[:8])
                    avg_page = (sum(prices) / len(prices)) if prices else None
                    min_page = min(prices) if prices else None
                    for p in prices[:8]:
                        hit = _build_hit(
                            brand,
                            model,
                            p,
                            self.SOURCE_NAME,
                            url,
                            avg_price=avg_page,
                            min_price=min_page,
                        )
                        if self._zalando_accept_hit(hit):
                            hits.append(hit)
                            accepted_count += 1
                    if prices and not hits:
                        partial_seen = True
                    if hits:
                        logger.info(
                            "[ZALANDO OPTIMIZED] → OK (%d produits, conf=%s, rel=%s)",
                            len(hits),
                            hits[0].get("precision_confidence_score"),
                            hits[0].get("precision_overall_reliability"),
                        )
                        return hits
                reason = detect_block_reason(html)
                if reason or not html:
                    reason_txt = str(reason or "empty_html").lower()
                    if any(k in reason_txt for k in ("akamai", "captcha", "forbidden", "blocked", "bot")):
                        blocked_seen = True
                        logger.warning("[ZALANDO OPTIMIZED] → BLOCKED (%s)", reason or "empty_html")
                    dump_snapshot(self.SOURCE_NAME, url, html, reason or "empty_html")

                html = fetch_playwright_stealth(url, source_name=self.SOURCE_NAME)
                if html:
                    prices = extract_search_result_prices(html, brand, model)
                    if prices:
                        raw_detected_count += len(prices[:8])
                    avg_page = (sum(prices) / len(prices)) if prices else None
                    min_page = min(prices) if prices else None
                    for p in prices[:8]:
                        hit = _build_hit(
                            brand,
                            model,
                            p,
                            self.SOURCE_NAME,
                            url,
                            avg_price=avg_page,
                            min_price=min_page,
                        )
                        if self._zalando_accept_hit(hit):
                            hits.append(hit)
                            accepted_count += 1
                    if prices and not hits:
                        partial_seen = True
                    if hits:
                        logger.info(
                            "[ZALANDO OPTIMIZED] → OK (%d produits, conf=%s, rel=%s)",
                            len(hits),
                            hits[0].get("precision_confidence_score"),
                            hits[0].get("precision_overall_reliability"),
                        )
                        return hits
                reason = detect_block_reason(html)
                if reason or not html:
                    reason_txt = str(reason or "empty_html").lower()
                    if any(k in reason_txt for k in ("akamai", "captcha", "forbidden", "blocked", "bot")):
                        blocked_seen = True
                        logger.warning("[ZALANDO OPTIMIZED] → BLOCKED (%s)", reason or "empty_html")
                    dump_snapshot(self.SOURCE_NAME, url, html, reason or "empty_html")

                html = fetch_via_scraperapi(url)
                if html:
                    prices = extract_search_result_prices(html, brand, model)
                    if prices:
                        raw_detected_count += len(prices[:8])
                    avg_page = (sum(prices) / len(prices)) if prices else None
                    min_page = min(prices) if prices else None
                    for p in prices[:8]:
                        hit = _build_hit(
                            brand,
                            model,
                            p,
                            self.SOURCE_NAME,
                            url,
                            avg_price=avg_page,
                            min_price=min_page,
                        )
                        if self._zalando_accept_hit(hit):
                            hits.append(hit)
                            accepted_count += 1
                    if prices and not hits:
                        partial_seen = True
                    if hits:
                        logger.info(
                            "[ZALANDO OPTIMIZED] → OK (%d produits, conf=%s, rel=%s)",
                            len(hits),
                            hits[0].get("precision_confidence_score"),
                            hits[0].get("precision_overall_reliability"),
                        )
                        return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] anti-bot fallback err: %s", self.SOURCE_NAME, e)
        if blocked_seen:
            logger.warning("[ZALANDO OPTIMIZED] → BLOCKED")
        elif partial_seen or raw_detected_count > accepted_count:
            logger.warning(
                "[ZALANDO OPTIMIZED] → PARTIAL (detected=%d, accepted=%d)",
                raw_detected_count,
                accepted_count,
            )
        else:
            logger.warning("[ZALANDO OPTIMIZED] → FAIL")
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
                r = fetch_url_get(
                    "https://app.scrapingbee.com/api/v1/",
                    params={
                        "api_key": bee_key,
                        "url": bee_url,
                        "render_js": "true",
                        "country_code": "fr",
                        "wait": "2000",
                    },
                    timeout=45.0,
                    attempts=3,
                )
                if r is not None and r.status_code == 200 and r.text:
                    prices = extract_search_result_prices(r.text, brand, model)
                    if prices:
                        avg_page = sum(prices) / len(prices)
                        min_page = min(prices)
                        return [
                            _build_hit(
                                brand,
                                model,
                                p,
                                self.SOURCE_NAME,
                                bee_url,
                                avg_price=avg_page,
                                min_price=min_page,
                            )
                            for p in prices[:8]
                        ]
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
        # Playwright désactivé — sport2000.fr timeout 100% sur Playwright.
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls, playwright_fallback=False)


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
        # Playwright désactivé — snipes.com timeout systématique.
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls, playwright_fallback=False)


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


class AsphaltgoldScraper:
    """asphaltgold.com/fr — boutique spécialisée sneakers premium, expédition FR, requests-only."""

    SOURCE_NAME = "Asphaltgold"
    BASE_URL = "https://www.asphaltgold.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        # /fr/search?q= → HTTP 500 ; /fr/search?type=product&q= → 200.
        urls = [
            f"{self.BASE_URL}/fr/search?type=product&q={q}",
            f"{self.BASE_URL}/fr/collections/sneaker?q={q}",
        ]
        hits = _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)
        if hits:
            return hits
        # Fallback stealth ciblé (évite freeze Playwright long).
        # Désactivé en mode bulk pipeline (fast mode) car Playwright prend 30-60s > twall 35s.
        if _is_pipeline_fast_mode():
            return hits
        try:
            from scrapers.anti_bot_diag import fetch_playwright_stealth

            for url in urls:
                html = fetch_playwright_stealth(url, source_name=self.SOURCE_NAME)
                if not html:
                    continue
                prices = extract_search_result_prices(html, brand, model)
                if not prices:
                    continue
                avg_page = sum(prices) / len(prices)
                min_page = min(prices)
                for p in prices[:8]:
                    hit = _build_hit(
                        brand,
                        model,
                        p,
                        self.SOURCE_NAME,
                        url,
                        avg_price=avg_page,
                        min_price=min_page,
                    )
                    if _precision_accept_price(hit):
                        hits.append(hit)
                if hits:
                    logger.info(
                        "[SCRAPER STABILITY] Asphaltgold → OK (confidence=%s, reliability=%s)",
                        hits[0].get("precision_confidence_score"),
                        hits[0].get("precision_overall_reliability"),
                    )
                    return hits
        except Exception as e:  # noqa: BLE001
            logger.debug("[Asphaltgold] fallback stealth error: %s", e)
        return hits


class SizeerFrScraper:
    """sizeer.fr — boutique FR spécialisée sneakers, requests-only."""

    SOURCE_NAME = "Sizeer"
    BASE_URL = "https://sizeer.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/{quote_plus(brand.lower())}/",
            f"{self.BASE_URL}/search?q={q}",
        ]
        return _scrape_search_urls(self.SOURCE_NAME, brand, model, urls)


# E-com Tier 1 (sans grande distribution — voir ``hypermarches``).
TIER1_ECOM_EXTRA_SCRAPER_CLASSES: tuple[tuple[str, type], ...] = (
    ("Basket4Ballers", Basket4BallersScraper),  # Activé 2026-04-08 — requests-only, 8 prix/modèle
    ("Sport 2000", Sport2000Scraper),            # Activé 2026-04-08 — requests-only, 8 prix/modèle
    ("Asphaltgold", AsphaltgoldScraper),         # Activé 2026-04-08 — requests-only, sneakers premium FR
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
