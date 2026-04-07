"""CORE scraper: Nike public listing."""

from __future__ import annotations

from typing import Any

import requests
from bs4 import BeautifulSoup

from app.normalize.currency import parse_price_eur

URL = "https://www.nike.com/fr/w/chaussures-y7ok"
SOURCE = "nike.com"
COUNTRY = "FR"
MAX_ITEMS = 10


def scrape() -> list[dict[str, Any]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(URL, headers=headers, timeout=12)
        response.raise_for_status()
    except requests.RequestException:
        return []

    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for card in soup.select("article, li, div"):
        if len(rows) >= MAX_ITEMS:
            break
        title_el = card.select_one("h3, h2, .product-card__title, .product-title")
        price_el = card.select_one(".product-price, .price, .product-card__price, span")
        if not title_el or not price_el:
            continue
        product = " ".join(title_el.get_text(" ", strip=True).split())
        price = parse_price_eur(price_el.get_text(" ", strip=True), currency_hint="EUR")
        if not product or price is None:
            continue
        key = (product, price)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "product": product,
                "price": price,
                "source": SOURCE,
                "country": COUNTRY,
            }
        )
    return rows

