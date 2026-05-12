"""Repli prix depuis le cache Google Shopping (aucun appel réseau)."""

from __future__ import annotations


def try_google_cached_prices(brand: str, model: str) -> list[float]:
    try:
        from app.google_shopping_verifier import get_cached_google_price, init_google_cache

        init_google_cache()
        c = get_cached_google_price(brand, model)
        if not c:
            return []
        gp = c.get("google_price")
        if gp is None:
            return []
        v = float(gp)
        if v <= 0:
            return []
        gmn, gmx = c.get("google_min"), c.get("google_max")
        try:
            a = float(gmn) if gmn is not None else v
            b = float(gmx) if gmx is not None else v
        except (TypeError, ValueError):
            return [v]
        if a > 0 and b > 0 and b >= a:
            return [a, v, b]
        return [v]
    except Exception:
        return []
