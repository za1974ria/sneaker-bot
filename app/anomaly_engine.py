"""
Moteur global de détection d'anomalies de prix — SneakerBot.

Par modèle, détecte :
  ① Low outliers  : prix < Q1 − 1.5·IQR OU < plancher absolu marque
  ② High outliers : prix > Q3 + 1.5·IQR OU > plafond absolu marque
  ③ Keyword false match : prix hors fourchette attendue (>30% des sources)
  ④ Women/kids mismatch : prix trop bas pour adulte, ou femme→hommes
  ⑤ Prix fiable = [Q1, Q3] (after exclusion outliers)
  ⑥ Stabilité marché : instable / variable / stable / fiable
  ⑦ Anomaly score 0-10

Produit 7 colonnes dans market_fr.csv — aucun refactor global.
"""

from __future__ import annotations

import csv
import logging
import os
import statistics
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Chemins ──────────────────────────────────────────────────────────────────
_DATA = Path(__file__).resolve().parent.parent / "data"
SOURCES_CSV = _DATA / "market_fr_sources.csv"
MARKET_CSV  = _DATA / "market_fr.csv"

# ── Nouvelles colonnes ────────────────────────────────────────────────────────
ANOMALY_COLUMNS: list[str] = [
    "price_q1",
    "price_q3",
    "outliers_low",
    "outliers_high",
    "anomaly_flags",
    "market_stability",
    "anomaly_score",
]

# ── Seuils absolus par marque (min€, max€) ────────────────────────────────────
# Women's models: même plafond haut, plancher légèrement plus bas (-10 €)
_BRAND_BOUNDS: dict[str, tuple[float, float]] = {
    "Nike":        (50.0, 350.0),
    "Adidas":      (40.0, 280.0),
    "New Balance": (50.0, 320.0),
    "Salomon":     (80.0, 350.0),
    "Asics":       (50.0, 300.0),
    "Puma":        (35.0, 220.0),
    "Reebok":      (35.0, 220.0),
    "Vans":        (35.0, 180.0),
    "Converse":    (35.0, 180.0),
    "On Running":  (80.0, 320.0),
    "default":     (30.0, 400.0),
}

# Modèles/familles premium autorisés à dépasser le plafond standard de marque
_PREMIUM_MODEL_KEYWORDS: frozenset[str] = frozenset({
    "990v6", "990v5", "made in usa", "made in uk", "gore-tex", "gtx",
    "collab", "limited", "off-white", "sacai", "union",
})

# Mots indiquant une edition femme dans le nom du modèle
_WOMEN_KEYWORDS: frozenset[str] = frozenset({
    "women", "femme", "wmns", "woman", "women's",
})

# Mots parasites faux-match (dans la description boutique – non présents dans sources CSV,
# mais on applique la détection via un spread anormal)
_FALSE_MATCH_OUTLIER_RATIO = 0.30   # > 30 % des prix hors range marque → keyword_mismatch

# ── Stabilité ────────────────────────────────────────────────────────────────
_STABILITY_FIABLE   = ("fiable",    "Fiabilité élevée")
_STABILITY_STABLE   = ("stable",    "")
_STABILITY_VARIABLE = ("variable",  "")
_STABILITY_INSTABLE = ("instable",  "Marché instable")


def _brand_abs_bounds(brand: str, model: str) -> tuple[float, float]:
    """Retourne (abs_min, abs_max) en € pour le couple brand/model."""
    lo, hi = _BRAND_BOUNDS.get(brand, _BRAND_BOUNDS["default"])
    # Modèles premium tolèrent un plafond +30%
    model_l = model.lower()
    if any(k in model_l for k in _PREMIUM_MODEL_KEYWORDS):
        hi = hi * 1.30
    # Women: plancher légèrement plus bas (-10€ plancher)
    if any(w in model_l for w in _WOMEN_KEYWORDS):
        lo = max(25.0, lo - 10.0)
    return lo, hi


