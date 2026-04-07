"""
main.py — Point d'entrée FastAPI alternatif (debug/legacy).

Production officielle: `app.app:app` (service `sneaker_bot.service`).
Démarrage local alternatif: `uvicorn main:app --host 0.0.0.0 --port 5003`
"""

from __future__ import annotations

import csv
import html
import json
import logging
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import unquote

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# ---------------------------------------------------------------------------
# A) INITIALISATION — logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sneakerbot.main")

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
MARKET_FR_CSV = DATA_DIR / "market_fr.csv"
MARKET_FR_SOURCES_CSV = DATA_DIR / "market_fr_sources.csv"
MODELS_LIST_JSON = DATA_DIR / "models_list.json"
FR_UPDATE_STATUS_JSON = DATA_DIR / "fr_update_status.json"
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "static"

app = FastAPI(
    title="SneakerBot API",
    version="2.0",
    description=(
        "API de veille prix sneakers (marché FR) : agrégats CSV, sources par boutique, "
        "scheduler APScheduler (run_market + refresh sources)."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fichiers statiques (si dossier présent)
if STATIC_DIR.is_dir():
    try:
        from fastapi.staticfiles import StaticFiles

        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        log.info("StaticFiles monté sur /static")
    except Exception as e:  # noqa: BLE001
        log.warning("StaticFiles non disponible: %s", e)

# Jinja2 (optionnel — non listé dans requirements.txt ; souvent installé avec tooling)
templates = None
if TEMPLATES_DIR.is_dir():
    try:
        from starlette.templating import Jinja2Templates

        templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    except ImportError:
        log.warning("Jinja2 non installé — routes HTML utiliseront du JSON ou HTML inline.")

# ---------------------------------------------------------------------------
# Imports projet (services réels)
# ---------------------------------------------------------------------------
try:
    from app.analytics.tracker import get_stats
except Exception as e:  # noqa: BLE001
    get_stats = None  # type: ignore[assignment]
    log.warning("app.analytics.tracker indisponible: %s", e)

try:
    from scheduler import (
        run_scheduled_fr_market,
        scheduler_is_running,
        shutdown_scheduler,
        start_scheduler,
    )
except Exception as e:  # noqa: BLE001
    start_scheduler = None  # type: ignore[assignment]
    shutdown_scheduler = None  # type: ignore[assignment]
    scheduler_is_running = lambda: False  # noqa: E731
    run_scheduled_fr_market = None  # type: ignore[assignment]
    log.warning("scheduler indisponible: %s", e)


# ---------------------------------------------------------------------------
# Helpers données (CSV / JSON)
# ---------------------------------------------------------------------------
def _read_market_fr_rows() -> list[dict[str, Any]]:
    if not MARKET_FR_CSV.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with MARKET_FR_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                out.append(dict(row))
    except Exception as e:  # noqa: BLE001
        log.exception("Lecture market_fr.csv: %s", e)
    return out


def _read_sources_rows() -> list[dict[str, Any]]:
    if not MARKET_FR_SOURCES_CSV.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with MARKET_FR_SOURCES_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                out.append(dict(row))
    except Exception as e:  # noqa: BLE001
        log.exception("Lecture market_fr_sources.csv: %s", e)
    return out


def _read_models_list() -> list[dict[str, str]]:
    if not MODELS_LIST_JSON.is_file():
        return []
    try:
        raw = json.loads(MODELS_LIST_JSON.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict):
                b = str(item.get("brand") or "").strip()
                m = str(item.get("model") or "").strip()
                if b and m:
                    out.append({"brand": b, "model": m})
        return out
    except Exception as e:  # noqa: BLE001
        log.exception("Lecture models_list.json: %s", e)
        return []


def _comparison_nav_fragment() -> str:
    return """
    <nav style="display:flex;align-items:center;justify-content:space-between;padding:16px 24px;
      background:#0a0a0a;border-bottom:1px solid #1e1e1e;position:sticky;top:0;z-index:100;">
      <a href="/" style="font-size:1.25rem;font-weight:800;color:#fff;text-decoration:none;">👟 SneakerBot</a>
      <div style="display:flex;gap:18px;">
        <a href="/" style="color:#888;text-decoration:none;font-size:.9rem;">Dashboard</a>
        <a href="/comparison" style="color:#00ff88;text-decoration:none;font-size:.9rem;font-weight:600;">Comparateur</a>
        <a href="/docs" style="color:#888;text-decoration:none;font-size:.9rem;">API</a>
      </div>
    </nav>
    """


def _comparison_base_style() -> str:
    return """
    <style>
      *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
      body{background:#0d0d0d;color:#e0e0e0;font-family:system-ui,-apple-system,sans-serif;min-height:100vh;}
      .btn{display:inline-block;padding:10px 22px;border-radius:10px;font-weight:700;cursor:pointer;border:none;font-size:.9rem;}
      .btn-primary{background:#00ff88;color:#0a0a0a;}
      .btn-primary:hover{background:#00cc6a;}
    </style>
    """


def _read_fr_update_status() -> dict[str, Any]:
    if not FR_UPDATE_STATUS_JSON.is_file():
        return {}
    try:
        raw = json.loads(FR_UPDATE_STATUS_JSON.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("fr_update_status.json: %s", e)
        return {}


def _sneaker_model_id(brand: str, model: str) -> str:
    return f"{brand.strip()}|{model.strip()}"


def _parse_csv_bool(val: Any) -> bool | None:
    if val is None:
        return None
    s = str(val).strip()
    if s == "":
        return None
    sl = s.lower()
    if sl in ("true", "1", "yes"):
        return True
    if sl in ("false", "0", "no"):
        return False
    return None


def _row_confidence_payload(
    row: dict[str, Any],
    claude_points: int | None = None,
) -> dict[str, Any]:
    from app.confidence_scorer import compute_confidence_score

    try:
        ns = int(float(str(row.get("nb_sources") or "1").replace(",", ".")))
    except (TypeError, ValueError):
        ns = 1
    ns = max(1, ns)

    try:
        pm = float(str(row.get("price_min") or "0").replace(",", "."))
        px = float(str(row.get("price_max") or "0").replace(",", "."))
        pa = float(str(row.get("price_avg") or "0").replace(",", "."))
    except (TypeError, ValueError):
        pm = px = pa = 0.0

    gconf_raw = row.get("groq_confidence")
    gconf: float | None = None
    if gconf_raw is not None and str(gconf_raw).strip() != "":
        try:
            gconf = float(str(gconf_raw).replace(",", "."))
        except (TypeError, ValueError):
            gconf = None

    gvalid = _parse_csv_bool(row.get("groq_valid"))

    return compute_confidence_score(
        nb_sources=ns,
        price_min=pm,
        price_max=px,
        price_avg=pa,
        updated_at=str(row.get("updated_at") or ""),
        groq_confidence=gconf,
        groq_valid=gvalid,
        claude_points=claude_points,
        brand=str(row.get("brand") or ""),
        model=str(row.get("model") or ""),
    )


def _parse_model_id(model_id: str) -> tuple[str, str]:
    decoded = unquote(model_id)
    if "|" not in decoded:
        raise HTTPException(status_code=400, detail="model_id attendu: Brand|Model (encodé URL)")
    brand, _, model = decoded.partition("|")
    if not brand.strip() or not model.strip():
        raise HTTPException(status_code=400, detail="Brand ou model vide")
    return brand.strip(), model.strip()


# ---------------------------------------------------------------------------
# B) STARTUP / SHUTDOWN
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _on_startup() -> None:
    if start_scheduler is not None:
        try:
            start_scheduler()
        except Exception as e:  # noqa: BLE001
            log.exception("start_scheduler a échoué: %s", e)
    try:
        from app.price_history import init_db

        init_db()
    except Exception as e:  # noqa: BLE001
        log.warning("price_history init au démarrage: %s", e)
    try:
        from app.price_alerts import init_alerts_db

        init_alerts_db()
    except Exception as e:  # noqa: BLE001
        log.warning("price_alerts init au démarrage: %s", e)
    try:
        from app.google_shopping_verifier import init_google_cache

        init_google_cache()
    except Exception as e:  # noqa: BLE001
        log.warning("google_shopping init au démarrage: %s", e)
    try:
        from scrapers.sitemap_scraper import init_sitemap_db

        init_sitemap_db()
    except Exception as e:  # noqa: BLE001
        log.warning("sitemap init au démarrage: %s", e)
    log.info("✅ SneakerBot démarré — scheduler actif")


@app.on_event("shutdown")
def _on_shutdown() -> None:
    if shutdown_scheduler is not None:
        try:
            shutdown_scheduler()
        except Exception as e:  # noqa: BLE001
            log.exception("shutdown_scheduler a échoué: %s", e)
    log.info("🛑 SneakerBot arrêté")


# ---------------------------------------------------------------------------
# Gestion d'erreurs globales
# ---------------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def _starlette_http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        return JSONResponse(
            status_code=404,
            content={"error": "Route non trouvée", "path": request.url.path},
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail if isinstance(exc.detail, str) else str(exc.detail)},
    )


@app.exception_handler(FastAPIHTTPException)
async def _fastapi_http_handler(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail if isinstance(exc.detail, str) else str(exc.detail)},
    )


