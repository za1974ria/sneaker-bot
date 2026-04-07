"""
Grande distribution & surfaces sport FR — scrapers au format Tier 1 (list[dict]).

User-Agent : identique au projet → ``scrapers.base_scraper.BaseScraper.USER_AGENT`` (iPhone / Safari).

Format de retour par hit : ``{brand, model, price, source, url, currency: "EUR"}``.

Enregistrement pipeline : uniquement ``HYPERMARCHE_SCRAPER_CLASSES_REGISTERED`` (tuple ``(nom affichage, classe)``),
fusionné dans ``scrapers.tier1_sites.TIER1_EXTRA_SCRAPER_CLASSES``.

Extraction des prix : ``tier1_sites._scrape_search_urls`` (requests) puis, si vide,
``extract_search_result_prices`` sur HTML rendu par Playwright (SPA). Le filtre métier utilise
``FranceScraper.filter_prices`` via ``extract_search_result_prices``.

Sites testés (probe utilisateur, URLs nike+air+force+1) : la plupart sont WAF / SPA ;
seuls les scrapers validés (≥ 1 prix sur Nike Air Force 1) sont dans ``REGISTERED``.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# Playwright sync n’est pas utilisable depuis plusieurs threads sur un même driver.
# On limite à 1 navigation concurrente pour éviter les OOM sur VPS limité.
_PLAYWRIGHT_SEMAPHORE = threading.Semaphore(1)

_PLAYWRIGHT_CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-translate",
    "--mute-audio",
    "--no-first-run",
    "--js-flags=--max-old-space-size=256",
    "--renderer-process-limit=1",
]


def close_shared_browser() -> None:
    """Rétrocompatibilité run_market (plus de singleton navigateur)."""
    return


# Métadonnée lue par la doc / sources.json (pas utilisée par FranceScraper)
CATEGORY_HYPERMARCHE = "hypermarche"
CATEGORY_SPORT_GRANDE_SURFACE = "sport_grande_surface"


def _go() -> tuple[Any, Any]:
    from scrapers.tier1_sites import _q, _scrape_search_urls

    return _q, _scrape_search_urls


def _hyper_sleep() -> None:
    time.sleep(random.uniform(0.45, 0.55))


def _fetch_html_playwright(url: str, *, source_name: str, max_retries: int = 2) -> str | None:
    """Rendu JS (SPA) avec retry exponentiel et limites mémoire Chromium.

    Retourne None si Playwright indisponible ou erreur — pas d’exception vers le pipeline.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        logger.debug("[%s] Playwright non installé", source_name)
        return None

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with _PLAYWRIGHT_SEMAPHORE:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=True,
                        args=_PLAYWRIGHT_CHROME_ARGS,
                    )
                    try:
                        _proxy_url = os.getenv("PROXY_URL", "").strip()
                        _ctx_kwargs: dict = {
                            "user_agent": BaseScraper.USER_AGENT,
                            "locale": "fr-FR",
                            "ignore_https_errors": True,
                            "extra_http_headers": {
                                "Accept-Language": "fr-FR,fr;q=0.9",
                                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            },
                        }
                        if _proxy_url:
                            _ctx_kwargs["proxy"] = {"server": _proxy_url}
                        context = browser.new_context(**_ctx_kwargs)
                        page = context.new_page()
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=40000)
                            try:
                                page.wait_for_load_state("networkidle", timeout=8000)
                            except Exception:
                                pass
                            page.mouse.wheel(0, 1400)
                            page.wait_for_timeout(800)
                            return page.content()
                        finally:
                            try:
                                context.close()
                            except Exception:  # noqa: BLE001
                                pass
                    finally:
                        try:
                            browser.close()
                        except Exception:  # noqa: BLE001
                            pass
        except Exception as e:  # noqa: BLE001
            last_exc = e
            err_txt = str(e).lower()
            # Les crashs renderer sont rarement récupérables immédiatement sur VPS contraint:
            # on stoppe les retries pour limiter l'impact CPU/RAM.
            if "target crashed" in err_txt or "page crashed" in err_txt:
                logger.warning(
                    "[%s] Playwright crash navigateur (%s) — fallback sans retry pour %s",
                    source_name,
                    e,
                    url,
                )
                break
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.debug(
                    "[%s] Playwright tentative %d/%d échouée (%s) — retry dans %ds",
                    source_name, attempt, max_retries, e, wait,
                )
                time.sleep(wait)
            elif "timeout" in err_txt:
                logger.warning("[%s] Playwright timeout final pour %s", source_name, url)

    logger.debug("[%s] Playwright échec définitif %s: %s", source_name, url, last_exc)
    return None


