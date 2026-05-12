"""
Awin Affiliate Feed Scraper — Prix officiels boutiques FR sneakers.

Télécharge les flux produits CSV Awin pour les programmes sneakers FR et injecte
les résultats dans data/market_fr_sources.csv.

Usage:
    python3 scrapers/awin_feed.py              # injection réelle
    python3 scrapers/awin_feed.py --dry-run    # simulation sans écriture CSV
"""

from __future__ import annotations

import argparse
import csv
import io
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
logger = logging.getLogger("awin_feed")

# ---------------------------------------------------------------------------
# Programmes Awin FR sneakers (merchant_id → nom boutique normalisé)
# Les IDs sont des placeholders — à remplacer avec les vrais IDs depuis
# le dashboard Awin : https://ui.awin.com/merchant-listing
# ---------------------------------------------------------------------------
AWIN_PROGRAMS_FR: dict[int, str] = {
    14693: "Courir",           # Courir.com
    13484: "Foot Locker FR",   # Foot Locker France
    13126: "Zalando.fr",       # Zalando France
    21063: "snipes.com",       # Snipes FR
    6364:  "ASOS",             # ASOS
    14218: "JD Sports France", # JD Sports FR
    6053:  "Intersport",       # Intersport FR
}

# Colonnes CSV cible (format market_fr_sources.csv + champs affiliation)
CSV_FIELDNAMES = [
    "brand", "model", "market", "shop",
    "price_min", "price_max", "price_avg", "price_count",
    "updated_at", "source_type", "validated",
]

# Colonnes de base (existantes dans le CSV)
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
    """
    Retourne une liste de tuples (brand, model, search_key) où search_key est
    la clé de recherche normalisée (brand + model en minuscules).
    """
    index = []
    for m in models:
        brand = m.get("brand", "")
        model = m.get("model", "")
        key = f"{brand} {model}".lower().strip()
        index.append((brand, model, key))
    return index


def _fuzzy_match(
    product_name: str,
    merchant_name: str,
    index: list[tuple[str, str, str]],
    threshold: int = 72,
) -> tuple[str, str] | None:
    """
    Retourne (brand, model) si le produit matche un modèle DB avec score >= threshold.
    Utilise rapidfuzz si disponible, sinon difflib.
    """
    query = f"{merchant_name} {product_name}".lower().strip()

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
# API Awin — téléchargement flux produits
# ---------------------------------------------------------------------------
def _awin_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/csv",
    }


