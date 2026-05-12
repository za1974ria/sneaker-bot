"""
Récupération HTML rendu (Chromium + Playwright) pour contourner blocages 403/404 côté bots.
"""

from __future__ import annotations

import random
import threading
import time

from playwright.sync_api import sync_playwright

# Max 2 instances Playwright simultanées pour éviter OOM
_playwright_sem = threading.Semaphore(2)

_PLAYWRIGHT_PAGE_TIMEOUT_MS = 30_000  # 30 s max par page
_MIN_FREE_RAM_MB = 500  # skip Playwright si RAM libre < 500 Mo


def _free_ram_mb() -> float:
    """Retourne la RAM libre en Mo (best-effort, 0 si indisponible)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


def scrape_site(url: str) -> str | None:
    """
    Charge l’URL dans Chromium headless, attend le rendu JS, retourne le HTML ou None.
    Délai aléatoire 3–6 s avant la requête (comportement plus « humain »).
    Limité à 2 instances simultanées + vérification RAM avant lancement.
    """
    if not url or not url.strip():
        return None

    free_ram = _free_ram_mb()
    if free_ram < _MIN_FREE_RAM_MB:
        print(f"⚠️ Playwright skip (RAM libre: {free_ram:.0f} Mo < {_MIN_FREE_RAM_MB} Mo) : {url}")
        return None

    for attempt in (1, 2):
        try:
            time.sleep(random.uniform(3.0, 6.0))
            with _playwright_sem:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.goto(url, timeout=_PLAYWRIGHT_PAGE_TIMEOUT_MS)
                        page.wait_for_timeout(5000)
                        html = page.content()
                        return html
                    finally:
                        browser.close()
        except Exception as e:
            print(f"⚠️ Playwright scrape_site (tentative {attempt}/2) : {e}")
            if attempt == 2:
                return None
            time.sleep(1.5)
    return None


def jdsports_search_url(product: str) -> str:
    """URL liste résultats JD Sports FR (segment encodé, ex. Nike%20Air%20Max)."""
    from urllib.parse import quote

    q = " ".join((product or "").strip().split())
    slug = quote(q, safe="")
    return f"https://www.jdsports.fr/search/{slug}/"