def _iqr_stats(prices: list[float]) -> tuple[float, float, float, float, float]:
    """Retourne (q1, q3, iqr, lo_fence, hi_fence). Fallback si < 4 pts."""
    n = len(prices)
    if n < 4:
        mn, mx = min(prices), max(prices)
        q1 = mn
        q3 = mx
        iqr = mx - mn
        return q1, q3, iqr, mn, mx
    try:
        q1, _, q3 = statistics.quantiles(sorted(prices), n=4, method="inclusive")
    except TypeError:
        q1, _, q3 = statistics.quantiles(sorted(prices), n=4)
    q1, q3 = float(q1), float(q3)
    iqr = q3 - q1
    return q1, q3, iqr, q1 - 1.5 * iqr, q3 + 1.5 * iqr


def analyze_model(
    brand: str,
    model: str,
    raw_prices: list[float],
) -> dict[str, Any]:
    """
    Analyse les prix bruts (une valeur par source/boutique) d'un modèle.

    Retourne :
    {
        "price_q1":        float | None,
        "price_q3":        float | None,
        "outliers_low":    int,
        "outliers_high":   int,
        "anomaly_flags":   str,   # séparé par ","
        "market_stability":str,   # fiable/stable/variable/instable
        "anomaly_score":   int,   # 0-10
    }
    """
    empty: dict[str, Any] = {
        "price_q1": None, "price_q3": None,
        "outliers_low": 0, "outliers_high": 0,
        "anomaly_flags": "", "market_stability": "variable", "anomaly_score": 0,
    }
    if not raw_prices:
        return empty

    prices = sorted(p for p in raw_prices if p > 0)
    if not prices:
        return empty

    abs_min, abs_max = _brand_abs_bounds(brand, model)
    is_women = any(w in model.lower() for w in _WOMEN_KEYWORDS)

    # ── ① ② Outliers absolus marque ──────────────────────────────────────────
    brand_lo_out = [p for p in prices if p < abs_min]
    brand_hi_out = [p for p in prices if p > abs_max]
    in_brand = [p for p in prices if abs_min <= p <= abs_max]

    # Si moins de 30% dans la plage marque, on garde quand même tout (données rares)
    if in_brand and len(in_brand) >= max(2, len(prices) * 0.30):
        working = in_brand
    else:
        working = prices

    # ── IQR sur les prix dans la plage marque ────────────────────────────────
    if len(working) >= 2:
        q1, q3, iqr, lo_fence, hi_fence = _iqr_stats(working)
        iqr_lo_out = [p for p in working if p < lo_fence]
        iqr_hi_out = [p for p in working if p > hi_fence]
        fiable = [p for p in working if lo_fence <= p <= hi_fence]
    else:
        q1, q3 = float(working[0]), float(working[-1])
        iqr_lo_out, iqr_hi_out = [], []
        fiable = working[:]

    if not fiable:
        fiable = working[:]

    # ── Prix fiable : Q1 → Q3 ────────────────────────────────────────────────
    if len(fiable) >= 2:
        q1_fiable, q3_fiable, _, _, _ = _iqr_stats(fiable)
    else:
        q1_fiable = q3_fiable = float(fiable[0]) if fiable else 0.0

    # ── ③ Keyword false match ─────────────────────────────────────────────────
    out_of_brand = len(brand_lo_out) + len(brand_hi_out)
    keyword_mismatch = (out_of_brand / max(1, len(prices))) > _FALSE_MATCH_OUTLIER_RATIO

    # ── ④ Women / Kids mismatch ───────────────────────────────────────────────
    kids_suspect = False
    women_mismatch = False
    if fiable:
        med = statistics.median(fiable)
        # Modèle adulte (pas women) avec médiane < plancher adulte * 0.75 → kids
        if not is_women and med < abs_min * 0.75:
            kids_suspect = True
        # Modèle women mais prix médiane = prix hommes haut → sourcing hommes
        if is_women and med > abs_max * 0.95:
            women_mismatch = True

    # ── ⑤ Stabilité ─────────────────────────────────────────────────────────
    total_outliers = len(brand_lo_out) + len(brand_hi_out) + len(iqr_lo_out) + len(iqr_hi_out)
    if fiable and len(fiable) >= 2:
        spread_pct = (max(fiable) - min(fiable)) / statistics.median(fiable) * 100
    elif fiable:
        spread_pct = 0.0
    else:
        spread_pct = 999.0

    nb_prices = len(prices)
    if spread_pct <= 50 and total_outliers == 0 and nb_prices >= 5:
        stability = "fiable"
    elif spread_pct <= 70 and total_outliers <= 1 and nb_prices >= 3:
        stability = "stable"
    elif spread_pct <= 120 and total_outliers <= 3:
        stability = "variable"
    else:
        stability = "instable"

    # ── ⑥ Flags ──────────────────────────────────────────────────────────────
    flags: list[str] = []
    if brand_lo_out:  flags.append("low_outliers")
    if brand_hi_out:  flags.append("high_outliers")
    if iqr_lo_out:    flags.append("iqr_low")
    if iqr_hi_out:    flags.append("iqr_high")
    if kids_suspect:  flags.append("kids_suspect")
    if women_mismatch:flags.append("women_mismatch")
    if keyword_mismatch: flags.append("keyword_mismatch")

    # ── ⑦ Anomaly score 0-10 ─────────────────────────────────────────────────
    score = 0
    score += min(3, len(brand_hi_out))        # High brand outliers (dangereux)
    score += min(2, len(brand_lo_out))         # Low brand outliers
    score += min(2, len(iqr_hi_out))           # IQR high
    score += min(1, len(iqr_lo_out))           # IQR low
    score += 1 if kids_suspect else 0
    score += 1 if women_mismatch else 0
    score += 1 if keyword_mismatch and not (brand_lo_out or brand_hi_out) else 0
    score = min(10, score)

    return {
        "price_q1":        round(q1_fiable, 2),
        "price_q3":        round(q3_fiable, 2),
        "outliers_low":    len(brand_lo_out) + len(iqr_lo_out),
        "outliers_high":   len(brand_hi_out) + len(iqr_hi_out),
        "anomaly_flags":   ",".join(flags),
        "market_stability": stability,
        "anomaly_score":   score,
    }


