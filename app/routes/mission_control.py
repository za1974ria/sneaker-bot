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
import subprocess
import time
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
FRESH_GREEN_MIN  = 240   # < 4h → green
FRESH_ORANGE_MIN = 720   # 4–12h → orange
# > 12h → red

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
        running = bool(callable(is_running) and is_running())
        get_status = getattr(sched, "get_last_refresh_status", None)
        last_refresh: dict[str, Any] = {}
        if callable(get_status):
            last_refresh = dict(get_status() or {})
        return {
            "running": running,
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
        import playwright  # noqa: F401
        # Vérifie la présence du binaire chromium
        result = subprocess.run(
            ["python3", "-m", "playwright", "install", "--dry-run"],
            capture_output=True, text=True, timeout=5
        )
        installed = (result.returncode == 0 or "chromium" in (result.stdout + result.stderr).lower())
        return {"installed": True, "color": "green" if installed else "orange", "note": "playwright disponible"}
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

    Services PRIMAIRES (affectent le statut global) :
      - fastapi_backend, scheduler, pricing_engine, source_aggregator, logs

    Services SECONDAIRES (optionnels — ne font pas degraded à eux seuls) :
      - playwright_scraper, celery_worker

    Règles :
      CRITICAL : backend KO  OU  csv absent  OU  scheduler down  OU  logs erreurs critiques (>20)
      DEGRADED : service primaire orange  OU  csv orange  OU  SerpAPI >70%  OU  logs orange
      HEALTHY  : tout le reste
    """
    # ── Services primaires ────────────────────────────────────────────────────
    PRIMARY_SERVICES = {"fastapi_backend", "scheduler", "pricing_engine", "source_aggregator", "logs"}

    primary_colors: list[str] = []
    for key, info in services.items():
        if key in PRIMARY_SERVICES:
            primary_colors.append(info.get("color", "green"))

    # ── CSV ───────────────────────────────────────────────────────────────────
    csv_colors: list[str] = [
        f.get("color") or f.get("status") or "green"
        for f in data.get("files", {}).values()
    ]

    # ── SerpAPI ───────────────────────────────────────────────────────────────
    serp_color = (keys.get("serpapi") or {}).get("color", "green")

    all_primary = primary_colors + csv_colors + [serp_color]

    if "red" in all_primary:
        return "critical"
    if "orange" in all_primary:
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
