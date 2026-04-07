"""
Vérificateur Google Shopping — SneakerBot
Utilise SerpAPI pour récupérer les prix Google Shopping FR
et les comparer avec nos données.
Cache SQLite 24h pour éviter les requêtes répétées et limiter les crédits.
"""

from __future__ import annotations

import logging
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _PROJECT_ROOT / "data" / "google_shopping_cache.db"
MARKET_FR_CSV = _PROJECT_ROOT / "data" / "market_fr.csv"


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_google_cache() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS google_prices (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                brand        TEXT NOT NULL,
                model        TEXT NOT NULL,
                google_price REAL,
                google_min   REAL,
                google_max   REAL,
                nb_results   INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'ok',
                fetched_at   TEXT NOT NULL,
                UNIQUE(brand, model)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS google_verifications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                brand        TEXT NOT NULL,
                model        TEXT NOT NULL,
                our_price    REAL NOT NULL,
                google_price REAL,
                deviation_pct REAL,
                verdict      TEXT NOT NULL,
                verified_at  TEXT NOT NULL
            )
            """
        )


def _get_cached_price(brand: str, model: str) -> dict[str, Any] | None:
    """Retourne le prix en cache si < 24h."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with _db() as conn:
        row = conn.execute(
            """
            SELECT * FROM google_prices
            WHERE brand=? AND model=? AND fetched_at > ?
            """,
            (brand, model, cutoff),
        ).fetchone()
    return dict(row) if row else None


def get_cached_google_price(brand: str, model: str) -> dict[str, Any] | None:
    """
    Lecture cache-only (24h) pour affichage comparateur.
    N'effectue jamais d'appel réseau.
    """
    cached = _get_cached_price(brand, model)
    if not cached:
        return None
    return {
        "google_price": cached.get("google_price"),
        "google_min": cached.get("google_min"),
        "google_max": cached.get("google_max"),
        "nb_results": cached.get("nb_results"),
        "status": "cached",
    }


