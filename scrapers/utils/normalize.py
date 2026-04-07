"""Normalization helpers for scraper outputs."""

from __future__ import annotations

import re
from typing import Any

EUR_RATES = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
}


def clean_text(value: Any) -> str:
    """Normalize text spacing."""
    return " ".join(str(value or "").strip().split())


def parse_price_to_eur(raw: Any, currency_hint: str = "EUR") -> float | None:
    """
    Convert messy price strings to EUR float.
    Returns None for invalid / out-of-range values.
    """
    txt = clean_text(raw).replace("\xa0", " ")
    if not txt:
        return None

    # detect currency from text first
    upper = txt.upper()
    if "$" in upper or "USD" in upper:
        currency = "USD"
    elif "£" in upper or "GBP" in upper:
        currency = "GBP"
    elif "€" in upper or "EUR" in upper:
        currency = "EUR"
    else:
        currency = currency_hint.upper()

    # keep only number-ish chars
    num = re.sub(r"[^0-9,.\-]", "", txt).replace(",", ".")
    if not num:
        return None
    try:
        val = float(num)
    except ValueError:
        return None

    rate = EUR_RATES.get(currency, 1.0)
    eur = round(val * rate, 2)

    # guardrails for sneaker prices
    if eur <= 0 or eur > 2000:
        return None
    return eur


def normalize_product_name(brand: str, model: str) -> str:
    """Build a standard product label."""
    b = clean_text(brand)
    m = clean_text(model)
    return clean_text(f"{b} {m}")

