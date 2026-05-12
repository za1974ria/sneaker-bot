"""Parallel v2 endpoints for strict matching market view."""

from __future__ import annotations

import json
import logging
import sqlite3
from statistics import median, mean
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["v2"])
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "sneakerbot.db"

# Prix plancher par marque — filtre les offres marketplace clairement fausses.
_BRAND_MIN_PRICE: dict[str, float] = {
    "Nike": 40.0,
    "Jordan": 70.0,
    "Adidas": 38.0,
    "New Balance": 45.0,
    "Asics": 45.0,
    "Converse": 30.0,
    "Vans": 28.0,
    "Reebok": 30.0,
    "Puma": 30.0,
    "Salomon": 70.0,
    "On Running": 80.0,
    "Hoka": 80.0,
    "Saucony": 50.0,
    "Brooks": 60.0,
    "Mizuno": 50.0,
    "New Balance": 45.0,
}
_DEFAULT_MIN_PRICE = 25.0


def _brand_min_price(brand: str) -> float:
    """Prix plancher pour une marque — élimine les offres manifestement fausses."""
    for key, floor in _BRAND_MIN_PRICE.items():
        if key.lower() in brand.lower():
            return floor
    return _DEFAULT_MIN_PRICE


def _remove_outliers_v2(prices: list[float]) -> list[float]:
    """
    IQR-based outlier removal — version autonome pour v2_routes (pas d'import circulaire).
    Retourne les prix nettoyés. Si pas assez de données pour filtrer, retourne les originaux.
    """
    if len(prices) < 4:
        return prices
    sp = sorted(prices)
    q1 = sp[int(len(sp) * 0.25)]
    q3 = sp[int(len(sp) * 0.75)]
    iqr = q3 - q1
    if iqr == 0:
        return prices
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    filtered = [p for p in prices if lower <= p <= upper]
    # Sécurité : ne pas trop supprimer
    if len(filtered) < max(2, len(prices) // 2):
        return prices
    return filtered


# ── helpers ──────────────────────────────────────────────────────────────────

def _conf_to_credibility(confidence: str) -> str:
    return "high" if confidence == "strong" else ("medium" if confidence == "medium" else "low")


def _market_state(price_min: float, price_max: float, price_avg: float) -> tuple[str, float]:
    spread = (price_max - price_min) / price_avg if price_avg > 0 else 0.0
    if spread > 0.40:
        return "unstable", spread
    if spread > 0.20:
        return "variable", spread
    return "stable", spread


def _recommandation(price_avg: float, price_min: float, price_max: float) -> str:
    if price_avg <= price_min * 1.05:
        return "💰 PRIX LE PLUS BAS"
    if price_avg <= (price_min + price_max) / 2 * 1.05:
        return "✅ PRIX MARCHÉ"
    if price_avg >= price_max * 0.92:
        return "⚠️ PRIX ÉLEVÉ"
    return "👍 BON PRIX"


def _position_client(price_avg: float, price_min: float, price_max: float) -> str:
    if price_max == price_min:
        return "Milieu de gamme"
    pct = (price_avg - price_min) / (price_max - price_min)
    if pct <= 0.10:
        return "Top 10%"
    if pct <= 0.25:
        return "Top 25%"
    if pct <= 0.65:
        return "Milieu de gamme"
    return "Haut de gamme"


@router.get("/canonical-id")
def get_v2_canonical_id(
    brand: str = Query(default=""),
    model: str = Query(default=""),
):
    """Return canonical_id for a given brand + model (used by the frontend JS)."""
    brand = brand.strip()
    model = model.strip()
    if not brand or not model:
        raise HTTPException(status_code=400, detail="brand et model requis")
    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT canonical_id FROM canonical_products WHERE brand=? AND model=?",
            (brand, model),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Produit introuvable dans le catalogue v2")
    return {"canonical_id": row["canonical_id"], "brand": brand, "model": model}


@router.get("/comparison/fr")
def get_v2_comparison_fr(
    brand: str = Query(default=""),
    model: str = Query(default=""),
):
    """V2 equivalent of /api/comparison/fr — returns items[] in v1-compatible format
    enriched with v2-specific fields (v2_confidence, v2_median, v2_outliers_removed)."""
    brand = brand.strip()
    model = model.strip()
    if not brand or not model:
        return {"items": [], "count": 0, "v2_enabled": True}

    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        conn.row_factory = sqlite3.Row

        product = conn.execute(
            "SELECT canonical_id, brand, model, image_url FROM canonical_products WHERE brand=? AND model=?",
            (brand, model),
        ).fetchone()
        if not product:
            return {"items": [], "count": 0, "v2_enabled": True}

        canonical_id = product["canonical_id"]

        offers = conn.execute(
            """SELECT source, price_eur, url, confidence
               FROM matched_rows_v2
               WHERE canonical_id=?
               ORDER BY price_eur ASC""",
            (canonical_id,),
        ).fetchall()

    if not offers:
        return {"items": [], "count": 0, "v2_enabled": True}

    # Étape 1 : filtre plancher par marque (élimine offres marketplace manifestement fausses)
    min_floor = _brand_min_price(brand)
    raw_offers = list(offers)
    offers_filtered = [o for o in raw_offers if float(o["price_eur"]) >= min_floor]
    if not offers_filtered:
        # Plancher trop strict — on garde les originaux
        offers_filtered = raw_offers
    n_before_floor = len(raw_offers)
    n_after_floor = len(offers_filtered)

    # Étape 2 : IQR outlier removal sur les prix restants
    all_prices_clean = [float(o["price_eur"]) for o in offers_filtered]
    clean_prices = _remove_outliers_v2(all_prices_clean)
    clean_price_set = set(clean_prices)
    # Garder les offres dont le prix est dans clean_prices (peut avoir des ex-aequo)
    offers_clean: list = []
    remaining = list(clean_prices)
    for o in offers_filtered:
        p = float(o["price_eur"])
        if p in clean_price_set and p in remaining:
            offers_clean.append(o)
            remaining.remove(p)
    if not offers_clean:
        offers_clean = offers_filtered  # fallback sécurité

    n_outliers_removed = n_before_floor - len(offers_clean)

    prices   = [float(o["price_eur"]) for o in offers_clean]
    sources  = [str(o["source"] or "") for o in offers_clean]
    confs    = [str(o["confidence"] or "") for o in offers_clean]
    p_min    = round(min(prices), 2)
    p_max    = round(max(prices), 2)
    p_avg    = round(mean(prices), 2)
    p_median = round(median(prices), 2)
    n        = len(prices)

    if n_outliers_removed > 0:
        logger.info(
            "v2_comparison_fr %s %s : %d offres filtrées (plancher %.0f€ + IQR), %d retenues",
            brand, model, n_outliers_removed, min_floor, n,
        )

    # overall credibility = best confidence seen
    credibility = _conf_to_credibility("strong" if "strong" in confs else "medium")
    mstate, disp = _market_state(p_min, p_max, p_avg)
    score = min(100, 55 + n * 4) if credibility == "high" else min(80, 45 + n * 3)

    # ── Trust Engine — enrichit le résultat v2 avec score de confiance explicable ──
    trust_report = None
    try:
        from app.trust_engine import build_trust_report as _btr
        # Convertir les offres v2 au format attendu par trust_engine (shop + price_avg)
        trust_sources = [
            {"shop": str(o["source"] or ""), "price_avg": float(o["price_eur"])}
            for o in offers_clean
        ]
        trust_report = _btr(
            brand=brand,
            model=model,
            sources=trust_sources,
            market_meta={"confidence_score": score},
        )
        # Utiliser le trust_score comme score canonique si disponible
        if trust_report and trust_report.get("trust_score"):
            score = trust_report["trust_score"]
    except Exception as _te:
        logger.warning("v2 trust_engine error %s %s: %s", brand, model, _te)

    item = {
        # v1-compatible core fields
        "brand":             brand,
        "model":             model,
        "shop":              "",
        "price_min":         p_min,
        "price_avg":         p_avg,
        "price_max":         p_max,
        "price_count":       n,
        "source_count":      n,
        "sources_preview":   sources[:5],
        "price_confidence":  "high" if credibility == "high" else "medium",
        "credibility":       credibility,
        "score":             score,
        "excluded":          False,
        "exclusion_reason":  "",
        "validated":         True,
        "validation_reasons": [],
        "google_badge":      "none",
        "google_deviation_pct": None,
        "recommandation":    _recommandation(p_avg, p_min, p_max),
        "position_client":   _position_client(p_avg, p_min, p_max),
        "market_state":      mstate,
        "dispersion":        round(disp, 4),
        "has_history":       False,
        # Trust Engine
        "trust_report":      trust_report,
        # v2-specific extras displayed by comparison_page.v2.js
        "v2_enabled":        True,
        "v2_confidence":     "strong" if "strong" in confs else "medium",
        "v2_median":         p_median,
        "v2_sources_count":  n,
        "v2_outliers_removed": n_outliers_removed,
        "v2_offers":         [
            {"source": str(o["source"] or ""), "price": float(o["price_eur"]),
             "confidence": str(o["confidence"] or "")}
            for o in offers_clean
        ],
    }
    return {"items": [item], "count": 1, "v2_enabled": True}


@router.get("/price-comparison/{canonical_id}")
def get_v2_price_comparison(canonical_id: str):
    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        product = conn.execute(
            """
            SELECT canonical_id, brand, model, ref, color, gender, category, variant, image_url, aliases
            FROM canonical_products WHERE canonical_id=?
            """,
            (canonical_id,),
        ).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="canonical_id introuvable")

        offers = conn.execute(
            """
            SELECT source, price_eur, url, confidence
            FROM matched_rows_v2
            WHERE canonical_id=?
            ORDER BY price_eur ASC
            """,
            (canonical_id,),
        ).fetchall()

    offer_list = [
        {
            "source": str(o["source"] or ""),
            "price": float(o["price_eur"] or 0.0),
            "url": str(o["url"] or ""),
            "confidence": str(o["confidence"] or "medium"),
        }
        for o in offers
    ]
    prices = [o["price"] for o in offer_list]
    if not prices:
        return {
            "canonical_id": canonical_id,
            "product": {"canonical_id": canonical_id},
            "prices": {"min": None, "max": None, "median": None, "sources_count": 0, "confidence": "NO_MATCH"},
            "offers": [],
        }
    confidence = "strong" if any(o["confidence"] == "strong" for o in offer_list) else "medium"
    return {
        "canonical_id": canonical_id,
        "product": {
            "canonical_id": str(product["canonical_id"]),
            "brand": str(product["brand"] or ""),
            "model": str(product["model"] or ""),
            "ref": str(product["ref"] or ""),
            "color": str(product["color"] or ""),
            "gender": str(product["gender"] or ""),
            "category": str(product["category"] or ""),
            "variant": product["variant"],
            "image_url": str(product["image_url"] or ""),
            "aliases": json.loads(str(product["aliases"] or "[]")),
        },
        "prices": {
            "min": round(min(prices), 2),
            "max": round(max(prices), 2),
            "median": round(median(prices), 2),
            "sources_count": len(offer_list),
            "confidence": confidence,
        },
        "offers": offer_list,
    }
