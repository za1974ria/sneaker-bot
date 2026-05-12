"""
Règles de fourchettes de prix par marque/modèle
Marché français 2026 — données de référence (sans appel LLM).
"""

from __future__ import annotations

# Fourchettes de prix attendues par marque (min, max) en €
BRAND_PRICE_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "nike": {
        "default":                (55, 350),
        "air force 1":            (85, 180),
        "air jordan 1":           (110, 400),
        "air jordan 1 low":       (100, 300),
        "air jordan 1 low women": (100, 350),
        "air max 97":             (150, 300),
        "air max 90":             (100, 250),
        "air max 1":              (110, 250),
        "dunk low":               (90, 280),
        "dunk high":              (100, 280),
        "cortez":                 (80, 160),
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
        "default":       (65, 300),
        "574":           (75, 150),
        "530":           (80, 160),
        "550":           (85, 180),
        "990":           (150, 350),
        "2002r":         (110, 280),
        "2002r gore-tex":(130, 400),
        "1906r":         (110, 220),
        "860":           (100, 180),
    },
    "on running": {
        "default": (120, 220),
    },
    "salomon": {
        "default":          (90, 350),
        "xt-4":             (120, 250),
        "xt-6":             (130, 260),
        "speedcross":       (100, 220),
        "pulsar platform":  (150, 700),
        "acs pro":          (150, 600),
        "acs pro advanced": (150, 650),
        "acs pro women":    (150, 600),
        "amphib bold":      (100, 220),
    },
    "asics": {
        "default":         (65, 300),
        "gel-kayano":      (90, 220),
        "gel-nimbus":      (100, 350),
        "gel-nimbus 9":    (100, 400),
        "gt-2160":         (80, 280),
        "gt-2160 premium": (100, 400),
        "gel-1130":        (80, 200),
        "gel-lyte":        (80, 250),
    },
    "puma": {
        "default": (55, 150),
        "suede": (60, 110),
        "speedcat": (70, 120),
        "palermo": (70, 120),
    },
    "reebok": {
        "default":               (55, 250),
        "classic leather":       (60, 300),
        "classic leather women": (60, 400),
        "club c":                (60, 180),
        "nano":                  (100, 200),
    },
    "vans": {
        "default":       (55, 300),
        "old skool":     (65, 180),
        "sk8-hi":        (70, 200),
        "slip-on":       (60, 350),
        "slip-on women": (60, 400),
        "era":           (60, 150),
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
        return (30.0, 800.0)

    # Tri par longueur décroissante : les clés les plus spécifiques matchent en premier
    for model_key in sorted((k for k in brand_rules if k != "default"), key=len, reverse=True):
        if model_key in model_lower:
            return brand_rules[model_key]

    return brand_rules.get("default", (30.0, 800.0))


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
