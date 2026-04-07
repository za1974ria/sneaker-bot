"""Aggregate normalized rows into market_live schema."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Any

from app.aggregate.outliers import filter_outliers


def aggregate_market(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        product = str(row.get("product") or "").strip()
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if not product or price <= 0:
            continue
        grouped[product].append(price)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    result: list[dict[str, Any]] = []
    for product, prices in grouped.items():
        cleaned = filter_outliers(prices)
        if not cleaned:
            continue
        pmin = round(min(cleaned), 2)
        pmax = round(max(cleaned), 2)
        pavg = round(mean(cleaned), 2)
        result.append(
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
    result.sort(key=lambda x: str(x["product"]).lower())
    return result

