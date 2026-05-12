"""Precision Engine 100% local: stats + regles (sans API externe)."""

from __future__ import annotations

import logging
import re
import sqlite3
import warnings
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
# Suppress sklearn joblib parallel warning that floods logs on every IsolationForest call
warnings.filterwarnings("ignore", message=".*sklearn.utils.parallel.delayed.*", category=UserWarning)
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


def _parse_iso_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _window_means(df: pd.DataFrame, now_utc: datetime) -> tuple[float | None, float | None]:
    """Moyennes glissantes 7j/30j basées sur recorded_at."""
    if df.empty or "recorded_at" not in df.columns:
        return None, None
    dates = df["recorded_at"].map(_parse_iso_utc)
    tmp = df.copy()
    tmp["_dt"] = dates
    tmp = tmp.dropna(subset=["_dt", "price_avg"])
    if tmp.empty:
        return None, None
    s7 = now_utc - timedelta(days=7)
    s30 = now_utc - timedelta(days=30)
    w7 = tmp[tmp["_dt"] >= s7]
    w30 = tmp[tmp["_dt"] >= s30]
    mean7 = float(w7["price_avg"].mean()) if not w7.empty else None
    mean30 = float(w30["price_avg"].mean()) if not w30.empty else None
    return mean7, mean30