def _save_cached_price(
    brand: str,
    model: str,
    google_price: float,
    google_min: float,
    google_max: float,
    nb_results: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO google_prices
            (brand, model, google_price, google_min,
             google_max, nb_results, status, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, 'ok', ?)
            """,
            (brand, model, google_price, google_min, google_max, nb_results, now),
        )


def scrape_google_shopping_price(brand: str, model: str) -> dict[str, Any]:
    """
    Récupère les prix Google Shopping via SerpAPI.
    Cache SQLite 24h pour économiser les crédits.
    """
    cached = _get_cached_price(brand, model)
    if cached:
        logger.debug("Cache hit: %s %s", brand, model)
        return {
            "google_price": cached["google_price"],
            "google_min": cached["google_min"],
            "google_max": cached["google_max"],
            "nb_results": cached["nb_results"],
            "status": "cached",
        }

    api_key = os.getenv("SERPAPI_KEY", "")
    if not api_key:
        return {
            "google_price": None,
            "google_min": None,
            "google_max": None,
            "nb_results": 0,
            "status": "no_api_key",
        }

    try:
        params = {
            "engine": "google_shopping",
            "q": f"{brand} {model}",
            "gl": "fr",
            "hl": "fr",
            "currency": "EUR",
            "api_key": api_key,
        }
        r = requests.get("https://serpapi.com/search", params=params, timeout=15)
        data = r.json()

        if "shopping_results" not in data:
            logger.warning("SerpAPI: pas de résultats pour %s %s", brand, model)
            return {
                "google_price": None,
                "google_min": None,
                "google_max": None,
                "nb_results": 0,
                "status": "no_results",
            }

        prices: list[float] = []
        for item in data["shopping_results"][:10]:
            price_str = str(item.get("price") or "").strip()
            if not price_str:
                continue
            # ex: "89,99 €" -> 89.99
            clean = price_str.replace("€", "").replace(" ", "").replace(",", ".").strip()
            clean = re.sub(r"[^0-9.]", "", clean)
            try:
                price = float(clean)
                if 30 <= price <= 600:
                    prices.append(price)
            except Exception:
                continue

        if not prices:
            return {
                "google_price": None,
                "google_min": None,
                "google_max": None,
                "nb_results": 0,
                "status": "no_valid_prices",
            }

        google_price = round(sum(prices) / len(prices), 2)
        google_min = round(min(prices), 2)
        google_max = round(max(prices), 2)
        nb_results = len(prices)

        _save_cached_price(
            brand,
            model,
            google_price,
            google_min,
            google_max,
            nb_results,
        )

        logger.info(
            "✅ SerpAPI %s %s: moy=%.2f\u00a0€ (%.2f-%.2f\u00a0€) sur %d offres",
            brand,
            model,
            google_price,
            google_min,
            google_max,
            nb_results,
        )

        return {"google_price": google_price, "google_min": google_min, "google_max": google_max, "nb_results": nb_results, "status": "ok"}

    except Exception as e:  # noqa: BLE001
        logger.error("SerpAPI erreur %s %s: %s", brand, model, e)
        return {
            "google_price": None,
            "google_min": None,
            "google_max": None,
            "nb_results": 0,
            "status": f"error: {str(e)[:50]}",
        }


def verify_against_google(brand: str, model: str, our_price: float) -> dict[str, Any]:
    """
    Compare notre prix avec Google Shopping.
    Retourne verdict : OK / SUSPECT / INVALIDE
    """
    result = scrape_google_shopping_price(brand, model)

    google_price = result.get("google_price")

    if not google_price:
        return {
            "verdict": "INCONNU",
            "our_price": our_price,
            "google_price": None,
            "deviation_pct": None,
            "message": "Prix Google non disponible",
            "status": result.get("status"),
        }

    deviation = ((our_price - google_price) / google_price) * 100

    if abs(deviation) < 25:
        verdict = "OK"
    elif abs(deviation) <= 40:
        verdict = "SUSPECT"
    else:
        verdict = "INVALIDE"

    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO google_verifications
            (brand, model, our_price, google_price,
             deviation_pct, verdict, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                brand,
                model,
                our_price,
                google_price,
                round(deviation, 2),
                verdict,
                now,
            ),
        )

    logger.info(
        "Google verify %s %s: nous=%s\u00a0€ google=%s\u00a0€ ecart=%+.1f%% -> %s",
        brand,
        model,
        our_price,
        google_price,
        deviation,
        verdict,
    )

    return {
        "verdict": verdict,
        "our_price": our_price,
        "google_price": google_price,
        "google_min": result.get("google_min"),
        "google_max": result.get("google_max"),
        "deviation_pct": round(deviation, 2),
        "nb_google_results": result.get("nb_results"),
        "message": f"Écart {deviation:+.1f}% vs Google Shopping FR",
        "status": result.get("status"),
    }


def run_google_verification_batch(sample_size: int = 5) -> dict[str, Any]:
    """
    Vérifie un échantillon de modèles contre Google Shopping.
    Appelé quotidiennement par le scheduler.
    """
    import pandas as pd

    if not MARKET_FR_CSV.is_file():
        return {"status": "skipped", "reason": "no_csv"}

    df = pd.read_csv(MARKET_FR_CSV)
    if df.empty:
        return {"status": "skipped", "reason": "empty_csv", "total": 0, "results": []}

    brands = df["brand"].dropna().unique().tolist()
    if not brands:
        return {"status": "skipped", "reason": "no_brands", "total": 0, "results": []}

    sample_models: list[dict[str, Any]] = []
    per_brand = max(1, sample_size // len(brands))
    for brand in brands:
        brand_df = df[df["brand"] == brand]
        if brand_df.empty:
            continue
        n = min(per_brand, len(brand_df))
        sample = brand_df.sample(n=n, random_state=None)
        sample_models.extend(sample.to_dict("records"))

    cap = min(max(1, int(sample_size)), 5)
    sample_models = sample_models[:cap]

    results: list[dict[str, Any]] = []
    ok = suspect = invalid = unknown = 0

    for row in sample_models:
        brand = str(row.get("brand") or "")
        model = str(row.get("model") or "")
        try:
            our_price = float(row.get("price_avg") or 0)
        except (TypeError, ValueError):
            continue
        if not brand or not model:
            continue

        logger.info("Google verify: %s %s (%.1f\u00a0€)", brand, model, our_price)

        result = verify_against_google(brand, model, our_price)
        result["brand"] = brand
        result["model"] = model
        results.append(result)

        verdict = result.get("verdict", "INCONNU")
        if verdict == "OK":
            ok += 1
        elif verdict == "SUSPECT":
            suspect += 1
        elif verdict == "INVALIDE":
            invalid += 1
        else:
            unknown += 1

        time.sleep(random.uniform(3.0, 5.0))

    summary = {
        "status": "ok",
        "total": len(results),
        "ok": ok,
        "suspect": suspect,
        "invalid": invalid,
        "unknown": unknown,
        "results": results,
        "run_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "Google batch: %d OK | %d suspects | %d invalides | %d inconnus",
        ok,
        suspect,
        invalid,
        unknown,
    )
    return summary


def get_recent_verifications(limit: int = 20) -> list[dict[str, Any]]:
    """Retourne les dernières vérifications Google."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM google_verifications
            ORDER BY verified_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_google_stats() -> dict[str, Any]:
    """Stats globales des vérifications Google."""
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM google_verifications").fetchone()
        ok = conn.execute(
            "SELECT COUNT(*) AS c FROM google_verifications WHERE verdict='OK'"
        ).fetchone()
        suspect = conn.execute(
            "SELECT COUNT(*) AS c FROM google_verifications WHERE verdict='SUSPECT'"
        ).fetchone()
        invalid = conn.execute(
            "SELECT COUNT(*) AS c FROM google_verifications WHERE verdict='INVALIDE'"
        ).fetchone()
    tc = int(total["c"]) if total else 0
    return {
        "total": tc,
        "ok": int(ok["c"]) if ok else 0,
        "suspect": int(suspect["c"]) if suspect else 0,
        "invalid": int(invalid["c"]) if invalid else 0,
        "fiabilite_pct": round((int(ok["c"]) / tc) * 100, 1) if tc else 0.0,
    }
