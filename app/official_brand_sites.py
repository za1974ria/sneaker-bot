"""
Repères « prix / site officiel » par marque (données éditoriales, hors scraping temps réel).

Source : ``data/official_brand_sites.json`` — à enrichir sans toucher au pipeline marché.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _ROOT / "data" / "official_brand_sites.json"


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    path = _DEFAULT_PATH
    if not path.is_file():
        logger.debug("official_brand_sites.json absent: %s", path)
        return {"disclaimer_fr": "", "brands": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"disclaimer_fr": "", "brands": {}}
    except Exception as e:  # noqa: BLE001
        logger.warning("Lecture official_brand_sites.json: %s", e)
        return {"disclaimer_fr": "", "brands": {}}


def reload_official_brand_cache() -> None:
    """Invalide le cache après édition manuelle du JSON (tests / admin)."""
    _load_raw.cache_clear()


def list_official_brands() -> dict[str, Any]:
    """Liste toutes les marques documentées + disclaimer."""
    raw = _load_raw()
    brands = raw.get("brands")
    if not isinstance(brands, dict):
        return {"disclaimer_fr": raw.get("disclaimer_fr") or "", "brands": {}}
    return {
        "disclaimer_fr": str(raw.get("disclaimer_fr") or ""),
        "brands": brands,
    }


def get_official_brand_reference(brand: str) -> dict[str, Any] | None:
    """
    Retourne un bloc API pour une marque du catalogue (clé dans le JSON),
    ou None si non documentée. Recherche insensible à la casse sur les clés.
    """
    b = (brand or "").strip()
    if not b:
        return None
    raw = _load_raw()
    brands = raw.get("brands")
    if not isinstance(brands, dict):
        return None
    entry = brands.get(b)
    if not isinstance(entry, dict):
        b_lower = b.lower()
        for k, v in brands.items():
            if isinstance(k, str) and k.strip().lower() == b_lower and isinstance(v, dict):
                entry = v
                b = k.strip()
                break
        else:
            return None
    if not isinstance(entry, dict):
        return None
    urls = entry.get("official_urls")
    if not isinstance(urls, list):
        urls = []
    out: dict[str, Any] = {
        "brand": b,
        "disclaimer_fr": str(raw.get("disclaimer_fr") or ""),
        "official_urls": urls,
        "reference_example": entry.get("reference_example"),
        "price_eur_min": entry.get("price_eur_min"),
        "price_eur_max": entry.get("price_eur_max"),
        "price_eur_label": _format_price_range(entry),
    }
    return out


def _format_price_range(entry: dict[str, Any]) -> str:
    try:
        lo = float(entry.get("price_eur_min"))
        hi = float(entry.get("price_eur_max"))
    except (TypeError, ValueError):
        return ""
    if lo == hi:
        return f"{lo:.0f}\u00a0€"
    return f"{lo:.0f}\u00a0€–{hi:.0f}\u00a0€"


__all__ = [
    "get_official_brand_reference",
    "list_official_brands",
    "reload_official_brand_cache",
]
