"""High Precision Core: Google + Precision + confirmation scraping."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from scrapers.precision_engine import validate_price

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_CACHE_PATH = _ROOT / "data" / "high_precision_cache.json"
_DEFAULT_TTL_SEC = 20 * 60

# Aligné avec le reste du projet: charge .env à l'import du module.
load_dotenv(_ROOT / ".env")


def _cache_key(brand: str, model: str, market: str = "FR") -> str:
    return f"{(brand or '').strip().lower()}|{(model or '').strip().lower()}|{(market or 'FR').strip().upper()}"


def _load_cache() -> dict[str, Any]:
    if not _CACHE_PATH.is_file():
        return {}
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug("high_precision cache save skip: %s", e)


def get_cached_high_precision_prices(brand: str, model: str, *, market: str = "FR", ttl_sec: int = _DEFAULT_TTL_SEC) -> list[float]:
    """Retourne les prix cache si frais, sinon []."""
    key = _cache_key(brand, model, market)
    cache = _load_cache()
    row = cache.get(key)
    if not isinstance(row, dict):
        return []
    ts = float(row.get("ts") or 0.0)
    if time.time() - ts > max(1, int(ttl_sec)):
        return []
    prices = row.get("prices") or []
    out: list[float] = []
    for p in prices:
        try:
            v = float(p)
            if v > 0:
                out.append(v)
        except (TypeError, ValueError):
            continue
    return out


def should_rescrape_model(brand: str, model: str, *, market: str = "FR", ttl_sec: int = _DEFAULT_TTL_SEC) -> bool:
    """
    Cache intelligent:
    - ne rescrape pas si cache frais et fiable
    - rescrape sinon
    """
    key = _cache_key(brand, model, market)
    cache = _load_cache()
    row = cache.get(key)
    if not isinstance(row, dict):
        return True
    ts = float(row.get("ts") or 0.0)
    if time.time() - ts > max(1, int(ttl_sec)):
        return True
    suspicious = bool(row.get("suspicious"))
    reliability = float(row.get("overall_reliability") or 0.0)
    prices = row.get("prices") or []
    if not prices:
        return True
    if suspicious:
        return True
    return reliability < 70.0


def run_high_precision_core(
    *,
    brand: str,
    model: str,
    market: str = "FR",
    scraped_prices: list[float] | None = None,
    confirm_scrape_fn: Callable[[], list[float]] | None = None,
) -> dict[str, Any]:
    """
    Architecture:
      Source 1: Google Shopping (SerpAPI cache)
      Source 2: Precision Engine + history
      Source 3: scraping direct seulement pour confirmation cas suspects/drops
    """
    scraped_prices = list(scraped_prices or [])

    google_prices: list[float] = []
    google_shops: list[dict[str, Any]] = []
    serpapi_key = (os.getenv("SERPAPI_KEY") or "").strip()
    if not serpapi_key:
        logger.warning("HighPrecisionCore: SERPAPI_KEY manquant, source Google désactivée.")
    try:
        from app.google_shopping_verifier import get_all_shops_prices

        if serpapi_key:
            google_shops = get_all_shops_prices(brand, model, force=False)
            for row in google_shops:
                try:
                    p = float(row.get("price"))
                    if p > 0:
                        google_prices.append(p)
                except (TypeError, ValueError):
                    continue
    except Exception as e:  # noqa: BLE001
        logger.debug("high_precision google source skip %s %s: %s", brand, model, e)

    # Source principale + source scrape existante
    all_candidates = []
    seen: set[float] = set()
    for p in google_prices + scraped_prices:
        try:
            v = round(float(p), 2)
        except (TypeError, ValueError):
            continue
        if v <= 0 or v in seen:
            continue
        seen.add(v)
        all_candidates.append(v)

    accepted: list[float] = []
    suspicious_count = 0
    best_conf = 0.0
    best_rel = 0.0
    for p in all_candidates:
        precision = validate_price(
            {
                "brand": brand,
                "model": model,
                "market": market,
                "site": "high_precision_core",
                "source": "high_precision_core",
                "price": p,
                "current_price": p,
            }
        )
        conf = float(precision.get("confidence_score") or 0.0)
        rel = float(precision.get("overall_reliability") or 0.0)
        best_conf = max(best_conf, conf)
        best_rel = max(best_rel, rel)
        suspicious = bool(precision.get("is_suspicious"))
        if suspicious:
            suspicious_count += 1
        # Tolérance: on garde aussi les "légèrement suspects" fiables.
        if (not suspicious) or conf >= 72.0 or rel >= 68.0:
            accepted.append(p)

    if not accepted:
        accepted = list(all_candidates)

    # Détection drop basée sur historique precision_engine (via reasoning interne)
    drop_like = False
    for p in accepted[:3]:
        precision = validate_price(
            {
                "brand": brand,
                "model": model,
                "market": market,
                "site": "high_precision_core",
                "source": "high_precision_core",
                "price": p,
                "current_price": p,
            }
        )
        rs = str(precision.get("reasoning") or "").lower()
        if "drop" in rs or "ghost_price_alert=true" in rs:
            drop_like = True
            break

    # Source tertiaire: confirmation seulement si nécessaire.
    confirmed_prices: list[float] = []
    if (suspicious_count > 0 or drop_like) and callable(confirm_scrape_fn):
        try:
            confirmed_prices = [float(x) for x in (confirm_scrape_fn() or []) if float(x) > 0]
        except Exception as e:  # noqa: BLE001
            logger.debug("high_precision confirm scrape skip %s %s: %s", brand, model, e)
            confirmed_prices = []
        for p in confirmed_prices:
            rp = round(float(p), 2)
            if rp not in seen and rp > 0:
                accepted.append(rp)
                seen.add(rp)

    accepted = sorted(accepted)[:60]
    suspicious_global = suspicious_count > 0 or drop_like
    overall_reliability = max(0.0, min(100.0, (best_rel * 0.65 + best_conf * 0.35)))

    # Cache intelligent
    key = _cache_key(brand, model, market)
    cache = _load_cache()
    cache[key] = {
        "ts": time.time(),
        "prices": accepted,
        "google_prices": google_prices,
        "google_shop_count": len(google_shops),
        "confirmed_prices": confirmed_prices,
        "suspicious": bool(suspicious_global),
        "overall_reliability": round(float(overall_reliability), 2),
    }
    _save_cache(cache)

    return {
        "prices": accepted,
        "google_prices": google_prices,
        "google_shop_count": len(google_shops),
        "confirmed_prices": confirmed_prices,
        "suspicious": bool(suspicious_global),
        "overall_reliability": round(float(overall_reliability), 2),
    }