@app.middleware("http")
async def _middleware_500_json(request: Request, call_next: Any) -> Any:
    try:
        return await call_next(request)
    except (StarletteHTTPException, FastAPIHTTPException):
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Erreur interne path=%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "Erreur interne", "detail": str(exc)},
        )


# ---------------------------------------------------------------------------
# C) ROUTES PRINCIPALES
# ---------------------------------------------------------------------------
@app.get("/")
async def root(request: Request) -> Any:
    """Accueil : template index.html si non vide, sinon JSON de bienvenue."""
    try:
        index_path = TEMPLATES_DIR / "index.html"
        if index_path.is_file() and index_path.stat().st_size > 0 and templates is not None:
            return templates.TemplateResponse("index.html", {"request": request})
        if index_path.is_file() and index_path.stat().st_size > 0:
            return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.exception("root: %s", e)
    return JSONResponse(
        {
            "service": "SneakerBot API",
            "version": "2.0",
            "docs": "/docs",
            "health": "/health",
        }
    )


@app.get("/liens", response_class=HTMLResponse)
async def liens_hub(request: Request) -> HTMLResponse:
    """Hub unique : tous les liens utiles SneakerBot (même schéma / hôte que la requête)."""
    base = str(request.base_url).rstrip("/")
    entries: list[tuple[str, str]] = [
        ("Dashboard (accueil)", f"{base}/"),
        ("Comparateur", f"{base}/comparison"),
        (
            "Exemple avec filtres",
            f"{base}/comparison?brand=Adidas&model=Samba+Classic",
        ),
        ("Doc API (Swagger)", f"{base}/docs"),
        ("Santé", f"{base}/health"),
    ]
    lis = []
    for label, url in entries:
        lis.append(
            "<li style=\"margin:1rem 0;\">"
            f'<a href="{html.escape(url, quote=True)}" style="color:#00ff88;font-weight:600;text-decoration:none;font-size:1.05rem;">{html.escape(label)}</a>'
            f'<div style="margin-top:0.35rem;"><code style="font-size:0.8rem;color:#9ca3af;word-break:break-all;">{html.escape(url)}</code></div>'
            "</li>"
        )
    body = f"""<!DOCTYPE html>
<html lang="fr"><head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Liens SneakerBot</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#0d0d0d; color:#e8e8e3; margin:0; min-height:100vh; padding:2rem 1.25rem; }}
    .wrap {{ max-width: 42rem; margin: 0 auto; }}
    h1 {{ font-size: 1.35rem; margin: 0 0 0.5rem; }}
    p {{ color: #9ca3af; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    ul {{ list-style: none; padding: 0; margin: 0; border: 1px solid #262626; border-radius: 12px; padding: 0.5rem 1.25rem; background: #141414; }}
    a:hover {{ text-decoration: underline; }}
    .back {{ margin-top: 2rem; }}
    .back a {{ color: #888; font-size: 0.9rem; }}
  </style>
</head><body>
  <div class="wrap">
    <h1>Liens SneakerBot</h1>
    <p>Page unique à mettre en favori : les URL s’adaptent au site (http/https, domaine).</p>
    <ul>{"".join(lis)}</ul>
    <p class="back"><a href="{html.escape(f"{base}/", quote=True)}">← Retour au dashboard</a></p>
  </div>
</body></html>"""
    return HTMLResponse(content=body)


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        running = scheduler_is_running() if callable(scheduler_is_running) else False
    except Exception:  # noqa: BLE001
        running = False
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scheduler": "running" if running else "stopped",
    }


