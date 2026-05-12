"""Precision Engine 100% local: stats + regles (sans API externe)."""

from __future__ import annotations

import logging
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_DB_PATH = _ROOT / "data" / "price_history.db"
_WEAK_MODEL_TOKENS = {
    "low",
    "mid",
    "high",
    "og",
    "retro",
    "premium",
    "essential",
    "se",
    "gs",
}
_COMMON_MODEL_HINTS = {
    "dunk low",
    "air force",
    "air max",
    "jordan",
    "yeezy",
    "new balance",
}


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


def _normalize_text(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _model_signature(model: str) -> str:
    tokens = [t for t in _normalize_text(model).split() if t and t not in _WEAK_MODEL_TOKENS]
    if not tokens:
        tokens = [t for t in _normalize_text(model).split() if t]
    return " ".join(tokens)


def _contains_common_hint(model: str) -> bool:
    sig = _model_signature(model)
    return any(h in sig for h in _COMMON_MODEL_HINTS)


def _load_fuzzy_history_df(brand: str, model: str, market: str = "FR", limit: int = 120) -> tuple[pd.DataFrame, str]:
    """
    Fallback fuzzy quand l'historique exact est vide.
    Retourne (dataframe, match_quality).
    """
    if not _DB_PATH.is_file():
        return pd.DataFrame(), "none"
    brand_n = _normalize_text(brand)
    model_sig = _model_signature(model)
    model_sig_nospace = model_sig.replace(" ", "")
    common_model = _contains_common_hint(model)
    query = """
        SELECT brand, model, market, price_avg, price_min, price_max, nb_sources, recorded_at
        FROM price_history
        WHERE market = ?
        ORDER BY recorded_at DESC
        LIMIT 5000
    """
    try:
        with sqlite3.connect(str(_DB_PATH), timeout=15.0) as conn:
            raw = pd.read_sql_query(query, conn, params=[market])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PrecisionEngine] fuzzy read impossible %s %s: %s", brand, model, exc)
        return pd.DataFrame(), "none"
    if raw.empty:
        return pd.DataFrame(), "none"

    raw["brand_n"] = raw["brand"].map(lambda x: _normalize_text(str(x)))
    raw["model_sig"] = raw["model"].map(lambda x: _model_signature(str(x)))
    raw["model_sig_ns"] = raw["model_sig"].str.replace(" ", "", regex=False)

    candidates = raw[raw["brand_n"] == brand_n].copy()
    match_quality = "brand_only"
    if candidates.empty:
        # Fallback ultime: fuzzy brand + model
        raw["brand_ratio"] = raw["brand_n"].map(lambda x: SequenceMatcher(None, brand_n, str(x)).ratio())
        candidates = raw[raw["brand_ratio"] >= 0.78].copy()
        if candidates.empty:
            return pd.DataFrame(), "none"
        match_quality = "brand_fuzzy"

    def _score_row(row: pd.Series) -> float:
        ms = str(row.get("model_sig") or "")
        ms_ns = str(row.get("model_sig_ns") or "")
        r1 = SequenceMatcher(None, model_sig, ms).ratio()
        r2 = SequenceMatcher(None, model_sig_nospace, ms_ns).ratio() if model_sig_nospace and ms_ns else 0.0
        bonus = 0.0
        if model_sig and model_sig in ms:
            bonus += 0.08
        if model_sig_nospace and model_sig_nospace in ms_ns:
            bonus += 0.08
        if common_model and any(h in ms for h in _COMMON_MODEL_HINTS):
            bonus += 0.05
        return max(r1, r2) + bonus

    candidates["model_score"] = candidates.apply(_score_row, axis=1)
    threshold = 0.74 if common_model else 0.78
    picked = candidates[candidates["model_score"] >= threshold].copy()
    if picked.empty:
        top = candidates.sort_values("model_score", ascending=False).head(1)
        if top.empty or float(top["model_score"].iloc[0]) < 0.70:
            return pd.DataFrame(), "none"
        picked = top
        match_quality = "fuzzy_low"
    elif match_quality == "brand_fuzzy":
        match_quality = "fuzzy_medium"
    else:
        match_quality = "fuzzy_high"

    picked = picked.sort_values("recorded_at", ascending=False).head(max(1, int(limit))).copy()
    for col in ("price_avg", "price_min", "price_max", "nb_sources"):
        picked[col] = pd.to_numeric(picked[col], errors="coerce")
    picked = picked.dropna(subset=["price_avg"])
    return picked[["price_avg", "price_min", "price_max", "nb_sources", "recorded_at"]], match_quality


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
    match_quality = "exact"
    if df.empty:
        df, match_quality = _load_fuzzy_history_df(brand, model, market=market)
    if df.empty:
        return {
            "confidence_score": 74,
            "is_suspicious": False,
            "risk_level": "MEDIUM",
            "match_quality": "none",
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
            "[PrecisionEngine] suspicious %s %s site=%s match=%s price=%.2f avg=%.2f min=%.2f z=%.2f iso=%.4f conf=%d",
            brand,
            model,
            site,
            match_quality,
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
        f"Site={site or 'N/A'}; match_quality={match_quality}; hist_n={len(df)}; avg_hist={avg_hist:.2f}; "
        f"min_hist={min_hist:.2f}; max_hist={max_hist:.2f}; current={current_price:.2f}; "
        f"drop_pct={drop_pct:.2f}; below_min_ratio={below_min_ratio:.3f}; "
        f"z_score={z_score:.3f}; iso_pred={iso_pred}; iso_score={iso_score:.4f}; "
        f"penalty={penalty}; rules={', '.join(reasons) if reasons else 'aucune'}."
    )

    return {
        "confidence_score": confidence,
        "is_suspicious": bool(is_suspicious),
        "risk_level": risk_level,
        "match_quality": match_quality,
        "explanation": explanation,
        "suggested_price": suggested_price,
        "reasoning": reasoning,
    }