def classify_anomaly_type(
    price_min: float,
    price_median: float,
    price_avg: float,
    anomaly_flags: str,
    market_stability: str,
    brand: str = "",
    model: str = "",
    source_count: int = 0,
) -> dict[str, str]:
    """
    Classifie le type d'anomalie pour les offres à prix bas.
    Retourne {"type": TYPE, "badge": LABEL_AFFICHAGE, "color": HEX}

    Types : PROMO_FLASH | LAST_SIZE | CLEARANCE | KIDS_VARIANT |
            USED_POSSIBLE | SCRAPING_ERROR | UNKNOWN | NORMAL
    """
    if price_avg <= 0 or price_median <= 0:
        return {"type": "UNKNOWN", "badge": "Vérification recommandée", "color": "#f59e0b"}

    ratio = price_min / price_median if price_median > 0 else 1.0
    flags = set(f.strip() for f in (anomaly_flags or "").split(",") if f.strip())
    model_l = (model or "").lower()
    brand_l = (brand or "").lower()

    # NORMAL : pas d'écart significatif
    if ratio >= 0.80:
        return {"type": "NORMAL", "badge": "", "color": ""}

    # KIDS_VARIANT : suspicion taille enfant détectée par le moteur
    if "kids_suspect" in flags:
        return {"type": "KIDS_VARIANT", "badge": "Variante enfant possible", "color": "#f59e0b"}

    # SCRAPING_ERROR : données très incohérentes + prix extrêmement bas
    if ratio < 0.30 and source_count <= 2 and market_stability == "instable":
        return {"type": "SCRAPING_ERROR", "badge": "Erreur de collecte probable", "color": "#ef4444"}

    # CLEARANCE : bas, marché instable, keyword mismatch
    if ratio < 0.55 and ("keyword_mismatch" in flags or market_stability == "instable"):
        return {"type": "CLEARANCE", "badge": "Liquidation possible", "color": "#f97316"}

    # LAST_SIZE : bas mais marché stable (peu de stock restant)
    if ratio < 0.70 and market_stability in ("stable", "fiable") and source_count >= 3:
        return {"type": "LAST_SIZE", "badge": "Dernière taille possible", "color": "#22c55e"}

    # PROMO_FLASH : modèle populaire + prix bas temporaire + marché variable
    popular_brands = {"nike", "adidas", "new balance", "jordan", "yeezy", "asics", "puma", "reebok"}
    is_popular = any(b in brand_l for b in popular_brands)
    if is_popular and ratio < 0.75 and market_stability in ("variable", "stable"):
        return {"type": "PROMO_FLASH", "badge": "Promo détectée", "color": "#6366f1"}

    # USED_POSSIBLE : marché très variable + prix très bas
    if ratio < 0.60 and market_stability == "instable":
        return {"type": "USED_POSSIBLE", "badge": "Occasion possible", "color": "#8b5cf6"}

    # Fallback
    if ratio < 0.80:
        return {"type": "UNKNOWN", "badge": "Offre atypique — vérification recommandée", "color": "#f59e0b"}

    return {"type": "NORMAL", "badge": "", "color": ""}


