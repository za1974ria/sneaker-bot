"""
Récupération HTML rendu (Chromium + Playwright) pour contourner blocages 403/404 côté bots.
"""

from __future__ import annotations

import random
import time

from playwright.sync_api import sync_playwright


def scrape_site(url: str) -> str | None:
    """
    Charge l’URL dans Chromium headless, attend le rendu JS, retourne le HTML ou None.
    Délai aléatoire 3–6 s avant la requête (comportement plus « humain »).
    """
    if not url or not url.strip():
        return None
    for attempt in (1, 2):
        try:
            time.sleep(random.uniform(3.0, 6.0))
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(url, timeout=60_000)
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
