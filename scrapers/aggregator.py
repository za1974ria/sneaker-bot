"""Market aggregator (France only)."""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import statistics
from statistics import mean, median
from threading import Lock
from typing import Any, Callable, Iterable

from scrapers.base_scraper import BaseScraper
from scrapers.hypermarches import HYPERMARCHE_SCRAPER_CLASSES_REGISTERED
from scrapers.scraper_fr import FranceScraper
from scrapers.tier1_sites import TIER1_ECOM_EXTRA_SCRAPER_CLASSES, TIER1_EXTRA_SCRAPER_CLASSES
from scrapers.tier2_sites import TIER2_SCRAPER_CLASSES_REGISTERED

# Sources « extra » FR = e-com Tier 1 + Tier 2 + hypers
# (même ensemble que ``TIER1_EXTRA_SCRAPER_CLASSES``).
ALL_EXTRA_SCRAPERS_FR: tuple[tuple[str, type], ...] = (
    TIER1_ECOM_EXTRA_SCRAPER_CLASSES
    + TIER2_SCRAPER_CLASSES_REGISTERED
    + HYPERMARCHE_SCRAPER_CLASSES_REGISTERED
)
assert ALL_EXTRA_SCRAPERS_FR == TIER1_EXTRA_SCRAPER_CLASSES

logger = logging.getLogger(__name__)

# Cache résultats vides par source (clé string) — 6 h (pipeline FR).
_empty_cache: dict[str, float] = {}
_EMPTY_CACHE_LOCK = Lock()
EMPTY_TTL = 6 * 3600


def _is_empty_cached(name: str, brand: str, model: str) -> bool:
    key = f"{name}|{brand}|{model}"
    with _EMPTY_CACHE_LOCK:
        ts = _empty_cache.get(key)
    return bool(ts and time.time() - ts < EMPTY_TTL)


def _mark_empty(name: str, brand: str, model: str) -> None:
    key = f"{name}|{brand}|{model}"
    with _EMPTY_CACHE_LOCK:
        _empty_cache[key] = time.time()


_ai_supervisor_lock = Lock()
_ai_supervisor_instance: Any = None


def _get_ai_supervisor() -> Any:
    global _ai_supervisor_instance
    with _ai_supervisor_lock:
        if _ai_supervisor_instance is None:
            from app.ai_supervisor import AISupervisor

            _ai_supervisor_instance = AISupervisor()
        return _ai_supervisor_instance


def _robust_price_aggregation(
    brand: str, model: str, prices: list[float],
) -> dict[str, float | int] | None:
    """
    Agrégation robuste : règles marque, IQR, médiane, filtre vs médiane.
    Ne renvoie jamais une liste vide côté stats : fallback sur les entrées disponibles.
    """
    from app.brand_price_rules import filter_prices_by_brand_rules

    if not prices:
        return None

    prices_f = [float(p) for p in prices]
    prices_valid, prices_ecartes = filter_prices_by_brand_rules(brand, model, prices_f)

    if prices_ecartes:
        logger.info(
            "[BrandRules] %s %s: %d prix écartés %s",
            brand,
            model,
            len(prices_ecartes),
            prices_ecartes[:12],
        )

    if not prices_valid:
        prices_valid = list(prices_f)

    if len(prices_valid) >= 4:
        try:
            qs = statistics.quantiles(prices_valid, n=4)
            q1, q3 = float(qs[0]), float(qs[2])
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            prices_iqr = [p for p in prices_valid if lower <= p <= upper]
            if len(prices_iqr) >= len(prices_valid) * 0.5:
                prices_valid = prices_iqr
        except (statistics.StatisticsError, ValueError, IndexError):
            pass

    med = float(statistics.median(prices_valid))
    prices_final = [p for p in prices_valid if med * 0.4 <= p <= med * 2.0]
    if not prices_final:
        prices_final = list(prices_valid)

    return {
        "price_min": round(min(prices_final), 2),
        "price_max": round(max(prices_final), 2),
        "price_avg": round(float(statistics.mean(prices_final)), 2),
        "price_median": round(med, 2),
        "nb_sources": len(prices_final),
        "nb_ecartés": len(prices_f) - len(prices_final),
    }


