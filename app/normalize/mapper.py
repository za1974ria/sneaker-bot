"""Map raw scraper payloads into standard schema."""

from __future__ import annotations

from typing import Any

from app.normalize.currency import parse_price_eur

STANDARD_KEYS = ("product", "price", "source", "country")


def normalize_row(raw: dict[str, Any], default_source: str = "unknown") -> dict[str, Any] | None:
    product = str(raw.get("product") or "").strip()
    source = str(raw.get("source") or default_source).strip() or default_source
    country = str(raw.get("country") or "EU").strip().upper() or "EU"
    price = parse_price_eur(raw.get("price"))
    if not product or price is None:
        return None
    return {
        "product": product,
        "price": price,
        "source": source,
        "country": country,
    }


def normalize_rows(rows: list[dict[str, Any]], default_source: str = "unknown") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        norm = normalize_row(row, default_source=default_source)
        if norm is not None:
            out.append(norm)
    return out