def _download_awin_product_feed(
    api_key: str,
    publisher_id: str,
    merchant_id: int,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """
    Télécharge le flux produits CSV Awin pour un programme donné.
    Endpoint officiel Awin Product Data :
    https://productdata.awin.com/datafeed/download/apikey/{api_key}/
        language/fr/ftype/csv/dtype/products/
        ?mid={merchant_id}&publisher_id={publisher_id}
    """
    url = (
        f"https://productdata.awin.com/datafeed/download"
        f"/apikey/{api_key}"
        f"/language/fr/ftype/csv/dtype/products/"
    )
    params = {
        "mid": str(merchant_id),
        "publisher_id": publisher_id,
        "fields": "product_name,merchant_name,aw_deep_link,search_price,currency,last_updated",
    }
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 401:
            logger.error("Awin API 401 — vérifier AWIN_API_KEY et AWIN_PUBLISHER_ID")
        elif resp.status_code == 403:
            logger.error("Awin API 403 — programme %d non autorisé pour ce publisher", merchant_id)
        else:
            logger.warning("Awin HTTP %s pour merchant=%d : %s", resp.status_code, merchant_id, e)
        return []
    except requests.exceptions.RequestException as e:
        logger.warning("Awin request error merchant=%d : %s", merchant_id, e)
        return []

    # Parse CSV
    try:
        content = resp.content.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        logger.debug("Awin merchant=%d : %d produits téléchargés", merchant_id, len(rows))
        return rows
    except Exception as e:
        logger.warning("Awin CSV parse error merchant=%d : %s", merchant_id, e)
        return []


# ---------------------------------------------------------------------------
# Normalisation prix Awin
# ---------------------------------------------------------------------------
def _parse_price(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        cleaned = raw.strip().replace(",", ".").replace(" ", "").replace("€", "")
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Lecture CSV existant (pour éviter les doublons affiliation)
# ---------------------------------------------------------------------------
def _read_existing_sources() -> dict[tuple[str, str, str], dict]:
    """
    Retourne un dict keyed par (brand, model, shop) → row existant.
    """
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
# Écriture CSV (upsert des lignes affiliation)
# ---------------------------------------------------------------------------
def _write_sources_csv(rows_to_upsert: list[dict[str, Any]]) -> int:
    """
    Lit le CSV existant, remplace/ajoute les lignes d'affiliation, réécrit.
    Retourne le nombre de lignes injectées (nouvelles + mises à jour).
    """
    existing: dict[tuple[str, str, str], dict] = {}
    existing_order: list[tuple[str, str, str]] = []

    # Lire l'existant
    if SOURCES_CSV.is_file():
        with open(SOURCES_CSV, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            existing_fields = reader.fieldnames or CSV_BASE_FIELDNAMES
            for row in reader:
                key = (row.get("brand", ""), row.get("model", ""), row.get("shop", ""))
                existing[key] = row
                existing_order.append(key)

    # Construire l'index des champs (union base + nouveaux)
    has_extra = any(
        k in (existing.get(list(existing.keys())[0]) or {})
        for k in ("source_type", "validated")
    ) if existing else False
    fieldnames = CSV_FIELDNAMES if (has_extra or rows_to_upsert) else CSV_BASE_FIELDNAMES

    # Appliquer upsert
    injected = 0
    for row in rows_to_upsert:
        key = (row["brand"], row["model"], row["shop"])
        if key not in existing:
            existing_order.append(key)
        existing[key] = row
        injected += 1

    # Réécrire
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SOURCES_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for key in existing_order:
            row = existing[key]
            # Compléter les champs manquants
            for fn in fieldnames:
                if fn not in row:
                    row[fn] = ""
            writer.writerow({fn: row.get(fn, "") for fn in fieldnames})

    return injected


# ---------------------------------------------------------------------------
# Pipeline principal Awin
# ---------------------------------------------------------------------------
def run_awin_feed(dry_run: bool = False) -> dict[str, int]:
    """
    Télécharge et injecte les flux Awin FR dans market_fr_sources.csv.
    Retourne des stats : boutiques_traitees, prix_injectes, modeles_matches.
    """
    api_key = os.getenv("AWIN_API_KEY", "").strip()
    publisher_id = os.getenv("AWIN_PUBLISHER_ID", "").strip()

    if not api_key or not publisher_id:
        logger.warning(
            "[AWIN] AWIN_API_KEY et/ou AWIN_PUBLISHER_ID manquants dans .env — "
            "flux ignoré. Configurer ces variables pour activer l'affiliation Awin."
        )
        return {"boutiques_traitees": 0, "prix_injectes": 0, "modeles_matches": 0}

    models = _load_models()
    if not models:
        logger.error("[AWIN] Aucun modèle chargé depuis models_list.json")
        return {"boutiques_traitees": 0, "prix_injectes": 0, "modeles_matches": 0}

    model_index = _build_model_index(models)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows_to_inject: list[dict[str, Any]] = []
    boutiques_traitees = 0
    modeles_matches = 0

    for merchant_id, shop_name in AWIN_PROGRAMS_FR.items():
        logger.info("[AWIN] Traitement boutique: %s (merchant_id=%d)", shop_name, merchant_id)

        if dry_run:
            logger.info("[AWIN][DRY-RUN] Simulation flux pour %s — aucune requête HTTP", shop_name)
            # Simuler quelques lignes fictives
            sample_products = [
                {"product_name": "Nike Air Force 1 Low blanc", "search_price": "99.99",
                 "currency": "EUR", "last_updated": now, "aw_deep_link": "https://example.com/p1"},
                {"product_name": "Adidas Stan Smith Blanc Vert", "search_price": "89.95",
                 "currency": "EUR", "last_updated": now, "aw_deep_link": "https://example.com/p2"},
                {"product_name": "New Balance 574 Gris", "search_price": "79.00",
                 "currency": "EUR", "last_updated": now, "aw_deep_link": "https://example.com/p3"},
            ]
            feed_rows = sample_products
        else:
            feed_rows = _download_awin_product_feed(api_key, publisher_id, merchant_id)

        if not feed_rows:
            logger.warning("[AWIN] Aucun produit pour %s", shop_name)
            continue

        boutiques_traitees += 1
        matched_this_shop = 0

        # Agréger par (brand, model) → liste de prix
        price_map: dict[tuple[str, str], list[float]] = {}
        link_map: dict[tuple[str, str], str] = {}

        for product in feed_rows:
            name = product.get("product_name", "")
            price_raw = product.get("search_price", "")
            deep_link = product.get("aw_deep_link", "")
            currency = product.get("currency", "EUR")

            if currency.upper() not in ("EUR", ""):
                continue  # Ignorer les non-EUR

            price = _parse_price(price_raw)
            if price is None or price <= 0 or price > 5000:
                continue

            # Fuzzy match sur les modèles DB
            match = _fuzzy_match(name, shop_name, model_index)
            if match is None:
                continue

            brand, model = match
            key = (brand, model)
            price_map.setdefault(key, []).append(price)
            if key not in link_map and deep_link:
                link_map[key] = deep_link

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
            "[AWIN] %s : %d produits traités → %d modèles matchés",
            shop_name, len(feed_rows), matched_this_shop,
        )

        # Pause courtoise entre requêtes
        if not dry_run:
            time.sleep(1.0)

    # Injection CSV
    if dry_run:
        logger.info(
            "[AWIN][DRY-RUN] Résumé : %d boutiques, %d prix à injecter, %d modèles matchés "
            "(AUCUNE ÉCRITURE — dry-run actif)",
            boutiques_traitees, len(rows_to_inject), modeles_matches,
        )
    else:
        if rows_to_inject:
            injected = _write_sources_csv(rows_to_inject)
            logger.info(
                "[AWIN] Injection terminée : %d boutiques traitées | %d prix injectés | %d modèles matchés",
                boutiques_traitees, injected, modeles_matches,
            )
        else:
            logger.warning("[AWIN] Aucune ligne à injecter (vérifier clés API et programmes)")

    return {
        "boutiques_traitees": boutiques_traitees,
        "prix_injectes": len(rows_to_inject),
        "modeles_matches": modeles_matches,
    }


# ---------------------------------------------------------------------------
# Point d'entrée CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Awin affiliate feed scraper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simuler sans écrire dans market_fr_sources.csv",
    )
    args = parser.parse_args()

    # Charger .env si présent
    env_path = PROJECT_DIR / ".env"
    if env_path.is_file():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    stats = run_awin_feed(dry_run=args.dry_run)
    print(
        f"\n[AWIN] STATS FINALES\n"
        f"  Boutiques traitées : {stats['boutiques_traitees']}\n"
        f"  Prix injectés      : {stats['prix_injectes']}\n"
        f"  Modèles matchés    : {stats['modeles_matches']}\n"
    )
