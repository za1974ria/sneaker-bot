"""
Règles de fourchettes de prix par marque/modèle
Marché français 2026 — données de référence (sans appel LLM).
"""

from __future__ import annotations

# Fourchettes de prix attendues par marque (min, max) en €
BRAND_PRICE_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "nike": {
        "default": (60, 250),
        "air force 1": (85, 150),
        "air jordan 1": (110, 300),
        "air max 97": (150, 250),
        "air max 90": (100, 200),
        "dunk low": (90, 180),
        "dunk high": (100, 200),
    },
    "adidas": {
        "default": (55, 220),
        "samba": (85, 130),
        "gazelle": (80, 130),
        "campus": (80, 130),
        "superstar": (75, 130),
        "forum": (85, 140),
        "stan smith": (70, 120),
    },
    "new balance": {
        "default": (70, 250),
        "574": (75, 120),
        "530": (80, 130),
        "550": (85, 140),
        "990": (150, 280),
        "2002r": (110, 180),
        "1906r": (110, 180),
    },
    "on running": {
        "default": (120, 220),
    },
    "salomon": {
        "default": (100, 200),
        "xt-4": (120, 180),
        "xt-6": (130, 190),
        "speedcross": (100, 170),
    },
    "asics": {
        "default": (70, 200),
        "gel-kayano": (90, 170),
        "gel-nimbus": (100, 180),
        "gt-2160": (80, 150),
        "gel-1130": (80, 150),
    },
    "puma": {
        "default": (55, 150),
        "suede": (60, 110),
        "speedcat": (70, 120),
        "palermo": (70, 120),
    },
    "reebok": {
        "default": (60, 180),
    },
    "vans": {
        "default": (60, 130),
        "old skool": (65, 110),
        "sk8-hi": (70, 120),
    },
    "converse": {
        "default": (55, 120),
        "chuck taylor": (60, 110),
        "chuck 70": (70, 120),
    },
}


def get_price_range(brand: str, model: str) -> tuple[float, float]:
    """Retourne la fourchette de prix attendue pour un modèle."""
    brand_lower = brand.lower().strip()
    model_lower = model.lower().strip()

    brand_rules = BRAND_PRICE_RANGES.get(brand_lower, {})
    if not brand_rules:
        return (30.0, 500.0)

    for model_key, price_range in brand_rules.items():
        if model_key != "default" and model_key in model_lower:
            return price_range

    return brand_rules.get("default", (30.0, 500.0))


def validate_price_by_brand_rules(brand: str, model: str, price: float) -> dict:
    """Valide un prix selon les règles métier de la marque."""
    min_p, max_p = get_price_range(brand, model)

    if price < min_p:
        return {
            "valid": False,
            "reason": f"Prix {price}\u00a0€ trop bas (min attendu {min_p}\u00a0€ pour {brand})",
            "expected_range": (min_p, max_p),
        }
    if price > max_p:
        return {
            "valid": False,
            "reason": f"Prix {price}\u00a0€ trop élevé (max attendu {max_p}\u00a0€ pour {brand})",
            "expected_range": (min_p, max_p),
        }
    return {
        "valid": True,
        "reason": f"Prix cohérent pour {brand} {model}",
        "expected_range": (min_p, max_p),
    }


def filter_prices_by_brand_rules(
    brand: str, model: str, prices: list[float],
) -> tuple[list[float], list[float]]:
    """
    Filtre une liste de prix selon les règles de la marque.
    Retourne : (prix_valides, prix_ecartés)
    """
    if not prices:
        return [], []

    min_p, max_p = get_price_range(brand, model)
    valid = [p for p in prices if min_p <= float(p) <= max_p]
    invalid = [p for p in prices if float(p) < min_p or float(p) > max_p]

    if len(valid) < len(prices) * 0.3 and prices:
        return [float(p) for p in prices], []

    return (valid if valid else [float(p) for p in prices], invalid)