def _apply_groq_price_filter(
    brand: str, model_name: str, prices: list[float],
) -> tuple[list[float], dict[str, Any] | None]:
    """
    Validation Groq (avec cache) sur les prix bruts ; retire anomalies / hors plage si besoin.
    Ne renvoie jamais une liste vide : fallback sur les prix d'origine.
    """
    if not prices:
        return prices, None
    try:
        sup = _get_ai_supervisor()
        vr = sup.validate_prices(model_name, brand, [float(p) for p in prices])
    except Exception as e:  # noqa: BLE001
        logger.warning("validate_prices skip pour %s %s: %s", brand, model_name, e)
        return list(prices), None

    prices_f = [float(p) for p in prices]
    anomalies = vr.get("anomalies") or []
    anom_rounded: set[float] = set()
    for a in anomalies:
        try:
            anom_rounded.add(round(float(a), 2))
        except (TypeError, ValueError):
            continue

    def _matches_anomaly(p: float) -> bool:
        rp = round(float(p), 2)
        if rp in anom_rounded:
            return True
        return any(abs(rp - x) <= 0.05 for x in anom_rounded)

    if not vr or vr.get("valid", True):
        filtered = [p for p in prices_f if not _matches_anomaly(p)]
    else:
        sr = vr.get("suggested_range") or []
        if len(sr) == 2:
            try:
                lo, hi = float(sr[0]), float(sr[1])
                filtered = [p for p in prices_f if lo * 0.7 <= p <= hi * 1.3]
            except (TypeError, ValueError):
                filtered = list(prices_f)
        else:
            filtered = list(prices_f)

    n_orig = len(prices_f)
    if len(filtered) < max(1, n_orig * 0.30):
        logger.info(
            "Groq: filtre trop agressif (%d -> %d) pour %s %s — conservation des prix bruts",
            n_orig,
            len(filtered),
            brand,
            model_name,
        )
        return list(prices_f), vr

    if len(filtered) < 1:
        logger.info(
            "Groq: filtre vide pour %s %s — conservation des %d prix bruts",
            brand,
            model_name,
            n_orig,
        )
        return list(prices_f), vr

    logger.info(
        "Groq prix %s %s valid=%s — %d -> %d prix",
        brand,
        model_name,
        vr.get("valid"),
        n_orig,
        len(filtered),
    )
    return filtered, vr

# Parallélisme FR : modèles en parallèle ; par modèle, sources via ThreadPoolExecutor (cap 4).
MAX_FR_PARALLEL_WORKERS = 4
MAX_FR_SOURCES_PARALLEL_WORKERS = 4
MAX_FR_SOURCES_WORKERS = 4
def _fr_pipeline_model_timeout_sec() -> float:
    """Budget scrape par modèle (mur wait() côté sources). Surcharge : FR_MODEL_PIPELINE_TIMEOUT_SEC."""
    raw = (os.environ.get("FR_MODEL_PIPELINE_TIMEOUT_SEC") or "").strip()
    try:
        v = float(raw)
        return max(15.0, v)
    except ValueError:
        return 180.0


