"""Aggregate normalized scraper rows into market statistics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any


def _reject_outliers(prices: list[float]) -> list[float]:
    """
    Lightweight outlier filter.
    - short lists: keep as-is
    - larger lists: trim extreme 10% each side
    """
    if len(prices) < 5:
        return prices
    values = sorted(prices)
    k = max(1, int(len(values) * 0.1))
    if len(values) - (2 * k) < 2:
        return values
    return values[k:-k]


def aggregate_market(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Group by product and compute min / max / avg.
    avg uses median for robustness against spikes.
    """
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        name = str(row.get("product") or "").strip()
        price_raw = row.get("price")
        if not name:
            continue
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        grouped[name].append(price)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out: list[dict[str, Any]] = []
    for product, prices in grouped.items():
        clean = _reject_outliers(prices)
        if not clean:
            continue
        pmin = round(min(clean), 2)
        pmax = round(max(clean), 2)
        pavg = round(median(clean), 2)
        out.append(
            {
                "product": product,
                "produit": product,
                "min": pmin,
                "max": pmax,
                "avg": pavg,
                "variation": 0.0,
                "trend": "STABLE",
                "timestamp": now,
                "updated_at": now,
            }
        )
    out.sort(key=lambda r: str(r["product"]).lower())
    return out

