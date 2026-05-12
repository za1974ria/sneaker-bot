"""
Tradedoubler Affiliate Feed Scraper — Prix officiels boutiques FR sneakers.

Télécharge les flux produits JSON Tradedoubler pour les programmes sneakers FR et injecte
les résultats dans data/market_fr_sources.csv.

Usage:
    python3 scrapers/tradedoubler_feed.py              # injection réelle
    python3 scrapers/tradedoubler_feed.py --dry-run    # simulation sans écriture CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SOURCES_CSV = DATA_DIR / "market_fr_sources.csv"
MODELS_JSON = DATA_DIR / "models_list.json"

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("tradedoubler_feed")

# ---------------------------------------------------------------------------
# Programmes Tradedoubler FR sneakers (program_id → nom boutique normalisé)
# Les IDs sont des placeholders — à remplacer avec les vrais IDs depuis
# le dashboard Tradedoubler : https://platform.tradedoubler.com/
# ---------------------------------------------------------------------------
TD_PROGRAMS_FR: dict[int, str] = {
    272868: "nike.com/fr",         # Nike FR
    264310: "adidas.fr",           # Adidas FR
    285661: "Puma FR",             # Puma France
    279428: "New Balance FR",      # New Balance France
}

# Nombre maximum de produits par programme (pagination)
TD_MAX_PRODUCTS_PER_PROGRAM = 500

# Colonnes CSV cible (format market_fr_sources.csv + champs affiliation)
CSV_FIELDNAMES = [
    "brand", "model", "market", "shop",
    "price_min", "price_max", "price_avg", "price_count",
    "updated_at", "source_type", "validated",
]

CSV_BASE_FIELDNAMES = [
    "brand", "model", "market", "shop",
    "price_min", "price_max", "price_avg", "price_count", "updated_at",
]


# ---------------------------------------------------------------------------
# Chargement des modèles cibles
# ---------------------------------------------------------------------------
def _load_models() -> list[dict[str, str]]:
    if not MODELS_JSON.is_file():
        logger.warning("models_list.json introuvable : %s", MODELS_JSON)
        return []
    with open(MODELS_JSON, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fuzzy matching brand+model → modèle DB
# ---------------------------------------------------------------------------
def _build_model_index(models: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    index = []
    for m in models:
        brand = m.get("brand", "")
        model = m.get("model", "")
        key = f"{brand} {model}".lower().strip()
        index.append((brand, model, key))
    return index


def _fuzzy_match(
    product_name: str,
    shop_name: str,
    index: list[tuple[str, str, str]],
    threshold: int = 72,
) -> tuple[str, str] | None:
    query = f"{shop_name} {product_name}".lower().strip()

    try:
        from rapidfuzz import process as rfprocess, fuzz as rffuzz
        result = rfprocess.extractOne(
            query,
            [t[2] for t in index],
            scorer=rffuzz.token_set_ratio,
            score_cutoff=threshold,
        )
        if result is None:
            return None
        best_key, score, idx = result
        return index[idx][0], index[idx][1]
    except ImportError:
        pass

    # Fallback difflib
    import difflib
    keys = [t[2] for t in index]
    matches = difflib.get_close_matches(query, keys, n=1, cutoff=threshold / 100.0)
    if not matches:
        return None
    idx = keys.index(matches[0])
    return index[idx][0], index[idx][1]


# ---------------------------------------------------------------------------
# API Tradedoubler — téléchargement flux produits
# ---------------------------------------------------------------------------
def _download_td_products(
    token: str,
    program_id: int,
    page: int = 1,
    page_size: int = 100,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """
    Télécharge les produits Tradedoubler via l'API REST v1.
    Endpoint : GET https://api.tradedoubler.com/1.0/products.json?token={token}
    Paramètres supportés : programId, page, pageSize, market (fr)
    """
    url = "https://api.tradedoubler.com/1.0/products.json"
    params = {
        "token": token,
        "programId": str(program_id),
        "market": "fr",
        "page": str(page),
        "pageSize": str(page_size),
        "currency": "EUR",
    }
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 401:
            logger.error("Tradedoubler API 401 — vérifier TRADEDOUBLER_TOKEN")
        elif resp.status_code == 403:
            logger.error("Tradedoubler API 403 — programme %d non autorisé", program_id)
        else:
            logger.warning("Tradedoubler HTTP %s programme=%d : %s", resp.status_code, program_id, e)
        return []
    except requests.exceptions.RequestException as e:
        logger.warning("Tradedoubler request error programme=%d : %s", program_id, e)
        return []

    try:
        data = resp.json()
        # La réponse peut être une liste directe ou un objet avec une clé "products"
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Chercher la clé produits dans différents formats de réponse TD
            for key in ("products", "items", "result", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        logger.debug("Tradedoubler programme=%d : réponse inattendue %s", program_id, type(data))
        return []
    except (ValueError, KeyError) as e:
        logger.warning("Tradedoubler JSON parse error programme=%d : %s", program_id, e)
        return []


def _fetch_all_td_products(
    token: str,
    program_id: int,
    max_products: int = TD_MAX_PRODUCTS_PER_PROGRAM,
) -> list[dict[str, Any]]:
    """
    Télécharge tous les produits avec pagination (max max_products).
    """
    all_products: list[dict[str, Any]] = []
    page_size = 100
    page = 1

    while len(all_products) < max_products:
        batch = _download_td_products(token, program_id, page=page, page_size=page_size)
        if not batch:
            break
        all_products.extend(batch)
        if len(batch) < page_size:
            break  # Dernière page
        page += 1
        time.sleep(0.5)  # Pause entre pages

    return all_products[:max_products]


# ---------------------------------------------------------------------------
# Normalisation prix Tradedoubler
# ---------------------------------------------------------------------------
def _parse_td_price(product: dict[str, Any]) -> float | None:
    """
    Extrait le prix depuis différents formats de réponse Tradedoubler.
    Champs possibles : price, Price, salePrice, originalPrice, priceInclVat
    """
    for field in ("price", "Price", "salePrice", "priceInclVat", "originalPrice"):
        raw = product.get(field)
        if raw is None:
            continue
        try:
            val = float(str(raw).replace(",", ".").replace(" ", "").replace("€", ""))
            if val > 0:
                return round(val, 2)
        except (ValueError, TypeError):
            continue
    return None


def _parse_td_name(product: dict[str, Any]) -> str:
    """Extrait le nom du produit depuis différents formats TD."""
    for field in ("name", "Name", "productName", "title", "Title"):
        val = product.get(field, "")
        if val:
            return str(val)
    return ""


# ---------------------------------------------------------------------------
# Lecture CSV existant
# ---------------------------------------------------------------------------
def _read_existing_sources() -> dict[tuple[str, str, str], dict]:
    existing: dict[tuple[str, str, str], dict] = {}
    if not SOURCES_CSV.is_file():
        return existing
    with open(SOURCES_CSV, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row.get("brand", ""), row.get("model", ""), row.get("shop", ""))
            existing[key] = row
    return existing


# ---------------------------------------------------------------------------
# Écriture CSV (upsert)
# ---------------------------------------------------------------------------
def _write_sources_csv(rows_to_upsert: list[dict[str, Any]]) -> int:
    existing: dict[tuple[str, str, str], dict] = {}
    existing_order: list[tuple[str, str, str]] = []

    if SOURCES_CSV.is_file():
        with open(SOURCES_CSV, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row.get("brand", ""), row.get("model", ""), row.get("shop", ""))
                existing[key] = row
                existing_order.append(key)

    fieldnames = CSV_FIELDNAMES

    injected = 0
    for row in rows_to_upsert:
        key = (row["brand"], row["model"], row["shop"])
        if key not in existing:
            existing_order.append(key)
        existing[key] = row
        injected += 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SOURCES_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for key in existing_order:
            row = existing[key]
            writer.writerow({fn: row.get(fn, "") for fn in fieldnames})

    return injected


# ---------------------------------------------------------------------------
# Pipeline principal Tradedoubler
# ---------------------------------------------------------------------------
def run_tradedoubler_feed(dry_run: bool = False) -> dict[str, int]:
    """
    Télécharge et injecte les flux Tradedoubler FR dans market_fr_sources.csv.
    Retourne des stats : boutiques_traitees, prix_injectes, modeles_matches.
    """
    token = os.getenv("TRADEDOUBLER_TOKEN", "").strip()

    if not token:
        logger.warning(
            "[TD] TRADEDOUBLER_TOKEN manquant dans .env — "
            "flux ignoré. Configurer cette variable pour activer l'affiliation Tradedoubler."
        )
        return {"boutiques_traitees": 0, "prix_injectes": 0, "modeles_matches": 0}

    models = _load_models()
    if not models:
        logger.error("[TD] Aucun modèle chargé depuis models_list.json")
        return {"boutiques_traitees": 0, "prix_injectes": 0, "modeles_matches": 0}

    model_index = _build_model_index(models)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows_to_inject: list[dict[str, Any]] = []
    boutiques_traitees = 0
    modeles_matches = 0

    for program_id, shop_name in TD_PROGRAMS_FR.items():
        logger.info("[TD] Traitement boutique: %s (program_id=%d)", shop_name, program_id)

        if dry_run:
            logger.info("[TD][DRY-RUN] Simulation flux pour %s — aucune requête HTTP", shop_name)
            # Données fictives pour le dry-run
            feed_products = [
                {"name": "Nike Air Max 90 Essential", "price": "109.99", "currency": "EUR"},
                {"name": "Adidas Superstar Blanc Noir", "price": "89.95", "currency": "EUR"},
                {"name": "Puma RS-X Blanc Bleu", "price": "74.99", "currency": "EUR"},
                {"name": "New Balance 990 Made in USA", "price": "189.00", "currency": "EUR"},
            ]
        else:
            feed_products = _fetch_all_td_products(token, program_id)

        if not feed_products:
            logger.warning("[TD] Aucun produit pour %s", shop_name)
            continue

        boutiques_traitees += 1
        matched_this_shop = 0

        # Agréger par (brand, model) → liste de prix
        price_map: dict[tuple[str, str], list[float]] = {}

        for product in feed_products:
            name = _parse_td_name(product)
            if not name:
                continue

            # Vérifier devise
            currency = product.get("currency", product.get("Currency", "EUR"))
            if str(currency).upper() not in ("EUR", ""):
                continue

            price = _parse_td_price(product)
            if price is None or price <= 0 or price > 5000:
                continue

            match = _fuzzy_match(name, shop_name, model_index)
            if match is None:
                continue

            brand, model = match
            key = (brand, model)
            price_map.setdefault(key, []).append(price)

        for (brand, model), prices in price_map.items():
            pmin = round(min(prices), 2)
            pmax = round(max(prices), 2)
            pavg = round(sum(prices) / len(prices), 2)

            row: dict[str, Any] = {
                "brand": brand,
                "model": model,
                "market": "FR",
                "shop": shop_name,
                "price_min": pmin,
                "price_max": pmax,
                "price_avg": pavg,
                "price_count": len(prices),
                "updated_at": now,
                "source_type": "affiliate_feed",
                "validated": "True",
            }
            rows_to_inject.append(row)
            matched_this_shop += 1

        modeles_matches += matched_this_shop
        logger.info(
            "[TD] %s : %d produits traités → %d modèles matchés",
            shop_name, len(feed_products), matched_this_shop,
        )

        if not dry_run:
            time.sleep(1.0)

    # Injection CSV
    if dry_run:
        logger.info(
            "[TD][DRY-RUN] Résumé : %d boutiques, %d prix à injecter, %d modèles matchés "
            "(AUCUNE ÉCRITURE — dry-run actif)",
            boutiques_traitees, len(rows_to_inject), modeles_matches,
        )
    else:
        if rows_to_inject:
            injected = _write_sources_csv(rows_to_inject)
            logger.info(
                "[TD] Injection terminée : %d boutiques traitées | %d prix injectés | %d modèles matchés",
                boutiques_traitees, injected, modeles_matches,
            )
        else:
            logger.warning("[TD] Aucune ligne à injecter (vérifier token et programmes)")

    return {
        "boutiques_traitees": boutiques_traitees,
        "prix_injectes": len(rows_to_inject),
        "modeles_matches": modeles_matches,
    }


# ---------------------------------------------------------------------------
# Point d'entrée CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tradedoubler affiliate feed scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simuler sans écrire dans market_fr_sources.csv",
    )
    args = parser.parse_args()

    env_path = PROJECT_DIR / ".env"
    if env_path.is_file():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    stats = run_tradedoubler_feed(dry_run=args.dry_run)
    print(
        f"\n[TD] STATS FINALES\n"
        f"  Boutiques traitées : {stats['boutiques_traitees']}\n"
        f"  Prix injectés      : {stats['prix_injectes']}\n"
        f"  Modèles matchés    : {stats['modeles_matches']}\n"
    )
