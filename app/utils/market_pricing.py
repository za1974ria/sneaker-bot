"""
Nettoyage de séries de prix et estimation prix marché — SneakerBot.

Utilitaire autonome : n’altère pas les CSV ni les routes ; à importer là où
les agrégations de prix sont calculées.
"""

from __future__ import annotations


def clean_prices(prices: list) -> list[float]:
    prices = [p for p in prices if isinstance(p, (int, float))]
    prices = [p for p in prices if 20 <= p <= 500]

    if len(prices) < 3:
        return prices

    prices = sorted(prices)
    median = prices[len(prices) // 2]

    filtered = [p for p in prices if abs(p - median) / median < 0.4]

    return filtered


def market_price(prices: list) -> float | None:
    if not prices:
        return None

    prices = clean_prices(prices)

    if not prices:
        return None

    prices = sorted(prices)

    if len(prices) > 5:
        trim = int(len(prices) * 0.1)
        # int(len * 0.1) peut être 0 (ex. 6–9 points) : prices[0:-0] serait vide en Python
        if trim > 0 and len(prices) > 2 * trim:
            prices = prices[trim:-trim]

    return round(sum(prices) / len(prices), 2)


def price_range(prices: list) -> tuple[float | None, float | None]:
    if not prices:
        return None, None

    prices = clean_prices(prices)

    if not prices:
        return None, None

    return round(min(prices), 2), round(max(prices), 2)


def confidence_score(prices: list) -> str:
    n = len(prices)

    if n < 5:
        return "Faible"
    elif n < 10:
        return "Moyenne"
    else:
        return "Élevée"