def _scrape_requests_then_playwright(
    source_name: str,
    brand: str,
    model: str,
    urls: list[str],
) -> list[dict]:
    """1) ``requests`` via ``_scrape_search_urls`` ; 2) si [], Playwright + ``extract_search_result_prices``."""
    _q, _scrape = _go()
    hits = _scrape(source_name, brand, model, urls)
    if hits:
        return hits
    from scrapers.tier1_sites import _build_hit, extract_search_result_prices

    for url in urls:
        try:
            _hyper_sleep()
            html = _fetch_html_playwright(url, source_name=source_name)
            if not html:
                continue
            prices = extract_search_result_prices(html, brand, model)
            if not prices:
                continue
            return [_build_hit(brand, model, p, source_name, url) for p in prices[:8]]
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] Erreur Playwright %s %s: %s", source_name, brand, model, e)
    return []


def _scrape_playwright_only(
    source_name: str,
    brand: str,
    model: str,
    urls: list[str],
) -> list[dict]:
    """Uniquement Playwright + extraction (ex. Sport 2000 : pas de prix en SSR)."""
    from scrapers.tier1_sites import _build_hit, extract_search_result_prices

    for url in urls:
        try:
            _hyper_sleep()
            html = _fetch_html_playwright(url, source_name=source_name)
            if not html:
                continue
            prices = extract_search_result_prices(html, brand, model)
            if not prices:
                continue
            return [_build_hit(brand, model, p, source_name, url) for p in prices[:8]]
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] Erreur scraping %s %s: %s", source_name, brand, model, e)
    return []


class DecathlonGrandeSurfaceScraper:
    SOURCE_NAME = "Decathlon (grande surface)"
    CATEGORY = CATEGORY_SPORT_GRANDE_SURFACE
    BASE_URL = "https://www.decathlon.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _scrape = _go()
        q = _q(brand, model)
        q_plus = f"{brand}+{model}".replace(" ", "+")
        urls = [
            f"{self.BASE_URL}/search?Ntt={q_plus}",
            f"{self.BASE_URL}/tous-les-sports/q?search={q}",
            f"{self.BASE_URL}/recherche?q={q}",
        ]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


class GoSportGrandeSurfaceScraper:
    SOURCE_NAME = "Go Sport (grande surface)"
    CATEGORY = CATEGORY_SPORT_GRANDE_SURFACE
    BASE_URL = "https://www.go-sport.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _scrape = _go()
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/search?q={q}",
            f"{self.BASE_URL}/catalogsearch/result/?q={q}",
        ]
        return _scrape(self.SOURCE_NAME, brand, model, urls)


class Sport2000GrandeSurfaceScraper:
    SOURCE_NAME = "Sport 2000 (grande surface)"
    CATEGORY = CATEGORY_SPORT_GRANDE_SURFACE
    BASE_URL = "https://www.sport2000.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/recherche?q={q}",
            f"{self.BASE_URL}/recherche?search={q}",
        ]
        return _scrape_playwright_only(self.SOURCE_NAME, brand, model, urls)


class AuchanFrScraper:
    SOURCE_NAME = "Auchan"
    CATEGORY = CATEGORY_HYPERMARCHE
    BASE_URL = "https://www.auchan.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/recherche?q={q}&page=1",
            f"{self.BASE_URL}/recherche?q={q}",
        ]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


class CarrefourFrScraper:
    SOURCE_NAME = "Carrefour"
    CATEGORY = CATEGORY_HYPERMARCHE
    BASE_URL = "https://www.carrefour.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        urls = [f"{self.BASE_URL}/s?q={q}"]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


