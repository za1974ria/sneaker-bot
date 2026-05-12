"""Métadonnées de fiabilité additives (non écrites dans le CSV marché)."""

from __future__ import annotations

from typing import Any


def _score_from_signals(
    n_sources: int,
    price_min: float,
    price_max: float,
    price_avg: float,
    has_google: bool,
) -> int:
    s = min(45, max(0, int(n_sources) * 12))
    if price_avg and price_avg > 0:
        sp = (price_max - price_min) / price_avg * 100.0
        if sp <= 20:
            s += 25
        elif sp <= 40:
            s += 18
        elif sp <= 60:
            s += 10
        else:
            s += 3
    else:
        s += 5
    if has_google:
        s += 15
    return int(max(0, min(100, s)))


def attach_reliability_metadata(
    row: dict[str, Any],
    *,
    brand: str,
    model: str,
    key: Any,
    fr_by_site: dict[Any, dict[str, list[float]]],
    google_fallback_used: bool,
) -> None:
    """Ajoute ``low_confidence`` et ``reliability_score`` au dict ligne (ignoré à l’écriture CSV)."""
    n = len(fr_by_site.get(key) or {})
    if google_fallback_used and n < 1:
        n = 1

    has_google = bool(google_fallback_used)
    if not has_google:
        try:
            from app.google_shopping_verifier import get_cached_google_price, init_google_cache

            init_google_cache()
            g = get_cached_google_price(brand, model)
            has_google = bool(g and g.get("google_price"))
        except Exception:
            pass

    try:
        pm = float(row.get("price_min") or 0.0)
        px = float(row.get("price_max") or 0.0)
        pa = float(row.get("price_avg") or 0.0)
    except (TypeError, ValueError):
        pm = px = pa = 0.0

    row["low_confidence"] = n < 2
    row["reliability_score"] = _score_from_signals(n, pm, px, pa, has_google)
    if n >= 8:
        row["reliability_label"] = "Fiabilité élevée"
    elif n < 5:
        row["reliability_label"] = "Fiabilité moyenne"
    else:
        row["reliability_label"] = ""
