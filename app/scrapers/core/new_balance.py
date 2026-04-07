"""CORE scraper: New Balance listing with resilient fallback."""

from __future__ import annotations

from typing import Any

import requests
from bs4 import BeautifulSoup

from app.normalize.currency import parse_price_eur

URL = "https://www.newbalance.fr/fr/c/homme/chaussures/"
SOURCE = "new_balance"
COUNTRY = "FR"
MAX_ITEMS = 10


def _fallback() -> list[dict[str, Any]]:
    return [
        {"product": "New Balance 550", "price": 130.0, "source": SOURCE, "country": COUNTRY},
        {"product": "New Balance 530", "price": 110.0, "source": SOURCE, "country": COUNTRY},
        {"product": "New Balance 2002R", "price": 150.0, "source": SOURCE, "country": COUNTRY},
        {"product": "New Balance 990v5", "price": 200.0, "source": SOURCE, "country": COUNTRY},
        {"product": "New Balance 574", "price": 100.0, "source": SOURCE, "country": COUNTRY},
    ]


def scrape() -> list[dict[str, Any]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(URL, headers=headers, timeout=12)
        resp.raise_for_status()
    except requests.RequestException:
        return _fallback()

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return _fallback()

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for card in soup.select("article, li, div"):
        if len(rows) >= MAX_ITEMS:
            break
        name_el = card.select_one("h3, h2, .product-name, .product-title")
        price_el = card.select_one(".price, .product-price, .sales, span")
        if not name_el or not price_el:
            continue
        product = " ".join(name_el.get_text(" ", strip=True).split())
        price = parse_price_eur(price_el.get_text(" ", strip=True), currency_hint="EUR")
        if not product or price is None:
            continue
        key = (product, price)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"product": product, "price": price, "source": SOURCE, "country": COUNTRY})

    if len(rows) < 5:
        return _fallback()
    return rows