def _weighted_baseline(df: pd.DataFrame) -> float | None:
    """
    Baseline pondérée par nb_sources historiques.
    Plus il y a de sources, plus le snapshot pèse.
    """
    if df.empty:
        return None
    vals = pd.to_numeric(df["price_avg"], errors="coerce")
    weights = pd.to_numeric(df.get("nb_sources"), errors="coerce").fillna(1.0).clip(lower=1.0)
    valid = ~(vals.isna() | weights.isna())
    if not valid.any():
        return None
    vv = vals[valid].to_numpy(dtype=float)
    ww = weights[valid].to_numpy(dtype=float)
    sw = float(ww.sum())
    if sw <= 0:
        return None
    return float(np.dot(vv, ww) / sw)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    stock_val = _safe_float(stock_raw)
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
    now_utc = datetime.now(timezone.utc)
    mean7, mean30 = _window_means(df, now_utc)
    weighted_avg = _weighted_baseline(df)
    baseline = weighted_avg if weighted_avg is not None else avg_hist
    momentum_pct = 0.0
    if mean7 is not None and mean30 is not None and mean30 > 0:
        momentum_pct = ((mean7 - mean30) / mean30) * 100.0
    trend_strength = min(100.0, abs(momentum_pct) * 2.5)

    # Z-score robuste (fallback std faible).
    denom = std_hist if std_hist > 1e-9 else max(baseline * 0.08, 1.0)
    z_score = abs((current_price - baseline) / denom)

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

    drop_pct = ((baseline - current_price) / baseline * 100.0) if baseline > 0 else 0.0
    below_min_ratio = (current_price / min_hist) if min_hist > 0 else 1.0

    # Regles metier.
    reasons: list[str] = []
    penalty = 0
    # Detection de drop soudain et anormal (phase 3).
    drop_alert = False
    if (
        mean7 is not None
        and mean30 is not None
        and mean30 > 0
        and current_price < mean7 * 0.82
        and current_price < mean30 * 0.78
    ):
        drop_alert = True
        penalty += 22
        reasons.append("drop soudain detecte (prix anormalement en baisse)")
    # Prix fantome: beaucoup trop bas vs min historique et baseline.
    # Seuils desserres pour tolerer les soldes legitimes (-30% a -40%).
    ghost_price_alert = False
    if current_price < min_hist * 0.55 and current_price < baseline * 0.60:
        ghost_price_alert = True
        penalty += 50
        reasons.append("prix fantome critique (seuil strict)")
    elif current_price < min_hist * 0.60 and current_price < baseline * 0.65:
        ghost_price_alert = True
        penalty += 25
        reasons.append("prix fantome (trop beau pour etre vrai)")
    if current_price < min_hist * 0.70:
        penalty += 20
        reasons.append("prix tres en dessous du minimum historique")
    if drop_pct > 35.0:
        penalty += 25
        reasons.append("baisse brutale superieure a 35%")
    # Velocity: acceleration de baisse/hausse court terme.
    if mean7 is not None and mean30 is not None:
        vel_delta = mean7 - mean30
        if vel_delta < 0 and abs(momentum_pct) > 18:
            penalty += 8
            reasons.append("price velocity baissiere anormale (7j vs 30j)")
        elif vel_delta > 0 and abs(momentum_pct) > 22:
            penalty += 5
            reasons.append("price velocity haussiere rapide")
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

    # Bonus de fiabilite historique selon profondeur/sources.
    hist_n = len(df)
    avg_sources = float(pd.to_numeric(df.get("nb_sources"), errors="coerce").fillna(1.0).clip(lower=1.0).mean())
    reliability_bonus = min(10.0, max(0.0, (hist_n / 20.0) * 4.0 + (avg_sources - 1.0) * 1.5))
    confidence = int(max(0, min(100, round(100 - penalty + reliability_bonus))))
    is_suspicious = confidence < 75 or penalty >= 30
    risk_level = _risk_from_score(confidence)
    # Auto-correction intelligente: combine baseline ponderee + momentum.
    suggested_float = baseline
    if mean7 is not None and mean30 is not None:
        momentum_factor = max(-0.15, min(0.15, momentum_pct / 100.0))
        suggested_float = baseline * (1.0 + momentum_factor * 0.35)
        # Precision supp: en tendance fortement baissiere, eviter de surestimer.
        if momentum_pct < -20:
            suggested_float = min(suggested_float, mean7 * 0.98)
        # En tendance haussiere forte, eviter un prix suggere trop bas.
        if momentum_pct > 20:
            suggested_float = max(suggested_float, mean7 * 1.02)
    suggested_float = max(min_hist * 0.92, min(max_hist * 1.05, suggested_float))
    suggested_price = int(round(suggested_float))

    # Overall reliability (0-100): combine confiance, qualite matching, tendance, profondeur historique.
    match_weight = {
        "exact": 100.0,
        "fuzzy_high": 88.0,
        "fuzzy_medium": 76.0,
        "fuzzy_low": 62.0,
        "none": 45.0,
    }.get(match_quality, 70.0)
    trend_reliability = max(0.0, 100.0 - min(70.0, trend_strength * 0.9))
    history_reliability = min(100.0, (hist_n / 60.0) * 100.0)
    source_reliability = min(100.0, (avg_sources / 8.0) * 100.0)
    anomaly_penalty = 0.0
    if ghost_price_alert:
        anomaly_penalty += 25.0
    if drop_alert:
        anomaly_penalty += 8.0  # was 18 — drop_alert already penalises confidence, avoid double-counting
    overall_reliability = (
        0.42 * float(confidence)
        + 0.22 * match_weight
        + 0.16 * trend_reliability
        + 0.10 * history_reliability
        + 0.10 * source_reliability
        - anomaly_penalty
    )
    overall_reliability = int(round(max(0.0, min(100.0, overall_reliability))))

    if is_suspicious:
        structured = {
            "event": "precision_engine_suspicious",
            "brand": brand,
            "model": model,
            "site": site or "N/A",
            "market": market,
            "match_quality": match_quality,
            "current_price": round(current_price, 2),
            "baseline": round(baseline, 2),
            "min_hist": round(min_hist, 2),
            "mean7": None if mean7 is None else round(float(mean7), 2),
            "mean30": None if mean30 is None else round(float(mean30), 2),
            "momentum_pct": round(float(momentum_pct), 2),
            "trend_strength": round(float(trend_strength), 2),
            "drop_alert": drop_alert,
            "ghost_price_alert": ghost_price_alert,
            "stock": stock_val,
            "confidence": confidence,
            "overall_reliability": overall_reliability,
            "risk_level": risk_level,
            "reasons": reasons,
        }
        logger.warning(
            "[PrecisionEngine][SUSPECT] %s",
            structured,
        )

    if reasons:
        explanation = f"Prix suspect: {reasons[0]}."
    else:
        explanation = "Prix coherent avec l'historique recent."

    reasoning = (
        f"Site={site or 'N/A'}; match_quality={match_quality}; hist_n={len(df)}; avg_hist={avg_hist:.2f}; "
        f"weighted_avg={baseline:.2f}; mean7={mean7 if mean7 is not None else 'NA'}; "
        f"mean30={mean30 if mean30 is not None else 'NA'}; momentum_pct={momentum_pct:.2f}; "
        f"trend_strength={trend_strength:.2f}; drop_alert={drop_alert}; ghost_price_alert={ghost_price_alert}; "
        f"avg_sources={avg_sources:.2f}; reliability_bonus={reliability_bonus:.2f}; "
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
        "overall_reliability": overall_reliability,
        "explanation": explanation,
        "suggested_price": suggested_price,
        "reasoning": reasoning,
    }