def _fr_scraper_max_sites() -> int:
    """
    Nombre max de sources FR interrogées par modèle (4 cœur + Tier 1 extra).
    Surcharge : variable d'environnement FR_SCRAPER_MAX_SITES (entier ≥ 1).
    """
    try:
        from scrapers.tier1_sites import TIER1_EXTRA_SCRAPER_CLASSES

        default_all = 4 + len(TIER1_EXTRA_SCRAPER_CLASSES)
    except Exception:  # noqa: BLE001
        default_all = 17
    raw = (os.environ.get("FR_SCRAPER_MAX_SITES") or "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return default_all


def _fr_tier1_display_names() -> tuple[str, ...]:
    try:
        from scrapers.tier1_sites import TIER1_EXTRA_SCRAPER_CLASSES

        return tuple(d for d, _ in TIER1_EXTRA_SCRAPER_CLASSES)
    except Exception:  # noqa: BLE001
        return ()


# Sources FR actives pour maintenance (ordre = FranceScraper.scrape_model)
ACTIVE_SCRAPERS_FR: tuple[str, ...] = (
    "Courir",
    "Foot Locker",
    "Snipes",
    "Sports Direct",
) + _fr_tier1_display_names()

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
MODELS_LIST_PATH = DATA_DIR / "models_list.json"
MANUAL_PRICES_PATH = DATA_DIR / "manual_prices.json"

CSV_COLUMNS = [
    "brand",
    "model",
    "market",
    "price_min",
    "price_max",
    "price_avg",
    "price_median",
    "updated_at",
    "nb_sources",
    "groq_valid",
    "groq_confidence",
]

SOURCES_CSV_COLUMNS = [
    "brand",
    "model",
    "market",
    "shop",
    "price_min",
    "price_max",
    "price_avg",
    "price_count",
    "updated_at",
]

_LOCKS: dict[Path, Lock] = {}
_LOCKS_GUARD = Lock()


def _get_lock_for_path(path: Path) -> Lock:
    with _LOCKS_GUARD:
        if path not in _LOCKS:
            _LOCKS[path] = Lock()
        return _LOCKS[path]


@dataclass(frozen=True)
class ModelKey:
    brand: str
    model: str


def _read_existing_csv(csv_path: Path) -> dict[ModelKey, dict[str, Any]]:
    """
    Read existing market CSV if present.

    Returns a mapping:
      (brand, model) -> row dict
    """
    if not csv_path.is_file():
        return {}

    out: dict[ModelKey, dict[str, Any]] = {}
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return {}

            for row in reader:
                brand = (row.get("brand") or "").strip()
                model = (row.get("model") or "").strip()
                if not brand or not model:
                    continue
                out[ModelKey(brand=brand, model=model)] = dict(row)
    except Exception as e:
        logger.exception("Failed reading existing CSV %s: %s", csv_path, e)
    return out


def _fill_market_fr_csv_row(row: dict[str, Any]) -> None:
    """Garantit toutes les colonnes CSV (DictWriter n'accepte pas les clés manquantes)."""
    for col in CSV_COLUMNS:
        if col not in row or row[col] is None:
            row[col] = "1" if col == "nb_sources" else ""
        elif col == "nb_sources" and str(row[col]).strip() == "":
            row[col] = "1"


def _groq_csv_fields(groq_meta: dict[str, Any] | None) -> tuple[str, str]:
    if not groq_meta:
        return "", ""
    gv = "true" if groq_meta.get("valid") else "false"
    conf = groq_meta.get("confidence")
    try:
        gc = "" if conf is None else str(round(float(conf), 4))
    except (TypeError, ValueError):
        gc = ""
    return gv, gc


def _groq_valid_bool_from_csv(val: Any) -> bool | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    sl = s.lower()
    if sl == "true":
        return True
    if sl == "false":
        return False
    return None


def _write_csv_rows(csv_path: Path, rows: Iterable[dict[str, Any]], *, fieldnames: list[str] | None = None) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    cols = fieldnames or CSV_COLUMNS
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for row in rows:
            if cols is CSV_COLUMNS:
                r = dict(row)
                _fill_market_fr_csv_row(r)
                writer.writerow({k: r.get(k, "") for k in cols})
            else:
                writer.writerow(row)


def _load_models_list() -> list[dict[str, str]]:
    if not MODELS_LIST_PATH.is_file():
        logger.warning("Missing models list: %s", MODELS_LIST_PATH)
        return []
    try:
        raw = json.loads(MODELS_LIST_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").strip()
            model = str(item.get("model") or "").strip()
            if not brand or not model:
                continue
            out.append({"brand": brand, "model": model})
        return out
    except Exception as e:
        logger.exception("Failed loading models list %s: %s", MODELS_LIST_PATH, e)
        return []


def _scrape_fr_prices(scraper: FranceScraper, brand: str, model: str) -> list[float]:
    """
    FR : uniquement requests (FranceScraper). Pas de Playwright ici.
    Si [] → aucun fallback automatique.
    """
    try:
        return scraper.scrape_model(brand, model, max_sites=_fr_scraper_max_sites())
    except Exception as e:  # noqa: BLE001
        logger.exception("FranceScraper failed for %s %s: %s", brand, model, e)
        return []


def _fr_one_item_impl(
    item: dict[str, str],
) -> tuple[ModelKey, list[float], dict[str, list[float]], int, dict[str, Any] | None]:
    """Un seul passage réseau : collecte parallèle par source, puis agrégat + Groq."""
    brand = item["brand"]
    model_name = item["model"]
    key = ModelKey(brand=brand, model=model_name)
    ms = _fr_scraper_max_sites()
    scraper = FranceScraper()
    by_site: dict[str, list[float]] = {}
    twall = max(30.0, _fr_pipeline_model_timeout_sec() - 25.0)
    try:
        pairs = scraper._collect_parallel_fr_sites(
            brand,
            model_name,
            max_sites=ms,
            wall_timeout_sec=twall,
            max_workers_cap=MAX_FR_SOURCES_PARALLEL_WORKERS,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("FR parallel collect failed brand=%s model=%s err=%s", brand, model_name, e)
        pairs = []

    all_raw: list[float] = []
    for site_name, prices in pairs:
        if prices:
            all_raw.extend(prices)
        try:
            filtered = scraper.filter_prices(brand, prices) if prices else []
            if filtered:
                by_site[site_name] = filtered[: scraper.MAX_PRICES]
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "FR by-site filter failed site=%s brand=%s model=%s err=%s",
                site_name,
                brand,
                model_name,
                e,
            )

    try:
        from scrapers.sitemap_scraper import get_sitemap_prices

        # Budget réseau strict pour ne pas faire dépasser le timeout global du modèle FR.
        sitemap_prices = get_sitemap_prices(brand, model_name, max_wall_sec=70.0)
        if sitemap_prices:
            logger.info(
                "Sitemap +%d prix pour %s %s",
                len(sitemap_prices),
                brand,
                model_name,
            )
            all_raw.extend(float(x) for x in sitemap_prices)
    except Exception as e:  # noqa: BLE001
        logger.debug("Sitemap skip: %s", e)

    seen: set[float] = set()
    unique: list[float] = []
    for p in all_raw:
        if p in seen:
            continue
        seen.add(p)
        unique.append(p)
        if len(unique) >= FranceScraper.MAX_PRICES:
            break

    try:
        agg_filtered = scraper.filter_prices(brand, unique)
    except Exception as e:  # noqa: BLE001
        logger.debug("FR aggregate filter failed brand=%s model=%s err=%s", brand, model_name, e)
        agg_filtered = []

    try:
        from app.brand_price_rules import filter_prices_by_brand_rules

        pre_groq, _ = filter_prices_by_brand_rules(brand, model_name, agg_filtered)
        prices_for_groq = pre_groq if pre_groq else list(agg_filtered)
    except Exception as e:  # noqa: BLE001
        logger.debug("Brand rules pre-Groq skip %s %s: %s", brand, model_name, e)
        prices_for_groq = list(agg_filtered)

    prices, groq_meta = _apply_groq_price_filter(brand, model_name, prices_for_groq)
    nb_sources = max(1, len(prices))
    return key, prices, by_site, nb_sources, groq_meta


def _fr_one_item(
    item: dict[str, str],
) -> tuple[ModelKey, list[float], dict[str, list[float]], int, dict[str, Any] | None]:
    try:
        return _fr_one_item_impl(item)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Échec pipeline FR brand=%s model=%s: %s",
            item.get("brand"),
            item.get("model"),
            e,
        )
        key = ModelKey(brand=item["brand"], model=item["model"])
        return key, [], {}, 0, None


def _fr_parallel_scrape(
    models: list[dict[str, str]],
) -> tuple[
    dict[ModelKey, list[float]],
    dict[ModelKey, dict[str, list[float]]],
    dict[ModelKey, int],
    dict[ModelKey, dict[str, Any] | None],
]:
    """Scrape tous les modèles FR en parallèle (une instance FranceScraper par thread)."""
    out: dict[ModelKey, list[float]] = {}
    by_site_out: dict[ModelKey, dict[str, list[float]]] = {}
    nb_sources_map: dict[ModelKey, int] = {}
    groq_meta_map: dict[ModelKey, dict[str, Any] | None] = {}
    n = len(models)
    workers = min(MAX_FR_PARALLEL_WORKERS, max(1, n))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fr_one_item, item) for item in models]
        for fut in as_completed(futures):
            key, prices, by_site, nb_sources, groq_meta = fut.result()
            out[key] = prices
            by_site_out[key] = by_site
            nb_sources_map[key] = nb_sources
            groq_meta_map[key] = groq_meta
    return out, by_site_out, nb_sources_map, groq_meta_map


