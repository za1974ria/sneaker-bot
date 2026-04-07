"""
app.py — Point d'entrée FastAPI alternatif (démo/legacy).
Production officielle: `uvicorn app.app:app` via `sneaker_bot.service`.
Démarrage dev alternatif : uvicorn app.app:app --reload (depuis /root/sneaker_bot, venv activé)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.analytics.tracker import get_stats, log_event

# ─────────────────────────────────────────────
# Instance FastAPI
# ─────────────────────────────────────────────
app = FastAPI(
    title="SneakerBot",
    description="Bot de recherche sneakers avec suivi analytics",
    version="1.0.0",
)

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
MARKET_FR_SOURCES_CSV = DATA_DIR / "market_fr_sources.csv"
FR_SOURCES_CATALOG_PATH = DATA_DIR / "fr_ecommerce_sources.json"


# ─────────────────────────────────────────────
# Helpers HTML (shared UI fragments)
# ─────────────────────────────────────────────

def _nav() -> str:
    return """
    <nav style="
        display:flex; align-items:center; justify-content:space-between;
        padding: 16px 32px; background:#0a0a0a;
        border-bottom: 1px solid #1e1e1e;
        position:sticky; top:0; z-index:100;
    ">
        <span style="font-size:1.4rem; font-weight:800; letter-spacing:-1px; color:#fff;">
            👟 SneakerBot
        </span>
        <div style="display:flex; gap:20px;">
            <a href="/" style="color:#888; text-decoration:none; font-size:.9rem;">🏠 Home</a>
            <a href="/search" style="color:#888; text-decoration:none; font-size:.9rem;">🔍 Recherche</a>
            <a href="/comparison" style="color:#888; text-decoration:none; font-size:.9rem;">🛒 Comparateur</a>
            <a href="/premium" style="color:#888; text-decoration:none; font-size:.9rem;">⭐ Premium</a>
            <a href="/analytics" style="color:#00ff88; text-decoration:none; font-size:.9rem; font-weight:600;">📊 Analytics</a>
        </div>
    </nav>
    """


def _base_style() -> str:
    return """
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: #0d0d0d;
            color: #e0e0e0;
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            min-height: 100vh;
        }
        a { color: inherit; }
        .btn {
            display: inline-block;
            padding: 12px 28px;
            border-radius: 8px;
            font-weight: 700;
            cursor: pointer;
            text-decoration: none;
            transition: all .2s;
            border: none;
            font-size: .95rem;
        }
        .btn-primary {
            background: #00ff88;
            color: #0a0a0a;
        }
        .btn-primary:hover { background: #00cc6a; transform: translateY(-1px); }
        .btn-outline {
            background: transparent;
            color: #00ff88;
            border: 1px solid #00ff88;
        }
        .btn-outline:hover { background: #00ff8811; }
    </style>
    """


# ─────────────────────────────────────────────
# Route  /  — Home
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home():
    return f"""
    <!DOCTYPE html><html lang="fr"><head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>SneakerBot</title>
        {_base_style()}
        <style>
            .hero {{
                display: flex; flex-direction: column; align-items: center;
                justify-content: center; text-align: center;
                padding: 120px 20px 80px;
                gap: 24px;
            }}
            .hero h1 {{
                font-size: clamp(2.5rem, 6vw, 5rem);
                font-weight: 900;
                letter-spacing: -2px;
                line-height: 1.1;
            }}
            .hero h1 span {{ color: #00ff88; }}
            .hero p {{
                color: #888;
                font-size: 1.1rem;
                max-width: 500px;
                line-height: 1.7;
            }}
            .ctas {{ display: flex; gap: 16px; flex-wrap: wrap; justify-content: center; }}
            .features {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
                gap: 20px;
                max-width: 900px;
                margin: 0 auto;
                padding: 40px 20px 80px;
            }}
            .feature-card {{
                background: #141414;
                border: 1px solid #1e1e1e;
                border-radius: 12px;
                padding: 28px;
                transition: border-color .2s;
            }}
            .feature-card:hover {{ border-color: #00ff8844; }}
            .feature-card .icon {{ font-size: 2rem; margin-bottom: 12px; }}
            .feature-card h3 {{ color: #fff; margin-bottom: 8px; font-size: 1rem; }}
            .feature-card p {{ color: #666; font-size: .9rem; line-height: 1.6; }}
        </style>
    </head><body>
        {_nav()}
        <div class="hero">
            <h1>Trouve ta paire<br><span>en 3 secondes.</span></h1>
            <p>SneakerBot scanne des milliers de sources en temps réel
               pour te trouver les meilleures deals sur tes sneakers préférées.</p>
            <div class="ctas">
                <a href="/search" class="btn btn-primary">🔍 Lancer une recherche</a>
                <a href="/premium" class="btn btn-outline">⭐ Passer Premium</a>
            </div>
        </div>
        <div class="features">
            <div class="feature-card">
                <div class="icon">⚡</div>
                <h3>Résultats instantanés</h3>
                <p>Agrégation multi-sources en temps réel. Toujours les prix les plus frais.</p>
            </div>
            <div class="feature-card">
                <div class="icon">🎯</div>
                <h3>Filtres intelligents</h3>
                <p>Taille, couleur, budget, marque. Trouve exactement ce que tu cherches.</p>
            </div>
            <div class="feature-card">
                <div class="icon">🔔</div>
                <h3>Alertes Premium</h3>
                <p>Sois notifié dès qu'une paire restock ou que le prix baisse.</p>
            </div>
        </div>
    </body></html>
    """


# ─────────────────────────────────────────────
# Route  /search  — Recherche de sneakers
# ─────────────────────────────────────────────

@app.get("/search", response_class=HTMLResponse)
async def search_page(q: str = ""):
    # Simuler des résultats si une query est présente
    results_html = ""
    if q:
        # ── événement analytics (ligne ~888) ──────────────────────────────
        log_event("search_views")
        # ─────────────────────────────────────────────────────────────────

        mock_results = [
            {"name": "Air Jordan 1 Retro High OG", "price": "€ 189", "store": "Nike.com", "tag": "🔥 Hot"},
            {"name": "Yeezy Boost 350 V2", "price": "€ 220", "store": "Adidas", "tag": "📦 Dispo"},
            {"name": "New Balance 550", "price": "€ 110", "store": "Foot Locker", "tag": "✅ Stock"},
            {"name": "Dunk Low Panda", "price": "€ 130", "store": "SNKRS", "tag": "⏳ Limité"},
        ]
        cards = ""
        for r in mock_results:
            cards += f"""
            <div class="result-card">
                <div class="result-tag">{r['tag']}</div>
                <div class="result-name">{r['name']}</div>
                <div class="result-store">{r['store']}</div>
                <div class="result-bottom">
                    <span class="result-price">{r['price']}</span>
                    <a href="/track/premium?ref={q}" class="btn btn-primary" style="padding:8px 18px; font-size:.85rem;">
                        Voir l'offre ⭐
                    </a>
                </div>
            </div>
            """
        results_html = f"""
        <div style="padding: 0 20px 20px;">
            <p style="color:#555; margin-bottom:16px; font-size:.9rem;">
                {len(mock_results)} résultats pour « <strong style="color:#fff">{q}</strong> »
            </p>
            <div class="results-grid">{cards}</div>
        </div>
        """

    return f"""
    <!DOCTYPE html><html lang="fr"><head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Recherche — SneakerBot</title>
        {_base_style()}
        <style>
            .search-hero {{
                padding: 60px 20px 40px;
                text-align: center;
            }}
            .search-hero h2 {{
                font-size: 2rem; font-weight: 800;
                letter-spacing: -1px; margin-bottom: 24px;
            }}
            .search-bar {{
                display: flex; gap: 12px;
                max-width: 600px; margin: 0 auto;
            }}
            .search-bar input {{
                flex: 1;
                background: #141414;
                border: 1px solid #2a2a2a;
                border-radius: 10px;
                padding: 14px 20px;
                color: #fff;
                font-size: 1rem;
                outline: none;
                transition: border-color .2s;
            }}
            .search-bar input:focus {{ border-color: #00ff88; }}
            .search-bar input::placeholder {{ color: #444; }}
            .results-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
                gap: 16px;
                margin-top: 8px;
            }}
            .result-card {{
                background: #141414;
                border: 1px solid #1e1e1e;
                border-radius: 12px;
                padding: 20px;
                display: flex; flex-direction: column; gap: 10px;
                transition: border-color .2s;
            }}
            .result-card:hover {{ border-color: #2a2a2a; }}
            .result-tag {{ font-size: .78rem; color: #888; }}
            .result-name {{ font-weight: 700; font-size: 1rem; color: #fff; }}
            .result-store {{ font-size: .85rem; color: #555; }}
            .result-bottom {{ display: flex; align-items: center; justify-content: space-between; margin-top: 4px; }}
            .result-price {{ font-size: 1.2rem; font-weight: 800; color: #00ff88; }}
        </style>
    </head><body>
        {_nav()}
        <div class="search-hero">
            <h2>🔍 Recherche</h2>
            <form action="/search" method="get" class="search-bar">
                <input
                    type="text"
                    name="q"
                    placeholder="Ex: Jordan 1, Yeezy 350, Dunk Low..."
                    value="{q}"
                    autofocus
                />
                <button type="submit" class="btn btn-primary">GO</button>
            </form>
        </div>
        {results_html}
    </body></html>
    """


# ─────────────────────────────────────────────
# Route  /track/premium  — Tracking clic premium (~ligne 367)
# ─────────────────────────────────────────────

@app.get("/track/premium")
async def track_premium(ref: str = ""):
    # ── événement analytics (ligne ~367) ──────────────────────────────
    log_event("premium_clicks")
    # ─────────────────────────────────────────────────────────────────
    return RedirectResponse(url="/premium", status_code=302)


# ─────────────────────────────────────────────
# Route  /premium  — Page offre Premium
# ─────────────────────────────────────────────

@app.get("/premium", response_class=HTMLResponse)
async def premium_page():
    return f"""
    <!DOCTYPE html><html lang="fr"><head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Premium — SneakerBot</title>
        {_base_style()}
        <style>
            .premium-hero {{
                text-align: center;
                padding: 80px 20px 40px;
            }}
            .premium-hero h2 {{
                font-size: 2.5rem; font-weight: 900;
                letter-spacing: -1px; margin-bottom: 12px;
            }}
            .premium-hero p {{ color: #888; font-size: 1.05rem; }}
            .plans {{
                display: flex; flex-wrap: wrap;
                gap: 24px; justify-content: center;
                padding: 40px 20px 80px;
            }}
            .plan {{
                background: #141414;
                border: 1px solid #1e1e1e;
                border-radius: 16px;
                padding: 36px 32px;
                width: 280px;
                display: flex; flex-direction: column; gap: 16px;
            }}
            .plan.featured {{
                border-color: #00ff88;
                position: relative;
            }}
            .plan.featured::before {{
                content: '⭐ POPULAIRE';
                position: absolute; top: -12px; left: 50%; transform: translateX(-50%);
                background: #00ff88; color: #0a0a0a;
                font-size: .72rem; font-weight: 800;
                padding: 4px 14px; border-radius: 20px;
            }}
            .plan-name {{ font-weight: 800; font-size: 1.1rem; color: #fff; }}
            .plan-price {{ font-size: 2.2rem; font-weight: 900; color: #00ff88; }}
            .plan-price span {{ font-size: 1rem; color: #555; }}
            .plan ul {{ list-style: none; display: flex; flex-direction: column; gap: 10px; flex: 1; }}
            .plan ul li {{ font-size: .9rem; color: #888; }}
            .plan ul li::before {{ content: '✓ '; color: #00ff88; font-weight: 700; }}
        </style>
    </head><body>
        {_nav()}
        <div class="premium-hero">
            <h2>Passe à ⭐ Premium</h2>
            <p>Débloque toutes les fonctionnalités et ne rate plus jamais une bonne affaire.</p>
        </div>
        <div class="plans">
            <div class="plan">
                <div class="plan-name">Free</div>
                <div class="plan-price">0€ <span>/ mois</span></div>
                <ul>
                    <li>5 recherches / jour</li>
                    <li>Résultats basiques</li>
                    <li>Pas d'alertes</li>
                </ul>
                <a href="/search" class="btn btn-outline">Continuer gratis</a>
            </div>
            <div class="plan featured">
                <div class="plan-name">Premium</div>
                <div class="plan-price">9€ <span>/ mois</span></div>
                <ul>
                    <li>Recherches illimitées</li>
                    <li>Alertes restock + prix</li>
                    <li>Accès prioritaire aux drops</li>
                    <li>Filtres avancés</li>
                </ul>
                <a href="#" class="btn btn-primary">S'abonner</a>
            </div>
            <div class="plan">
                <div class="plan-name">Pro</div>
                <div class="plan-price">24€ <span>/ mois</span></div>
                <ul>
                    <li>Tout le plan Premium</li>
                    <li>API privée</li>
                    <li>Bot Telegram dédié</li>
                    <li>Support prioritaire</li>
                </ul>
                <a href="#" class="btn btn-outline">Contacter</a>
            </div>
        </div>
    </body></html>
    """


# ─────────────────────────────────────────────
# Route  /analytics  — Dashboard analytics
# ─────────────────────────────────────────────

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page():
    stats = get_stats()
    search_views: int = stats["search_views"]
    premium_clicks: int = stats["premium_clicks"]

    # Formule de conversion
    conversion: float = 0.0
    if search_views > 0:
        conversion = round((premium_clicks / search_views) * 100, 2)

    # Barre de progression visuelle (cap à 100%)
    bar_width = min(conversion, 100)

    # Indicateur de tendance
    if conversion >= 10:
        trend = "🟢 Excellente"
        trend_color = "#00ff88"
    elif conversion >= 5:
        trend = "🟡 Correcte"
        trend_color = "#ffcc00"
    else:
        trend = "🔴 À améliorer"
        trend_color = "#ff4444"

    return f"""
    <!DOCTYPE html><html lang="fr"><head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>📊 Analytics — SneakerBot</title>
        {_base_style()}
        <style>
            @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;800&display=swap');

            body {{ font-family: 'JetBrains Mono', monospace; }}

            .analytics-header {{
                padding: 60px 40px 0;
                max-width: 1000px;
                margin: 0 auto;
            }}
            .analytics-header h1 {{
                font-size: 2.4rem;
                font-weight: 800;
                color: #fff;
                letter-spacing: -1px;
            }}
            .analytics-header p {{
                color: #444;
                font-size: .85rem;
                margin-top: 6px;
            }}

            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
                gap: 20px;
                max-width: 1000px;
                margin: 40px auto 0;
                padding: 0 40px;
            }}

            .stat-card {{
                background: #111;
                border: 1px solid #1c1c1c;
                border-radius: 14px;
                padding: 30px 28px;
                display: flex;
                flex-direction: column;
                gap: 10px;
                transition: border-color .2s, transform .2s;
            }}
            .stat-card:hover {{
                border-color: #00ff8833;
                transform: translateY(-2px);
            }}

            .stat-label {{
                font-size: .75rem;
                text-transform: uppercase;
                letter-spacing: 2px;
                color: #444;
                font-weight: 700;
            }}
            .stat-value {{
                font-size: 3.2rem;
                font-weight: 800;
                color: #00ff88;
                line-height: 1;
            }}
            .stat-sub {{
                font-size: .78rem;
                color: #555;
            }}

            .conversion-section {{
                max-width: 1000px;
                margin: 28px auto 0;
                padding: 0 40px;
            }}
            .conversion-card {{
                background: #111;
                border: 1px solid #1c1c1c;
                border-radius: 14px;
                padding: 30px 28px;
            }}
            .conversion-header {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 20px;
            }}
            .conversion-label {{
                font-size: .75rem;
                text-transform: uppercase;
                letter-spacing: 2px;
                color: #444;
                font-weight: 700;
            }}
            .conversion-pct {{
                font-size: 2rem;
                font-weight: 800;
                color: #00ff88;
            }}
            .progress-bg {{
                background: #1a1a1a;
                border-radius: 100px;
                height: 10px;
                overflow: hidden;
            }}
            .progress-fill {{
                height: 100%;
                border-radius: 100px;
                background: linear-gradient(90deg, #00ff88, #00cc6a);
                width: {bar_width}%;
                transition: width 1s ease;
            }}
            .trend {{
                margin-top: 14px;
                font-size: .82rem;
                color: {trend_color};
            }}

            .formula-section {{
                max-width: 1000px;
                margin: 28px auto 60px;
                padding: 0 40px;
            }}
            .formula-box {{
                background: #0a0a0a;
                border: 1px solid #1a1a1a;
                border-radius: 10px;
                padding: 18px 24px;
                font-size: .82rem;
                color: #444;
                line-height: 1.8;
            }}
            .formula-box code {{
                color: #00ff88;
                background: #0d1a12;
                padding: 2px 6px;
                border-radius: 4px;
            }}
        </style>
    </head><body>
        {_nav()}

        <div class="analytics-header">
            <h1>📊 ANALYTICS</h1>
            <p>Tableau de bord temps réel — données depuis data/tracking.json</p>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Visites</div>
                <div class="stat-value">{search_views:,}</div>
                <div class="stat-sub">Recherches déclenchées</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Clics premium</div>
                <div class="stat-value">{premium_clicks:,}</div>
                <div class="stat-sub">Redirections /track/premium</div>
            </div>
        </div>

        <div class="conversion-section">
            <div class="conversion-card">
                <div class="conversion-header">
                    <div class="conversion-label">Taux de conversion</div>
                    <div class="conversion-pct">{conversion}%</div>
                </div>
                <div class="progress-bg">
                    <div class="progress-fill"></div>
                </div>
                <div class="trend">Tendance : {trend}</div>
            </div>
        </div>

        <div class="formula-section">
            <div class="formula-box">
                📐 Formule : <code>conversion = round((premium_clicks / search_views) × 100, 2)</code>
                si search_views &gt; 0, sinon <code>0.0%</code>
            </div>
        </div>

    </body></html>
    """


@app.get("/api/comparison/fr")
async def api_comparison_fr(brand: str = "", model: str = ""):
    """
    Comparateur par boutique FR (style Trivago) basé sur les sources scraping.
    """
    if not MARKET_FR_SOURCES_CSV.is_file():
        return JSONResponse({"items": [], "count": 0, "message": "Aucune donnée source FR disponible"})

    bq = brand.strip().lower()
    mq = model.strip().lower()
    items: list[dict[str, object]] = []
    with MARKET_FR_SOURCES_CSV.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rb = (r.get("brand") or "").strip()
            rm = (r.get("model") or "").strip()
            if bq and rb.lower() != bq:
                continue
            if mq and mq not in rm.lower():
                continue
            try:
                pmin = float(r.get("price_min") or 0.0)
                pmax = float(r.get("price_max") or 0.0)
                pavg = float(r.get("price_avg") or 0.0)
                pcount = int(float(r.get("price_count") or 0))
            except ValueError:
                continue
            credibility = "high"
            if pcount <= 1 or pmax > (pmin * 2.2):
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
                }
            )

    items.sort(key=lambda x: (str(x["brand"]).lower(), str(x["model"]).lower(), float(x["price_min"])))
    return JSONResponse({"items": items, "count": len(items)})


@app.get("/api/sources/fr")
async def api_sources_fr():
    if not FR_SOURCES_CATALOG_PATH.is_file():
        return JSONResponse({"sources": [], "count": 0})
    try:
        raw = json.loads(FR_SOURCES_CATALOG_PATH.read_text(encoding="utf-8"))
        sources = raw if isinstance(raw, list) else []
    except Exception:
        sources = []
    return JSONResponse({"sources": sources, "count": len(sources)})


@app.get("/comparison", response_class=HTMLResponse)
async def comparison_page(brand: str = "", model: str = ""):
    b = brand.strip()
    m = model.strip()
    return f"""
    <!DOCTYPE html><html lang="fr"><head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Comparateur FR — SneakerBot</title>
      {_base_style()}
      <style>
        .wrap {{ max-width: 1100px; margin: 0 auto; padding: 26px 18px 50px; }}
        .head {{ display:flex; align-items:end; justify-content:space-between; gap:16px; flex-wrap:wrap; }}
        .head h2 {{ font-size:1.5rem; }}
        .sub {{ color:#7a7a7a; font-size:.92rem; margin-top:6px; }}
        .filters {{ display:flex; gap:10px; flex-wrap:wrap; margin:18px 0 14px; }}
        .filters input {{ background:#141414; border:1px solid #2a2a2a; color:#fff; border-radius:10px; padding:10px 12px; min-width:220px; }}
        .table {{ border:1px solid #1f1f1f; border-radius:12px; overflow:hidden; }}
        table {{ width:100%; border-collapse:collapse; }}
        th, td {{ padding:10px 12px; border-bottom:1px solid #1b1b1b; text-align:left; font-size:.9rem; }}
        th {{ background:#121212; color:#9a9a9a; font-size:.78rem; text-transform:uppercase; letter-spacing:.06em; }}
        .pill {{ padding:3px 8px; border-radius:999px; font-size:.72rem; font-weight:700; }}
        .p-high {{ background:#0f2f1f; color:#4dff9f; }}
        .p-medium {{ background:#332a10; color:#ffd66f; }}
        .muted {{ color:#787878; }}
      </style>
    </head><body>
      {_nav()}
      <div class="wrap">
        <div class="head">
          <div>
            <h2>🛒 Comparateur FR multi-boutiques</h2>
            <div class="sub">Comparaison type Trivago entre boutiques e-commerce sneakers/sport opérant en France.</div>
          </div>
        </div>
        <form class="filters" method="get" action="/comparison">
          <input name="brand" placeholder="Marque (ex: Nike)" value="{b}">
          <input name="model" placeholder="Modèle (ex: Air Force 1)" value="{m}">
          <button class="btn btn-primary" type="submit">Comparer</button>
        </form>
        <div class="table">
          <table>
            <thead>
              <tr><th>Produit</th><th>Boutique</th><th>Min</th><th>Moyen</th><th>Max</th><th>Nb prix</th><th>Crédibilité</th></tr>
            </thead>
            <tbody id="rows"><tr><td colspan="7" class="muted">Chargement…</td></tr></tbody>
          </table>
        </div>
      </div>
      <script>
        const qs = new URLSearchParams({{ brand: {b!r}, model: {m!r} }});
        fetch('/api/comparison/fr?' + qs.toString())
          .then(r => r.json())
          .then(data => {{
            const rows = document.getElementById('rows');
            if (!data.items || data.items.length === 0) {{
              rows.innerHTML = '<tr><td colspan=\"7\" class=\"muted\">Aucune donnée pour ce filtre.</td></tr>';
              return;
            }}
            rows.innerHTML = data.items.map(it => {{
              const c = it.credibility === 'high' ? 'p-high' : 'p-medium';
              const product = `${{it.brand}} ${{it.model}}`;
              return `<tr>
                <td>${{product}}</td>
                <td>${{it.shop}}</td>
                <td>${{it.price_min.toFixed(2)}}\u00a0€</td>
                <td>${{it.price_avg.toFixed(2)}}\u00a0€</td>
                <td>${{it.price_max.toFixed(2)}}\u00a0€</td>
                <td>${{it.price_count}}</td>
                <td><span class="pill ${{c}}">${{it.credibility}}</span></td>
              </tr>`;
            }}).join('');
          }})
          .catch(() => {{
            document.getElementById('rows').innerHTML = '<tr><td colspan=\"7\" class=\"muted\">Erreur API comparaison.</td></tr>';
          }});
      </script>
    </body></html>
    """