def load_raw_prices_from_sources(
    sources_csv: Path = SOURCES_CSV,
) -> dict[tuple[str, str], list[float]]:
    """
    Lit market_fr_sources.csv.
    Retourne un dict {(brand, model): [price_avg, …]} (un float par shop).
    """
    prices: dict[tuple[str, str], list[float]] = {}
    if not sources_csv.is_file():
        return prices
    try:
        with open(sources_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                b = str(row.get("brand") or "").strip()
                m = str(row.get("model") or "").strip()
                if not b or not m:
                    continue
                try:
                    p = float(row.get("price_avg") or 0)
                    if p > 0:
                        prices.setdefault((b, m), []).append(p)
                except (ValueError, TypeError):
                    pass
    except Exception as exc:
        logger.error("anomaly_engine: lecture sources CSV: %s", exc)
    return prices


def run_anomaly_engine(
    market_csv: Path = MARKET_CSV,
    sources_csv: Path = SOURCES_CSV,
) -> int:
    """
    Applique l'analyse d'anomalies sur tous les modèles de market_fr.csv.
    Met à jour les colonnes anomalie en place (atomic write).
    Retourne le nombre de lignes traitées.
    """
    if not market_csv.is_file():
        logger.warning("anomaly_engine: %s absent", market_csv)
        return 0

    raw_prices = load_raw_prices_from_sources(sources_csv)

    with open(market_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames: list[str] = list(reader.fieldnames or [])
        rows = list(reader)

    # Ajoute les colonnes manquantes
    for col in ANOMALY_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)

    updated = 0
    for row in rows:
        brand = str(row.get("brand") or "").strip()
        model = str(row.get("model") or "").strip()
        prices = raw_prices.get((brand, model), [])
        result = analyze_model(brand, model, prices)
        for k, v in result.items():
            row[k] = str(v) if v is not None else ""
        updated += 1

    tmp = market_csv.with_suffix(".tmp")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, market_csv)
    except Exception as exc:
        logger.error("anomaly_engine: écriture CSV: %s", exc)
        tmp.unlink(missing_ok=True)
        return 0

    logger.info("anomaly_engine: %d modèles analysés (%d avec prix sources)", updated, len(raw_prices))
    return updated