BRAND_MAX_SPREAD_RATIO: dict[str, float] = {
    "Nike": 1.75,
    "Adidas": 1.75,
    "New Balance": 1.80,
    "Salomon": 1.70,
    "Asics": 1.75,
    "Puma": 1.80,
    "Reebok": 1.80,
    "Vans": 1.75,
    "Converse": 1.75,
    "On Running": 1.65,
    "default": 1.85,
}


def _max_spread_ratio_for_brand(brand: str) -> float:
    return float(BRAND_MAX_SPREAD_RATIO.get(brand, BRAND_MAX_SPREAD_RATIO["default"]))


def _price_floor_for_brand(brand: str) -> float:
    return float(BaseScraper.PRICE_FLOORS.get(brand, BaseScraper.PRICE_FLOORS["default"]))


def _normalized_prices_for_stats(brand: str, prices: list[float]) -> list[float]:
    """
    Réduit les outliers pour éviter des spreads min/max non crédibles.
    Garde un cluster autour de la médiane, borné par la plage marque.
    """
    if not prices:
        return []

    # Bornes métier marque (exclut déjà les erreurs grossières)
    min_allowed, max_allowed = BaseScraper.PRICE_RANGES.get(brand, BaseScraper.PRICE_RANGES["default"])
    in_range = [float(p) for p in prices if min_allowed <= float(p) <= max_allowed]
    if not in_range:
        return []

    in_range.sort()
    n = len(in_range)
    m = median(in_range)

    # Trim symétrique (10%) pour atténuer les extrêmes si assez d'échantillons.
    trim = int(n * 0.10) if n >= 6 else 0
    core = in_range[trim : n - trim] if trim > 0 and (n - trim) > trim else in_range
    if not core:
        core = in_range

    # Bande robuste autour de la médiane (beaucoup plus stricte).
    low = max(min_allowed, m * 0.72)
    high = min(max_allowed, m * 1.38)
    cluster = [p for p in core if low <= p <= high]

    # Si le filtre est trop agressif, fallback au core trimé.
    if len(cluster) >= 2:
        return cluster
    if len(core) >= 2:
        return core
    return in_range


