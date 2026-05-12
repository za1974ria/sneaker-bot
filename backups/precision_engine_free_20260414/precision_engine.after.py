"""Precision Engine 100% local: stats + regles (sans API externe)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_DB_PATH = _ROOT / "data" / "price_history.db"


def _risk_from_score(score: int) -> str:
    if score >= 85:
        return "LOW"
    if score >= 70:
        return "MEDIUM"
    return "HIGH"


def _load_history_df(brand: str, model: str, market: str = "FR", limit: int = 120) -> pd.DataFrame:
    if not _DB_PATH.is_file():
        return pd.DataFrame(columns=["price_avg", "price_min", "price_max", "nb_sources", "recorded_at"])
    query = """
        SELECT price_avg, price_min, price_max, nb_sources, recorded_at
        FROM price_history
        WHERE brand = ? AND model = ? AND market = ?
        ORDER BY recorded_at DESC
        LIMIT ?
    """
    try:
        with sqlite3.connect(str(_DB_PATH), timeout=15.0) as conn:
            df = pd.read_sql_query(query, conn, params=[brand, model, market, int(limit)])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PrecisionEngine] lecture historique impossible %s %s: %s", brand, model, exc)
        return pd.DataFrame(columns=["price_avg", "price_min", "price_max", "nb_sources", "recorded_at"])
    if df.empty:
        return df
    for col in ("price_avg", "price_min", "price_max", "nb_sources"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["price_avg"]).copy()


def validate_price(product: dict) -> dict:
    """
    Validation locale d'un prix produit.

    Retour:
    {
      "confidence_score": int (0-100),
      "is_suspicious": bool,
      "risk_level": "LOW"|"MEDIUM"|"HIGH",
      "explanation": "texte court en français",
      "suggested_price": int ou null,
      "reasoning": "explication détaillée"
    }
    """
    brand = str(product.get("brand") or "").strip()
    model = str(product.get("model") or "").strip()
    site = str(product.get("site") or product.get("source") or "").strip()
    stock_raw = product.get("stock")
    market = str(product.get("market") or "FR").strip().upper() or "FR"
    try:
        current_price = float(product.get("current_price", product.get("price")))
    except (TypeError, ValueError):
        return {
            "confidence_score": 20,
            "is_suspicious": True,
            "risk_level": "HIGH",
            "explanation": "Prix actuel invalide ou absent.",
            "suggested_price": None,
            "reasoning": "Impossible de valider sans prix numerique.",
        }

    df = _load_history_df(brand, model, market=market)
    if df.empty:
        return {
            "confidence_score": 74,
            "is_suspicious": False,
            "risk_level": "MEDIUM",
            "explanation": "Historique insuffisant, validation partielle.",
            "suggested_price": int(round(current_price)),
            "reasoning": "Aucun historique disponible dans price_history.db pour ce modele.",
        }

    avg_hist = float(df["price_avg"].mean())
    min_hist = float(df["price_min"].min())
    max_hist = float(df["price_max"].max())
    std_hist = float(df["price_avg"].std(ddof=0)) if len(df) > 1 else 0.0

    # Z-score robuste (fallback std faible).
    denom = std_hist if std_hist > 1e-9 else max(avg_hist * 0.08, 1.0)
    z_score = abs((current_price - avg_hist) / denom)

    # IsolationForest sur historique + point courant.
    arr_hist = df["price_avg"].to_numpy(dtype=float).reshape(-1, 1)
    contamination = min(0.25, max(0.05, 1.0 / max(8, len(arr_hist))))
    iso = IsolationForest(
        n_estimators=120,
        contamination=contamination,
        random_state=42,
    )
    iso.fit(arr_hist)
    iso_pred = int(iso.predict(np.array([[current_price]], dtype=float))[0])  # 1 normal, -1 anomalie
    iso_score = float(iso.score_samples(np.array([[current_price]], dtype=float))[0])

    drop_pct = ((avg_hist - current_price) / avg_hist * 100.0) if avg_hist > 0 else 0.0
    below_min_ratio = (current_price / min_hist) if min_hist > 0 else 1.0

    # Regles metier.
    reasons: list[str] = []
    penalty = 0
    if current_price < min_hist * 0.85:
        penalty += 35
        reasons.append("prix tres en dessous du minimum historique")
    if drop_pct > 35.0:
        penalty += 30
        reasons.append("baisse brutale superieure a 35%")
    if z_score >= 3.0:
        penalty += 22
        reasons.append("z-score eleve (outlier statistique)")
    elif z_score >= 2.0:
        penalty += 12
        reasons.append("z-score modere (ecart important)")
    if iso_pred == -1:
        penalty += 20
        reasons.append("IsolationForest detecte une anomalie")
    if stock_raw is not None:
        try:
            stock = float(stock_raw)
            if stock >= 300 and any(k in model.lower() for k in ("limited", "rare", "og", "retro")):
                penalty += 10
                reasons.append("stock eleve incoherent avec modele potentiellement rare")
        except (TypeError, ValueError):
            pass

    confidence = int(max(0, min(100, round(100 - penalty))))
    is_suspicious = confidence < 75 or penalty >= 30
    risk_level = _risk_from_score(confidence)
    suggested_price = int(round(avg_hist))

    if is_suspicious:
        logger.warning(
            "[PrecisionEngine] suspicious %s %s site=%s price=%.2f avg=%.2f min=%.2f z=%.2f iso=%.4f conf=%d",
            brand,
            model,
            site,
            current_price,
            avg_hist,
            min_hist,
            z_score,
            iso_score,
            confidence,
        )

    if reasons:
        explanation = f"Prix suspect: {reasons[0]}."
    else:
        explanation = "Prix coherent avec l'historique recent."

    reasoning = (
        f"Site={site or 'N/A'}; hist_n={len(df)}; avg_hist={avg_hist:.2f}; "
        f"min_hist={min_hist:.2f}; max_hist={max_hist:.2f}; current={current_price:.2f}; "
        f"drop_pct={drop_pct:.2f}; below_min_ratio={below_min_ratio:.3f}; "
        f"z_score={z_score:.3f}; iso_pred={iso_pred}; iso_score={iso_score:.4f}; "
        f"penalty={penalty}; rules={', '.join(reasons) if reasons else 'aucune'}."
    )

    return {
        "confidence_score": confidence,
        "is_suspicious": bool(is_suspicious),
        "risk_level": risk_level,
        "explanation": explanation,
        "suggested_price": suggested_price,
        "reasoning": reasoning,
    }

