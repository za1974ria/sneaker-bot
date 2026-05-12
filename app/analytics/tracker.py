"""Simple local JSON tracker for product analytics."""

from __future__ import annotations

import json
import logging
import os
import csv
import time
from pathlib import Path

try:
    import pandas as pd
except Exception:  # pragma: no cover - fallback runtime
    pd = None  # type: ignore[assignment]

TRACKING_PATH = Path(__file__).resolve().parents[2] / "data" / "tracking.json"
MARKET_FR_SOURCES_PATH = Path(__file__).resolve().parents[2] / "data" / "market_fr_sources.csv"
logger = logging.getLogger(__name__)
DEFAULT_TRACKING = {
    "premium_clicks": 0,
    "search_views": 0,
}
_CACHE_TTL_SEC = int((os.getenv("CACHE_TTL_SEC") or "600").strip() or "600")


def _ttl() -> int:
    try:
        from app.central_cache import default_ttl_sec

        return int(default_ttl_sec())
    except Exception:
        return max(60, _CACHE_TTL_SEC)


def _set_scraper_health_cache(rows: list[dict[str, object]]) -> None:
    """Persiste le résultat dans le cache central (Redis + repli local)."""
    try:
        from app.central_cache import set_json

        set_json("tracker:health", [dict(r) for r in rows if isinstance(r, dict)], _ttl())
    except Exception:
        pass


def clear_tracker_caches() -> None:
    """Invalidate tracker caches (stats + scraper health)."""
    try:
        from app.central_cache import delete_key

        delete_key("tracker:stats")
        delete_key("tracker:health")
    except Exception:
        pass