class LeclercFrScraper:
    SOURCE_NAME = "E.Leclerc"
    CATEGORY = CATEGORY_HYPERMARCHE
    BASE_URL = "https://www.e.leclerc"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        q_plus = f"{brand}+{model}".replace(" ", "+")
        urls = [
            f"{self.BASE_URL}/cat?t={q_plus}&page=1",
            f"{self.BASE_URL}/cat?t={q_plus}",
            f"{self.BASE_URL}/recherche?q={q}",
        ]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


class MagasinsUFrScraper:
    SOURCE_NAME = "Magasins U"
    CATEGORY = CATEGORY_HYPERMARCHE
    BASE_URL = "https://www.magasins-u.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        urls = [f"{self.BASE_URL}/recherche?q={q}"]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


class CasinoFrScraper:
    SOURCE_NAME = "Casino"
    CATEGORY = CATEGORY_HYPERMARCHE
    BASE_URL = "https://www.casino.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        urls = [f"{self.BASE_URL}/recherche?q={q}"]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


class MonoprixFrScraper:
    SOURCE_NAME = "Monoprix"
    CATEGORY = CATEGORY_HYPERMARCHE
    BASE_URL = "https://www.monoprix.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        urls = [f"{self.BASE_URL}/recherche?q={q}"]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


class CoraFrScraper:
    SOURCE_NAME = "Cora"
    CATEGORY = CATEGORY_HYPERMARCHE
    BASE_URL = "https://www.cora.fr"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/search?q={q}",
            f"{self.BASE_URL}/recherche?q={q}",
            f"{self.BASE_URL}/navigation/recherche?text={q}",
        ]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


class IntermarcheFrScraper:
    SOURCE_NAME = "Intermarché"
    CATEGORY = CATEGORY_HYPERMARCHE
    BASE_URL = "https://www.intermarche.com"

    def scrape_model(self, brand: str, model: str) -> list[dict]:
        _q, _ = _go()
        q = _q(brand, model)
        urls = [
            f"{self.BASE_URL}/courses/recherche?q={q}",
            f"{self.BASE_URL}/recherche?q={q}",
            f"{self.BASE_URL}/accueil/recherche?q={q}",
        ]
        return _scrape_requests_then_playwright(self.SOURCE_NAME, brand, model, urls)


# Alias noms courts (scripts / tests utilisateur)
AuchanScraper = AuchanFrScraper
CarrefourScraper = CarrefourFrScraper
LeclercScraper = LeclercFrScraper
SystemeUScraper = MagasinsUFrScraper
CasinoScraper = CasinoFrScraper
MonoprixScraper = MonoprixFrScraper
CoraScraper = CoraFrScraper
IntermarcheScraper = IntermarcheFrScraper

# Toutes les classes « hypers » pour tests / introspection
ALL_HYPERMARCHE_SCRAPER_CLASSES: tuple[type, ...] = (
    DecathlonGrandeSurfaceScraper,
    GoSportGrandeSurfaceScraper,
    Sport2000GrandeSurfaceScraper,
    AuchanFrScraper,
    CarrefourFrScraper,
    LeclercFrScraper,
    MagasinsUFrScraper,
    CasinoFrScraper,
    MonoprixFrScraper,
    CoraFrScraper,
    IntermarcheFrScraper,
)

# Pipeline France : scrapers validés (≥ 1 prix fiable ; évite charge Playwright inutile sur chaque modèle).
HYPERMARCHE_SCRAPER_CLASSES_REGISTERED: tuple[tuple[str, type], ...] = (
    # ("Sport 2000 (grande surface)", Sport2000GrandeSurfaceScraper),  # Désactivé 2026-04-07 — Playwright timeout 100%
)

# Alias demandé (liste fusionnable avec Tier 1 e-com dans l’agrégateur)
HYPERMARCHE_SCRAPER_CLASSES: tuple[tuple[str, type], ...] = HYPERMARCHE_SCRAPER_CLASSES_REGISTERED