@app.get("/api/reference/official-brands")
async def api_reference_official_brands() -> dict[str, Any]:
    """Catalogue des marques avec site officiel + repère prix indicatif (JSON éditorial)."""
    try:
        from app.official_brand_sites import list_official_brands

        return list_official_brands()
    except Exception as e:  # noqa: BLE001
        log.exception("api_reference_official_brands: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/comparison/fr")
async def api_comparison_fr(
    brand: str = Query("", description="Filtre marque (exact, insensible à la casse)"),
    model: str = Query("", description="Filtre modèle (sous-chaîne)"),
    include_official: bool = Query(
        True,
        description="Inclure repère prix / liens site officiel marque (si marque documentée)",
    ),
) -> JSONResponse:
    """Comparateur par boutique FR (CSV `market_fr_sources.csv`), style Trivago."""
    if not MARKET_FR_SOURCES_CSV.is_file():
        return JSONResponse(
            {"items": [], "count": 0, "message": "Aucune donnée source FR disponible"},
        )

    bq = brand.strip().lower()
    mq = model.strip().lower()
    items: list[dict[str, Any]] = []
    try:
        with MARKET_FR_SOURCES_CSV.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rb = (r.get("brand") or "").strip()
                rm = (r.get("model") or "").strip()
                if bq and rb.lower() != bq:
                    continue
                if mq and mq not in rm.lower():
                    continue
                try:
                    pmin = float(str(r.get("price_min") or 0.0).replace(",", "."))
                    pmax = float(str(r.get("price_max") or 0.0).replace(",", "."))
                    pavg = float(str(r.get("price_avg") or 0.0).replace(",", "."))
                    pcount = int(float(str(r.get("price_count") or 0).replace(",", ".")))
                except (TypeError, ValueError):
                    continue
                credibility = "high"
                if pcount <= 1 or (pmin > 0 and pmax > (pmin * 2.2)):
                    credibility = "medium"
                if pcount <= 0:
                    credibility = "low"
                items.append(
                    {
                        "brand": rb,
                        "model": rm,
                        "shop": (r.get("shop") or "").strip(),
                        "price_min": pmin,
                        "price_max": pmax,
                        "price_avg": pavg,
                        "price_count": pcount,
                        "credibility": credibility,
                        "updated_at": (r.get("updated_at") or "").strip(),
                    },
                )
    except Exception as e:  # noqa: BLE001
        log.exception("api_comparison_fr: %s", e)
        return JSONResponse(
            {"items": [], "count": 0, "message": "Erreur lecture sources"},
            status_code=500,
        )

    items.sort(
        key=lambda x: (
            str(x["brand"]).lower(),
            str(x["model"]).lower(),
            float(x["price_min"]),
        ),
    )
    payload: dict[str, Any] = {"items": items, "count": len(items)}
    if include_official and brand.strip():
        try:
            from app.official_brand_sites import get_official_brand_reference

            rb = brand.strip()
            for r in items:
                if str(r.get("brand") or "").strip().lower() == rb.lower():
                    rb = str(r["brand"]).strip()
                    break
            ref = get_official_brand_reference(rb)
            if ref is not None:
                payload["official_reference"] = ref
        except Exception as e:  # noqa: BLE001
            log.debug("official_reference skip: %s", e)
    return JSONResponse(payload)


