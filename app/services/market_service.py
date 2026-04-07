"""Market data loading and signal generation service."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore[assignment]

from app.models.product import Product
from app.utils.signal_engine import compute_signal

ROOT_DIR = Path(__file__).resolve().parents[2]
APP_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
MARKET_LIVE_PRIMARY = APP_DATA_DIR / "market_live.csv"
MARKET_LIVE_FALLBACK = ROOT_DIR / "market_live.csv"
TRADER_PRICES_PATH = ROOT_DIR / "trader_prices.csv"
TOP_PRODUCTS_LIMIT = 50
COUNTRIES = ("FR", "BE", "LU")
ARBITRAGE_THRESHOLD_EUR = 8.0
ESTIMATED_FEES_EUR = 5.0
DEFAULT_SHOPS = ["Courir", "JD Sports", "StockX", "GOAT", "eBay"]
SNEAKER_CATALOG: dict[str, list[str]] = {
    "Nike": ["Air Max", "Air Force 1", "Pegasus", "Dunk Low", "ZoomX", "React Infinity"],
    "Adidas": ["Samba", "Gazelle", "Campus", "Ultraboost", "NMD R1", "Forum Low"],
    "New Balance": ["550", "574", "9060", "2002R", "1906R", "327"],
    "Puma": ["Suede Classic", "RS-X", "Palermo", "Clyde", "Future Rider", "Slipstream"],
    "Asics": ["Gel-Kayano", "Gel-Nimbus", "Gel-Lyte III", "GT-2000", "Novablast", "Gel-1130"],
    "Jordan": ["Jordan 1 Low", "Jordan 1 Mid", "Jordan 3", "Jordan 4", "Jordan 11", "Jordan 5"],
    "Reebok": ["Club C 85", "Classic Leather", "Nano X", "Question Mid", "Floatride", "Instapump Fury"],
    "Salomon": ["XT-6", "ACS Pro", "Speedcross", "XT-Wings 2", "Pulsar Trail", "Sense Ride"],
    "On Running": ["Cloud 5", "Cloudmonster", "Cloudflow", "Cloudswift", "Cloudsurfer", "Cloud X"],
    "Converse": ["Chuck Taylor", "Run Star Hike", "Weapon", "One Star", "Pro Leather", "Chuck 70"],
}

# Priorité métier demandée :
# 1) STRONG BUY, 2) BUY, 3) HOLD, 4) WAIT, 5) SELL
SIGNAL_SCORE_BY_KIND = {
    "buy-strong": 5,
    "buy": 4,
    "hold": 3,
    "wait": 2,
    "sell": 1,
}


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _position_from_variation(variation: float | None) -> str:
    """
    Position simplifiée pour piloter le signal sans casser l'existant:
    - variation >= +1.0 => BAS
    - variation <= -1.0 => HAUT
    - sinon => MOYEN
    """
    if variation is None:
        return "MOYEN"
    if variation >= 1.0:
        return "BAS"
    if variation <= -1.0:
        return "HAUT"
    return "MOYEN"


def _split_brand_model(product_name: str) -> tuple[str, str]:
    parts = [p for p in (product_name or "").strip().split(" ") if p]
    if not parts:
        return "UNKNOWN", "UNKNOWN"
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], " ".join(parts[1:])


def _catalog_full_names() -> set[str]:
    names: set[str] = set()
    for brand, models in SNEAKER_CATALOG.items():
        for model in models:
            names.add(f"{brand} {model}".strip())
    return names


def _is_catalog_product(product_name: str) -> bool:
    return product_name in _catalog_full_names()


def _load_trader_prices() -> dict[str, dict[str, dict[str, float]]]:
    """
    Retourne {produit: {FR:{min,max,avg}, BE:{...}, LU:{...}}}.
    """
    if not TRADER_PRICES_PATH.is_file():
        return {}
    out: dict[str, dict[str, dict[str, float]]] = {}
    try:
        with TRADER_PRICES_PATH.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("produit") or "").strip()
                if not name:
                    continue
                country_values: dict[str, dict[str, float]] = {}
                ok = True
                for c in COUNTRIES:
                    lo = _safe_float(row.get(f"{c.lower()}_min"))
                    hi = _safe_float(row.get(f"{c.lower()}_max"))
                    if lo is None or hi is None:
                        ok = False
                        break
                    country_values[c] = {
                        "min": lo,
                        "max": hi,
                        "avg": round((lo + hi) / 2, 2),
                    }
                if ok:
                    out[name] = country_values
    except OSError:
        return {}
    return out


def _fallback_country_prices(avg: float | None) -> dict[str, dict[str, float]]:
    base = float(avg or 100.0)
    return {
        "FR": {"min": round(base - 5, 2), "max": round(base + 5, 2), "avg": round(base, 2)},
        "BE": {"min": round(base - 4, 2), "max": round(base + 6, 2), "avg": round(base + 1, 2)},
        "LU": {"min": round(base - 3, 2), "max": round(base + 7, 2), "avg": round(base + 2, 2)},
    }


def _compute_arbitrage(country_prices: dict[str, dict[str, float]]) -> dict[str, Any]:
    # buy = pays au prix moyen le plus bas, sell = plus haut
    ranked = sorted(
        ((c, country_prices[c]["avg"]) for c in COUNTRIES if c in country_prices),
        key=lambda x: x[1],
    )
    if len(ranked) < 2:
        return {
            "opportunity": False,
            "signal": "⚠️ WAIT",
            "buy_country": "FR",
            "sell_country": "LU",
            "difference": 0.0,
            "message": "Données pays insuffisantes",
        }
    buy_country, buy_price = ranked[0]
    sell_country, sell_price = ranked[-1]
    diff = round(sell_price - buy_price, 2)
    is_opportunity = diff > ARBITRAGE_THRESHOLD_EUR
    signal = "🔥 ARBITRAGE OPPORTUNITY" if is_opportunity else "⚖️ NO ARBITRAGE"
    profit_estimate = round(max(0.0, diff - ESTIMATED_FEES_EUR), 2)
    opportunity_score = int(round(max(0.0, min(100.0, (diff / 25.0) * 100.0))))
    return {
        "opportunity": is_opportunity,
        "signal": signal,
        "buy_country": buy_country,
        "sell_country": sell_country,
        "difference": diff,
        "profit_estimate": profit_estimate,
        "opportunity_score": opportunity_score,
        "message": f"Buy in {buy_country} \u2192 Sell in {sell_country}",
    }


def _pick_market_live_path() -> Path:
    if MARKET_LIVE_PRIMARY.is_file():
        return MARKET_LIVE_PRIMARY
    return MARKET_LIVE_FALLBACK


def _has_minimum_columns(columns: set[str]) -> bool:
    """CSV minimum contract to avoid crashes with malformed files."""
    return bool({"produit", "product"} & columns)


def _load_rows_with_pandas(path: Path) -> list[dict[str, Any]]:
    if pd is None:
        return []
    try:
        df = pd.read_csv(path, encoding="utf-8")
    except Exception:
        return []
    if df.empty:
        return []
    cols = {str(c).strip().lower() for c in df.columns}
    if not _has_minimum_columns(cols):
        return []
    name_col = "produit" if "produit" in df.columns else "product" if "product" in df.columns else None
    if name_col is None:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        rows.append(
            {
                "produit": str(row.get(name_col, "")).strip(),
                "avg": row.get("avg"),
                "variation": row.get("variation"),
                "trend": str(row.get("trend", "")).strip().upper(),
                "updated_at": row.get("updated_at") or row.get("timestamp"),
            }
        )
    return rows


def _load_rows_with_csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = {str(c).strip().lower() for c in (reader.fieldnames or [])}
            if not _has_minimum_columns(fieldnames):
                return []
            out: list[dict[str, Any]] = []
            for r in reader:
                out.append(
                    {
                        "produit": (r.get("produit") or r.get("product") or "").strip(),
                        "avg": r.get("avg"),
                        "variation": r.get("variation"),
                        "trend": (r.get("trend") or "").strip().upper(),
                        "updated_at": (r.get("updated_at") or r.get("timestamp") or "").strip(),
                    }
                )
            return out
    except OSError:
        return []


def load_market_live_rows() -> list[dict[str, Any]]:
    path = _pick_market_live_path()
    if not path.is_file():
        return []

    rows = _load_rows_with_pandas(path)
    if rows:
        return rows
    return _load_rows_with_csv(path)


def _row_to_product(
    row: dict[str, Any],
    trader_prices: dict[str, dict[str, dict[str, float]]],
) -> Product | None:
    product_name = (row.get("produit") or "").strip()
    if not product_name:
        return None
    if not _is_catalog_product(product_name):
        return None

    avg = _safe_float(row.get("avg"))
    variation = _safe_float(row.get("variation"))
    trend = (row.get("trend") or "").strip().upper() or "STABLE"
    brand, model = _split_brand_model(product_name)

    position = _position_from_variation(variation)
    signal_data = compute_signal(trend, position)
    signal = signal_data["label"]
    signal_kind = signal_data["kind"]
    score = SIGNAL_SCORE_BY_KIND.get(signal_kind, 2)
    country_prices = trader_prices.get(product_name) or _fallback_country_prices(avg)
    arbitrage = _compute_arbitrage(country_prices)
    if arbitrage["opportunity"]:
        signal = arbitrage["signal"]
        signal_kind = "buy-strong"
        score = max(score, 5)

    return Product(
        product=product_name,
        brand=brand,
        model=model,
        country=arbitrage["buy_country"],
        shops=DEFAULT_SHOPS,
        avg=avg,
        variation=variation,
        trend=trend,
        signal=signal,
        signal_kind=signal_kind,
        score=score,
        price_min={c: country_prices[c]["min"] for c in COUNTRIES},
        price_max={c: country_prices[c]["max"] for c in COUNTRIES},
        price_avg={c: country_prices[c]["avg"] for c in COUNTRIES},
        arbitrage=arbitrage,
    )


def _enforce_business_signal_mix(items: list[dict[str, Any]]) -> None:
    """
    Règle business UX:
    si tous les produits sont STABLE, forcer au moins 1 BUY et 1 SELL.
    """
    if not items:
        return
    if not all(str(i.get("trend", "")).upper() == "STABLE" for i in items):
        return

    buy_idx = None
    sell_idx = None
    for idx, item in enumerate(items):
        kind = str(item.get("signal_kind", ""))
        if kind in ("buy-strong", "buy"):
            buy_idx = idx
        if kind == "sell":
            sell_idx = idx

    if buy_idx is None:
        target = 0
        items[target]["signal"] = "🟢 BUY"
        items[target]["signal_kind"] = "buy"
        items[target]["score"] = SIGNAL_SCORE_BY_KIND["buy"]

    if sell_idx is None:
        target = len(items) - 1 if len(items) > 1 else 0
        items[target]["signal"] = "❌ SELL"
        items[target]["signal_kind"] = "sell"
        items[target]["score"] = SIGNAL_SCORE_BY_KIND["sell"]


def _last_update_value(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        candidate = str(row.get("updated_at") or "").strip()
        if candidate:
            return candidate
    return "dynamic (ex: 2 min ago)"


def get_market_products() -> list[dict[str, Any]]:
    """
    Expose une liste JSON-ready de produits live.
    Le signal est dérivé de trend + position simplifiée calculée depuis variation.
    """
    items: list[dict[str, Any]] = []
    rows = load_market_live_rows()
    trader_prices = _load_trader_prices()
    for row in rows:
        product = _row_to_product(row, trader_prices)
        if product is None:
            continue
        items.append(product.to_dict())
    _enforce_business_signal_mix(items)
    items.sort(key=lambda x: x.get("score", 2), reverse=True)
    return items[:TOP_PRODUCTS_LIMIT]


def get_market_snapshot() -> dict[str, Any]:
    """
    Données UI globales robustes même si CSV absent/vide.
    """
    rows = load_market_live_rows()
    products = get_market_products()
    top = products[0] if products else None
    return {
        "products": products,
        "last_update": _last_update_value(rows),
        "top_opportunity": top["product"] if top else "N/A",
    }


def get_arbitrage_opportunities(limit: int = 10) -> list[dict[str, Any]]:
    """
    Retourne uniquement les produits avec arbitrage, triés par différence décroissante.
    """
    rows = []
    for p in get_market_products():
        arb = p.get("arbitrage") or {}
        if not arb.get("opportunity"):
            continue
        rows.append(p)
    rows.sort(key=lambda x: float((x.get("arbitrage") or {}).get("difference") or 0.0), reverse=True)
    return rows[: max(1, limit)]


def get_sneaker_catalog() -> dict[str, list[str]]:
    return SNEAKER_CATALOG


def get_catalog_products_flat() -> list[str]:
    out: list[str] = []
    for brand, models in SNEAKER_CATALOG.items():
        for model in models:
            out.append(f"{brand} {model}")
    return out
