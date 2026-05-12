"""
France market scraper.

Sites (ordre) — requêtes publiques + BeautifulSoup :
  cœur : courir.com, footlocker.fr, snipes.com/fr, sportsdirect.com
  + Tier 1 extra : JD, Chausport, Sarenza, Spartoo, Zalando, Basket4Ballers,
    Go Sport, Intersport, Sport 2000, Decathlon, Kickz, Snipes fr-fr, Footshop
  (voir scrapers.tier1_sites.TIER1_EXTRA_SCRAPER_CLASSES)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, wait
from typing import Callable, List
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from scrapers.base_scraper import BaseScraper
from scrapers.high_precision_core import (
    get_cached_high_precision_prices,
    run_high_precision_core,
    should_rescrape_model,
)
from scrapers.precision_engine import validate_price
from scrapers.utils.normalize import clean_text, parse_price_to_eur


logger = logging.getLogger(__name__)

# Parallélisation intra-modèle (sources) — plafond par défaut si l’appelant ne fixe pas max_workers_cap.
MAX_PARALLEL_FR_SITES = 6
# Timeout par tâche source (secondes). Réduit de 25 → 12 pour respecter le budget global par modèle.
_FR_SITE_FUTURE_TIMEOUT_SEC = 12


def _mount_zero_retry_session(session: requests.Session, *, retry_total: int) -> None:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry = Retry(
        total=retry_total,
        connect=retry_total,
        read=retry_total,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)


def _fr_run_one_site(
    site_name: str,
    func: Callable[["FranceScraper", str, str], List[float]],
    brand: str,
    model: str,
    *,
    fast: bool,
) -> tuple[str, List[float]]:
    from scrapers.aggregator import _is_empty_cached, _mark_empty

    if _is_empty_cached(site_name, brand, model):
        return site_name, []
    s = FranceScraper()
    if fast:
        s._fast_mode = True
        s.MIN_DELAY_SEC = 0.0
        s.MAX_DELAY_SEC = 0.0
        # Timeout strict 8s pour respecter le budget global par modèle (40s wall).
        s.TIMEOUT_SEC = 8
        _mount_zero_retry_session(s.session, retry_total=1)
    try:
        from scrapers.last_good_cache import get_last_good_prices, save_last_good_prices

        prices = func(s, brand, model)
        if prices:
            save_last_good_prices(site_name, brand, model, list(prices))
            print(f"[SCRAPER] {site_name} → OK")
            return site_name, list(prices)
        # Cache secours : uniquement si le scraper n’a aucun prix (ne pas écraser des prix réels).
        fallback = get_last_good_prices(site_name, brand, model)
        if fallback:
            print(f"[SCRAPER] {site_name} → OK (cache secours)")
            return site_name, fallback
        _mark_empty(site_name, brand, model)
        print(f"[SCRAPER] {site_name} → FAIL")
        return site_name, []
    except Exception as e:  # noqa: BLE001
        logger.debug("FR site %s brand=%s model=%s err=%s", site_name, brand, model, e)
        print(f"[SCRAPER] {site_name} → FAIL")
        return site_name, []


# Regex pour prix EUR dans le HTML brut (fallback si extraction générique insuffisante)
_EUR_PRICE_RE = re.compile(
    r"(?:€\s*)?(\d{1,3}(?:[.,\s]\d{3})*[.,]\d{2})\s*€|€\s*(\d{1,3}(?:[.,\s]\d{3})*[.,]\d{2})",
    re.IGNORECASE,
)
# Prix entiers type "180 €" ou "180€"
_EUR_INT_RE = re.compile(r"(?:€\s*)(\d{2,3})(?!\d)|(\d{2,3})\s*€")


class FranceScraper(BaseScraper):
    """
    Scrapes prices for FR using multiple public e-commerce sites.
    """

    MAX_PRICES = 60
    _fast_mode: bool = False
    ACTIVE_SOURCES: tuple[tuple[str, str], ...] = (
        ("Courir", "courir.com"),
        ("Foot Locker", "footlocker.fr"),
        ("Snipes", "snipes.com/fr"),
        ("Sports Direct", "sportsdirect.com"),
    )

    def _build_precision_hit(self, *, brand: str, model: str, price: float, site: str) -> dict[str, object]:
        """Construit une vue hit enrichie precision_* (API optionnelle, non cassante)."""
        precision = validate_price(
            {
                "brand": brand,
                "model": model,
                "title": f"{brand} {model}",
                "price": float(price),
                "current_price": float(price),
                "site": site,
                "source": site,
                "market": "FR",
            }
        )
        hit = {
            "brand": brand,
            "model": model,
            "price": round(float(price), 2),
            "source": site,
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
        if bool(precision.get("is_suspicious")):
            logger.warning(
                "[FR Precision] suspicious brand=%s model=%s site=%s price=%.2f conf=%s risk=%s overall_reliability=%s",
                brand,
                model,
                site,
                float(price),
                precision.get("confidence_score"),
                precision.get("risk_level"),
                precision.get("overall_reliability"),
            )
        return hit

    def fetch_html(self, url: str, *, raise_for_status: bool = True) -> str:
        """Délai court en mode pipeline (``max_sites`` défini), sinon jitter classe de base."""
        # Mode rapide : pas de sleep ici — ``fetch_with_retry`` applique déjà un jitter avant chaque GET.
        if not getattr(self, "_fast_mode", False):
            self._sleep_jitter()
        from scrapers.utils.fetch_retry import fetch_with_retry
        from scrapers.utils.safe_http import get_safe_headers

        extra: dict[str, object] = {}
        if "courir.com" in url or "footlocker.fr" in url:
            h = {**dict(self.session.headers), **get_safe_headers()}
            extra["headers"] = h
            extra["min_body_chars"] = 100
            extra["require_status_ok"] = True

        resp = fetch_with_retry(self.session, url, timeout=10.0, attempts=3, **extra)
        if resp is None:
            if raise_for_status:
                raise requests.HTTPError(f"GET failed after retries: {url}")
            return ""
        if raise_for_status:
            resp.raise_for_status()
        return resp.text

    def is_relevant_product(self, element, brand: str, model: str) -> bool:
        # Remonter jusqu'à 3 niveaux parents avec matching strict marque + tokens modèle.
        parent = element
        brand_norm = clean_text(brand).lower()
        model_tokens = [t for t in re.split(r"\s+", clean_text(model).lower()) if len(t) >= 2]
        # On ignore les suffixes trop génériques pour limiter le bruit.
        weak = {"low", "high", "mid", "og", "pro", "women", "premium", "essential"}
        strong_tokens = [t for t in model_tokens if t not in weak]
        if not strong_tokens:
            strong_tokens = model_tokens[:1]

        for _ in range(3):
            parent = getattr(parent, "parent", None)
            if parent is None:
                break
            try:
                text = clean_text(parent.get_text(" ", strip=True)).lower()
            except Exception:
                text = ""
            if not text:
                continue
            if brand_norm and brand_norm not in text:
                continue
            # strict: marque + au moins un token fort du modèle
            if any(tok in text for tok in strong_tokens):
                return True
        return False

    def _is_relevant_context(self, text: str, brand: str, model: str) -> bool:
        ctx = clean_text(text).lower()
        b = clean_text(brand).lower()
        m = clean_text(model).lower()
        if not ctx:
            return False
        if b and b in ctx:
            return True
        if m and m in ctx:
            return True
        model_tokens = [t for t in re.split(r"\s+", m) if t]
        if len(model_tokens) >= 2 and all(t in ctx for t in model_tokens[:2]):
            return True
        return False

    # --- Query variants (ex. Salomon XT-4 vs XT4 / ADV) ----------------------------
    def _query_variants(self, brand: str, model: str) -> List[tuple[str, str]]:
        """Variantes de recherche pour améliorer le hit-rate (sans multiplier les doublons inutiles)."""
        b = (brand or "").strip()
        m = (model or "").strip()
        if not b or not m:
            return []
        if getattr(self, "_fast_mode", False):
            return [(b, m)]
        seen: set[tuple[str, str]] = set()
        out: List[tuple[str, str]] = []

        def add(bb: str, mm: str) -> None:
            key = (bb.strip(), mm.strip())
            if not key[0] or not key[1] or key in seen:
                return
            seen.add(key)
            out.append(key)

        add(b, m)
        # XT-4 -> XT4
        if "-" in m:
            add(b, m.replace("-", ""))
        add(b, f"{m} ADV")
        add(b.lower(), m.lower())
        # espaces normalisés
        add(b, " ".join(m.split()))
        return out

    # --- Price extraction helpers -------------------------------------------------
    def _parse_price_nodes(
        self,
        nodes: list,
        seen: set[float],
        out: List[float],
        brand: str | None = None,
        model: str | None = None,
    ) -> None:
        for node in nodes:
            if brand and model and not self.is_relevant_product(node, brand, model):
                continue
            raw: str | None = None
            if getattr(node, "name", "").lower() == "meta":
                raw = node.get("content")
            else:
                raw = node.get("data-price") or node.get("data-test-price")
                if not raw:
                    raw = node.get_text(" ", strip=True)
                else:
                    raw = str(raw)
            if not raw:
                continue
            norm = clean_text(raw)
            upper = norm.upper()
            if "€" not in upper and not any(ch in norm for ch in [",", "."]):
                continue
            price = parse_price_to_eur(norm, currency_hint="EUR")
            if price is None or price in seen:
                continue
            seen.add(price)
            out.append(price)
            if len(out) >= self.MAX_PRICES:
                return

    def _extract_json_ld_prices(self, soup: BeautifulSoup, seen: set[float], out: List[float]) -> None:
        def walk(obj: object) -> None:
            if isinstance(obj, dict):
                if "price" in obj and isinstance(obj.get("price"), (str, int, float)):
                    p = parse_price_to_eur(str(obj["price"]), currency_hint="EUR")
                    if p is not None and p not in seen:
                        seen.add(p)
                        out.append(p)
                if "offers" in obj:
                    off = obj["offers"]
                    if isinstance(off, dict) and "price" in off:
                        p = parse_price_to_eur(str(off["price"]), currency_hint="EUR")
                        if p is not None and p not in seen:
                            seen.add(p)
                            out.append(p)
                    elif isinstance(off, list):
                        for o in off:
                            if isinstance(o, dict) and "price" in o:
                                p = parse_price_to_eur(str(o["price"]), currency_hint="EUR")
                                if p is not None and p not in seen:
                                    seen.add(p)
                                    out.append(p)
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        walk(v)
            elif isinstance(obj, list):
                for x in obj:
                    walk(x)

        for script in soup.select('script[type="application/ld+json"]'):
            txt = script.string or script.get_text() or ""
            if not txt.strip():
                continue
            try:
                data = json.loads(txt)
            except json.JSONDecodeError:
                continue
            walk(data)
            if len(out) >= self.MAX_PRICES:
                return

    def _extract_prices_regex_fallback(self, html: str, seen: set[float], out: List[float]) -> None:
        for m in _EUR_PRICE_RE.finditer(html):
            g = m.group(1) or m.group(2)
            if not g:
                continue
            norm = g.replace(" ", "").replace("\xa0", "")
            p = parse_price_to_eur(norm + "\u00a0€", currency_hint="EUR")
            if p is None or p in seen or p < 15 or p > 2000:
                continue
            seen.add(p)
            out.append(p)
            if len(out) >= self.MAX_PRICES:
                return
        for m in _EUR_INT_RE.finditer(html):
            g = m.group(1) or m.group(2)
            if not g:
                continue
            p = parse_price_to_eur(f"{g}\u00a0€", currency_hint="EUR")
            if p is None or p in seen or p < 25 or p > 2000:
                continue
            seen.add(p)
            out.append(p)
            if len(out) >= self.MAX_PRICES:
                return

    def _extract_prices_from_html(self, html: str, brand: str | None = None, model: str | None = None) -> List[float]:
        soup = BeautifulSoup(html, "html.parser")
        out: List[float] = []
        seen: set[float] = set()

        # Generic price selectors (best effort, resilient to markup variations).
        selectors = [
            'span[itemprop="price"]',
            'meta[itemprop="price"]',
            'span[class*="price" i]',
            'div[class*="price" i]',
            'p[class*="price" i]',
            '[class*="Price" i]',
            '[class*="price"]',
            '[data-testid*="price" i]',
            '[data-testid*="Price"]',
            'span[class*="Price"]',
            'div[class*="Price"]',
            '[data-price]',
        ]

        nodes = []
        for sel in selectors:
            try:
                nodes.extend(soup.select(sel))
            except Exception:
                continue

        text_nodes = soup.select('[class*="price" i], [data-testid*="price" i]')
        if text_nodes:
            nodes.extend(text_nodes[:50])

        self._parse_price_nodes(nodes, seen, out, brand=brand, model=model)
        if len(out) >= self.MAX_PRICES:
            return out[: self.MAX_PRICES]

        if not (brand and model):
            self._extract_json_ld_prices(soup, seen, out)
            if len(out) >= self.MAX_PRICES:
                return out[: self.MAX_PRICES]

        if not (brand and model):
            self._extract_prices_regex_fallback(html, seen, out)
        return out[: self.MAX_PRICES]

    # --- Site-specific search URLs -----------------------------------------------
    def _search_urls_courir(self, brand: str, model: str) -> List[str]:
        q = quote_plus(f"{brand} {model}")
        # Courir est sur Salesforce Commerce Cloud (SFCC/Demandware).
        # /fr/search?q= est l'URL correcte (200) ; /recherche/ et /search sans /fr/ → 404.
        return [
            f"https://www.courir.com/fr/search?q={q}",
        ]

    def _search_urls_footlocker(self, brand: str, model: str) -> List[str]:
        q = quote_plus(f"{brand} {model}")
        # Footlocker uses /search?q=... (best guess / based on web search).
        return [
            f"https://www.footlocker.fr/search?q={q}",
        ]

    def _search_urls_snipes(self, brand: str, model: str) -> List[str]:
        q = quote_plus(f"{brand} {model}")
        return [
            f"https://www.snipes.com/fr/search?q={q}",
            f"https://www.snipes.fr/recherche?q={q}",
        ]

    def _search_urls_sportsdirect(self, brand: str, model: str) -> List[str]:
        q = quote_plus(f"{brand} {model}")
        return [
            f"https://www.sportsdirect.com/search/?q={q}",
            f"https://www.sportsdirect.com/search?text={q}",
            f"https://www.sportsdirect.fr/recherche/?q={q}",
            f"https://www.sportsdirect.fr/search?query={q}",
        ]

    # --- Site scraping -------------------------------------------------------------
    def _scrape_site_first_hit(
        self,
        urls: List[str],
        extract_fn: Callable[[str, str | None, str | None], List[float]] | None = None,
        brand: str | None = None,
        model: str | None = None,
    ) -> List[float]:
        """
        Try URLs sequentially until we find at least 1 price.
        extract_fn: par défaut _extract_prices_from_html.
        """
        extract = extract_fn or self._extract_prices_from_html
        for url in urls:
            try:
                html = self.fetch_html(url, raise_for_status=False)
                prices = extract(html, brand, model)
                if prices:
                    return prices
            except Exception as e:  # noqa: BLE001
                logger.debug("FR site scrape failed url=%s err=%s", url, e)
                continue
        return []

    def _scrape_courir(self, brand: str, model: str) -> List[float]:
        for vb, vm in self._query_variants(brand, model):
            prices = self._scrape_site_first_hit(self._search_urls_courir(vb, vm), brand=brand, model=model)
            if prices:
                return prices
        # Courir (SFCC) rend les prix en JS — fallback Playwright.
        try:
            from scrapers.anti_bot_diag import detect_block_reason, dump_snapshot
            from scrapers.hypermarches import _fetch_html_playwright
            from scrapers.tier1_sites import extract_search_result_prices

            for vb, vm in self._query_variants(brand, model):
                for url in self._search_urls_courir(vb, vm):
                    html = _fetch_html_playwright(url, source_name="Courir")
                    if not html:
                        dump_snapshot("Courir", url, "", "empty_html")
                        continue
                    reason = detect_block_reason(html)
                    if reason:
                        dump_snapshot("Courir", url, html, reason)
                    prices = extract_search_result_prices(html, brand, model)
                    if prices:
                        return self.filter_prices(brand, prices)
        except Exception as e:  # noqa: BLE001
            logger.debug("FR courir playwright fallback err=%s", e)
        return []

    def _scrape_footlocker(self, brand: str, model: str) -> List[float]:
        for vb, vm in self._query_variants(brand, model):
            prices = self._scrape_site_first_hit(self._search_urls_footlocker(vb, vm), brand=brand, model=model)
            if prices:
                return prices
        return []

    def _scrape_snipes(self, brand: str, model: str) -> List[float]:
        old_timeout = self.TIMEOUT_SEC
        self.TIMEOUT_SEC = 5
        extract = self._extract_prices_from_html
        try:
            for vb, vm in self._query_variants(brand, model):
                for url in self._search_urls_snipes(vb, vm):
                    try:
                        time.sleep(5.0)
                        html = self.fetch_html(url, raise_for_status=False)
                        prices = extract(html, brand, model)
                        if prices:
                            return prices
                    except Exception as e:  # noqa: BLE001
                        logger.debug("FR snipes url=%s err=%s", url, e)
                        continue
            return []
        finally:
            self.TIMEOUT_SEC = old_timeout

    def _scrape_sportsdirect(self, brand: str, model: str) -> List[float]:
        for vb, vm in self._query_variants(brand, model):
            prices = self._scrape_site_first_hit(self._search_urls_sportsdirect(vb, vm), brand=brand, model=model)
            if prices:
                return prices
        # Sports Direct is frequently JS-rendered; fallback to Playwright-rendered HTML.
        try:
            from scrapers.anti_bot_diag import detect_block_reason, dump_snapshot
            from scrapers.hypermarches import _fetch_html_playwright
            from scrapers.tier1_sites import extract_search_result_prices

            for vb, vm in self._query_variants(brand, model):
                for url in self._search_urls_sportsdirect(vb, vm):
                    html = _fetch_html_playwright(url, source_name="Sports Direct")
                    if not html:
                        dump_snapshot("Sports Direct", url, "", "empty_html")
                        continue
                    reason = detect_block_reason(html)
                    if reason:
                        dump_snapshot("Sports Direct", url, html, reason)
                    prices = extract_search_result_prices(html, brand, model)
                    if prices:
                        return self.filter_prices(brand, prices)
        except Exception as e:  # noqa: BLE001
            logger.debug("FR sportsdirect playwright fallback err=%s", e)
        return []

    def _scrape_tier1_site(self, display_name: str, scraper_cls: type, brand: str, model: str) -> List[float]:
        """
        Appelle un scraper Tier 1 (list[dict]) et renvoie des floats pour l’agrégateur.
        En mode fast (_fast_mode=True), désactive le fallback Playwright dans les scrapers Tier1.
        """
        from scrapers.tier1_sites import hits_to_prices, _set_pipeline_fast_mode

        if self._fast_mode:
            _set_pipeline_fast_mode(True)
        try:
            hits = scraper_cls().scrape_model(brand, model)
            return hits_to_prices(hits)
        except Exception as e:  # noqa: BLE001
            logger.debug("Tier1 %s %s %s: %s", display_name, brand, model, e)
            return []
        finally:
            if self._fast_mode:
                _set_pipeline_fast_mode(False)

    @staticmethod
    def _fr_site_job_specs(max_sites: int | None) -> list[tuple[str, Callable[["FranceScraper", str, str], List[float]]]]:
        from scrapers.tier1_sites import TIER1_EXTRA_SCRAPER_CLASSES

        jobs: list[tuple[str, Callable[["FranceScraper", str, str], List[float]]]] = [
            ("Courir", lambda s, b, m: s._scrape_courir(b, m)),
            # ("Foot Locker", lambda s, b, m: s._scrape_footlocker(b, m)),  # Désactivé 2026-04-10 — HTTP 400/403 systématique, bloqué anti-bot
            # ("Snipes", lambda s, b, m: s._scrape_snipes(b, m)),  # Désactivé 2026-04-10 — timeout systématique, IP bloquée
            # ("Sports Direct", lambda s, b, m: s._scrape_sportsdirect(b, m)),  # Désactivé 2026-04-07 — timeout 100%, bloqué même via proxy
        ]
        for disp, cls in TIER1_EXTRA_SCRAPER_CLASSES:
            jobs.append((disp, lambda s, b, m, d=disp, c=cls: s._scrape_tier1_site(d, c, b, m)))
        # Rotation légère des sources en mode partiel (max_sites fixé):
        # évite de toujours interroger exactement les mêmes premières sources.
        rotate_enabled = (os.getenv("FR_SOURCES_ROTATION_ENABLED") or "1").strip() == "1"
        if rotate_enabled and isinstance(max_sites, int) and max_sites > 0 and len(jobs) > max_sites:
            shift = int(time.time() // 1800) % len(jobs)  # pas de 30 min
            jobs = jobs[shift:] + jobs[:shift]
        if isinstance(max_sites, int) and max_sites > 0:
            jobs = jobs[:max_sites]
        return jobs

    def _collect_parallel_fr_sites(
        self,
        brand: str,
        model: str,
        *,
        max_sites: int | None,
        wall_timeout_sec: float | None = None,
        max_workers_cap: int | None = None,
    ) -> list[tuple[str, List[float]]]:
        specs = FranceScraper._fr_site_job_specs(max_sites)
        fast = max_sites is not None
        cap = max_workers_cap if max_workers_cap is not None else MAX_PARALLEL_FR_SITES
        cap = max(1, int(cap))
        workers = min(cap, max(1, len(specs)))
        results: list[tuple[str, List[float]]] = []
        if not specs:
            return results
        wall = float(wall_timeout_sec) if wall_timeout_sec is not None else 100.0
        wall = max(25.0, wall)
        deadline = time.monotonic() + wall
        ex = ThreadPoolExecutor(max_workers=workers)
        wall_hit = False
        try:
            pending_set = {
                ex.submit(_fr_run_one_site, name, fn, brand, model, fast=fast) for name, fn in specs
            }
            while pending_set:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.debug(
                        "FR parallel wall timeout (%.0fs) brand=%s model=%s — %d tâches non terminées",
                        wall,
                        brand,
                        model,
                        len(pending_set),
                    )
                    wall_hit = True
                    break
                step = min(max(0.5, remaining), float(_FR_SITE_FUTURE_TIMEOUT_SEC))
                done, not_done = wait(pending_set, timeout=step, return_when=FIRST_COMPLETED)
                pending_set = not_done
                for fut in done:
                    try:
                        results.append(fut.result())
                    except FuturesTimeoutError:
                        logger.debug("FR site future timeout brand=%s model=%s", brand, model)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("FR parallel site err: %s", e)
        finally:
            # cancel_futures=True (Python 3.9+) : annule les futures non démarrées et
            # n'attend pas les Playwright encore en vol — essentiel en mode rebuild court.
            ex.shutdown(wait=not wall_hit, cancel_futures=wall_hit)
        return results

    # --- Public API ---------------------------------------------------------------
    def scrape_model(self, brand: str, model: str, *, max_sites: int | None = None) -> List[float]:
        """
        Scrape all targeted FR sites and return a combined list of prices found.
        """
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model:
            return []
        # High-Precision Core cache gate (compat: fallback sur flow classique si erreur).
        try:
            if not should_rescrape_model(brand, model, market="FR"):
                cached = get_cached_high_precision_prices(brand, model, market="FR")
                if cached:
                    logger.info("FR scrape_model cache hit %s %s (%d prix)", brand, model, len(cached))
                    return self.filter_prices(brand, cached[: self.MAX_PRICES])
        except Exception as e:  # noqa: BLE001
            logger.debug("FR scrape_model high_precision cache skip %s %s: %s", brand, model, e)

        pairs = self._collect_parallel_fr_sites(brand, model, max_sites=max_sites)
        all_prices: List[float] = []
        for _site, prices in pairs:
            if prices:
                all_prices.extend(prices)
            if len(all_prices) >= self.MAX_PRICES * 3:
                break

        seen: set[float] = set()
        unique: List[float] = []
        precision_hits: list[dict[str, object]] = []
        for p in all_prices:
            if p in seen:
                continue
            seen.add(p)
            unique.append(p)
            precision_hits.append(
                self._build_precision_hit(
                    brand=brand,
                    model=model,
                    price=float(p),
                    site="FranceScraper",
                )
            )
            if len(unique) >= self.MAX_PRICES:
                break
        # High-Precision Core fusion (safe fallback).
        try:
            hp = run_high_precision_core(
                brand=brand,
                model=model,
                market="FR",
                scraped_prices=list(unique),
                confirm_scrape_fn=None,  # évite récursion ici
            )
            hp_prices = hp.get("prices") or []
            if hp_prices:
                unique = [float(x) for x in hp_prices][: self.MAX_PRICES]
                logger.info(
                    "FR scrape_model high_precision %s %s -> %d prix (reliability=%s)",
                    brand,
                    model,
                    len(unique),
                    hp.get("overall_reliability"),
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("FR scrape_model high_precision fusion skip %s %s: %s", brand, model, e)
        # Compat ascendante: scrape_model continue de retourner List[float].
        # Les hits enrichis restent disponibles pour les appels qui en ont besoin.
        self._last_precision_hits = precision_hits
        return self.filter_prices(brand, unique)

    def scrape_model_hits(self, brand: str, model: str, *, max_sites: int | None = None) -> list[dict[str, object]]:
        """
        Variante enrichie avec precision_*.
        Ne remplace pas scrape_model() pour préserver la compatibilité existante.
        """
        self.scrape_model(brand, model, max_sites=max_sites)
        return list(getattr(self, "_last_precision_hits", []))

    def scrape_model_by_site(
        self,
        brand: str,
        model: str,
        *,
        max_sites: int | None = None,
        wall_timeout_sec: float | None = None,
    ) -> dict[str, List[float]]:
        """
        Retourne les prix par site pour comparaison type Trivago.
        wall_timeout_sec : budget global par modèle (défaut 100 s). Passer ~35 s en mode rebuild.
        """
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model:
            return {}

        pairs = self._collect_parallel_fr_sites(brand, model, max_sites=max_sites, wall_timeout_sec=wall_timeout_sec)
        out: dict[str, List[float]] = {}
        for site_name, prices in pairs:
            try:
                filtered = self.filter_prices(brand, prices) if prices else []
                if filtered:
                    out[site_name] = filtered[: self.MAX_PRICES]
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "FR by-site scrape failed site=%s brand=%s model=%s err=%s",
                    site_name,
                    brand,
                    model,
                    e,
                )
        return out


def debug_fr_urls(brand: str, model: str) -> None:
    """
    Test manuel : affiche les URLs exactes construites pour chaque site FR
    et toutes les variantes de requête (_query_variants).
    """
    s = FranceScraper()
    print("=== Variantes (brand, model) ===")
    for vb, vm in s._query_variants(brand, model):
        print(f"  {vb!r}  +  {vm!r}")
    print("\n=== courir.com ===")
    for vb, vm in s._query_variants(brand, model):
        print(f"  -- {vb} / {vm}")
        for u in s._search_urls_courir(vb, vm):
            print(f"     {u}")
    print("\n=== footlocker.fr ===")
    for vb, vm in s._query_variants(brand, model):
        print(f"  -- {vb} / {vm}")
        for u in s._search_urls_footlocker(vb, vm):
            print(f"     {u}")
    print("\n=== snipes.com/fr ===")
    for vb, vm in s._query_variants(brand, model):
        print(f"  -- {vb} / {vm}")
        for u in s._search_urls_snipes(vb, vm):
            print(f"     {u}")
    print("\n=== sportsdirect.com ===")
    for vb, vm in s._query_variants(brand, model):
        print(f"  -- {vb} / {vm}")
        for u in s._search_urls_sportsdirect(vb, vm):
            print(f"     {u}")


if __name__ == "__main__":
    import sys

    _b = sys.argv[1] if len(sys.argv) > 1 else "Salomon"
    _m = sys.argv[2] if len(sys.argv) > 2 else "XT-4"
    debug_fr_urls(_b, _m)
    print("\n=== scrape_model (prix trouvés) ===")
    print(FranceScraper().scrape_model(_b, _m))