@app.get("/comparison", response_class=HTMLResponse)
async def comparison_page(
    brand: str = Query("", description="Marque"),
    model: str = Query("", description="Modèle"),
    pqs: str | None = Query(None, description="Score affichage (optionnel, ex. lien externe)"),
    perf: str | None = Query(None, description="Score perf (optionnel)"),
    show_official: bool = Query(
        True,
        description="Afficher le repère prix site officiel (query persistée dans le formulaire)",
    ),
) -> HTMLResponse:
    """Page comparateur multi-boutiques (données `market_fr_sources.csv`)."""
    b = brand.strip()
    m = model.strip()
    b_esc = html.escape(b, quote=True)
    m_esc = html.escape(m, quote=True)
    scores_line = ""
    if (pqs is not None and str(pqs).strip()) or (perf is not None and str(perf).strip()):
        parts: list[str] = []
        if pqs is not None and str(pqs).strip():
            parts.append(f"PQS <strong>{html.escape(str(pqs))}</strong>")
        if perf is not None and str(perf).strip():
            parts.append(f"Perf <strong>{html.escape(str(perf))}</strong>")
        scores_line = (
            f'<div style="margin:10px 0 0;font-size:.85rem;color:#9a9a9a;">{" · ".join(parts)}</div>'
        )

    page = f"""<!DOCTYPE html>
<html lang="fr"><head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Comparateur FR — SneakerBot</title>
  {_comparison_base_style()}
  <style>
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 26px 18px 50px; }}
    .head h2 {{ font-size: 1.5rem; }}
    .sub {{ color: #7a7a7a; font-size: .92rem; margin-top: 6px; }}
    .filters {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 18px 0 14px; }}
    .filters input {{
      background: #141414; border: 1px solid #2a2a2a; color: #fff;
      border-radius: 10px; padding: 10px 12px; min-width: 220px;
    }}
    .table {{ border: 1px solid #1f1f1f; border-radius: 12px; overflow: hidden; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      padding: 10px 12px; border-bottom: 1px solid #1b1b1b;
      text-align: left; font-size: .9rem;
    }}
    th {{
      background: #121212; color: #9a9a9a; font-size: .78rem;
      text-transform: uppercase; letter-spacing: .06em;
    }}
    .pill {{ padding: 3px 8px; border-radius: 999px; font-size: .72rem; font-weight: 700; }}
    .p-high {{ background: #0f2f1f; color: #4dff9f; }}
    .p-medium {{ background: #332a10; color: #ffd66f; }}
    .muted {{ color: #787878; }}
    .official-box {{
      margin: 14px 0 8px; padding: 14px 16px; border-radius: 12px;
      border: 1px solid #2a3d32; background: #0f1a14; font-size: .88rem; line-height: 1.45;
    }}
    .official-box a {{ color: #5ee9a8; }}
    .official-box .disc {{ color: #6a8a78; font-size: .78rem; margin-top: 8px; }}
    .chk-row {{ display: flex; align-items: center; gap: 8px; color: #9a9a9a; font-size: .85rem; }}
  </style>
</head><body>
  {_comparison_nav_fragment()}
  <div class="wrap">
    <div class="head">
      <h2>🛒 Comparateur FR multi-boutiques</h2>
      <div class="sub">Prix par boutique (données scraping) — même logique que l’API <code>/api/comparison/fr</code>.</div>
      {scores_line}
    </div>
    <form class="filters" method="get" action="/comparison">
      <input name="brand" placeholder="Marque (ex: Adidas)" value="{b_esc}">
      <input name="model" placeholder="Modèle (ex: Samba Classic)" value="{m_esc}">
      <input type="hidden" name="pqs" value="{html.escape(str(pqs or ""), quote=True)}">
      <input type="hidden" name="perf" value="{html.escape(str(perf or ""), quote=True)}">
      <label class="chk-row">
        <input type="hidden" id="show_official_field" name="show_official" value="{"true" if show_official else "false"}">
        <input type="checkbox" id="show_official_chk" {"checked" if show_official else ""}
          onchange="document.getElementById('show_official_field').value = this.checked ? 'true' : 'false';">
        Repère prix site officiel marque
      </label>
      <button class="btn btn-primary" type="submit">Comparer</button>
    </form>
    <div id="officialPanel" class="official-box" style="display:none;"></div>
    <div class="table">
      <table>
        <thead>
          <tr>
            <th>Produit</th><th>Boutique</th><th>Min</th><th>Moyen</th><th>Max</th>
            <th>Nb prix</th><th>Crédibilité</th>
          </tr>
        </thead>
        <tbody id="rows"><tr><td colspan="7" class="muted">Chargement…</td></tr></tbody>
      </table>
    </div>
  </div>
  <script>
    const showOfficial = {json.dumps(show_official)};
    const qs = new URLSearchParams({{ brand: {json.dumps(b)}, model: {json.dumps(m)} }});
    if (showOfficial) qs.set('include_official', 'true');
    else qs.set('include_official', 'false');
    fetch('/api/comparison/fr?' + qs.toString())
      .then(r => r.json())
      .then(data => {{
        const op = document.getElementById('officialPanel');
        if (showOfficial && data.official_reference) {{
          const ref = data.official_reference;
          const links = (ref.official_urls || []).map(u =>
            `<a href="${{String(u.url).replace(/"/g,'')}}" target="_blank" rel="noopener">${{(u.label || u.url || '').replace(/</g,'&lt;')}}</a>`
          ).join(' · ');
          op.style.display = 'block';
          op.innerHTML = `<strong style="color:#7af5b5;">Site officiel — repère prix</strong>
            <div style="margin-top:6px;">${{links}}</div>
            <div><span class="muted">Ex. ${{String(ref.reference_example || '').replace(/</g,'&lt;')}} :</span>
            <strong> ${{String(ref.price_eur_label || '').replace(/</g,'&lt;')}}</strong></div>
            <div class="disc">${{(ref.disclaimer_fr || '').replace(/</g,'&lt;')}}</div>`;
        }} else {{ op.style.display = 'none'; op.innerHTML = ''; }}
        const rows = document.getElementById('rows');
        if (!data.items || data.items.length === 0) {{
          rows.innerHTML = '<tr><td colspan="7" class="muted">Aucune donnée pour ce filtre.</td></tr>';
          return;
        }}
        rows.innerHTML = data.items.map(it => {{
          const c = it.credibility === 'high' ? 'p-high' : 'p-medium';
          const product = (it.brand + ' ' + it.model).replace(/</g,'&lt;');
          return `<tr>
            <td>${{product}}</td>
            <td>${{String(it.shop).replace(/</g,'&lt;')}}</td>
            <td>${{Number(it.price_min).toFixed(2)}}\u00a0€</td>
            <td>${{Number(it.price_avg).toFixed(2)}}\u00a0€</td>
            <td>${{Number(it.price_max).toFixed(2)}}\u00a0€</td>
            <td>${{it.price_count}}</td>
            <td><span class="pill ${{c}}">${{it.credibility}}</span></td>
          </tr>`;
        }}).join('');
      }})
      .catch(() => {{
        document.getElementById('rows').innerHTML =
          '<tr><td colspan="7" class="muted">Erreur API comparaison.</td></tr>';
      }});
  </script>
</body></html>"""
    return HTMLResponse(content=page)


