"""Currency parsing and EUR conversion."""

from __future__ import annotations

import re
from typing import Any

EUR_RATES = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
}


def parse_price_eur(value: Any, currency_hint: str = "EUR") -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    upper = raw.upper()
    if "$" in upper or "USD" in upper:
        currency = "USD"
    elif "£" in upper or "GBP" in upper:
        currency = "GBP"
    elif "€" in upper or "EUR" in upper:
        currency = "EUR"
    else:
        currency = currency_hint.upper()

    cleaned = re.sub(r"[^0-9,.\-]", "", raw).replace(",", ".")
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None

    eur = round(number * EUR_RATES.get(currency, 1.0), 2)
    if eur <= 0 or eur > 5000:
        return None
    return eur

