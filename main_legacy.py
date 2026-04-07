from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

app = FastAPI()

# =========================
# LOGIQUE
# =========================

def analyze(product, client_price):
    market_sources = {
        "Nike Pegasus": [110, 120, 130, 140],
        "Nike ZoomX": [120, 140, 150, 160],
    }

    prices = market_sources.get(product, [])

    if not prices:
        return None

    min_price = min(prices)
    max_price = max(prices)
    avg_price = sum(prices) / len(prices)

    position = (client_price - min_price) / (max_price - min_price)

    if position < 0.3:
        pos = "🟢 BAS"
        action = "💰 ACHETER"
    elif position < 0.7:
        pos = "🟡 MOYEN"
        action = "⚖️ AJUSTER"
    else:
        pos = "🔴 HAUT"
        action = "❌ BAISSER PRIX"

    return {
        "product": product,
        "min": min_price,
        "max": max_price,
        "avg": round(avg_price, 2),
        "client": client_price,
        "position": pos,
        "action": action
    }

# =========================
# UI MODERNE
# =========================

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
    <head>
        <title>Sneaker Intelligence</title>
        <style>
            body {
                background: #0f172a;
                color: white;
                font-family: Arial;
                text-align: center;
                padding: 50px;
            }
            .box {
                background: #1e293b;
                padding: 30px;
                border-radius: 15px;
                width: 400px;
                margin: auto;
                box-shadow: 0 0 20px rgba(0,0,0,0.5);
            }
            input, button {
                padding: 10px;
                margin: 10px;
                width: 80%;
                border-radius: 10px;
                border: none;
            }
            button {
                background: #22c55e;
                color: white;
                font-weight: bold;
                cursor: pointer;
            }
            h1 {
                color: #22c55e;
            }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>🔥 Sneaker Intelligence</h1>
            <form action="/analyze" method="post">
                <input name="product" placeholder="Produit"><br>
                <input name="price" type="number" placeholder="Ton prix"><br>
                <button type="submit">Analyser</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.post("/analyze", response_class=HTMLResponse)
def result(product: str = Form(...), price: float = Form(...)):
    data = analyze(product, price)

    if not data:
        return "<h2 style='color:red'>Produit non trouvé</h2>"

    return f"""
    <html>
    <head>
        <style>
            body {{
                background: #0f172a;
                color: white;
                font-family: Arial;
                text-align: center;
                padding: 50px;
            }}
            .card {{
                background: #1e293b;
                padding: 30px;
                border-radius: 15px;
                width: 400px;
                margin: auto;
            }}
            h2 {{
                color: #22c55e;
            }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>{data['product']}</h1>
            <p>Min: {data['min']}\u00a0€</p>
            <p>Max: {data['max']}\u00a0€</p>
            <p>Moyenne: {data['avg']}\u00a0€</p>
            <p>Ton prix: {data['client']}\u00a0€</p>
            <h2>{data['position']}</h2>
            <h2>{data['action']}</h2>
            <br><a href="/" style="color:white;">⬅ Retour</a>
        </div>
    </body>
    </html>
    """
