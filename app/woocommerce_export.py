"""
Exports WooCommerce (CSV / JSON) et PrestaShop (CSV) depuis ``data/market_fr.csv``.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = _ROOT / "data"
MARKET_FR_CSV = DATA_DIR / "market_fr.csv"
EXPORTS_DIR = DATA_DIR / "exports"

_WOO_CSV_COLUMNS = [
    "ID",
    "Type",
    "SKU",
    "Name",
    "Published",
    "Short description",
    "Description",
    "Regular price",
    "Sale price",
    "Categories",
    "Tags",
    "In stock?",
    "Meta: _price_min",
    "Meta: _price_max",
    "Meta: _price_avg",
    "Meta: _nb_sources",
    "Meta: _last_update",
]


def _ensure_exports_dir() -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORTS_DIR


def _read_market_rows() -> list[dict[str, str]]:
    if not MARKET_FR_CSV.is_file():
        return []
    rows: list[dict[str, str]] = []
    with MARKET_FR_CSV.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append({k: (v or "").strip() if isinstance(v, str) else str(v or "") for k, v in r.items()})
    return rows


def _brand_prefix_3(brand: str) -> str:
    letters = re.sub(r"[^A-Za-z]", "", brand or "")
    return (letters[:3] or "XXX").upper()


def _model_slug(model: str, max_len: int = 14) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (model or "").strip())
    s = re.sub(r"-+", "-", s).strip("-").upper()
    return s[:max_len] if s else "MODEL"


def generate_sku(brand: str, model: str) -> str:
    """SKU type SB-NIK-AIR-FORC (3 lettres marque + extrait modèle)."""
    b = _brand_prefix_3(brand)
    m = _model_slug(model)
    sku = f"SB-{b}-{m}"
    return sku[:32]


def generate_woocommerce_csv() -> Path:
    """CSV compatible import WooCommerce (colonnes demandées)."""
    out_dir = _ensure_exports_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"woocommerce_import_{ts}.csv"

    rows = _read_market_rows()
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_WOO_CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            brand = r.get("brand", "")
            model = r.get("model", "")
            try:
                pmin = float(str(r.get("price_min", "0")).replace(",", "."))
                pmax = float(str(r.get("price_max", "0")).replace(",", "."))
                pavg = float(str(r.get("price_avg", "0")).replace(",", "."))
            except ValueError:
                continue
            nb = str(r.get("nb_sources", "1") or "1")
            upd = r.get("updated_at", "")
            name = f"{brand} {model}".strip()
            sku = generate_sku(brand, model)
            short_desc = f"Prix agrégés marché FR — {nb} source(s)."
            long_desc = (
                f"SneakerBot — agrégat multi-boutiques FR. Min {pmin:.2f}\u00a0€, max {pmax:.2f}\u00a0€, "
                f"moyenne {pavg:.2f}\u00a0€. Dernière mise à jour : {upd}."
            )
            w.writerow(
                {
                    "ID": "",
                    "Type": "simple",
                    "SKU": sku,
                    "Name": name,
                    "Published": "1",
                    "Short description": short_desc,
                    "Description": long_desc,
                    "Regular price": f"{pmax:.2f}",
                    "Sale price": f"{pavg:.2f}",
                    "Categories": f"Sneakers > {brand}",
                    "Tags": f"{brand},sneakers,FR",
                    "In stock?": "1",
                    "Meta: _price_min": f"{pmin:.2f}",
                    "Meta: _price_max": f"{pmax:.2f}",
                    "Meta: _price_avg": f"{pavg:.2f}",
                    "Meta: _nb_sources": nb,
                    "Meta: _last_update": upd,
                }
            )
    return path


def generate_woocommerce_json() -> Path:
    """JSON structuré type lot produits REST WooCommerce v3."""
    out_dir = _ensure_exports_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"woocommerce_products_{ts}.json"

    products: list[dict[str, Any]] = []
    for r in _read_market_rows():
        brand = r.get("brand", "")
        model = r.get("model", "")
        try:
            pmin = float(str(r.get("price_min", "0")).replace(",", "."))
            pmax = float(str(r.get("price_max", "0")).replace(",", "."))
            pavg = float(str(r.get("price_avg", "0")).replace(",", "."))
        except ValueError:
            continue
        nb = str(r.get("nb_sources", "1") or "1")
        upd = r.get("updated_at", "")
        name = f"{brand} {model}".strip()
        sku = generate_sku(brand, model)
        products.append(
            {
                "name": name,
                "type": "simple",
                "sku": sku,
                "regular_price": f"{pmax:.2f}",
                "sale_price": f"{pavg:.2f}",
                "categories": [{"name": f"Sneakers > {brand}"}],
                "tags": [{"name": brand}, {"name": "sneakers"}, {"name": "FR"}],
                "meta_data": [
                    {"key": "_price_min", "value": f"{pmin:.2f}"},
                    {"key": "_price_max", "value": f"{pmax:.2f}"},
                    {"key": "_price_avg", "value": f"{pavg:.2f}"},
                    {"key": "_nb_sources", "value": nb},
                    {"key": "_last_update", "value": upd},
                ],
            }
        )

    path.write_text(json.dumps({"products": products}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def generate_prestashop_csv() -> Path:
    """CSV PrestaShop avec séparateur `;`."""
    out_dir = _ensure_exports_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"prestashop_import_{ts}.csv"

    cols = [
        "Active",
        "Name",
        "Categories",
        "Price",
        "Reference",
        "Short description",
        "Description",
        "Meta description",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in _read_market_rows():
            brand = r.get("brand", "")
            model = r.get("model", "")
            try:
                pavg = float(str(r.get("price_avg", "0")).replace(",", "."))
                pmin = float(str(r.get("price_min", "0")).replace(",", "."))
                pmax = float(str(r.get("price_max", "0")).replace(",", "."))
            except ValueError:
                continue
            name = f"{brand} {model}".strip()
            ref = generate_sku(brand, model)
            short_d = f"Agrégat FR SneakerBot — {r.get('nb_sources', '1')} sources."
            desc = (
                f"Prix min {pmin:.2f}\u00a0€, max {pmax:.2f}\u00a0€, moyenne {pavg:.2f}\u00a0€. "
                f"Mise à jour : {r.get('updated_at', '')}."
            )
            meta = f"{name} — à partir de {pmin:.0f}\u00a0€ (marché FR)."
            w.writerow(
                {
                    "Active": "1",
                    "Name": name,
                    "Categories": f"Sneakers|{brand}",
                    "Price": f"{pavg:.2f}".replace(".", ","),
                    "Reference": ref,
                    "Short description": short_d,
                    "Description": desc,
                    "Meta description": meta,
                }
            )
    return path


__all__ = [
    "generate_prestashop_csv",
    "generate_sku",
    "generate_woocommerce_csv",
    "generate_woocommerce_json",
]
