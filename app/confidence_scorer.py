"""
Score de confiance par modèle sneaker — SneakerBot
Score de 0 à 100 basé sur 5 critères pondérés (+ bonus optionnel Claude 0–5 sur fiche détail API).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def compute_confidence_score(
    nb_sources: int,
    price_min: float,
    price_max: float,
    price_avg: float,
    updated_at: Optional[str] = None,
    groq_confidence: Optional[float] = None,
    groq_valid: Optional[bool] = None,
    claude_points: Optional[int] = None,
    brand: Optional[str] = None,
    model: Optional[str] = None,
) -> dict[str, Any]:
    """
    Calcule un score de confiance 0-100 pour un modèle sneaker.

    Retourne :
    {
        "score": int,           # 0-100
        "label": str,           # "Faible" / "Moyen" / "Bon" / "Excellent"
        "stars": int,           # 1-5
        "color": str,           # "red" / "orange" / "yellow" / "green"
        "details": dict         # détail de chaque critère (+ claude 0-5 si second avis Anthropic)
    }
    """
    scores: dict[str, int] = {}

    # ── CRITÈRE 1 : Nombre de sources (40 pts max) ────────────────────────
    if nb_sources >= 5:
        scores["nb_sources"] = 40
    elif nb_sources >= 3:
        scores["nb_sources"] = 25
    elif nb_sources >= 2:
        scores["nb_sources"] = 15
    else:
        scores["nb_sources"] = 8

    # ── CRITÈRE 2 : Cohérence des prix (25 pts max) ───────────────────────
    if price_avg > 0:
        spread_pct = (price_max - price_min) / price_avg * 100
        if spread_pct <= 15:
            scores["price_coherence"] = 25
        elif spread_pct <= 30:
            scores["price_coherence"] = 18
        elif spread_pct <= 50:
            scores["price_coherence"] = 10
        elif spread_pct <= 80:
            scores["price_coherence"] = 5
        else:
            scores["price_coherence"] = 2
    else:
        scores["price_coherence"] = 0

    # ── CRITÈRE 3 : Fraîcheur des données (20 pts max) ────────────────────
    scores["freshness"] = 5
    if updated_at:
        try:
            if isinstance(updated_at, str):
                ts = updated_at.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = updated_at  # type: ignore[assignment]

            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600

            if age_hours <= 2:
                scores["freshness"] = 20
            elif age_hours <= 12:
                scores["freshness"] = 15
            elif age_hours <= 24:
                scores["freshness"] = 10
            elif age_hours <= 48:
                scores["freshness"] = 5
            else:
                scores["freshness"] = 2
        except Exception:
            scores["freshness"] = 5

    # ── CRITÈRE 4 : Validation Groq IA (10 pts max) ───────────────────────
    if groq_valid is True:
        scores["groq"] = 10
    elif groq_valid is False:
        scores["groq"] = 4
    elif groq_confidence is not None:
        gc = max(0.0, min(1.0, float(groq_confidence)))
        scores["groq"] = min(10, int(gc * 10))
    else:
        scores["groq"] = 5

    # ── CRITÈRE 5 : Prix dans fourchette réaliste (5 pts max) ────────────
    if 40 <= price_avg <= 400:
        scores["price_range"] = 5
    elif 25 <= price_avg <= 500:
        scores["price_range"] = 3
    else:
        scores["price_range"] = 0

    total = sum(scores.values())
    details_map: dict[str, int] = {
        "sources": scores["nb_sources"],
        "coherence": scores["price_coherence"],
        "freshness": scores["freshness"],
        "groq": scores["groq"],
        "price_range": scores["price_range"],
    }
    if brand and model and (brand.strip() and model.strip()):
        try:
            from app.brand_price_rules import get_price_range

            lo, hi = get_price_range(brand, model)
            if lo <= float(price_avg) <= hi:
                total += 5
                details_map["brand_rules"] = 5
        except Exception:
            pass
    if claude_points is not None:
        cp = max(0, min(5, int(claude_points)))
        total += cp
        details_map["claude"] = cp
    total = max(0, min(100, int(round(total))))

    # Plafond selon nombre de sources
    source_caps = {1: 70, 2: 80, 3: 88, 4: 93}
    cap = source_caps.get(nb_sources, 100)
    if nb_sources >= 5:
        cap = 100
    total = min(total, cap)

    # Recalcule label/stars/color selon nouveau total
    if total >= 80:
        label, stars, color = "Excellent", 5, "green"
    elif total >= 60:
        label, stars, color = "Bon", 4, "green"
    elif total >= 40:
        label, stars, color = "Moyen", 3, "yellow"
    elif total >= 20:
        label, stars, color = "Faible", 2, "orange"
    else:
        label, stars, color = "Très faible", 1, "red"

    return {
        "score": total,
        "label": label,
        "stars": stars,
        "color": color,
        "details": details_map,
    }


def _parse_groq_valid_cell(val: Any) -> bool | None:
    if val is None or (isinstance(val, float) and str(val) == "nan"):
        return None
    s = str(val).strip()
    if s == "" or s.lower() == "nan":
        return None
    if s.lower() in ("true", "1", "yes"):
        return True
    if s.lower() in ("false", "0", "no"):
        return False
    return None


def compute_scores_for_dataframe(df: Any) -> list[dict[str, Any]]:
    """
    Applique compute_confidence_score sur tout un DataFrame market_fr.csv.
    Retourne une liste de dicts avec brand, model, confidence.
    """
    import pandas as pd

    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame")

    results: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            gconf: float | None = None
            if "groq_confidence" in row.index and pd.notna(row.get("groq_confidence")):
                gconf = float(row["groq_confidence"])
            gvalid = _parse_groq_valid_cell(row.get("groq_valid") if "groq_valid" in row.index else None)

            score = compute_confidence_score(
                nb_sources=int(row.get("nb_sources", 1) or 1),
                price_min=float(row.get("price_min", 0) or 0),
                price_max=float(row.get("price_max", 0) or 0),
                price_avg=float(row.get("price_avg", 0) or 0),
                updated_at=str(row.get("updated_at", "") or ""),
                groq_confidence=gconf,
                groq_valid=gvalid,
                brand=str(row.get("brand", "") or ""),
                model=str(row.get("model", "") or ""),
            )
        except Exception:
            score = {
                "score": 0,
                "label": "Inconnu",
                "stars": 1,
                "color": "red",
                "details": {},
            }

        results.append(
            {
                "brand": str(row.get("brand", "") or ""),
                "model": str(row.get("model", "") or ""),
                "confidence": score,
            }
        )
    return results
