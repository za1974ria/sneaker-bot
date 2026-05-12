"""
Scraping e-commerce sneakers (Courir, JD Sports) — extraction défensive, cache disque.

- get_prices(product, live=False) : lit le cache (usage app, non bloquant).
- get_prices(product, live=True) : scrape réseau + mise à jour cache (cron / run_scraper).
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from pathlib import Path
from urllib.parse import quote_plus

from collections import Counter

from bs4 import BeautifulSoup
import requests

from scraper_browser import jdsports_search_url, scrape_site
from scrapers.utils.fetch_retry import fetch_with_retry

PRICE_MIN_EUR = 20.0
PRICE_MAX_EUR = 500.0
MAX_RESULTS_PER_SITE = 10
USD_TO_EUR = 0.92
GBP_TO_EUR = 1.17

_ROOT = Path(__file__).resolve().parent
CACHE_PATH = _ROOT / "scrape_cache.json"
HTTP_TIMEOUT_SEC = 18
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def _price_is_hard_invalid(p: float | int | None) -> bool:
    """Rejet strict : None, non numérique, NaN/inf, <= 0."""
    if p is None:
        return True
    try:
        x = float(p)
    except (TypeError, ValueError):
        return True
    if math.isnan(x) or math.isinf(x):
        return True
    if x <= 0:
        return True
    return False


def _fetch_html(url: str) -> str | None:
    """
    Priorité requests/bs4 (léger/stable), fallback Playwright si nécessaire.
    """
    try:
        sess = requests.Session()
        sess.headers.update(HTTP_HEADERS)
        r = fetch_with_retry(sess, url, timeout=float(HTTP_TIMEOUT_SEC), attempts=3)
        if r is not None and r.status_code == 200 and r.text:
            return r.text
    except Exception as e:
        print(f"⚠️ requests fetch ({url}) : {e}")
    return scrape_site(url)

def _parse_euro_token(raw: str) -> float | None:
    token = (raw or "").strip().replace("\xa0", " ").replace(" ", "")
    if not token:
        return None
    mult = 1.0
    if "$" in token or "usd" in token.lower():
        mult = USD_TO_EUR
    elif "£" in token or "gbp" in token.lower():
        mult = GBP_TO_EUR

    s = re.sub(r"[^\d,.\-]", "", token.replace("€", ""))
    if not s:
        return None
    s = s.replace(",", ".")
    try:
        v = float(s) * mult
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v) or v <= 0:
        return None
    return v


def _extract_prices_from_html(html: str) -> list[float]:
    """Plusieurs stratégies : JSON-LD, meta, data-*, classes usuelles, regex."""
    found: list[float] = []
    seen: set[float] = set()

    def add(v: float | None) -> None:
        if v is None:
            return
        if v not in seen:
            seen.add(v)
            found.append(v)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None

    if soup:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads((script.string or "").strip() or "{}")
                stack = [data]
                while stack:
                    cur = stack.pop()
                    if isinstance(cur, dict):
                        if "offers" in cur and isinstance(cur["offers"], dict):
                            p = cur["offers"].get("price")
                            if p is not None:
                                add(_parse_euro_token(str(p)))
                        for v in cur.values():
                            stack.append(v)
                    elif isinstance(cur, list):
                        stack.extend(cur)
            except Exception:
                continue

        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if "price" in prop:
                add(_parse_euro_token(meta.get("content") or ""))

        for tag in soup.find_all(True):
            dp = tag.get("data-price") or tag.get("data-product-price")
            if dp:
                add(_parse_euro_token(str(dp)))

    for m in re.finditer(
        r"(?:(€|\$|£)\s*)?(\d{2,4})[.,](\d{2})(?:\s*(€|\$|£))?",
        html,
        flags=re.IGNORECASE,
    ):
        c1, major, minor, c2 = m.groups()
        curr = c1 or c2 or "€"
        add(_parse_euro_token(f"{curr}{major}.{minor}"))

    return found[: MAX_RESULTS_PER_SITE * 2]


def _dedupe_cap(prices: list[float]) -> list[float]:
    """Dédoublonne avec plage marque en soft filter : si tout serait exclu, garder les prix valides."""

    def collect(*, prefer_range: bool) -> list[float]:
        out: list[float] = []
        seen: set[float] = set()
        for p in prices:
            if _price_is_hard_invalid(p):
                continue
            fp = float(p)
            if prefer_range and not (PRICE_MIN_EUR <= fp <= PRICE_MAX_EUR):
                continue
            if fp not in seen:
                seen.add(fp)
                out.append(fp)
            if len(out) >= MAX_RESULTS_PER_SITE:
                break
        return out

    out = collect(prefer_range=True)
    if not out:
        out = collect(prefer_range=False)
    return out


def clean_prices(prices: list[float]) -> list[float]:
    """Trie ; rejette uniquement <= 0, NaN, None (pas de plage arbitraire)."""
    out: list[float] = []
    for p in prices:
        if _price_is_hard_invalid(p):
            print("[INFO FILTER] valeur ignorée:", p)
            continue
        out.append(float(p))
    return sorted(out)


def scrape_courir(product: str) -> list[float]:
    """Recherche Courir FR (HTML rendu Playwright) — jusqu'à 10 prix valides."""
    q = (product or "").strip()
    if not q:
        return []
    url = f"https://www.courir.com/fr/recherche?q={quote_plus(q)}"
    try:
        html = _fetch_html(url)
        if not html:
            return []
        raw = _extract_prices_from_html(html)
        return _dedupe_cap(raw)
    except Exception as e:
        print(f"⚠️ Courir erreur : {e}")
        return []


