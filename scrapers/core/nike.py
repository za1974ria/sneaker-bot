"""Nike core scraper (requests + BeautifulSoup)."""

from __future__ import annotations

from typing import Any

import requests
from bs4 import BeautifulSoup

from scrapers.utils.normalize import clean_text, parse_price_to_eur

NIKE_SEARCH_URL = "https://www.nike.com/fr/w/chaussures-y7ok"
NIKE_SOURCE = "nike.com"
NIKE_COUNTRY = "FR"
MAX_PRODUCTS = 10
REQUEST_TIMEOUT_S = 12


def _extract_cards(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()

    # Generic selectors to stay resilient across markup changes
    cards = soup.select("article, li, div")
    for card in cards:
        if len(out) >= MAX_PRODUCTS:
            break

        name_el = card.select_one("h3, h2, .product-card__title, .product-title")
        price_el = card.select_one(
            ".product-price, .price, .price-current, .product-card__price, span"
        )
        if not name_el or not price_el:
            continue

        product_name = clean_text(name_el.get_text(" ", strip=True))
        price_txt = clean_text(price_el.get_text(" ", strip=True))
        price = parse_price_to_eur(price_txt, currency_hint="EUR")
        if not product_name or price is None:
            continue

        key = (product_name, price)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "product": product_name,
                "price": price,
                "source": NIKE_SOURCE,
                "country": NIKE_COUNTRY,
            }
        )

    return out


def scrape_nike() -> list[dict[str, Any]]:
    """
    Returns standardized rows:
    {product, price, source, country}
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(NIKE_SEARCH_URL, headers=headers, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException:
        return []

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        return _extract_cards(soup)
    except Exception:
        return []