@app.get("/api/sneakers/search")
async def api_sneakers_search(
    q: str = Query(..., min_length=1, description="Fragment marque ou modèle"),
) -> dict[str, Any]:
    """Recherche par nom (marque ou modèle), insensible à la casse."""
    try:
        qn = q.strip().lower()
        rows = _read_market_fr_rows()
        matches = []
        for r in rows:
            brand = str(r.get("brand") or "")
            model = str(r.get("model") or "")
            blob = f"{brand} {model}".lower()
            if qn in blob:
                item = {
                    "model_id": _sneaker_model_id(brand, model),
                    "brand": brand,
                    "model": model,
                    "market": r.get("market", "FR"),
                    "price_min": r.get("price_min"),
                    "price_max": r.get("price_max"),
                    "price_avg": r.get("price_avg"),
                    "updated_at": r.get("updated_at"),
                }
                item["confidence"] = _row_confidence_payload(r)
                matches.append(item)
        return {"query": q, "count": len(matches), "results": matches}
    except Exception as e:  # noqa: BLE001
        log.exception("api_sneakers_search: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/sneakers")
async def api_sneakers(
    brand: str | None = Query(None, description="Filtrer par marque"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Liste des sneakers avec prix agrégés (data/market_fr.csv)."""
    try:
        rows = _read_market_fr_rows()
        items: list[dict[str, Any]] = []
        for r in rows:
            b = str(r.get("brand") or "").strip()
            m = str(r.get("model") or "").strip()
            if not b or not m:
                continue
            if brand and b.lower() != brand.strip().lower():
                continue
            item = {
                "model_id": _sneaker_model_id(b, m),
                "brand": b,
                "model": m,
                "market": str(r.get("market") or "FR"),
                "price_min": r.get("price_min"),
                "price_max": r.get("price_max"),
                "price_avg": r.get("price_avg"),
                "updated_at": r.get("updated_at"),
            }
            item["confidence"] = _row_confidence_payload(r)
            items.append(item)
        total = len(items)
        page = items[offset : offset + limit]
        return {"total": total, "limit": limit, "offset": offset, "items": page}
    except Exception as e:  # noqa: BLE001
        log.exception("api_sneakers: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/sneakers/{model_id:path}/history")
async def api_sneaker_history(
    model_id: str,
    days: int = Query(30, ge=1, le=120, description="Fenêtre glissante (jours)"),
) -> dict[str, Any]:
    """Historique des snapshots de prix (SQLite, 30 jours max recommandé)."""
    try:
        from app.price_history import get_history

        b, m = _parse_model_id(model_id)
        history = get_history(b, m, days=days)
        return {"brand": b, "model": m, "market": "FR", "days": days, "history": history}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("api_sneaker_history: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/sneakers/{model_id:path}/trend")
async def api_sneaker_trend(
    model_id: str,
    days: int = Query(30, ge=1, le=120),
) -> dict[str, Any]:
    """Tendance agrégée sur l'historique (prix moyens)."""
    try:
        from app.price_history import get_price_trend

        b, m = _parse_model_id(model_id)
        out = get_price_trend(b, m, days=days)
        out["brand"] = b
        out["model"] = m
        return out
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("api_sneaker_trend: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/history/stats")
async def api_history_stats() -> dict[str, Any]:
    """Statistiques globales sur la base d'historique."""
    try:
        from app.price_history import get_global_stats

        return get_global_stats()
    except Exception as e:  # noqa: BLE001
        log.exception("api_history_stats: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/sneakers/{model_id:path}")
async def api_sneaker_detail(
    model_id: str,
    include_official: bool = Query(
        True,
        description="Inclure repère prix / site officiel marque (JSON éditorial)",
    ),
) -> dict[str, Any]:
    """Détail d'un modèle (min/max/avg + lignes sources par boutique)."""
    try:
        b, m = _parse_model_id(model_id)
        rows = _read_market_fr_rows()
        agg = None
        for r in rows:
            if str(r.get("brand") or "").strip() == b and str(r.get("model") or "").strip() == m:
                agg = r
                break
        if not agg:
            raise HTTPException(status_code=404, detail="Modèle introuvable dans market_fr.csv")

        sources_all = _read_sources_rows()
        sources = [
            s
            for s in sources_all
            if str(s.get("brand") or "").strip() == b and str(s.get("model") or "").strip() == m
        ]
        prices_for_ai: list[float] = []
        for s in sources:
            for key in ("price_avg", "price_min", "price_max"):
                try:
                    prices_for_ai.append(float(str(s.get(key) or "").replace(",", ".")))
                except (TypeError, ValueError):
                    pass
        if not prices_for_ai:
            for key in ("price_min", "price_avg", "price_max"):
                try:
                    prices_for_ai.append(float(str(agg.get(key) or "").replace(",", ".")))
                except (TypeError, ValueError):
                    pass

        ai_validation: dict[str, Any] | None = None
        try:
            from app.ai_supervisor import AISupervisor

            vr = AISupervisor().validate_prices(m, b, prices_for_ai)
            ai_validation = {
                "valid": bool(vr.get("valid")),
                "reason": str(vr.get("reason") or ""),
                "confidence": float(vr.get("confidence") or 0.0),
                "anomalies": vr.get("anomalies") if isinstance(vr.get("anomalies"), list) else [],
            }
        except Exception as e:  # noqa: BLE001
            log.warning("ai_validation skip: %s", e)
            ai_validation = {
                "valid": True,
                "reason": "Validation IA indisponible",
                "confidence": 0.0,
                "anomalies": [],
            }

        claude_pts: int | None = None
        try:
            from app.claude_credibility import get_claude_credibility_bonus

            try:
                c_ns = int(float(str(agg.get("nb_sources") or "1").replace(",", ".")))
            except (TypeError, ValueError):
                c_ns = 1
            c_ns = max(1, c_ns)
            try:
                c_pm = float(str(agg.get("price_min") or "0").replace(",", "."))
                c_px = float(str(agg.get("price_max") or "0").replace(",", "."))
                c_pa = float(str(agg.get("price_avg") or "0").replace(",", "."))
            except (TypeError, ValueError):
                c_pm = c_px = c_pa = 0.0
            av = ai_validation or {}
            claude_pts = get_claude_credibility_bonus(
                brand=b,
                model=m,
                price_min=c_pm,
                price_max=c_px,
                price_avg=c_pa,
                nb_sources=c_ns,
                updated_at=str(agg.get("updated_at") or ""),
                groq_valid=bool(av.get("valid")) if av else None,
                groq_confidence=float(av.get("confidence") or 0.0) if av else None,
                groq_reason=str(av.get("reason") or ""),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("claude_credibility skip: %s", e)
            claude_pts = None

        official_reference: dict[str, Any] | None = None
        if include_official:
            try:
                from app.official_brand_sites import get_official_brand_reference

                official_reference = get_official_brand_reference(b)
            except Exception as e:  # noqa: BLE001
                log.debug("official_reference skip: %s", e)

        out: dict[str, Any] = {
            "model_id": _sneaker_model_id(b, m),
            "brand": b,
            "model": m,
            "aggregated": agg,
            "sources": sources,
            "sources_count": len(sources),
            "confidence": _row_confidence_payload(agg, claude_points=claude_pts),
            "ai_validation": ai_validation,
        }
        if official_reference is not None:
            out["official_reference"] = official_reference
        return out
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("api_sneaker_detail: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/export/woocommerce-csv")
async def api_export_woocommerce_csv() -> FileResponse:
    """Génère et télécharge un CSV import WooCommerce depuis ``market_fr.csv``."""
    try:
        from app.woocommerce_export import generate_woocommerce_csv

        path = generate_woocommerce_csv()
        if not path.is_file():
            raise HTTPException(status_code=500, detail="Fichier export introuvable")
        return FileResponse(
            path=str(path),
            filename=path.name,
            media_type="text/csv; charset=utf-8",
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("api_export_woocommerce_csv: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/export/woocommerce-json")
async def api_export_woocommerce_json() -> FileResponse:
    """Génère et télécharge un JSON produits (structure type WooCommerce REST v3)."""
    try:
        from app.woocommerce_export import generate_woocommerce_json

        path = generate_woocommerce_json()
        if not path.is_file():
            raise HTTPException(status_code=500, detail="Fichier export introuvable")
        return FileResponse(
            path=str(path),
            filename=path.name,
            media_type="application/json; charset=utf-8",
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("api_export_woocommerce_json: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/export/prestashop-csv")
async def api_export_prestashop_csv() -> FileResponse:
    """Génère et télécharge un CSV PrestaShop (séparateur `;`)."""
    try:
        from app.woocommerce_export import generate_prestashop_csv

        path = generate_prestashop_csv()
        if not path.is_file():
            raise HTTPException(status_code=500, detail="Fichier export introuvable")
        return FileResponse(
            path=str(path),
            filename=path.name,
            media_type="text/csv; charset=utf-8",
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("api_export_prestashop_csv: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/alerts")
async def api_alerts(
    limit: int = Query(20, ge=1, le=200),
    unseen_only: bool = Query(False),
) -> dict[str, Any]:
    """Liste des alertes prix récentes + compteurs."""
    try:
        from app.price_alerts import get_alerts_count, get_recent_alerts

        alerts = get_recent_alerts(limit=limit, unseen_only=unseen_only)
        cnt = get_alerts_count()
        return {"alerts": alerts, "count": cnt}
    except Exception as e:  # noqa: BLE001
        log.exception("api_alerts: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/alerts/count")
async def api_alerts_count() -> dict[str, int]:
    """Compteurs total / non vues."""
    try:
        from app.price_alerts import get_alerts_count

        return get_alerts_count()
    except Exception as e:  # noqa: BLE001
        log.exception("api_alerts_count: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/alerts/mark-seen")
async def api_alerts_mark_seen(
    body: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    """Marque des alertes comme vues : ``{{\"ids\":[1,2]}}`` ou corps vide / ids vides = toutes."""
    try:
        from app.price_alerts import mark_alerts_seen

        ids: list[int] | None = None
        if body and isinstance(body.get("ids"), list):
            raw = body["ids"]
            if raw:
                ids = [int(x) for x in raw]
        n = mark_alerts_seen(alert_ids=ids)
        return {"marked": n}
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="ids doit être une liste d’entiers") from e
    except Exception as e:  # noqa: BLE001
        log.exception("api_alerts_mark_seen: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/aegis/recent")
async def api_aegis_recent(limit: int = 25) -> dict[str, Any]:
    """Dernières vérifications Claude (web) post-scraping — voir AEGIS_VERIFIER=1."""
    try:
        from app.aegis_verifier import aegis_enabled, get_recent_checks

        return {
            "enabled": aegis_enabled(),
            "items": get_recent_checks(limit),
        }
    except Exception as e:  # noqa: BLE001
        log.exception("api_aegis_recent: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/aegis/verify-now")
async def api_aegis_verify_now() -> dict[str, Any]:
    """Relance une passe AEGIS sur market_fr.csv (ignore le TTL cache)."""
    try:
        from app.aegis_verifier import run_aegis_verify_now_from_disk

        return run_aegis_verify_now_from_disk()
    except Exception as e:  # noqa: BLE001
        log.exception("api_aegis_verify_now: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/google/stats")
async def api_google_stats() -> dict[str, Any]:
    """Statistiques Google Shopping (SQLite)."""
    try:
        from app.google_shopping_verifier import get_google_stats, init_google_cache

        init_google_cache()
        return get_google_stats()
    except Exception as e:  # noqa: BLE001
        log.exception("api_google_stats: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/google/recent")
async def api_google_recent(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """Dernières vérifications Google Shopping."""
    try:
        from app.google_shopping_verifier import get_recent_verifications, init_google_cache

        init_google_cache()
        return {"verifications": get_recent_verifications(limit)}
    except Exception as e:  # noqa: BLE001
        log.exception("api_google_recent: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/sitemap/test")
async def api_sitemap_test(brand: str, model: str) -> dict[str, Any]:
    """Prix issus des sitemaps (cache 6 h + pages produit)."""
    try:
        from scrapers.sitemap_scraper import get_sitemap_prices, init_sitemap_db

        init_sitemap_db()
        prices = get_sitemap_prices(brand, model)
        return {
            "brand": brand,
            "model": model,
            "sitemap_prices": prices,
            "nb_prices": len(prices),
        }
    except Exception as e:  # noqa: BLE001
        log.exception("api_sitemap_test: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/google/verify")
async def api_google_verify_now(sample_size: int = Query(5, ge=1, le=5)) -> dict[str, Any]:
    """Batch Playwright (thread) — max 5 modèles."""
    try:
        from app.google_shopping_verifier import init_google_cache, run_google_verification_batch

        init_google_cache()
        loop = asyncio.get_event_loop()

        def _run() -> dict[str, Any]:
            return run_google_verification_batch(sample_size)

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = await loop.run_in_executor(pool, _run)
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("api_google_verify_now: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/scrape/trigger")
async def api_scrape_trigger() -> dict[str, str]:
    """Lance run_market(FR) en arrière-plan (thread)."""
    if run_scheduled_fr_market is None:
        raise HTTPException(status_code=503, detail="Scheduler / run_market indisponible")

    def _run() -> None:
        try:
            run_scheduled_fr_market()
        except Exception as e:  # noqa: BLE001
            log.exception("run_scheduled_fr_market (thread): %s", e)

    threading.Thread(target=_run, name="scrape-trigger", daemon=True).start()
    return {"status": "triggered", "message": "Scraping lancé"}


@app.get("/api/scrape/status")
async def api_scrape_status() -> dict[str, Any]:
    """État dernier scraping (fr_update_status.json + métadonnées CSV)."""
    try:
        status = _read_fr_update_status()
        rows = _read_market_fr_rows()
        err = None
        msg = str(status.get("last_message") or "")
        if "erreur" in msg.lower():
            err = msg
        return {
            "fr_update_status": status,
            "models_in_market_fr_csv": len(rows),
            "last_error_hint": err,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("api_scrape_status: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/analytics")
async def analytics_page(request: Request) -> Any:
    """Page analytics : Jinja si possible, sinon HTML inline ou JSON."""
    try:
        if templates is not None and (TEMPLATES_DIR / "analytics.html").is_file():
            return templates.TemplateResponse("analytics.html", {"request": request})
    except Exception as e:  # noqa: BLE001
        log.warning("analytics template: %s", e)

    stats = get_stats() if get_stats is not None else {}
    views = int(stats.get("search_views", 0))
    clicks = int(stats.get("premium_clicks", 0))
    conversion = round((clicks / views) * 100, 2) if views else 0.0
    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"/><title>Analytics</title></head>
<body style="background:#0b0b0b;color:#f8fafc;font-family:system-ui;padding:2rem;">
<h1 style="color:#22c55e;">Analytics</h1>
<p>Visites recherche: <strong style="color:#22c55e;">{views}</strong></p>
<p>Clics premium: <strong style="color:#22c55e;">{clicks}</strong></p>
<p>Conversion: <strong style="color:#22c55e;">{conversion}%</strong></p>
</body></html>"""
    return HTMLResponse(content=html)


@app.get("/api/analytics/stats")
async def api_analytics_stats() -> dict[str, Any]:
    """Statistiques agrégées pour tableaux de bord."""
    try:
        rows = _read_market_fr_rows()
        models_catalog = _read_models_list()
        avgs: list[float] = []
        for r in rows:
            try:
                avgs.append(float(str(r.get("price_avg") or "0").replace(",", ".")))
            except ValueError:
                continue
        brands = [str(r.get("brand") or "").strip() for r in rows if r.get("brand")]
        top_brands = [ {"brand": b, "count": n} for b, n in Counter(brands).most_common(10)]
        status = _read_fr_update_status()
        track = get_stats() if get_stats is not None else {}
        return {
            "total_models_catalog": len(models_catalog),
            "total_rows_market_fr_csv": len(rows),
            "avg_price_all": round(mean(avgs), 2) if avgs else None,
            "top_brands": top_brands,
            "last_update": status.get("updated_at") or status.get("last_end_at"),
            "last_scrape_message": status.get("last_message"),
            "last_scrape_success": status.get("last_success"),
            "tracking_counters": track,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("api_analytics_stats: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


# ---------------------------------------------------------------------------
# E) LANCEMENT DIRECT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=5003, reload=False)