def scrape_jdsports(product: str) -> list[float]:
    """Recherche JD Sports FR — URL /search/{slug}/ + Playwright + BeautifulSoup."""
    q = (product or "").strip()
    if not q:
        return []
    url = jdsports_search_url(q)
    try:
        html = _fetch_html(url)
        if not html:
            return []
        raw = _extract_prices_from_html(html)
        return _dedupe_cap(raw)
    except Exception as e:
        print(f"⚠️ JD Sports ({url}) : {e}")
        return []


def _scrape_search_source(source: str, url: str) -> list[float]:
    """Scrape générique source externe (StockX/GOAT/eBay), robuste aux erreurs réseau."""
    try:
        html = _fetch_html(url)
        if not html:
            return []
        return _dedupe_cap(_extract_prices_from_html(html))
    except Exception as e:
        print(f"⚠️ {source} ({url}) : {e}")
        return []


def scrape_stockx(product: str) -> list[float]:
    q = (product or "").strip()
    if not q:
        return []
    url = f"https://stockx.com/search?s={quote_plus(q)}"
    return _scrape_search_source("StockX", url)


def scrape_goat(product: str) -> list[float]:
    q = (product or "").strip()
    if not q:
        return []
    url = f"https://www.goat.com/search?query={quote_plus(q)}"
    return _scrape_search_source("GOAT", url)


def scrape_ebay(product: str) -> list[float]:
    q = (product or "").strip()
    if not q:
        return []
    url = f"https://www.ebay.fr/sch/i.html?_nkw={quote_plus(q)}"
    return _scrape_search_source("eBay", url)


def _aggregate(prices: list[float]) -> dict[str, float] | None:
    hard_ok = [float(p) for p in prices if not _price_is_hard_invalid(p)]
    if not hard_ok:
        return None
    in_range = [p for p in hard_ok if PRICE_MIN_EUR <= p <= PRICE_MAX_EUR]
    clean = in_range if in_range else hard_ok
    return {
        "min": round(min(clean), 2),
        "max": round(max(clean), 2),
        "avg": round(statistics.median(clean), 2),
    }


_UI_FILTER_THRESHOLD = 3  # valeur considérée slider UI si présente comme min/max dans >N entrées


def _detect_ui_filter_values() -> set[float]:
    """
    Retourne l'ensemble des prix qui se répètent comme min ou max dans >_UI_FILTER_THRESHOLD
    entrées du cache — signature d'un filtre de prix UI (slider) plutôt qu'un prix produit.
    """
    cache = _load_cache()
    freq: Counter[float] = Counter()
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        mn = entry.get("min")
        mx = entry.get("max")
        if mn is not None:
            freq[round(float(mn), 2)] += 1
        if mx is not None:
            freq[round(float(mx), 2)] += 1
    return {v for v, count in freq.items() if count > _UI_FILTER_THRESHOLD}


def _normalize_key(product: str) -> str:
    return " ".join((product or "").strip().split())


def _load_cache() -> dict:
    if not CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_get(product: str) -> dict[str, float] | None:
    key = _normalize_key(product)
    data = _load_cache()
    entry = data.get(key) or data.get(product.strip())
    if not entry or not isinstance(entry, dict):
        return None
    try:
        return {
            "min": float(entry["min"]),
            "max": float(entry["max"]),
            "avg": float(entry["avg"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _cache_set(product: str, market: dict[str, float]) -> None:
    key = _normalize_key(product)
    blob = _load_cache()
    blob[key] = {
        "min": market["min"],
        "max": market["max"],
        "avg": market["avg"],
        "updated_at": time.time(),
    }
    try:
        CACHE_PATH.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ Cache non écrit : {e}")


def get_prices(product: str, *, live: bool = False) -> dict[str, float] | None:
    """
    Retourne {"min", "max", "avg"} ou None.

    live=False (défaut) : lecture cache uniquement — adapté à l'app web.
    live=True : scrape multi-sources (e-commerce + marketplaces), puis cache.
    """
    print(f"🔍 Scraping : {product}")
    if not live:
        cached = _cache_get(product)
        if cached:
            print(f"✅ Prix trouvés : {cached}")
        else:
            print("✅ Prix trouvés : []")
        return cached

    try:
        p1 = scrape_courir(product)
        p2 = scrape_jdsports(product)
        p3 = scrape_stockx(product)
        p4 = scrape_goat(product)
        p5 = scrape_ebay(product)
        merged = p1 + p2 + p3 + p4 + p5
        prices = clean_prices(merged)

        # Filtre slider UI (soft) : ne jamais supprimer toutes les valeurs
        ui_filters = _detect_ui_filter_values()
        if ui_filters:
            filtered = [p for p in prices if round(p, 2) not in ui_filters]
            if filtered:
                removed = {round(p, 2) for p in prices} - {round(p, 2) for p in filtered}
                for v in sorted(removed):
                    print("[INFO FILTER] valeur ignorée:", v)
                prices = filtered

        if len(prices) < 2:
            print("✅ Prix trouvés : []")
            return None
        market = _aggregate(prices)
        if market:
            print(f"✅ Prix trouvés : {market}")
            _cache_set(product, market)
        else:
            print("✅ Prix trouvés : []")
        return market
    except Exception as e:
        print(f"⚠️ get_prices(live) : {e}")
        return None
