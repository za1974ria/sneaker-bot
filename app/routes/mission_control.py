"""
SneakerBot Mission Control — module de supervision premium.

Routes :
  GET  /mission-control              → dashboard HTML (admin only)
  GET  /api/mission/status           → statut global synthétique
  GET  /api/mission/services         → état de chaque service
  GET  /api/mission/data             → fraîcheur et qualité des CSV
  GET  /api/mission/keys             → état des clés API (sans exposer les valeurs)
  GET  /api/mission/sources          → analyse du fichier sources
  GET  /api/mission/alerts           → centre d'alertes JSON
  GET  /api/mission/diagnostic       → diagnostic complet + recommandations

Sécurité : toutes les routes admin redirigent vers /login si non authentifié.
Les vraies clés API ne sont JAMAIS retournées dans les réponses.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import platform
import re
import subprocess
import time
import urllib.request as _urllib_req
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mission-control"])

# Résolution des chemins depuis ce fichier → app/ → sneaker_bot/
_APP_DIR   = Path(__file__).resolve().parent.parent          # sneaker_bot/app/
_ROOT_DIR  = _APP_DIR.parent                                 # sneaker_bot/
_DATA_DIR  = _ROOT_DIR / "data"
_TEMPLATES_DIR = _ROOT_DIR / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ── Seuils de fraîcheur (minutes) ─────────────────────────────────────────────
# Refresh 2×/jour (08:00 / 20:00) → un CSV de 7–12h est normal.
FRESH_GREEN_MIN  = 720   # < 12h → green
FRESH_ORANGE_MIN = 1080  # 12–18h → orange (warning visible, non-critique)
# > 18h → red (critique)

# ── Seuils SerpAPI (% utilisé) ────────────────────────────────────────────────
SERP_GREEN_PCT  = 70
SERP_ORANGE_PCT = 85
# > 85 → red


# ══════════════════════════════════════════════════════════════════════════════
# Helpers internes — tous isolés, aucun crash si ressource absente
# ══════════════════════════════════════════════════════════════════════════════

def _now_ts() -> float:
    return time.time()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_info(path: Path, stale_orange_min: int = FRESH_ORANGE_MIN) -> dict[str, Any]:
    """Retourne l'état d'un fichier CSV : existence, âge, lignes, statut couleur."""
    if not path.exists():
        return {
            "exists": False,
            "status": "red",
            "message": "Fichier absent",
            "path": str(path),
        }
    try:
        mtime = path.stat().st_mtime
        age_min = round((_now_ts() - mtime) / 60, 1)
        updated_at = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        rows = sum(1 for _ in open(path, encoding="utf-8", errors="replace")) - 1  # hors header
        rows = max(0, rows)

        if age_min < FRESH_GREEN_MIN:
            status, color = "green", "green"
        elif age_min < FRESH_ORANGE_MIN:
            status, color = "orange", "orange"
        else:
            status, color = "red", "red"

        return {
            "exists": True,
            "status": status,
            "color": color,
            "path": str(path),
            "updated_at": updated_at,
            "age_minutes": age_min,
            "rows": rows,
            "size_kb": round(path.stat().st_size / 1024, 1),
        }
    except Exception as exc:
        logger.debug("_file_info %s: %s", path, exc)
        return {"exists": True, "status": "orange", "message": str(exc), "path": str(path)}


def _serpapi_status() -> dict[str, Any]:
    """Retourne l'état SerpAPI depuis serpapi_budget.py (sans clé en clair)."""
    try:
        from app.serpapi_budget import get_budget_status
        budget = get_budget_status()
        used_pct = float(budget.get("serpapi_used_pct") or 0)
        mode = str(budget.get("serpapi_budget_status") or "unknown")
        hard_stop = bool(budget.get("serpapi_hard_stop"))

        if hard_stop or used_pct >= SERP_ORANGE_PCT:
            color = "red"
        elif used_pct >= SERP_GREEN_PCT:
            color = "orange"
        else:
            color = "green"

        key_present = bool(os.environ.get("SERPAPI_KEY") or os.environ.get("SERP_API_KEY"))
        return {
            "key_present": key_present,
            "key_display": "****présente****" if key_present else "absente",
            "calls_this_month": budget.get("serpapi_calls_this_month"),
            "monthly_cap": budget.get("serpapi_monthly_cap"),
            "used_pct": used_pct,
            "remaining": budget.get("serpapi_budget_remaining"),
            "remaining_pct": budget.get("serpapi_budget_remaining_pct"),
            "daily_average": budget.get("serpapi_daily_average"),
            "mode": mode,
            "hard_stop": hard_stop,
            "color": color,
            "month": budget.get("month"),
        }
    except Exception as exc:
        logger.debug("_serpapi_status: %s", exc)
        return {
            "key_present": bool(os.environ.get("SERPAPI_KEY") or os.environ.get("SERP_API_KEY")),
            "key_display": "unknown",
            "color": "orange",
            "mode": "unknown",
            "error": str(exc),
        }


def _scheduler_status() -> dict[str, Any]:
    """Retourne l'état du scheduler APScheduler."""
    try:
        import importlib
        try:
            sched = importlib.import_module("app.scheduler")
        except Exception:
            sched = importlib.import_module("scheduler")
        is_running = getattr(sched, "scheduler_is_running", None)
        apscheduler_running = bool(callable(is_running) and is_running())
        get_status = getattr(sched, "get_last_refresh_status", None)
        last_refresh: dict[str, Any] = {}
        if callable(get_status):
            last_refresh = dict(get_status() or {})
        # Effective: APScheduler running OR Celery pipeline completed a successful refresh
        has_recent_success = bool(last_refresh.get("last_success"))
        running = apscheduler_running or has_recent_success
        return {
            "running": running,
            "apscheduler_running": apscheduler_running,  # raw state for diagnostics
            "color": "green" if running else "red",
            "last_refresh": last_refresh,
        }
    except Exception as exc:
        logger.debug("_scheduler_status: %s", exc)
        return {"running": False, "color": "orange", "error": str(exc)}


def _celery_status() -> dict[str, Any]:
    """Vérifie si Celery est configuré et accessible."""
    try:
        from celery_app import app as celery_app
        # ping via inspect avec timeout court
        inspect = celery_app.control.inspect(timeout=2.0)
        ping_result = inspect.ping()
        if ping_result:
            workers = list(ping_result.keys())
            return {"available": True, "color": "green", "workers": workers, "count": len(workers)}
        return {"available": False, "color": "orange", "workers": [], "count": 0, "message": "Aucun worker actif"}
    except Exception as exc:
        return {"available": False, "color": "orange", "error": str(exc), "workers": [], "count": 0}


def _playwright_status() -> dict[str, Any]:
    """Détecte si Playwright est installé et opérationnel."""
    try:
        import glob as _glob
        import playwright  # noqa: F401
        # Vérifie la présence du binaire chromium dans le cache ms-playwright
        # (évite un subprocess qui utiliserait le python système hors venv)
        patterns = [
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        ]
        browser_found = any(_glob.glob(p) for p in patterns)
        if browser_found:
            return {"installed": True, "color": "green", "note": "playwright opérationnel"}
        return {"installed": True, "color": "orange", "note": "playwright installé — navigateur chromium manquant"}
    except ImportError:
        return {"installed": False, "color": "orange", "note": "playwright non installé"}
    except Exception as exc:
        return {"installed": False, "color": "orange", "note": str(exc)}


