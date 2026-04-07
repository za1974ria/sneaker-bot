"""Simple web UI for Sneakers Market Intelligence."""

from __future__ import annotations

from flask import Blueprint, abort, render_template_string

from app.services.market_service import get_market_products

web_bp = Blueprint("web", __name__)


HOME_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Luxury Sneaker Intelligence</title>
  <style>
    :root{
      --bg:#0b0b0b;
      --panel:#111111;
      --text:#ffffff;
      --muted:#b2b2b2;
      --line:#232323;
      --buyStrong:#00c896;
      --buy:#38d39f;
      --hold:#f2a65a;
      --sell:#ff5a5f;
      --wait:#f6d65f;
      --accent:#00c896;
      --gold:#b99a5b;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      background:radial-gradient(circle at 15% -10%, #111f1b 0%, var(--bg) 45%);
      color:var(--text);
      font-family:"Avenir Next", "Helvetica Neue", ui-sans-serif, system-ui, -apple-system, sans-serif;
      padding:1.1rem;
      line-height:1.35;
    }
    .wrap{max-width:1080px;margin:0 auto}
    .top{
      display:flex;align-items:flex-end;justify-content:space-between;gap:1.1rem;
      margin-bottom:1.4rem;flex-wrap:wrap
    }
    h1{
      margin:0;
      font-size:1.72rem;
      letter-spacing:.02em;
      font-weight:700;
      text-transform:uppercase;
    }
    .sub{margin:.35rem 0 0;color:var(--muted);font-size:.96rem}
    .count{color:var(--gold);font-size:.82rem;letter-spacing:.08em;text-transform:uppercase}
    .grid{display:grid;grid-template-columns:1fr;gap:.9rem}
    .card{
      border:1px solid var(--line);
      border-radius:16px;
      background:linear-gradient(165deg,#121212,#0f0f0f);
      padding:1rem;
      box-shadow:0 16px 32px rgba(0,0,0,.34);
    }
    .row{display:flex;justify-content:space-between;gap:.9rem;align-items:center}
    .name{font-size:1.07rem;font-weight:650}
    .meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.65rem;margin:.85rem 0 .95rem}
    .k{display:block;font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
    .v{display:block;font-size:1rem;font-weight:700}
    .signal{
      font-size:1.26rem;font-weight:850;letter-spacing:.02em;
      margin:.3rem 0 .78rem
    }
    .signal-buy-strong{color:var(--buyStrong)}
    .signal-buy{color:var(--buy)}
    .signal-hold{color:var(--hold)}
    .signal-sell{color:var(--sell)}
    .signal-wait{color:var(--wait)}
    .btn{
      display:inline-block;
      text-decoration:none;
      border:1px solid #1f9f53;
      color:#d8ffe8;
      background:linear-gradient(180deg,#18b886,#0f8e69);
      border-radius:999px;
      padding:.58rem .92rem;
      font-weight:700;
      font-size:.84rem;
      letter-spacing:.02em;
    }
    .btn:hover{filter:brightness(1.08)}
    .cta{
      display:inline-block;
      text-decoration:none;
      color:#f7fff9;
      border:1px solid #1f9f53;
      border-radius:999px;
      padding:.62rem 1rem;
      background:#0f8e69;
      font-size:.82rem;
      font-weight:700;
      letter-spacing:.05em;
      text-transform:uppercase;
    }
    @media (min-width:760px){
      body{padding:1.5rem}
      .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}
      h1{font-size:1.94rem}
    }
  </style>
</head>
<body>
  <main class="wrap">
    <div class="top">
      <div>
        <h1>Luxury Sneaker Intelligence</h1>
        <p class="sub">Real-time market signals for premium sneakers</p>
      </div>
      <div style="display:flex;align-items:center;gap:.7rem;flex-wrap:wrap">
        <div class="count">{{ products|length }} produits</div>
        <a class="cta" href="/api/products">Get Access</a>
      </div>
    </div>
    <section class="grid">
      {% for p in products %}
      <article class="card">
        <div class="row">
          <div class="name">{{ p.product }}</div>
          <div class="count">Score {{ p.score }}/5</div>
        </div>
        <p class="signal signal-{{ p.signal_kind }}">{{ p.signal_display }}</p>
        <div class="meta">
          <div><span class="k">Prix moyen</span><span class="v">{{ "%.2f"|format(p.avg or 0) }}&nbsp;€</span></div>
          <div><span class="k">Variation</span><span class="v">{{ "%.2f"|format(p.variation or 0) }}&nbsp;€</span></div>
          <div><span class="k">Tendance</span><span class="v">{{ p.trend }}</span></div>
        </div>
        <a class="btn" href="{{ url_for('web.product_detail', product_name=p.product) }}">Get Access</a>
      </article>
      {% endfor %}
    </section>
  </main>
</body>
</html>
"""


DETAIL_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{{ p.product }} - Luxury Detail</title>
  <style>
    :root{
      --bg:#0b0b0b;
      --panel:#111111;
      --text:#ffffff;
      --muted:#b2b2b2;
      --line:#232323;
      --buyStrong:#00c896;
      --buy:#38d39f;
      --hold:#f2a65a;
      --sell:#ff5a5f;
      --wait:#f6d65f;
      --accent:#00c896;
      --gold:#b99a5b;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      min-height:100vh;
      background:radial-gradient(circle at 20% -15%, #13231f 0%, var(--bg) 42%);
      color:var(--text);
      font-family:"Avenir Next", "Helvetica Neue", ui-sans-serif, system-ui, -apple-system, sans-serif;
      padding:1.4rem 1rem;
      display:flex;
      align-items:center;
      justify-content:center;
    }
    .card{
      width:100%;
      max-width:720px;
      margin:0 auto;
      border:1px solid var(--line);
      border-radius:18px;
      background:linear-gradient(165deg,#121212,#0f0f0f);
      padding:1.35rem 1.2rem;
      box-shadow:0 20px 40px rgba(0,0,0,.36);
    }
    h1{
      margin:0 0 .35rem;
      font-size:1.85rem;
      font-weight:700;
      letter-spacing:.02em;
      text-transform:uppercase;
      line-height:1.15;
    }
    .sub{margin:0 0 1.1rem;color:var(--muted)}
    .signal{
      font-size:1.36rem;
      font-weight:850;
      margin:0 0 1rem;
      letter-spacing:.02em;
    }
    .signal-buy-strong{color:var(--buyStrong)}
    .signal-buy{color:var(--buy)}
    .signal-hold{color:var(--hold)}
    .signal-sell{color:var(--sell)}
    .signal-wait{color:var(--wait)}
    .grid{
      display:grid;
      grid-template-columns:repeat(2,minmax(0,1fr));
      gap:.75rem;
      margin:0 0 1.1rem;
    }
    .stat{
      border:1px solid var(--line);
      border-radius:12px;
      background:rgba(255,255,255,.01);
      padding:.72rem .8rem;
    }
    .stat .k{
      display:block;
      font-size:.7rem;
      color:var(--muted);
      text-transform:uppercase;
      letter-spacing:.06em;
      margin-bottom:.2rem;
    }
    .stat .v{
      display:block;
      font-size:1rem;
      font-weight:700;
    }
    .insight{
      border:1px solid rgba(185,154,91,.35);
      background:linear-gradient(165deg, rgba(185,154,91,.09), rgba(185,154,91,.03));
      border-radius:14px;
      padding:.85rem .95rem;
      margin-bottom:1.15rem;
    }
    .insight h2{
      margin:0 0 .45rem;
      font-size:.9rem;
      letter-spacing:.07em;
      text-transform:uppercase;
      color:var(--gold);
    }
    .insight p{
      margin:0;
      color:#ececec;
      line-height:1.5;
      font-size:.96rem;
    }
    .actions{
      display:flex;
      gap:.65rem;
      flex-wrap:wrap;
      align-items:center;
    }
    .btn{
      display:inline-block;
      text-decoration:none;
      border-radius:999px;
      padding:.62rem 1rem;
      font-size:.82rem;
      font-weight:700;
      letter-spacing:.05em;
      text-transform:uppercase;
    }
    .btn-access{
      color:#f7fff9;
      border:1px solid #1f9f53;
      background:#0f8e69;
    }
    .btn-back{
      color:#d8d8d8;
      border:1px solid #3a3a3a;
      background:transparent;
    }
    @media (max-width:620px){
      .grid{grid-template-columns:1fr}
      h1{font-size:1.5rem}
    }
  </style>
</head>
<body>
  <article class="card">
    <h1>{{ p.product }}</h1>
    <p class="sub">Luxury Sneaker Intelligence — Product Detail</p>
    <p class="signal signal-{{ p.signal_kind }}">{{ p.signal_display }}</p>

    <div class="grid">
      <div class="stat"><span class="k">Prix moyen</span><span class="v">{{ "%.2f"|format(p.avg or 0) }}&nbsp;€</span></div>
      <div class="stat"><span class="k">Variation</span><span class="v">{{ "%.2f"|format(p.variation or 0) }}&nbsp;€</span></div>
      <div class="stat"><span class="k">Tendance</span><span class="v">{{ p.trend }}</span></div>
      <div class="stat"><span class="k">Score</span><span class="v">{{ p.score }}/5</span></div>
    </div>

    <section class="insight">
      <h2>Market Insight</h2>
      <p>{{ insight }}</p>
    </section>

    <div class="actions">
      <a class="btn btn-access" href="/api/products">Get Access</a>
      <a class="btn btn-back" href="/">← Retour à la liste</a>
    </div>
  </article>
</body>
</html>
"""


@web_bp.get("/")
def home():
    products = get_market_products()
    premium_label = {
        "buy-strong": "🔥 Prime Opportunity",
        "buy": "🟢 Opportunity",
        "hold": "⚖️ Stable Value",
        "wait": "⚠️ Observation",
        "sell": "❌ Exit Risk",
    }
    premium_products = []
    for p in products:
        item = dict(p)
        item["signal_display"] = premium_label.get(item.get("signal_kind", "wait"), "⚠️ Observation")
        premium_products.append(item)
    return render_template_string(HOME_TEMPLATE, products=premium_products)


@web_bp.get("/product/<path:product_name>")
def product_detail(product_name: str):
    products = get_market_products()
    key = (product_name or "").strip().lower()
    item = next((p for p in products if str(p.get("product", "")).strip().lower() == key), None)
    if item is None:
        abort(404)
    premium_label = {
        "buy-strong": "🔥 Prime Opportunity",
        "buy": "🟢 Opportunity",
        "hold": "⚖️ Stable Value",
        "wait": "⚠️ Observation",
        "sell": "❌ Exit Risk",
    }
    insight_map = {
        "buy-strong": "This model shows strong upside potential with active demand and favorable pricing momentum.",
        "buy": "This model presents a healthy opportunity with positive momentum and attractive market entry conditions.",
        "hold": "This model shows stable pricing with moderate market activity.",
        "wait": "This model is currently in observation mode with limited conviction from recent market movement.",
        "sell": "This model shows downside pressure and elevated exit risk in current market conditions.",
    }
    enriched = dict(item)
    kind = enriched.get("signal_kind", "wait")
    enriched["signal_display"] = premium_label.get(kind, "⚠️ Observation")
    insight = insight_map.get(kind, insight_map["wait"])
    return render_template_string(DETAIL_TEMPLATE, p=enriched, insight=insight)