def _ensure_file() -> None:
    try:
        TRACKING_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not TRACKING_PATH.is_file():
            TRACKING_PATH.write_text(
                json.dumps(DEFAULT_TRACKING, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        # Never break the app because of analytics writes.
        return


def _load_tracking() -> dict[str, int]:
    _ensure_file()
    try:
        raw = json.loads(TRACKING_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return dict(DEFAULT_TRACKING)
        out = dict(DEFAULT_TRACKING)
        for k in DEFAULT_TRACKING:
            try:
                out[k] = int(raw.get(k, 0))
            except (TypeError, ValueError):
                out[k] = 0
        return out
    except Exception:
        return dict(DEFAULT_TRACKING)


def _save_tracking(data: dict[str, int]) -> None:
    try:
        TRACKING_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Never break the app because of analytics writes.
        return


def log_event(event_name: str) -> None:
    """
    Increment a tracked event counter in local JSON.
    """
    data = _load_tracking()
    if event_name not in data:
        data[event_name] = 0
    try:
        data[event_name] = int(data[event_name]) + 1
    except Exception:
        data[event_name] = 1
    _save_tracking(data)
    try:
        from app.central_cache import delete_key

        delete_key("tracker:stats")
    except Exception:
        pass


def get_stats() -> dict[str, int]:
    """Read current tracking counters."""
    try:
        from app.central_cache import get_json, set_json

        cached = get_json("tracker:stats")
        if isinstance(cached, dict) and cached:
            out = dict(DEFAULT_TRACKING)
            for k, v in cached.items():
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    out[k] = 0
            for k in DEFAULT_TRACKING:
                if k not in out:
                    out[k] = int(DEFAULT_TRACKING[k])
            return out
    except Exception:
        pass
    value = _load_tracking()
    try:
        from app.central_cache import set_json

        set_json("tracker:stats", dict(value), _ttl())
    except Exception:
        pass
    return value


def _market_fr_sources_buffer():
    """Texte CSV préchargé (mémoire) si disponible."""
    try:
        from io import StringIO

        from app.csv_preload import get_csv_text

        t = get_csv_text("market_fr_sources")
        if t and t.strip():
            return StringIO(t)
    except Exception:
        pass
    return None


def get_scraper_health_rows() -> list[dict[str, object]]:
    """
    Build scraper health rows from data/market_fr_sources.csv.

    Output row keys:
    - scraper_name
    - last_success
    - nb_products
    - last_run
    - error_message
    """
    try:
        from app.central_cache import get_json

        cached = get_json("tracker:health")
        if isinstance(cached, list) and cached:
            return [dict(r) for r in cached if isinstance(r, dict)]
    except Exception:
        pass

    buf = _market_fr_sources_buffer()
    # Fallback explicite si la source n'existe pas encore.
    if not MARKET_FR_SOURCES_PATH.is_file() and buf is None:
        logger.info("analytics tracker: CSV absent %s", MARKET_FR_SOURCES_PATH)
        _set_scraper_health_cache([])
        return []

    if pd is None:
        logger.warning("analytics tracker: pandas indisponible, fallback csv module")
        try:
            from contextlib import nullcontext

            rows: list[dict[str, object]] = []
            fh_ctx = nullcontext(buf) if buf is not None else MARKET_FR_SOURCES_PATH.open("r", encoding="utf-8", newline="")
            with fh_ctx as f:
                reader = csv.DictReader(f)
                for row in reader:
                    scraper_name = str(
                        row.get("source")
                        or row.get("scraper_name")
                        or row.get("scraper")
                        or row.get("shop")
                        or row.get("site")
                        or "unknown"
                    ).strip() or "unknown"
                    model_name = str(
                        row.get("model") or row.get("product") or row.get("sneaker") or row.get("name") or ""
                    ).strip()
                    last_run = str(
                        row.get("updated_at") or row.get("last_run") or row.get("timestamp") or row.get("scraped_at") or ""
                    ).strip()
                    error_message = str(
                        row.get("error_message") or row.get("error") or row.get("last_error") or ""
                    ).strip()
                    success_raw = str(row.get("last_success") or row.get("success") or row.get("ok") or "").strip().lower()
                    if success_raw in {"1", "true", "yes", "ok", "success"}:
                        last_success = True
                    elif success_raw in {"0", "false", "no", "ko", "error", "failed", "fail"}:
                        last_success = False
                    else:
                        last_success = True if model_name else False
                    rows.append(
                        {
                            "scraper_name": scraper_name,
                            "last_success": last_success,
                            "nb_products": 1 if model_name else 0,
                            "last_run": last_run,
                            "error_message": error_message,
                        }
                    )
            if not rows:
                return []
            merged: dict[str, dict[str, object]] = {}
            for r in rows:
                key = str(r.get("scraper_name") or "unknown")
                curr = merged.get(
                    key,
                    {
                        "scraper_name": key,
                        "last_success": False,
                        "nb_products": 0,
                        "last_run": "",
                        "error_message": "",
                    },
                )
                curr["nb_products"] = int(curr.get("nb_products") or 0) + int(r.get("nb_products") or 0)
                curr["last_success"] = bool(curr.get("last_success")) or bool(r.get("last_success"))
                if not curr.get("last_run") and r.get("last_run"):
                    curr["last_run"] = r.get("last_run")
                if r.get("error_message"):
                    curr["error_message"] = r.get("error_message")
                merged[key] = curr
            out = list(merged.values())
            out.sort(key=lambda r: str(r.get("scraper_name", "")).lower())
            _set_scraper_health_cache(out)
            return out
        except Exception as exc:
            logger.exception("analytics tracker: fallback csv read impossible")
            fallback = [
                {
                    "scraper_name": "market_fr_sources",
                    "last_success": False,
                    "nb_products": 0,
                    "last_run": "",
                    "error_message": f"csv_fallback_error: {exc}",
                }
            ]
            _set_scraper_health_cache(fallback)
            return fallback

    try:
        df = pd.read_csv(buf if buf is not None else MARKET_FR_SOURCES_PATH)  # type: ignore[union-attr]
    except Exception as exc:
        # Ne jamais casser la page analytics pour une erreur de lecture CSV.
        logger.exception("analytics tracker: lecture CSV impossible")
        fallback = [
            {
                "scraper_name": "market_fr_sources",
                "last_success": False,
                "nb_products": 0,
                "last_run": "",
                "error_message": f"csv_read_error: {exc}",
            }
        ]
        _set_scraper_health_cache(fallback)
        return fallback

    try:
        if df.empty:
            _set_scraper_health_cache([])
            return []

        # Noms de colonnes tolérants selon historiques de pipeline.
        scraper_col = None
        for candidate in ("source", "scraper_name", "scraper", "shop", "site"):
            if candidate in df.columns:
                scraper_col = candidate
                break
        if scraper_col is None:
            scraper_col = df.columns[0]

        model_col = None
        for candidate in ("model", "product", "sneaker", "name"):
            if candidate in df.columns:
                model_col = candidate
                break

        run_col = None
        for candidate in ("updated_at", "last_run", "timestamp", "scraped_at"):
            if candidate in df.columns:
                run_col = candidate
                break
        error_col = None
        for candidate in ("error_message", "error", "last_error"):
            if candidate in df.columns:
                error_col = candidate
                break
        success_col = None
        for candidate in ("last_success", "success", "ok", "status"):
            if candidate in df.columns:
                success_col = candidate
                break

        # Normalisation minimale pour éviter les NaN/vides.
        df = df.copy()
        df[scraper_col] = (
            df[scraper_col]
            .astype(str)
            .str.strip()
            .replace({"": "unknown", "nan": "unknown", "None": "unknown"})
        )

        rows: list[dict[str, object]] = []
        grouped = df.groupby(scraper_col, dropna=False)
        for scraper_name, grp in grouped:
            try:
                nb_products = int(grp[model_col].dropna().nunique()) if model_col else int(len(grp))
            except Exception:
                nb_products = int(len(grp))

            last_run = ""
            if run_col:
                try:
                    parsed = pd.to_datetime(grp[run_col], errors="coerce")
                    if parsed.notna().any():
                        last_run = parsed.max().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        last_run = str(grp[run_col].dropna().astype(str).max() or "")
                except Exception:
                    last_run = ""

            # Privilégie un indicateur explicite si présent dans le CSV,
            # sinon fallback simple: succès si la source a produit au moins 1 ligne.
            last_success = bool(len(grp) > 0)
            if success_col:
                try:
                    vals = [str(v).strip().lower() for v in grp[success_col].dropna().tolist()]
                    if vals:
                        ok_tokens = {"1", "true", "yes", "ok", "success"}
                        ko_tokens = {"0", "false", "no", "ko", "error", "failed", "fail"}
                        ok_count = sum(1 for v in vals if v in ok_tokens)
                        ko_count = sum(1 for v in vals if v in ko_tokens)
                        if ok_count or ko_count:
                            last_success = ok_count >= ko_count
                except Exception:
                    last_success = bool(len(grp) > 0)

            error_message = ""
            if error_col:
                try:
                    errors = [str(v).strip() for v in grp[error_col].dropna().tolist()]
                    errors = [e for e in errors if e and e.lower() not in {"nan", "none"}]
                    if errors:
                        error_message = errors[-1]
                except Exception:
                    error_message = ""

            rows.append(
                {
                    "scraper_name": str(scraper_name),
                    "last_success": last_success,
                    "nb_products": nb_products,
                    "last_run": last_run,
                    "error_message": error_message,
                }
            )

        rows.sort(key=lambda r: str(r.get("scraper_name", "")).lower())
        _set_scraper_health_cache(rows)
        return rows
    except Exception as exc:
        # Dernier filet de sécurité: la route analytics reste servie.
        logger.exception("analytics tracker: construction health rows impossible")
        fallback = [
            {
                "scraper_name": "market_fr_sources",
                "last_success": False,
                "nb_products": 0,
                "last_run": "",
                "error_message": f"health_build_error: {exc}",
            }
        ]
        _set_scraper_health_cache(fallback)
        return fallback