def _is_valid_triplet(brand: str, price_min: float, price_max: float, price_avg: float) -> bool:
    """
    Validation stricte des prix pour éviter toute incohérence exploitable UI/API.
    """
    if price_min <= 0 or price_max <= 0 or price_avg <= 0:
        return False
    if price_min > price_max:
        return False
    if not (price_min <= price_avg <= price_max):
        return False
    min_allowed, max_allowed = BaseScraper.PRICE_RANGES.get(brand, BaseScraper.PRICE_RANGES["default"])
    if not (min_allowed <= price_min <= max_allowed and min_allowed <= price_max <= max_allowed):
        return False
    spread_ratio = (price_max / price_min) if price_min > 0 else 999.0
    if spread_ratio > _max_spread_ratio_for_brand(brand):
        return False
    return True


def _load_manual_prices() -> dict[str, dict[str, float]]:
    if not MANUAL_PRICES_PATH.is_file():
        return {}
    try:
        raw = json.loads(MANUAL_PRICES_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Lecture impossible %s: %s", MANUAL_PRICES_PATH, e)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            out[str(k)] = {
                "price_min": float(v["price_min"]),
                "price_max": float(v["price_max"]),
                "price_avg": float(v["price_avg"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _apply_manual_prices(rows: list[dict[str, Any]], market_norm: str, manual: dict[str, dict[str, float]]) -> None:
    """
    Remplace par des prix manuels si scrape douteux et clé ``{brand}|{model}|{market}`` présente.

    Déclencheurs : ``price_min == price_max`` ; ou toute la marque avec le même triple ;
    ou au moins deux modèles de la marque partagent le même triple (scrape « plat »,
    ex. plusieurs Reebok identiques alors qu’un autre modèle diffère).
    """
    if not manual or not rows:
        return

    by_brand: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        b = str(r.get("brand") or "").strip()
        if b:
            by_brand.setdefault(b, []).append(r)

    # Comptage (marque, triple min/max/avg) : plusieurs modèles avec le même agrégat = suspect.
    triple_count: dict[tuple[str, tuple[float, float, float]], int] = {}
    for brand, brand_rows in by_brand.items():
        for r in brand_rows:
            try:
                t = (
                    float(r.get("price_min")),
                    float(r.get("price_max")),
                    float(r.get("price_avg")),
                )
            except (TypeError, ValueError):
                continue
            triple_count[(brand, t)] = triple_count.get((brand, t), 0) + 1

    for brand, brand_rows in by_brand.items():
        triples: list[tuple[float, float, float]] = []
        for r in brand_rows:
            try:
                triples.append(
                    (
                        float(r.get("price_min")),
                        float(r.get("price_max")),
                        float(r.get("price_avg")),
                    )
                )
            except (TypeError, ValueError):
                continue
        brand_all_identical = len(triples) >= 2 and len(set(triples)) == 1

        for r in brand_rows:
            key = f"{str(r.get('brand') or '').strip()}|{str(r.get('model') or '').strip()}|{market_norm}"
            if key not in manual:
                continue
            try:
                pm = float(r.get("price_min"))
                px = float(r.get("price_max"))
                pa = float(r.get("price_avg"))
            except (TypeError, ValueError):
                continue
            t_row = (pm, px, pa)
            same_triple_repeated = triple_count.get((brand, t_row), 0) >= 2
            spread_ratio = (px / pm) if pm > 0 else 999.0
            use_manual = (
                pm == px
                or brand_all_identical
                or same_triple_repeated
                or spread_ratio > _max_spread_ratio_for_brand(brand)
            )
            if use_manual:
                m = manual[key]
                r["price_min"] = m["price_min"]
                r["price_max"] = m["price_max"]
                r["price_avg"] = m["price_avg"]
                logger.info(
                    "Prix manuels utilisés pour %s %s",
                    str(r.get("brand") or "").strip(),
                    str(r.get("model") or "").strip(),
                )


def diagnose_fr_requests_per_site(brand: str, model: str) -> None:
    """
    DIAGNOSTIC temporaire (marché FR) : affiche combien de prix chaque site
    retourne via FranceScraper (requests), pour un seul modèle.
    """
    s = FranceScraper()
    from scrapers.tier1_sites import TIER1_EXTRA_SCRAPER_CLASSES

    site_fns: list[tuple[str, Callable[..., list[float]]]] = [
        ("Courir", s._scrape_courir),
        ("Footlocker", s._scrape_footlocker),
        ("Snipes", s._scrape_snipes),
        ("Sportsdirect", s._scrape_sportsdirect),
    ]
    for disp, cls in TIER1_EXTRA_SCRAPER_CLASSES:
        site_fns.append(
            (disp, lambda b, m, d=disp, c=cls, inst=s: inst._scrape_tier1_site(d, c, b, m))
        )
    print(f"=== FranceScraper (requests) | {brand} {model} ===", flush=True)
    for site_name, fn in site_fns:
        print(f"  ... scraping [{site_name}] ...", flush=True)
        try:
            prices = fn(brand, model)
            n = len(prices) if prices else 0
            print(f"  [{site_name}] {n} prix trouvés", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [{site_name}] erreur: {e}", flush=True)


def run_market(market: str) -> None:
    """
    Scrape one market and update CSV with min/max/avg.

    CSV columns:
      brand, model, market, price_min, price_max, price_avg, price_median, updated_at,
      nb_sources, groq_valid, groq_confidence
    """
    market_norm = (market or "").strip().upper()
    if market_norm != "FR":
        raise ValueError("market must be FR")
    logger.info("market=%s start", market_norm)

    csv_path = DATA_DIR / f"market_{market_norm.lower()}.csv"
    sources_csv_path = DATA_DIR / f"market_{market_norm.lower()}_sources.csv"
    lock = _get_lock_for_path(csv_path)

    # Keep last known values
    with lock:
        existing = _read_existing_csv(csv_path)

        models = _load_models_list()
        if not models:
            return

        try:
            from app.price_history import init_db

            init_db()
        except Exception as e:  # noqa: BLE001
            logger.warning("price_history init_db skip: %s", e)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # We'll build output rows for all models, but only update values
        # when we got new prices. If a model didn't exist in the previous
        # CSV and no prices are found, we skip it.
        out_rows: list[dict[str, Any]] = []

        fr_prices_by_key, fr_prices_by_site, fr_nb_sources, fr_groq_meta = _fr_parallel_scrape(models)
        manual_prices = _load_manual_prices()
        source_rows: list[dict[str, Any]] = []
        # Monitoring scraper health (par run, par source).
        try:
            from app.scraper_monitor import init_scraper_monitor_db, upsert_scraper_health

            init_scraper_monitor_db()
            site_product_counts: dict[str, int] = {name: 0 for name in ACTIVE_SCRAPERS_FR}
            for by_site in fr_prices_by_site.values():
                for site_name, prices in (by_site or {}).items():
                    if prices:
                        site_product_counts[site_name] = site_product_counts.get(site_name, 0) + 1
            for site_name in ACTIVE_SCRAPERS_FR:
                n_prod = int(site_product_counts.get(site_name, 0))
                upsert_scraper_health(
                    site_name,
                    last_success=n_prod > 0,
                    nb_products=n_prod,
                    error_message="" if n_prod > 0 else "no_data",
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("scraper_monitor skip: %s", e)

        for item in models:
            brand = item["brand"]
            model_name = item["model"]
            key = ModelKey(brand=brand, model=model_name)

            last_row = existing.get(key)

            try:
                prices = fr_prices_by_key.get(key, [])
            except Exception as e:  # noqa: BLE001
                logger.exception("scrape failed market=%s brand=%s model=%s: %s", market_norm, brand, model_name, e)
                prices = []

            if prices:
                rob = _robust_price_aggregation(brand, model_name, list(prices))
                if rob:
                    price_min = float(rob["price_min"])
                    price_max = float(rob["price_max"])
                    price_avg = float(rob["price_avg"])
                    price_median = float(rob["price_median"])
                    nb_src = max(1, int(rob["nb_sources"]))
                else:
                    normalized = _normalized_prices_for_stats(brand, prices)
                    prices_for_stats = normalized if normalized else prices
                    price_min = round(min(prices_for_stats), 2)
                    price_max = round(max(prices_for_stats), 2)
                    price_avg = round(mean(prices_for_stats), 2)
                    price_median = round(float(median(prices_for_stats)), 2)
                    nb_src = fr_nb_sources.get(key, len(prices))
                    if nb_src < 1:
                        nb_src = max(1, len(prices))
                gv, gc = _groq_csv_fields(fr_groq_meta.get(key))

                out_rows.append(
                    {
                        "brand": brand,
                        "model": model_name,
                        "market": market_norm,
                        "price_min": price_min,
                        "price_max": price_max,
                        "price_avg": price_avg,
                        "price_median": price_median,
                        "updated_at": now,
                        "nb_sources": str(nb_src),
                        "groq_valid": gv,
                        "groq_confidence": gc,
                    }
                )
                if price_min < _price_floor_for_brand(brand):
                    logger.warning("Prix suspect détecté : %s %s min=%.2f", brand, model_name, price_min)
            else:
                if last_row:
                    # Preserve last known values (do not overwrite with empties).
                    # We still bump updated_at to now to indicate "checked".
                    preserved = dict(last_row)
                    preserved["market"] = market_norm
                    preserved["updated_at"] = now
                    out_rows.append(preserved)
                    try:
                        preserved_min = float(preserved.get("price_min") or 0.0)
                    except (TypeError, ValueError):
                        preserved_min = 0.0
                    if preserved_min < _price_floor_for_brand(brand):
                        logger.warning("Prix suspect détecté : %s %s min=%.2f", brand, model_name, preserved_min)
                else:
                    # No prior value known; use manual prices if available.
                    manual_key = f"{brand}|{model_name}|{market_norm}"
                    manual = manual_prices.get(manual_key)
                    if manual:
                        pa_m = float(manual["price_avg"])
                        out_rows.append(
                            {
                                "brand": brand,
                                "model": model_name,
                                "market": market_norm,
                                "price_min": manual["price_min"],
                                "price_max": manual["price_max"],
                                "price_avg": manual["price_avg"],
                                "price_median": round(pa_m, 2),
                                "updated_at": now,
                                "nb_sources": "1",
                                "groq_valid": "",
                                "groq_confidence": "",
                            }
                        )
                        logger.info("Prix manuels utilisés pour %s %s", brand, model_name)
                    else:
                        # No prior value known; skip.
                        continue

        _apply_manual_prices(out_rows, market_norm, manual_prices)

        # Durcissement final : aucune ligne incohérente ne doit sortir.
        for r in out_rows:
            b = str(r.get("brand") or "").strip()
            m = str(r.get("model") or "").strip()
            key = f"{b}|{m}|{market_norm}"
            try:
                pm = float(r.get("price_min"))
                px = float(r.get("price_max"))
                pa = float(r.get("price_avg"))
            except (TypeError, ValueError):
                pm = px = pa = 0.0

            if _is_valid_triplet(b, pm, px, pa):
                continue

            # 1) Priorité absolue aux prix manuels validés.
            manual = manual_prices.get(key)
            if manual:
                r["price_min"] = manual["price_min"]
                r["price_max"] = manual["price_max"]
                r["price_avg"] = manual["price_avg"]
                try:
                    r["price_median"] = round(float(manual["price_avg"]), 2)
                except (TypeError, ValueError):
                    r["price_median"] = manual.get("price_avg", "")
                logger.info("Prix manuels utilisés pour %s %s", b, m)
                continue

            # 2) Fallback sécurisé: recentrer avg + cap spread.
            if pm <= 0:
                pm = max(_price_floor_for_brand(b), 1.0)
            if px < pm:
                px = pm
            max_cap = round(pm * _max_spread_ratio_for_brand(b), 2)
            if px > max_cap:
                px = max_cap
            pa = min(max(pa if pa > 0 else pm, pm), px)
            r["price_min"] = round(pm, 2)
            r["price_max"] = round(px, 2)
            r["price_avg"] = round(pa, 2)
            r["price_median"] = round(pa, 2)
            logger.warning("Prix recalés en mode strict: %s %s", b, m)

        for r in out_rows:
            if not str(r.get("price_median") or "").strip():
                try:
                    r["price_median"] = round(float(r.get("price_avg") or 0), 2)
                except (TypeError, ValueError):
                    r["price_median"] = ""

        # Sources par boutique pour comparaison crédible frontend (type Trivago).
        for item in models:
            brand = item["brand"]
            model_name = item["model"]
            key = ModelKey(brand=brand, model=model_name)
            for shop, prices in (fr_prices_by_site.get(key) or {}).items():
                if not prices:
                    continue
                normalized = _normalized_prices_for_stats(brand, prices)
                prices_for_stats = normalized if normalized else prices
                source_rows.append(
                    {
                        "brand": brand,
                        "model": model_name,
                        "market": market_norm,
                        "shop": shop,
                        "price_min": round(min(prices_for_stats), 2),
                        "price_max": round(max(prices_for_stats), 2),
                        "price_avg": round(mean(prices_for_stats), 2),
                        "price_count": len(prices_for_stats),
                        "updated_at": now,
                    }
                )

        # Durcissement des lignes sources : supprime toute incohérence résiduelle.
        strict_sources: list[dict[str, Any]] = []
        for r in source_rows:
            b = str(r.get("brand") or "").strip()
            try:
                pm = float(r.get("price_min"))
                px = float(r.get("price_max"))
                pa = float(r.get("price_avg"))
            except (TypeError, ValueError):
                continue
            if not _is_valid_triplet(b, pm, px, pa):
                continue
            strict_sources.append(r)
        source_rows = strict_sources

        # Deterministic output (stable ordering)
        out_rows.sort(key=lambda r: (str(r.get("brand") or "").lower(), str(r.get("model") or "").lower()))
        source_rows.sort(
            key=lambda r: (
                str(r.get("brand") or "").lower(),
                str(r.get("model") or "").lower(),
                str(r.get("shop") or "").lower(),
            )
        )
        _write_csv_rows(csv_path, out_rows, fieldnames=CSV_COLUMNS)
        _write_csv_rows(sources_csv_path, source_rows, fieldnames=SOURCES_CSV_COLUMNS)

        try:
            from app.price_history import purge_old_records, record_snapshot

            for r in out_rows:
                b = str(r.get("brand") or "").strip()
                m = str(r.get("model") or "").strip()
                if not b or not m:
                    continue
                try:
                    pm = float(r.get("price_min"))
                    px = float(r.get("price_max"))
                    pa = float(r.get("price_avg"))
                except (TypeError, ValueError):
                    continue
                try:
                    nb_src = int(float(str(r.get("nb_sources") or "1").replace(",", ".")))
                except (TypeError, ValueError):
                    nb_src = 1
                nb_src = max(1, nb_src)
                gvb = _groq_valid_bool_from_csv(r.get("groq_valid"))
                try:
                    record_snapshot(
                        brand=b,
                        model=m,
                        price_min=pm,
                        price_max=px,
                        price_avg=pa,
                        nb_sources=nb_src,
                        groq_valid=gvb,
                        market=market_norm,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Historique prix non enregistré pour %s %s: %s", b, m, e)
                try:
                    from app.price_alerts import check_price_alerts

                    check_price_alerts(b, m, pa)
                except Exception as e:  # noqa: BLE001
                    logger.debug("Alerte prix skip %s %s: %s", b, m, e)
            try:
                purge_old_records()
            except Exception as e:  # noqa: BLE001
                logger.warning("Purge historique échouée: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("Module price_history indisponible ou erreur: %s", e)

    try:
        from scrapers.hypermarches import close_shared_browser

        close_shared_browser()
    except Exception:  # noqa: BLE001
        pass
    logger.info("market=%s scraped/updated ok", market_norm)


def rebuild_fr_sources_csv(*, max_sites: int | None = None, workers: int = MAX_FR_SOURCES_WORKERS) -> int:
    """
    Reconstruit data/market_fr_sources.csv par boutique.
    max_sites=None → même limite que run_market (FR_SCRAPER_MAX_SITES / toutes les sources).
    Retourne le nombre de lignes écrites.
    """
    if max_sites is None:
        max_sites = _fr_scraper_max_sites()
    models = _load_models_list()
    if not models:
        return 0

    market_norm = "FR"
    csv_path = DATA_DIR / "market_fr.csv"
    sources_csv_path = DATA_DIR / "market_fr_sources.csv"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing = _read_existing_csv(csv_path)

    def one(item: dict[str, str]) -> list[dict[str, Any]]:
        b = item["brand"]
        m = item["model"]
        key = ModelKey(brand=b, model=m)
        scraper = FranceScraper()
        rows: list[dict[str, Any]] = []
        try:
            by_site = scraper.scrape_model_by_site(b, m, max_sites=max_sites)
        except Exception:  # noqa: BLE001
            by_site = {}

        if by_site:
            for shop, prices in by_site.items():
                if not prices:
                    continue
                normalized = _normalized_prices_for_stats(b, prices)
                p = normalized if normalized else prices
                rows.append(
                    {
                        "brand": b,
                        "model": m,
                        "market": market_norm,
                        "shop": shop,
                        "price_min": round(min(p), 2),
                        "price_max": round(max(p), 2),
                        "price_avg": round(mean(p), 2),
                        "price_count": len(p),
                        "updated_at": (existing.get(key) or {}).get("updated_at") or now,
                    }
                )
            return rows

        # fallback: garder une ligne agrégée pour ne jamais retourner vide.
        last = existing.get(key)
        if not last:
            return rows
        rows.append(
            {
                "brand": b,
                "model": m,
                "market": market_norm,
                "shop": "Agrege FR",
                "price_min": last.get("price_min", ""),
                "price_max": last.get("price_max", ""),
                "price_avg": last.get("price_avg", ""),
                "price_count": 1,
                "updated_at": last.get("updated_at") or now,
            }
        )
        return rows

    out: list[dict[str, Any]] = []
    max_workers = min(max(1, workers), len(models))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(one, item) for item in models]
        for fut in as_completed(futures):
            out.extend(fut.result())

    out.sort(
        key=lambda r: (
            str(r.get("brand") or "").lower(),
            str(r.get("model") or "").lower(),
            str(r.get("shop") or "").lower(),
        )
    )
    _write_csv_rows(sources_csv_path, out, fieldnames=SOURCES_CSV_COLUMNS)
    logger.info("market=FR sources rebuilt ok rows=%d max_sites=%d", len(out), max_sites)
    return len(out)


if __name__ == "__main__":
    # DIAGNOSTIC : python -m scrapers.aggregator
    diagnose_fr_requests_per_site("Nike", "Air Force 1")
    print()
    diagnose_fr_requests_per_site("Reebok", "BB 4000")

