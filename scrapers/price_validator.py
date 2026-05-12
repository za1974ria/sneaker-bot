"""
Validation déterministe des prix scrappés.
Aucun appel externe — logique basée sur des seuils calibrés sur les données FR.
"""
from __future__ import annotations

import csv
import logging
import math
from pathlib import Path
from typing import Any

from scrapers.source_robustness import normalize_price

logger = logging.getLogger(__name__)

PRICE_MIN_FLOOR = 20.0      # En dessous → invalide
PRICE_MAX_CEIL  = 800.0     # Au dessus  → invalide
MAX_SPREAD_RATIO = 5.0      # max/min > 5x → invalide


def is_valid_price(price: Any) -> bool:
    """
    Filtre qualité unifié:
    - None / non numérique / NaN / inf
    - <= 0
    - hors intervalle raisonnable (> 5000)
    """
    v = normalize_price(price)
    if v is None:
        return False
    return not (math.isnan(v) or math.isinf(v))


def validate_price_row(
    brand: str,
    model: str,
    price_min: float | None,
    price_max: float | None,
    price_avg: float | None,
) -> dict[str, Any]:
    """
    Valide et corrige une ligne de prix.

    Retourne un dict:
      valid        : bool
      reason       : str | None  — raison si invalide
      cleaned_min  : float | None
      cleaned_max  : float | None
      cleaned_avg  : float | None
    """
    result: dict[str, Any] = {
        "valid": True,
        "reason": None,
        "cleaned_min": price_min,
        "cleaned_max": price_max,
        "cleaned_avg": price_avg,
    }

    # Valeurs manquantes → pas de validation possible, on laisse passer
    if price_min is None or price_max is None:
        return result

    mn_n = normalize_price(price_min)
    mx_n = normalize_price(price_max)
    avg_n = normalize_price(price_avg) if price_avg is not None else None
    if mn_n is None or mx_n is None:
        result["valid"] = False
        result["reason"] = "prix min/max invalides"
        return result

    mn, mx = mn_n, mx_n

    # Règle e: min > max → swap
    if mn > mx:
        mn, mx = mx, mn
        result["cleaned_min"] = mn
        result["cleaned_max"] = mx

    # Règle a: prix trop bas
    if mn < PRICE_MIN_FLOOR:
        result["valid"] = False
        result["reason"] = f"prix trop bas (min={mn:.2f}€ < {PRICE_MIN_FLOOR}€)"
        return result

    # Règle b: prix trop élevé — plafond spécifique marque/modèle si disponible
    effective_ceil = PRICE_MAX_CEIL
    if brand and model:
        try:
            from app.brand_price_rules import get_price_range
            _, bp_max = get_price_range(brand, model)
            if bp_max > PRICE_MAX_CEIL:
                effective_ceil = bp_max
        except Exception:  # noqa: BLE001
            pass
    if mx > effective_ceil:
        result["valid"] = False
        result["reason"] = f"prix trop élevé (max={mx:.2f}€ > {effective_ceil:.0f}€)"
        return result

    # Règle c: écart suspect
    if mn > 0 and (mx / mn) > MAX_SPREAD_RATIO:
        result["valid"] = False
        result["reason"] = f"écart suspect (max/min={mx/mn:.1f}x > {MAX_SPREAD_RATIO}x)"
        return result

    # Règle d: avg hors [min, max] → correction
    if avg_n is not None and not (mn <= avg_n <= mx):
        corrected = round((mn + mx) / 2.0, 2)
        result["cleaned_avg"] = corrected
    elif avg_n is not None:
        result["cleaned_avg"] = avg_n

    return result


def validate_csv(filepath: str | Path) -> dict[str, Any]:
    """
    Lit un CSV de sources (market_fr_sources.csv) et applique validate_price_row.

    Retourne un résumé:
      total_rows    : int
      valid_rows    : int
      invalid_rows  : int
      invalid_ratio : float  (0.0–1.0)
      avg_corrected : int    — lignes dont avg a été corrigé
      errors        : list[dict]  — détail des lignes invalides (brand, model, shop, reason)
    """
    path = Path(filepath)
    total = 0
    valid = 0
    invalid = 0
    avg_corrected = 0
    errors: list[dict[str, str]] = []

    if not path.is_file():
        return {
            "total_rows": 0,
            "valid_rows": 0,
            "invalid_rows": 0,
            "invalid_ratio": 0.0,
            "avg_corrected": 0,
            "errors": [],
            "error": f"fichier introuvable: {path}",
        }

    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            total += 1
            brand = str(row.get("brand") or "").strip()
            model = str(row.get("model") or "").strip()
            shop  = str(row.get("shop")  or "").strip()
            try:
                pmin = float(row["price_min"]) if row.get("price_min") else None
                pmax = float(row["price_max"]) if row.get("price_max") else None
                pavg = float(row["price_avg"]) if row.get("price_avg") else None
            except (ValueError, TypeError):
                pmin = pmax = pavg = None

            res = validate_price_row(brand, model, pmin, pmax, pavg)

            if not res["valid"]:
                invalid += 1
                errors.append({"brand": brand, "model": model, "shop": shop, "reason": res["reason"] or ""})
                logger.warning("PRIX_INVALIDE: [%s] [%s] [%s] — %s", brand, model, shop, res["reason"])
            else:
                valid += 1
                if res["cleaned_avg"] != pavg:
                    avg_corrected += 1

    return {
        "total_rows": total,
        "valid_rows": valid,
        "invalid_rows": invalid,
        "invalid_ratio": round(invalid / total, 4) if total else 0.0,
        "avg_corrected": avg_corrected,
        "errors": errors,
    }