def _logs_error_status() -> dict[str, Any]:
    """Analyse les logs récents pour détecter les erreurs critiques."""
    results: dict[str, Any] = {"error_count": 0, "warning_count": 0, "color": "green", "recent_errors": []}
    log_paths = [
        _ROOT_DIR / "logs" / "sneakerbot.log",
        _ROOT_DIR / "logs" / "app.log",
        Path("/var/log/sneaker_bot.log"),
    ]
    for lp in log_paths:
        if lp.exists():
            try:
                # Lire les 500 dernières lignes
                lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
                errors = [l for l in lines if " ERROR " in l or "CRITICAL" in l]
                warnings = [l for l in lines if " WARNING " in l]
                recent = errors[-5:] if errors else []
                results["error_count"] = len(errors)
                results["warning_count"] = len(warnings)
                results["recent_errors"] = recent
                results["log_file"] = str(lp)
                if len(errors) > 10:
                    results["color"] = "red"
                elif len(errors) > 0 or len(warnings) > 20:
                    results["color"] = "orange"
                return results
            except Exception:
                pass
    # Aucun log trouvé — journald fallback
    try:
        result = subprocess.run(
            ["journalctl", "-u", "sneaker_bot", "-n", "200", "--no-pager", "-q"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.splitlines()
        errors = [l for l in lines if "ERROR" in l or "CRITICAL" in l]
        warnings = [l for l in lines if "WARNING" in l]
        results["error_count"] = len(errors)
        results["warning_count"] = len(warnings)
        results["recent_errors"] = errors[-5:]
        results["log_source"] = "journald"
        if len(errors) > 5:
            results["color"] = "red"
        elif len(errors) > 0:
            results["color"] = "orange"
    except Exception as exc:
        results["color"] = "orange"
        results["error"] = str(exc)
    return results


def _sources_analysis() -> dict[str, Any]:
    """Analyse market_fr_sources.csv pour détecter anomalies."""
    path = _DATA_DIR / "market_fr_sources.csv"
    if not path.exists():
        return {"exists": False, "color": "red", "message": "market_fr_sources.csv absent"}
    try:
        rows: list[dict] = []
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

        if not rows:
            return {"exists": True, "color": "orange", "total_sources": 0, "message": "Fichier vide"}

        # Colonnes détectées (flexible selon le schéma)
        cols = list(rows[0].keys()) if rows else []

        # Prix invalides (tentative sur colonnes communes)
        price_cols = [c for c in cols if "price" in c.lower() or "prix" in c.lower()]
        invalid_prices = 0
        suspect_sources = 0
        shops: set[str] = set()
        shop_cols = [c for c in cols if "shop" in c.lower() or "boutique" in c.lower() or "source" in c.lower() or "site" in c.lower()]

        for row in rows:
            for pc in price_cols:
                val = str(row.get(pc) or "").strip()
                if val:
                    try:
                        fval = float(val.replace(",", ".").replace("€", "").strip())
                        if fval <= 0 or fval > 10000:
                            invalid_prices += 1
                    except ValueError:
                        invalid_prices += 1
            for sc in shop_cols:
                v = str(row.get(sc) or "").strip()
                if v:
                    shops.add(v.lower())
            # Source suspecte si prix très bas ou champ url manquant
            url_cols = [c for c in cols if "url" in c.lower()]
            if url_cols and not any(str(row.get(c) or "").strip() for c in url_cols):
                suspect_sources += 1

        total = len(rows)
        color = "green"
        if invalid_prices / max(total, 1) > 0.1:
            color = "red"
        elif invalid_prices > 0 or suspect_sources > 5:
            color = "orange"

        return {
            "exists": True,
            "color": color,
            "total_sources": total,
            "distinct_shops": len(shops),
            "valid_sources": total - suspect_sources - invalid_prices,
            "suspect_sources": suspect_sources,
            "invalid_prices": invalid_prices,
            "columns_detected": cols,
            "price_columns": price_cols,
        }
    except Exception as exc:
        logger.debug("_sources_analysis: %s", exc)
        return {"exists": True, "color": "orange", "error": str(exc)}


def _build_alerts(services: dict, data: dict, keys: dict) -> list[dict[str, Any]]:
    """Génère un centre d'alertes structuré depuis l'état des composants."""
    alerts: list[dict[str, Any]] = []
    ts = _utcnow_iso()

    def _add(atype: str, source: str, message: str, suggestion: str = "") -> None:
        alerts.append({
            "type": atype,
            "source": source,
            "message": message,
            "suggestion": suggestion,
            "timestamp": ts,
        })

    # Services — seuls les services primaires remontent des alertes warning/critical.
    # Les services optionnels (playwright, celery) n'alertent qu'en cas d'erreur rouge.
    PRIMARY_SERVICES = {"fastapi_backend", "scheduler", "pricing_engine", "source_aggregator", "logs"}
    OPTIONAL_SERVICES = {"playwright_scraper", "celery_worker"}

    for svc_name, svc_data in services.items():
        color = svc_data.get("color", "green")
        if color == "red":
            if svc_name in OPTIONAL_SERVICES:
                _add("warning", svc_name, f"Service optionnel {svc_name} indisponible", "Non bloquant — vérifier si nécessaire")
            else:
                _add("critical", svc_name, f"Service {svc_name} en état critique", "Vérifier les logs systemd")
        elif color == "orange" and svc_name in PRIMARY_SERVICES:
            _add("warning", svc_name, f"Service {svc_name} dégradé", "Surveiller les prochaines minutes")

    # Data freshness
    for csv_name, csv_data in data.get("files", {}).items():
        color = csv_data.get("color") or csv_data.get("status", "green")
        if color == "red":
            if not csv_data.get("exists"):
                _add("critical", "data", f"{csv_name} absent — données manquantes", "Lancer un refresh FR immédiat")
            else:
                age = csv_data.get("age_minutes", 0)
                _add("critical", "data", f"{csv_name} périmé ({age} min)", "Déclencher refresh via /api/refresh/fr")
        elif color == "orange":
            age = csv_data.get("age_minutes", 0)
            _add("warning", "data", f"{csv_name} vieillissant ({age} min)", "Prochain refresh prévu automatiquement")

    # SerpAPI
    serp = keys.get("serpapi", {})
    if serp.get("color") == "red":
        _add("critical", "serpapi", f"SerpAPI {serp.get('mode','?')} — quota à {serp.get('used_pct','?')}%",
             "Réduire les appels ou augmenter le cap mensuel")
    elif serp.get("color") == "orange":
        _add("warning", "serpapi", f"SerpAPI prudent — {serp.get('used_pct','?')}% utilisé",
             "Surveiller la consommation journalière")

    if not alerts:
        _add("info", "system", "Tous les systèmes opérationnels", "Rien à signaler")

    return alerts


def _compute_global_status(services: dict, data: dict, keys: dict) -> str:
    """
    Retourne healthy / degraded / critical selon des règles strictes.

    Services CRITIQUES (affectent le statut global) :
      - fastapi_backend, scheduler, celery_worker, pricing_engine, source_aggregator

    Services NON-CRITIQUES (n'affectent pas le statut global) :
      - playwright_scraper, logs, diagnostics optionnels, SerpAPI budget

    CSV freshness :
      - orange (12–18h) : warning visible dans l'UI uniquement, n'impacte pas le statut global
      - red (>18h ou absent) : critique, remonte en CRITICAL

    Règles :
      CRITICAL : service critique rouge  OU  csv absent/périmé (>18h, rouge)
      DEGRADED : service critique orange
      HEALTHY  : tout le reste (warnings CSV/playwright/logs visibles mais non bloquants)
    """
    # ── Services critiques ────────────────────────────────────────────────────
    CRITICAL_SERVICES = {
        "fastapi_backend", "scheduler", "celery_worker",
        "pricing_engine", "source_aggregator",
    }

    critical_colors: list[str] = [
        info.get("color", "green")
        for key, info in services.items()
        if key in CRITICAL_SERVICES
    ]

    # ── CSV : seuls les rouges (absent ou >18h) sont critiques ───────────────
    # Les oranges (12–18h) restent visibles dans les cards mais n'impactent pas le global.
    csv_red: list[str] = [
        "red"
        for f in data.get("files", {}).values()
        if (f.get("color") or f.get("status") or "green") == "red"
    ]

    all_critical = critical_colors + csv_red

    if "red" in all_critical:
        return "critical"
    if "orange" in all_critical:
        return "degraded"
    return "healthy"


# ══════════════════════════════════════════════════════════════════════════════
# Builders des blocs principaux
# ══════════════════════════════════════════════════════════════════════════════

def _build_services() -> dict[str, Any]:
    """Construit le bloc services complet."""
    market_fr    = _file_info(_DATA_DIR / "market_fr.csv")
    market_src   = _file_info(_DATA_DIR / "market_fr_sources.csv")

    # FastAPI backend : si on répond c'est qu'il est up
    backend = {"running": True, "color": "green", "note": "Processus actif (répond aux requêtes)"}

    # Pricing engine : heuristique sur les données
    pricing_ok = market_fr.get("exists") and (market_fr.get("rows") or 0) > 0
    pricing = {
        "color": "green" if pricing_ok else "red",
        "rows_available": market_fr.get("rows", 0),
        "note": "Données disponibles" if pricing_ok else "Aucune donnée produit",
    }

    # Source aggregator
    src_ok = market_src.get("exists") and (market_src.get("rows") or 0) > 0
    source_agg = {
        "color": "green" if src_ok else "orange",
        "sources_count": market_src.get("rows", 0),
        "note": "Sources agrégées" if src_ok else "Sources absentes",
    }

    scheduler  = _scheduler_status()
    celery     = _celery_status()
    playwright = _playwright_status()
    logs       = _logs_error_status()

    return {
        "fastapi_backend": backend,
        "scheduler": scheduler,
        "pricing_engine": pricing,
        "source_aggregator": source_agg,
        "celery_worker": celery,
        "playwright_scraper": playwright,
        "logs": logs,
    }


def _build_data() -> dict[str, Any]:
    """Construit le bloc fraîcheur des fichiers CSV."""
    market_fr    = _file_info(_DATA_DIR / "market_fr.csv")
    market_src   = _file_info(_DATA_DIR / "market_fr_sources.csv")
    return {
        "files": {
            "market_fr_csv": market_fr,
            "market_fr_sources_csv": market_src,
        },
        "checked_at": _utcnow_iso(),
    }


def _build_keys() -> dict[str, Any]:
    """Construit le bloc clés API — sans jamais exposer les valeurs réelles."""
    other_keys: dict[str, Any] = {}
    for env_var in ("OPENAI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "RAPIDAPI_KEY"):
        val = os.environ.get(env_var)
        other_keys[env_var] = {
            "present": bool(val),
            "display": "****présente****" if val else "absente",
            "color": "green" if val else "orange",
        }
    return {
        "serpapi": _serpapi_status(),
        "other": other_keys,
        "warning": "Les valeurs réelles ne sont jamais exposées par cette API.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Routes FastAPI
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/mission-control", response_class=HTMLResponse, response_model=None)
def mission_control_page(request: Request):
    """Page admin Mission Control — redirige vers /login si non authentifié."""
    # Import local pour éviter la circularité avec app.py
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return RedirectResponse("/login?next=/mission-control", status_code=303)
    return templates.TemplateResponse("mission_control.html", {"request": request})


@router.get("/api/mission/status")
def api_mission_status(request: Request) -> JSONResponse:
    """Statut global synthétique : healthy / degraded / critical."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    services = _build_services()
    data     = _build_data()
    keys     = _build_keys()
    status   = _compute_global_status(services, data, keys)

    color_map = {"healthy": "green", "degraded": "orange", "critical": "red"}
    return JSONResponse({
        "status": status,
        "color": color_map.get(status, "orange"),
        "checked_at": _utcnow_iso(),
        "services_summary": {k: v.get("color", "green") for k, v in services.items()},
        "data_summary": {k: v.get("color") or v.get("status") for k, v in data["files"].items()},
        "serpapi_mode": (keys.get("serpapi") or {}).get("mode", "unknown"),
    })


@router.get("/api/mission/services")
def api_mission_services(request: Request) -> JSONResponse:
    """État détaillé de chaque service backend."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_build_services())


@router.get("/api/mission/data")
def api_mission_data(request: Request) -> JSONResponse:
    """Fraîcheur et qualité des fichiers CSV."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_build_data())


@router.get("/api/mission/keys")
def api_mission_keys(request: Request) -> JSONResponse:
    """État des clés API — sans exposer les valeurs réelles."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_build_keys())


@router.get("/api/mission/sources")
def api_mission_sources(request: Request) -> JSONResponse:
    """Analyse détaillée du fichier sources (boutiques, anomalies)."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_sources_analysis())


@router.get("/api/mission/alerts")
def api_mission_alerts(request: Request) -> JSONResponse:
    """Centre d'alertes structuré (info / warning / critical)."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    services = _build_services()
    data     = _build_data()
    keys     = _build_keys()
    alerts   = _build_alerts(services, data, keys)
    criticals = [a for a in alerts if a["type"] == "critical"]
    warnings  = [a for a in alerts if a["type"] == "warning"]
    infos     = [a for a in alerts if a["type"] == "info"]
    return JSONResponse({
        "total": len(alerts),
        "critical_count": len(criticals),
        "warning_count": len(warnings),
        "info_count": len(infos),
        "alerts": alerts,
        "generated_at": _utcnow_iso(),
    })


@router.get("/api/mission/diagnostic")
def api_mission_diagnostic(request: Request) -> JSONResponse:
    """Diagnostic complet avec recommandations — déclenché par le bouton Run Diagnostic."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    t_start = _now_ts()
    services = _build_services()
    data     = _build_data()
    keys     = _build_keys()
    sources  = _sources_analysis()
    alerts   = _build_alerts(services, data, keys)
    status   = _compute_global_status(services, data, keys)

    recommendations: list[str] = []

    # Backend
    backend_ok = services.get("fastapi_backend", {}).get("running", False)
    if not backend_ok:
        recommendations.append("CRITIQUE : FastAPI backend non disponible — vérifier le service systemd")

    # Scheduler
    if not services.get("scheduler", {}).get("running"):
        recommendations.append("Scheduler APScheduler arrêté — les refreshs auto ne fonctionnent plus")

    # CSV freshness
    for csv_name, csv_info in data.get("files", {}).items():
        c = csv_info.get("color") or csv_info.get("status")
        if not csv_info.get("exists"):
            recommendations.append(f"Fichier {csv_name} absent — déclencher POST /api/refresh/fr")
        elif c == "red":
            age = csv_info.get("age_minutes", 0)
            recommendations.append(f"{csv_name} périmé ({age:.0f} min) — refresh urgent recommandé")
        elif c == "orange":
            recommendations.append(f"{csv_name} vieillissant — surveiller la prochaine fenêtre de refresh")

    # SerpAPI
    serp = keys.get("serpapi", {})
    if serp.get("color") == "red":
        recommendations.append(f"SerpAPI en hard stop ({serp.get('used_pct','?')}%) — aucun appel possible ce mois")
    elif serp.get("color") == "orange":
        recommendations.append(f"SerpAPI à {serp.get('used_pct','?')}% — réduire la fréquence des scrapers")
    if not serp.get("key_present"):
        recommendations.append("Clé SerpAPI absente — variable SERPAPI_KEY non définie")

    # Sources
    if sources.get("invalid_prices", 0) > 0:
        recommendations.append(f"Sources : {sources['invalid_prices']} prix invalides détectés — audit conseillé")
    if sources.get("suspect_sources", 0) > 10:
        recommendations.append(f"Sources : {sources['suspect_sources']} entrées suspectes (URLs manquantes)")

    # Logs
    logs_data = services.get("logs", {})
    if logs_data.get("error_count", 0) > 10:
        recommendations.append(f"Logs : {logs_data['error_count']} erreurs récentes — consulter journalctl -u sneaker_bot")

    # Celery
    if not services.get("celery_worker", {}).get("available"):
        recommendations.append("Celery : aucun worker actif — les tâches async peuvent être bloquées")

    if not recommendations:
        recommendations.append("Tous les systèmes sont opérationnels. Aucune action requise.")

    elapsed_ms = round((_now_ts() - t_start) * 1000)

    return JSONResponse({
        "status": status,
        "checked_at": _utcnow_iso(),
        "elapsed_ms": elapsed_ms,
        "backend": {
            "ok": backend_ok,
            "color": "green" if backend_ok else "red",
        },
        "files": data.get("files", {}),
        "sources": sources,
        "pricing": {
            "color": services.get("pricing_engine", {}).get("color", "orange"),
            "rows": services.get("pricing_engine", {}).get("rows_available", 0),
        },
        "keys": {
            "serpapi": {
                "present": serp.get("key_present"),
                "mode": serp.get("mode"),
                "color": serp.get("color"),
                "used_pct": serp.get("used_pct"),
            }
        },
        "scheduler": {
            "running": services.get("scheduler", {}).get("running"),
            "color": services.get("scheduler", {}).get("color"),
        },
        "celery": {
            "available": services.get("celery_worker", {}).get("available"),
            "color": services.get("celery_worker", {}).get("color"),
        },
        "logs": {
            "error_count": logs_data.get("error_count", 0),
            "warning_count": logs_data.get("warning_count", 0),
            "color": logs_data.get("color", "green"),
        },
        "alerts_summary": {
            "total": len(alerts),
            "critical": len([a for a in alerts if a["type"] == "critical"]),
            "warning": len([a for a in alerts if a["type"] == "warning"]),
        },
        "recommendations": recommendations,
    })


# ══════════════════════════════════════════════════════════════════════════════
# V2 — Helpers additionnels (system, timeline, source-health, logs, watchdog,
#       metrics, telegram, actions)
# Aucune modification du code V1 ci-dessus.
# ══════════════════════════════════════════════════════════════════════════════

# ── Masquage secrets ──────────────────────────────────────────────────────────

_SECRET_RE: list[re.Pattern] = [
    re.compile(r'(?i)(api[_\-]?key|token|secret|password|passwd|authorization|auth)\s*[=:]\s*\S{4,}'),
    re.compile(r'Bearer\s+[A-Za-z0-9._\-]{8,}', re.I),
    re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'),
]

def _mask_secrets(text: str) -> str:
    """Masque clés API, tokens et emails dans un texte de log."""
    for pat in _SECRET_RE:
        text = pat.sub('[REDACTED]', text)
    return text


# ── System Resources ──────────────────────────────────────────────────────────

def _system_resources() -> dict[str, Any]:
    """CPU, RAM, Disk, Load, Uptime via psutil. Fallback os/shutil si absent."""
    def _col(pct: float, warn: float = 75, crit: float = 90) -> str:
        return "red" if pct >= crit else "orange" if pct >= warn else "green"

    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=0.2)
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        ups  = int(_now_ts() - psutil.boot_time())
        d, r = divmod(ups, 86400); h, r = divmod(r, 3600)
        uptime_str = (f"{d}j " if d else "") + f"{h}h {r // 60}m"
        try:
            l1, l5, l15 = os.getloadavg()
        except Exception:
            l1 = l5 = l15 = 0.0
        return {
            "cpu_pct":      round(cpu, 1),
            "cpu_color":    _col(cpu, 70, 90),
            "ram_pct":      round(mem.percent, 1),
            "ram_used_gb":  round(mem.used  / 1e9, 2),
            "ram_total_gb": round(mem.total / 1e9, 2),
            "ram_color":    _col(mem.percent),
            "disk_pct":     round(disk.percent, 1),
            "disk_used_gb": round(disk.used  / 1e9, 1),
            "disk_total_gb":round(disk.total / 1e9, 1),
            "disk_color":   _col(disk.percent),
            "load_1m":      round(l1, 2),
            "load_5m":      round(l5, 2),
            "load_15m":     round(l15, 2),
            "uptime":       uptime_str,
            "uptime_seconds": ups,
            "source":       "psutil",
        }
    except ImportError:
        # Fallback minimal
        try:
            l1, l5, l15 = os.getloadavg()
        except Exception:
            l1 = l5 = l15 = 0.0
        try:
            import shutil
            tot, used, _ = shutil.disk_usage('/')
            dpct = round(used / tot * 100, 1)
        except Exception:
            dpct = 0.0
        return {
            "cpu_pct": None, "cpu_color": "orange",
            "ram_pct": None, "ram_color": "orange",
            "disk_pct": dpct, "disk_color": _col(dpct),
            "load_1m": round(l1, 2), "load_5m": round(l5, 2), "load_15m": round(l15, 2),
            "uptime": "unknown", "source": "fallback",
        }
    except Exception as exc:
        return {"error": str(exc), "source": "error"}


# ── Refresh Timeline ──────────────────────────────────────────────────────────

def _refresh_timeline() -> list[dict[str, Any]]:
    """
    Timeline des derniers refresh FR, construite depuis :
      1. scraper.log (lignes market=FR + SERP SYNC)
      2. fr_update_status.json (dernier état connu)
    """
    events: list[dict[str, Any]] = []

    log_path = _ROOT_DIR / "logs" / "scraper.log"
    if log_path.exists():
        try:
            all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in reversed(all_lines):
                parts = [p.strip() for p in line.split("|")]
                ts_str = parts[0] if parts else ""
                msg    = parts[-1] if len(parts) > 1 else line

                evt: dict[str, Any] | None = None
                if "market=FR scraped/updated ok" in line:
                    evt = {"time": ts_str, "type": "full_refresh",
                           "status": "success", "message": "FR — scraped/updated OK", "color": "green"}
                elif "[SERP SYNC]" in line and "completed" in line:
                    evt = {"time": ts_str, "type": "serp_sync",
                           "status": "success", "message": msg[:80], "color": "green"}
                elif "market=FR" in line and ("error" in line.lower() or "fail" in line.lower()):
                    evt = {"time": ts_str, "type": "full_refresh",
                           "status": "failed", "message": msg[:80], "color": "red"}

                if evt:
                    events.append(evt)
                if len(events) >= 14:
                    break
        except Exception:
            pass

    # fr_update_status.json en tête (état le plus récent)
    status_path = _DATA_DIR / "fr_update_status.json"
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                color = "green" if data.get("last_success") else "red"
                events.insert(0, {
                    "time":    data.get("last_end_at") or data.get("updated_at") or "",
                    "type":    "status_file",
                    "status":  "success" if data.get("last_success") else "failed",
                    "message": f"Refresh : {data.get('last_message','?')} "
                               f"(start {data.get('last_start_at','?')})",
                    "color":   color,
                    "running": bool(data.get("running")),
                })
        except Exception:
            pass

    return events[:15]


# ── Source Health par Brand ───────────────────────────────────────────────────

def _source_health() -> dict[str, Any]:
    """
    Agrège market_fr_sources.csv par brand.
    Retourne état (green/orange/red), nb de lignes, shops, prix moyen.
    """
    path = _DATA_DIR / "market_fr_sources.csv"
    if not path.exists():
        return {"brands": {}, "total_brands": 0, "error": "market_fr_sources.csv absent"}
    try:
        brand_map: dict[str, dict] = {}
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                brand = str(row.get("brand") or "").strip()
                if not brand:
                    continue
                if brand not in brand_map:
                    brand_map[brand] = {"rows": 0, "shops": set(), "prices": []}
                brand_map[brand]["rows"] += 1
                shop = str(row.get("shop") or "").strip()
                if shop:
                    brand_map[brand]["shops"].add(shop)
                for pcol in ("price_avg", "price_min"):
                    v = str(row.get(pcol) or "").strip()
                    if v:
                        try:
                            brand_map[brand]["prices"].append(float(v))
                        except ValueError:
                            pass

        result: dict[str, Any] = {}
        for brand, st in sorted(brand_map.items()):
            rows = st["rows"]
            prices = st["prices"]
            color = "green" if rows >= 5 else "orange" if rows >= 1 else "red"
            result[brand] = {
                "rows":      rows,
                "shops":     len(st["shops"]),
                "avg_price": round(sum(prices) / len(prices), 0) if prices else None,
                "min_price": round(min(prices), 0) if prices else None,
                "color":     color,
            }
        return {"brands": result, "total_brands": len(result), "checked_at": _utcnow_iso()}
    except Exception as exc:
        logger.debug("_source_health: %s", exc)
        return {"brands": {}, "total_brands": 0, "error": str(exc)}


# ── Live Logs ─────────────────────────────────────────────────────────────────

def _read_live_logs(limit: int = 80) -> dict[str, Any]:
    """
    Lit les dernières lignes des fichiers de log.
    Masque systématiquement clés, tokens et emails.
    """
    sources = [
        ("scraper",  _ROOT_DIR / "logs" / "scraper.log"),
        ("watchdog", _ROOT_DIR / "watchdog.log"),
        ("celery",   _ROOT_DIR / "celery.log"),
    ]
    result: dict[str, Any] = {}
    for name, lp in sources:
        if not lp.exists():
            result[name] = {"available": False}
            continue
        try:
            lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
            result[name] = {
                "available":   True,
                "lines":       [_mask_secrets(l) for l in lines[-limit:]],
                "total_lines": len(lines),
                "mtime":       datetime.fromtimestamp(lp.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size_kb":     round(lp.stat().st_size / 1024, 1),
            }
        except Exception as exc:
            result[name] = {"available": False, "error": str(exc)}

    # Fallback journald si aucun fichier log présent
    if not any(v.get("available") for v in result.values()):
        try:
            proc = subprocess.run(
                ["journalctl", "-u", "sneaker_bot", "-n", str(limit), "--no-pager", "-q"],
                capture_output=True, text=True, timeout=5,
            )
            result["journald"] = {
                "available":   True,
                "lines":       [_mask_secrets(l) for l in proc.stdout.splitlines()],
                "total_lines": len(proc.stdout.splitlines()),
            }
        except Exception:
            pass

    return result


# ── Watchdog ──────────────────────────────────────────────────────────────────

def _watchdog_info() -> dict[str, Any]:
    """Analyse watchdog.log : activité récente, incidents, statut."""
    path = _ROOT_DIR / "watchdog.log"
    if not path.exists():
        return {"exists": False, "color": "orange", "status": "unknown",
                "message": "watchdog.log absent"}
    try:
        stat    = path.stat()
        age_min = round((_now_ts() - stat.st_mtime) / 60, 1)
        lines   = path.read_text(encoding="utf-8", errors="replace").splitlines()
        recent  = lines[-20:]

        # Positions des derniers événements dans les 20 lignes
        warn_pos = recovered_pos = ok_pos = -1
        for i, l in enumerate(recent):
            if "WARN" in l or "CRITICAL" in l:
                warn_pos = i
            if "RECOVERED" in l:
                recovered_pos = i
            if " OK " in l:
                ok_pos = i

        if age_min > 120:
            status, color = "stale", "orange"
        elif warn_pos > recovered_pos and warn_pos > ok_pos:
            status, color = "degraded", "orange"
        else:
            status, color = "active", "green"

        return {
            "exists":        True,
            "color":         color,
            "status":        status,
            "last_activity": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "age_minutes":   age_min,
            "total_lines":   len(lines),
            "recent_lines":  [_mask_secrets(l) for l in recent[-8:]],
        }
    except Exception as exc:
        return {"exists": True, "color": "orange", "status": "error", "error": str(exc)}


# ── API Response Metrics ──────────────────────────────────────────────────────

def _api_metrics() -> dict[str, Any]:
    """
    Mesure les temps de réponse :
    - /health (HTTP réel, sans auth)
    - data build interne (CSV read)
    - serpapi budget read (JSON I/O)
    """
    results: dict[str, Any] = {}

    # /health — appel HTTP réel
    t0 = _now_ts()
    try:
        req = _urllib_req.Request(
            "http://127.0.0.1:5003/health",
            headers={"User-Agent": "sneakerbot-metrics/1.0"},
        )
        with _urllib_req.urlopen(req, timeout=5) as resp:
            _ = resp.read()
            ms   = round((_now_ts() - t0) * 1000)
            code = resp.status
        color = "green" if ms < 300 else "orange" if ms < 1000 else "red"
        results["health"] = {"elapsed_ms": ms, "status_code": code, "color": color}
    except Exception as exc:
        results["health"] = {
            "elapsed_ms": round((_now_ts() - t0) * 1000),
            "status_code": 0, "color": "red", "error": str(exc)[:80],
        }

    # data build — CSV read interne
    t0 = _now_ts()
    try:
        _build_data()
        ms = round((_now_ts() - t0) * 1000)
        results["data_build"] = {
            "elapsed_ms": ms, "note": "CSV read + stat",
            "color": "green" if ms < 200 else "orange" if ms < 800 else "red",
        }
    except Exception as exc:
        results["data_build"] = {"elapsed_ms": 0, "color": "red", "error": str(exc)[:80]}

    # serpapi budget — JSON I/O
    t0 = _now_ts()
    try:
        _serpapi_status()
        ms = round((_now_ts() - t0) * 1000)
        results["serpapi_budget"] = {
            "elapsed_ms": ms, "note": "Budget JSON read",
            "color": "green" if ms < 100 else "orange",
        }
    except Exception as exc:
        results["serpapi_budget"] = {"elapsed_ms": 0, "color": "red", "error": str(exc)[:80]}

    return {"endpoints": results, "measured_at": _utcnow_iso()}


# ── Telegram ──────────────────────────────────────────────────────────────────

def _telegram_config() -> dict[str, Any]:
    """Vérifie la configuration Telegram sans exposer les tokens."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {
            "configured": False,
            "status":     "not_configured",
            "message":    "TELEGRAM_BOT_TOKEN et/ou TELEGRAM_CHAT_ID non définis",
        }
    return {
        "configured":      True,
        "status":          "ready",
        "token_present":   True,
        "chat_id_present": True,
    }


def _send_telegram(token: str, chat_id: str, text: str) -> dict[str, Any]:
    """Envoie un message Telegram. Retourne le résultat sans exposer le token."""
    import urllib.parse
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        req = _urllib_req.Request(url, data=data, method="POST")
        with _urllib_req.urlopen(req, timeout=10) as resp:
            r = json.loads(resp.read())
            return {
                "sent":       True,
                "ok":         r.get("ok"),
                "message_id": (r.get("result") or {}).get("message_id"),
            }
    except Exception as exc:
        return {"sent": False, "error": str(exc)[:200]}


# ══════════════════════════════════════════════════════════════════════════════
# V2 Routes (additives — aucune modification des routes V1)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/mission/system")
def api_mission_system(request: Request) -> JSONResponse:
    """Ressources système live : CPU, RAM, Disk, Load, Uptime."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_system_resources())


@router.get("/api/mission/refresh-timeline")
def api_mission_refresh_timeline(request: Request) -> JSONResponse:
    """Timeline des derniers refreshs FR (scraper.log + fr_update_status.json)."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse({"events": _refresh_timeline(), "generated_at": _utcnow_iso()})


@router.get("/api/mission/source-health")
def api_mission_source_health(request: Request) -> JSONResponse:
    """Santé des données par brand depuis market_fr_sources.csv."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_source_health())


@router.get("/api/mission/logs")
def api_mission_logs(request: Request, limit: int = 80) -> JSONResponse:
    """Dernières lignes des fichiers de log (secrets masqués)."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    limit = max(10, min(limit, 200))
    return JSONResponse(_read_live_logs(limit))


@router.get("/api/mission/watchdog")
def api_mission_watchdog(request: Request) -> JSONResponse:
    """État du watchdog (watchdog.log)."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_watchdog_info())


@router.get("/api/mission/metrics")
def api_mission_metrics(request: Request) -> JSONResponse:
    """Temps de réponse internes : /health, CSV build, SerpAPI budget."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_api_metrics())


@router.get("/api/mission/telegram-test")
def api_mission_telegram_test(request: Request) -> JSONResponse:
    """Vérifie la config Telegram et envoie un message de test si configuré."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    cfg = _telegram_config()
    if not cfg.get("configured"):
        return JSONResponse({**cfg, "test_sent": False})
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    result  = _send_telegram(
        token, chat_id,
        f"🟢 SneakerBot Mission Control — test alerte OK ({_utcnow_iso()})",
    )
    return JSONResponse({**cfg, "test_result": result})


@router.post("/api/mission/actions/scraper-test")
def api_mission_scraper_test(request: Request) -> JSONResponse:
    """
    Test léger du pipeline de scraping.
    Vérifie SerpAPI budget, fraîcheur CSV, scheduler.
    N'exécute aucun scrape réel.
    """
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    t0 = _now_ts()
    checks: dict[str, Any] = {}

    # 1. Budget SerpAPI
    try:
        from app.serpapi_budget import get_budget_status
        b = get_budget_status()
        checks["serpapi_budget"] = {
            "ok":      not bool(b.get("serpapi_hard_stop")),
            "mode":    b.get("serpapi_budget_status"),
            "used_pct": b.get("serpapi_used_pct"),
        }
    except Exception as exc:
        checks["serpapi_budget"] = {"ok": False, "error": str(exc)}

    # 2. Fraîcheur CSV
    fr = _file_info(_DATA_DIR / "market_fr.csv")
    checks["market_fr_csv"] = {
        "ok":      bool(fr.get("exists") and fr.get("color") != "red"),
        "age_min": fr.get("age_minutes"),
        "rows":    fr.get("rows"),
    }

    # 3. Scheduler
    sched = _scheduler_status()
    checks["scheduler"] = {"ok": bool(sched.get("running")), "color": sched.get("color")}

    all_ok = all(bool(v.get("ok")) for v in checks.values())
    return JSONResponse({
        "status":     "ok" if all_ok else "degraded",
        "checks":     checks,
        "elapsed_ms": round((_now_ts() - t0) * 1000),
        "note":       "Test léger — aucun scrape déclenché",
        "tested_at":  _utcnow_iso(),
    })


@router.post("/api/mission/actions/restart-scheduler")
def api_mission_restart_scheduler(request: Request) -> JSONResponse:
    """
    Redémarre le scheduler APScheduler si arrêté.
    Retourne 'manual_action_required' si impossible programmatiquement.
    """
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        import importlib
        try:
            sched = importlib.import_module("app.scheduler")
        except Exception:
            sched = importlib.import_module("scheduler")

        is_running = getattr(sched, "scheduler_is_running", None)
        if callable(is_running) and is_running():
            return JSONResponse({
                "status":  "already_running",
                "message": "Scheduler déjà actif — aucune action requise",
            })

        start_fn = getattr(sched, "start_scheduler", None)
        if not callable(start_fn):
            return JSONResponse({
                "status":   "manual_action_required",
                "message":  "start_scheduler non disponible",
                "fallback": "sudo systemctl restart sneaker_bot",
            })

        start_fn()
        return JSONResponse({
            "status":       "restarted",
            "message":      "Scheduler redémarré avec succès",
            "restarted_at": _utcnow_iso(),
        })
    except Exception as exc:
        return JSONResponse({
            "status":   "error",
            "message":  str(exc),
            "fallback": "sudo systemctl restart sneaker_bot",
        })


# ══════════════════════════════════════════════════════════════════════════════
# MODULE HEALTH — extended coverage (all 17 modules)
# ══════════════════════════════════════════════════════════════════════════════

def _trust_engine_status() -> dict[str, Any]:
    """Trust Engine: import + smoke call."""
    t0 = _now_ts()
    try:
        from app.trust_engine import build_trust_report
        sample = build_trust_report(
            brand="Nike", model="Test",
            sources=[{"shop": "test", "price_avg": 100.0}],
            market_meta={"confidence_score": 80},
        )
        ms = round((_now_ts() - t0) * 1000)
        ok = isinstance(sample, dict) and "trust_score" in sample
        return {
            "status": "HEALTHY" if ok else "DEGRADED",
            "color": "green" if ok else "orange",
            "latency_ms": ms,
            "last_activity": _utcnow_iso(),
            "error_count": 0,
            "note": f"trust_score={sample.get('trust_score')} (smoke OK)" if ok else "Réponse inattendue",
        }
    except Exception as exc:
        return {
            "status": "DOWN", "color": "red",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(),
            "error_count": 1,
            "note": str(exc)[:120],
        }


def _anomaly_detector_status() -> dict[str, Any]:
    """Anomaly Detector: vérifie module + données sources."""
    t0 = _now_ts()
    try:
        src_path = _DATA_DIR / "market_fr_sources.csv"
        if not src_path.exists():
            return {
                "status": "DEGRADED", "color": "orange",
                "latency_ms": 0, "last_activity": _utcnow_iso(),
                "error_count": 0,
                "note": "market_fr_sources.csv absent — détection impossible",
            }
        # Heuristique : compter les prix hors-IQR dans les données
        import csv as _csv
        prices: list[float] = []
        with open(src_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                try:
                    prices.append(float(row.get("price_eur") or row.get("price") or 0))
                except (ValueError, TypeError):
                    pass
        outliers = 0
        if len(prices) >= 4:
            sp = sorted(prices)
            q1 = sp[int(len(sp) * 0.25)]
            q3 = sp[int(len(sp) * 0.75)]
            iqr = q3 - q1
            lo, hi = q1 - 3 * iqr, q3 + 3 * iqr
            outliers = sum(1 for p in prices if p < lo or p > hi)
        ms = round((_now_ts() - t0) * 1000)
        color = "orange" if outliers > 20 else "green"
        return {
            "status": "DEGRADED" if outliers > 20 else "HEALTHY",
            "color": color,
            "latency_ms": ms,
            "last_activity": _utcnow_iso(),
            "error_count": outliers,
            "note": f"{outliers} prix anomaux détectés sur {len(prices)} entrées",
        }
    except Exception as exc:
        return {
            "status": "DOWN", "color": "red",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(),
            "error_count": 1,
            "note": str(exc)[:120],
        }


def _auth_status() -> dict[str, Any]:
    """Auth/access-control: lecture DB + intégrité."""
    t0 = _now_ts()
    try:
        from app.app import _load_access_control, AUTH_TOKEN
        ac = _load_access_control()
        users = ac.get("users") or []
        n_users = len(users) if isinstance(users, list) else 0
        n_admin = sum(1 for u in (users if isinstance(users, list) else [])
                      if isinstance(u, dict) and u.get("role") == "admin")
        token_ok = bool(AUTH_TOKEN and len(AUTH_TOKEN) >= 20)
        ms = round((_now_ts() - t0) * 1000)
        return {
            "status": "HEALTHY" if token_ok else "DEGRADED",
            "color": "green" if token_ok else "orange",
            "latency_ms": ms,
            "last_activity": _utcnow_iso(),
            "error_count": 0,
            "note": f"{n_users} utilisateurs ({n_admin} admins) · token {'OK' if token_ok else 'ABSENT'}",
        }
    except Exception as exc:
        return {
            "status": "DOWN", "color": "red",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(),
            "error_count": 1,
            "note": str(exc)[:120],
        }


def _subscriptions_db_status() -> dict[str, Any]:
    """Subscriptions DB: vérifie la table subscriptions dans sneakerbot.db."""
    t0 = _now_ts()
    db_path = _DATA_DIR / "sneakerbot.db"
    try:
        import sqlite3
        if not db_path.exists():
            return {
                "status": "DOWN", "color": "red",
                "latency_ms": 0, "last_activity": _utcnow_iso(),
                "error_count": 1, "note": "sneakerbot.db absent",
            }
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            subs = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] if "subscriptions" in tables else None
        ms = round((_now_ts() - t0) * 1000)
        note_parts = [f"tables: {', '.join(sorted(tables)[:6])}"]
        if subs is not None:
            note_parts.append(f"{subs} abonnements")
        return {
            "status": "HEALTHY", "color": "green",
            "latency_ms": ms, "last_activity": _utcnow_iso(),
            "error_count": 0, "note": " · ".join(note_parts),
        }
    except Exception as exc:
        return {
            "status": "DOWN", "color": "red",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(),
            "error_count": 1, "note": str(exc)[:120],
        }


def _visitor_analytics_status() -> dict[str, Any]:
    """Visitor analytics: vérifie le tracker + stats récentes."""
    t0 = _now_ts()
    try:
        from app.analytics.tracker import get_stats
        stats = get_stats() or {}
        ms = round((_now_ts() - t0) * 1000)
        total = stats.get("total_visits") or stats.get("visits") or 0
        return {
            "status": "HEALTHY", "color": "green",
            "latency_ms": ms, "last_activity": _utcnow_iso(),
            "error_count": 0,
            "note": f"{total} visites enregistrées",
        }
    except ImportError as exc:
        # Module analytics optionnel absent — non bloquant
        return {
            "status": "HEALTHY", "color": "orange",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(),
            "error_count": 0,
            "note": f"analytics non disponible: {str(exc)[:80]}",
        }
    except Exception as exc:
        return {
            "status": "DEGRADED", "color": "orange",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(),
            "error_count": 1,
            "note": str(exc)[:120],
        }


def _comparison_api_status() -> dict[str, Any]:
    """Comparison API: appel HTTP réel sur /api/v2/comparison/fr."""
    t0 = _now_ts()
    try:
        req = _urllib_req.Request(
            "http://127.0.0.1:5003/api/v2/comparison/fr?brand=Nike&model=Air+Force+1+Low",
            headers={"User-Agent": "sneakerbot-healthcheck/1.0"},
        )
        with _urllib_req.urlopen(req, timeout=6) as resp:
            body = json.loads(resp.read().decode())
            ms = round((_now_ts() - t0) * 1000)
            count = body.get("count", 0)
        color = "green" if ms < 800 else "orange" if ms < 2000 else "red"
        return {
            "status": "HEALTHY",  # HTTP 200 = API saine; count=0 est un état data, pas une panne
            "color": color,
            "latency_ms": ms, "last_activity": _utcnow_iso(),
            "error_count": 0,
            "note": f"{count} résultat(s) · {ms}ms",
        }
    except Exception as exc:
        return {
            "status": "DOWN", "color": "red",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(),
            "error_count": 1, "note": str(exc)[:120],
        }


def _nginx_status() -> dict[str, Any]:
    """Nginx: vérifie le processus + répond sur port 80."""
    t0 = _now_ts()
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "nginx"],
            capture_output=True, text=True, timeout=4,
        )
        active = result.stdout.strip() == "active"
        ms = round((_now_ts() - t0) * 1000)

        # Vérif config
        cfg_ok = True
        try:
            cfg = subprocess.run(
                ["nginx", "-t"],
                capture_output=True, text=True, timeout=5,
            )
            cfg_ok = cfg.returncode == 0
        except Exception:
            cfg_ok = None  # type: ignore[assignment]

        color = "green" if active else "red"
        note_parts = ["active" if active else "inactif"]
        if cfg_ok is True:
            note_parts.append("config OK")
        elif cfg_ok is False:
            note_parts.append("config ERROR")
            color = "orange"
        return {
            "status": "HEALTHY" if active else "DOWN",
            "color": color, "latency_ms": ms,
            "last_activity": _utcnow_iso(), "error_count": 0 if active else 1,
            "note": " · ".join(note_parts),
        }
    except Exception as exc:
        return {
            "status": "DOWN", "color": "red",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(), "error_count": 1,
            "note": str(exc)[:120],
        }


def _fastapi_backend_status() -> dict[str, Any]:
    """FastAPI backend: appel HTTP /health + uptime systemd."""
    t0 = _now_ts()
    try:
        req = _urllib_req.Request(
            "http://127.0.0.1:5003/health",
            headers={"User-Agent": "sneakerbot-healthcheck/1.0"},
        )
        with _urllib_req.urlopen(req, timeout=5) as resp:
            ms = round((_now_ts() - t0) * 1000)
            code = resp.status
        color = "green" if ms < 400 else "orange" if ms < 1500 else "red"
        # Uptime systemd
        uptime_note = ""
        try:
            r2 = subprocess.run(
                ["systemctl", "show", "sneaker_bot", "--property=ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=3,
            )
            ts_str = r2.stdout.strip().split("=", 1)[-1].strip()
            uptime_note = f" · up since {ts_str[:16]}" if ts_str else ""
        except Exception:
            pass
        return {
            "status": "HEALTHY", "color": color,
            "latency_ms": ms, "last_activity": _utcnow_iso(),
            "error_count": 0,
            "note": f"HTTP {code} · {ms}ms{uptime_note}",
        }
    except Exception as exc:
        return {
            "status": "DOWN", "color": "red",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(), "error_count": 1,
            "note": str(exc)[:120],
        }


def _serpapi_module_status() -> dict[str, Any]:
    """SerpAPI budget module: budget + circuit breaker."""
    t0 = _now_ts()
    try:
        data = _serpapi_status()
        ms = round((_now_ts() - t0) * 1000)
        pct = data.get("used_pct") or 0
        hard_stop = bool(data.get("hard_stop"))
        color = data.get("color", "green")
        return {
            "status": "DOWN" if hard_stop else ("DEGRADED" if pct > 85 else "HEALTHY"),
            "color": "red" if hard_stop else color,
            "latency_ms": ms, "last_activity": _utcnow_iso(),
            "error_count": 1 if hard_stop else 0,
            "note": f"{pct}% utilisé · {'HARD STOP' if hard_stop else data.get('mode','normal')}",
        }
    except Exception as exc:
        return {
            "status": "DOWN", "color": "red",
            "latency_ms": round((_now_ts() - t0) * 1000),
            "last_activity": _utcnow_iso(), "error_count": 1,
            "note": str(exc)[:120],
        }


def _csv_freshness_status() -> dict[str, Any]:
    """CSV freshness: fraîcheur des deux fichiers de données principaux."""
    t0 = _now_ts()
    fr = _file_info(_DATA_DIR / "market_fr.csv")
    src = _file_info(_DATA_DIR / "market_fr_sources.csv")
    ms = round((_now_ts() - t0) * 1000)
    colors = [fr.get("color", "red"), src.get("color", "red")]
    worst = "red" if "red" in colors else "orange" if "orange" in colors else "green"
    age_fr  = fr.get("age_minutes") or 0
    age_src = src.get("age_minutes") or 0
    return {
        "status": "HEALTHY" if worst == "green" else ("DEGRADED" if worst == "orange" else "DOWN"),
        "color": worst, "latency_ms": ms, "last_activity": _utcnow_iso(),
        "error_count": 0,
        "note": (
            f"market_fr.csv {round(age_fr/60,1)}h · "
            f"sources.csv {round(age_src/60,1)}h"
            f" · {fr.get('rows',0)} + {src.get('rows',0)} lignes"
        ),
    }


def _pricing_engine_status() -> dict[str, Any]:
    """Pricing engine: disponibilité + couverture modèles."""
    t0 = _now_ts()
    fr = _file_info(_DATA_DIR / "market_fr.csv")
    src = _file_info(_DATA_DIR / "market_fr_sources.csv")
    ms = round((_now_ts() - t0) * 1000)
    rows_fr = fr.get("rows") or 0
    rows_src = src.get("rows") or 0
    ok = rows_fr > 0 and rows_src > 0
    return {
        "status": "HEALTHY" if ok else "DEGRADED",
        "color": "green" if ok else "orange",
        "latency_ms": ms, "last_activity": _utcnow_iso(),
        "error_count": 0 if ok else 1,
        "note": f"{rows_fr} modèles · {rows_src} offres sources",
    }


def _source_aggregator_status() -> dict[str, Any]:
    """Source aggregator: nombre de shops distincts détectés."""
    t0 = _now_ts()
    src = _file_info(_DATA_DIR / "market_fr_sources.csv")
    ms = round((_now_ts() - t0) * 1000)
    rows = src.get("rows") or 0
    # Tenter de compter les shops distincts
    distinct_shops = 0
    try:
        import csv as _csv
        with open(_DATA_DIR / "market_fr_sources.csv", newline="", encoding="utf-8", errors="replace") as f:
            reader = _csv.DictReader(f)
            shops: set[str] = set()
            for row in reader:
                s = row.get("source") or row.get("shop") or ""
                if s:
                    shops.add(s.strip().lower())
            distinct_shops = len(shops)
    except Exception:
        pass
    ok = rows > 0
    return {
        "status": "HEALTHY" if ok else "DEGRADED",
        "color": "green" if ok else "orange",
        "latency_ms": ms, "last_activity": _utcnow_iso(),
        "error_count": 0 if ok else 1,
        "note": f"{rows} offres · {distinct_shops} boutiques distinctes",
    }


def _telegram_module_status() -> dict[str, Any]:
    """Telegram alerts: configuration présente (optionnel — absence non bloquante)."""
    cfg = _telegram_config()
    configured = bool(cfg.get("configured"))
    return {
        "status": "HEALTHY",  # Canal optionnel — non configuré n'est pas une panne
        "color": "green" if configured else "orange",
        "latency_ms": 0, "last_activity": _utcnow_iso(),
        "error_count": 0,
        "note": cfg.get("status", "unknown"),
    }


def _system_resources_status() -> dict[str, Any]:
    """System resources: CPU/RAM/Disk wrapped as module."""
    sys = _system_resources()
    cpu = sys.get("cpu_pct") or 0
    ram = sys.get("ram_pct") or 0
    disk = sys.get("disk_pct") or 0
    worst_color = (
        "red" if sys.get("cpu_color") == "red" or sys.get("ram_color") == "red" or sys.get("disk_color") == "red"
        else "orange" if sys.get("cpu_color") == "orange" or sys.get("ram_color") == "orange"
        else "green"
    )
    return {
        "status": "HEALTHY" if worst_color == "green" else ("DEGRADED" if worst_color == "orange" else "DOWN"),
        "color": worst_color, "latency_ms": 0, "last_activity": _utcnow_iso(),
        "error_count": 0,
        "note": f"CPU {cpu}% · RAM {ram}% · Disk {disk}% · up {sys.get('uptime','?')}",
    }


def _watchdog_module_status() -> dict[str, Any]:
    """Watchdog: état récent (optionnel — log absent non bloquant)."""
    w = _watchdog_info()
    color = w.get("color", "orange")
    # "unknown" = log absent = watchdog non configuré, pas une panne
    status_map = {"active": "HEALTHY", "stale": "DEGRADED", "degraded": "DEGRADED", "unknown": "HEALTHY", "error": "DOWN"}
    return {
        "status": status_map.get(w.get("status", "unknown"), "DEGRADED"),
        "color": color, "latency_ms": 0, "last_activity": w.get("last_activity") or _utcnow_iso(),
        "error_count": 0,
        "note": w.get("message") or f"watchdog {w.get('status','?')} · {w.get('age_minutes','?')}min",
    }


def _scheduler_module_status() -> dict[str, Any]:
    """Scheduler: running status + last refresh."""
    sched = _scheduler_status()
    running = bool(sched.get("running"))
    apscheduler_active = bool(sched.get("apscheduler_running"))
    last_ref = sched.get("last_refresh") or {}
    last_at = str(last_ref.get("last_end_at") or last_ref.get("updated_at") or _utcnow_iso())
    if running and not apscheduler_active:
        note = f"Celery pipeline actif · dernier refresh {last_at[:16]}"
    elif running:
        note = "APScheduler actif"
    else:
        note = sched.get("error", "Scheduler arrêté")
    return {
        "status": "HEALTHY" if running else "DOWN",
        "color": "green" if running else "red",
        "latency_ms": 0, "last_activity": last_at[:19],
        "error_count": 0 if running else 1,
        "note": note,
    }


def _celery_module_status() -> dict[str, Any]:
    """Celery worker status."""
    c = _celery_status()
    available = bool(c.get("available"))
    workers = c.get("workers") or []
    return {
        "status": "HEALTHY" if available else "DEGRADED",
        "color": "green" if available else "orange",
        "latency_ms": 0, "last_activity": _utcnow_iso(),
        "error_count": 0,
        "note": f"{len(workers)} worker(s) actif(s)" if available else "Celery non configuré ou inactif",
    }


def _build_all_modules() -> dict[str, Any]:
    """
    Construit le snapshot santé de tous les modules de la plateforme.
    Retourne un dict {module_name: {status, color, latency_ms, last_activity, error_count, note}}.
    """
    modules: dict[str, Any] = {}

    def _safe(name: str, fn: Any) -> None:
        try:
            modules[name] = fn()
        except Exception as exc:
            modules[name] = {
                "status": "DOWN", "color": "red",
                "latency_ms": 0, "last_activity": _utcnow_iso(),
                "error_count": 1, "note": f"Exception: {exc!s:.100}",
            }

    _safe("fastapi_backend",     _fastapi_backend_status)
    _safe("scheduler",           _scheduler_module_status)
    _safe("celery_worker",       _celery_module_status)
    _safe("pricing_engine",      _pricing_engine_status)
    _safe("source_aggregator",   _source_aggregator_status)
    _safe("trust_engine",        _trust_engine_status)
    _safe("anomaly_detector",    _anomaly_detector_status)
    _safe("serpapi_budget",      _serpapi_module_status)
    _safe("csv_freshness",       _csv_freshness_status)
    _safe("access_control_auth", _auth_status)
    _safe("subscriptions_db",    _subscriptions_db_status)
    _safe("visitor_analytics",   _visitor_analytics_status)
    _safe("comparison_api",      _comparison_api_status)
    _safe("watchdog",            _watchdog_module_status)
    _safe("telegram_alerts",     _telegram_module_status)
    _safe("nginx",               _nginx_status)
    _safe("system_resources",    _system_resources_status)

    # Global summary
    statuses = [m.get("status", "DOWN") for m in modules.values()]
    n_down     = statuses.count("DOWN")
    n_degraded = statuses.count("DEGRADED")
    global_status = "critical" if n_down > 0 else ("degraded" if n_degraded > 0 else "healthy")
    global_color  = "red" if n_down > 0 else ("orange" if n_degraded > 0 else "green")

    return {
        "modules": modules,
        "summary": {
            "global_status": global_status,
            "global_color":  global_color,
            "total": len(modules),
            "healthy": statuses.count("HEALTHY"),
            "degraded": n_degraded,
            "down": n_down,
        },
        "checked_at": _utcnow_iso(),
    }


# ── New action endpoints ───────────────────────────────────────────────────────

@router.get("/api/mission/modules")
def api_mission_modules(request: Request) -> JSONResponse:
    """Snapshot santé de tous les modules de la plateforme (17 modules)."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_build_all_modules())


@router.post("/api/mission/actions/restart-backend")
def api_mission_restart_backend(request: Request) -> JSONResponse:
    """Redémarre le service sneaker_bot via systemctl (déclenche restart propre)."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        result = subprocess.run(
            ["systemctl", "restart", "sneaker_bot"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            return JSONResponse({
                "status":       "restarted",
                "message":      "sneaker_bot redémarré — reconnexion dans 5s",
                "restarted_at": _utcnow_iso(),
            })
        return JSONResponse({
            "status":  "error",
            "message": result.stderr.strip()[:200] or "Erreur inconnue",
            "code":    result.returncode,
        })
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)[:200]})


@router.post("/api/mission/actions/trust-test")
def api_mission_trust_test(request: Request) -> JSONResponse:
    """Smoke-test du Trust Engine avec un jeu de données synthétique."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    t0 = _now_ts()
    try:
        from app.trust_engine import build_trust_report
        report = build_trust_report(
            brand="Nike",
            model="Air Force 1 Low",
            sources=[
                {"shop": "Foot Locker", "price_avg": 110.0},
                {"shop": "Zalando", "price_avg": 119.99},
                {"shop": "JD Sports", "price_avg": 105.0},
            ],
            market_meta={"confidence_score": 85},
        )
        ms = round((_now_ts() - t0) * 1000)
        return JSONResponse({
            "status":     "ok",
            "trust_score": report.get("trust_score"),
            "spread_pct":  report.get("price_spread_pct"),
            "elapsed_ms":  ms,
            "report":      report,
            "tested_at":   _utcnow_iso(),
        })
    except Exception as exc:
        return JSONResponse({
            "status":  "error",
            "message": str(exc)[:200],
            "elapsed_ms": round((_now_ts() - t0) * 1000),
        })


@router.post("/api/mission/actions/force-refresh")
def api_mission_force_refresh(request: Request) -> JSONResponse:
    """Déclenche un refresh pricing FR via /api/refresh/fr (non bloquant)."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    t0 = _now_ts()
    try:
        req = _urllib_req.Request(
            "http://127.0.0.1:5003/api/refresh/fr",
            data=b"",
            method="POST",
            headers={"User-Agent": "sneakerbot-admin/1.0"},
        )
        with _urllib_req.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        ms = round((_now_ts() - t0) * 1000)
        return JSONResponse({
            "status":       "triggered",
            "message":      "Refresh FR déclenché",
            "response":     body,
            "elapsed_ms":   ms,
            "triggered_at": _utcnow_iso(),
        })
    except Exception as exc:
        return JSONResponse({
            "status":  "error",
            "message": str(exc)[:200],
            "elapsed_ms": round((_now_ts() - t0) * 1000),
        })


@router.post("/api/mission/actions/rebuild-csv")
def api_mission_rebuild_csv(request: Request) -> JSONResponse:
    """Force la reconstruction des CSV depuis les données DB."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    t0 = _now_ts()
    rebuilt: list[str] = []
    errors: list[str] = []
    try:
        req = _urllib_req.Request(
            "http://127.0.0.1:5003/api/update/fr",
            data=b"",
            method="POST",
            headers={"User-Agent": "sneakerbot-admin/1.0"},
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        rebuilt.append(f"update/fr → {body.get('status','ok')}")
    except Exception as exc:
        errors.append(f"update/fr: {exc!s:.100}")

    ms = round((_now_ts() - t0) * 1000)
    return JSONResponse({
        "status":       "ok" if not errors else "partial",
        "rebuilt":      rebuilt,
        "errors":       errors,
        "elapsed_ms":   ms,
        "rebuilt_at":   _utcnow_iso(),
    })


@router.post("/api/mission/actions/clear-stuck-jobs")
def api_mission_clear_stuck_jobs(request: Request) -> JSONResponse:
    """Tente de vider les jobs bloqués : scheduler reset + cache clear."""
    from app.app import _is_admin_session
    if not _is_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    t0 = _now_ts()
    cleared: list[str] = []
    errors: list[str] = []

    # 1. Cache clear
    try:
        req = _urllib_req.Request(
            "http://127.0.0.1:5003/api/cache/clear",
            data=b"",
            method="POST",
            headers={"User-Agent": "sneakerbot-admin/1.0"},
        )
        with _urllib_req.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode())
        cleared.append(f"cache clear → {body.get('status','ok')}")
    except Exception as exc:
        errors.append(f"cache: {exc!s:.80}")

    # 2. Scheduler status check + restart if stopped
    try:
        sched = _scheduler_status()
        if not sched.get("running"):
            import importlib
            try:
                smod = importlib.import_module("app.scheduler")
            except Exception:
                smod = importlib.import_module("scheduler")
            start_fn = getattr(smod, "start_scheduler", None)
            if callable(start_fn):
                start_fn()
                cleared.append("scheduler redémarré")
            else:
                errors.append("start_scheduler non disponible")
        else:
            cleared.append("scheduler déjà actif")
    except Exception as exc:
        errors.append(f"scheduler: {exc!s:.80}")

    ms = round((_now_ts() - t0) * 1000)
    return JSONResponse({
        "status":     "ok" if not errors else "partial",
        "cleared":    cleared,
        "errors":     errors,
        "elapsed_ms": ms,
        "cleared_at": _utcnow_iso(),
    })
