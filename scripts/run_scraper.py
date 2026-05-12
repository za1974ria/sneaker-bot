#!/usr/bin/env python3
"""CRON / manuel : scrape live + écriture market_live.csv."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from app.services.market_service import get_catalog_products_flat
from scraper_ecommerce import get_prices

_ROOT = Path(__file__).resolve().parent
MARKET_LIVE_CSV = _ROOT / "market_live.csv"

# Seuil en € : au-delà, la tendance n'est plus considérée comme stable.
TREND_EPSILON = 0.01

PRODUCTS = get_catalog_products_flat()


def _safe_write_csv(df: pd.DataFrame, path: Path) -> None:
    """Écriture atomique pour éviter les fichiers partiels/corrompus."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp_path, index=False, encoding="utf-8", na_rep="")
    tmp_path.replace(path)


def _load_previous_avg_by_product() -> dict[str, float]:
    """Lit l'ancien market_live.csv et retourne {product: avg} (compat product/produit)."""
    if not MARKET_LIVE_CSV.is_file():
        return {}
    try:
        prev_df = pd.read_csv(MARKET_LIVE_CSV, encoding="utf-8")
    except Exception:
        return {}
    if prev_df.empty or "avg" not in prev_df.columns:
        return {}
    name_col = "product" if "product" in prev_df.columns else "produit" if "produit" in prev_df.columns else None
    if name_col is None:
        return {}
    out: dict[str, float] = {}
    for _, r in prev_df.iterrows():
        name = str(r[name_col]).strip()
        if not name or name.lower() == "nan":
            continue
        try:
            out[name] = float(r["avg"])
        except (ValueError, TypeError):
            continue
    return out


def _trend_from_variation(variation: float) -> str:
    if variation > TREND_EPSILON:
        return "UP"
    if variation < -TREND_EPSILON:
        return "DOWN"
    return "STABLE"


def run_market_update() -> int:
    """Scrape live, calcule variation/trend, puis met à jour market_live.csv."""
    prev_avgs = _load_previous_avg_by_product()
    data: list[dict] = []

    for p in PRODUCTS:
        print(f"🔍 Scraping : {p}")
        market = get_prices(p, live=True)

        if market:
            new_avg = round(market["avg"], 2)
            old_avg = prev_avgs.get(p)

            if old_avg is not None:
                prev_avg = round(old_avg, 2)
                variation = round(new_avg - prev_avg, 2)
                trend = _trend_from_variation(variation)
            else:
                prev_avg = float("nan")
                variation = float("nan")
                trend = "STABLE"

            data.append(
                {
                    "product": p,
                    "produit": p,
                    "min": round(market["min"], 2),
                    "max": round(market["max"], 2),
                    "avg": new_avg,
                    "prev_avg": prev_avg,
                    "variation": variation,
                    "trend": trend,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
            )

    if not data:
        print("⚠️ Aucun prix collecté — market_live.csv non modifié (évite d’effacer les données).")
        return 1

    df = pd.DataFrame(data)
    cols = [
        "product",
        "min",
        "max",
        "avg",
        "variation",
        "trend",
        "timestamp",
        "produit",
        "prev_avg",
        "updated_at",
    ]
    df = df[[c for c in cols if c in df.columns]]
    _safe_write_csv(df, MARKET_LIVE_CSV)
    print("✅ market_live.csv mis à jour")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_market_update())
